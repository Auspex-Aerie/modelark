"""Descriptor-confined, read-only map replay admission.

The caller separately proves the old/new commits. This observer checks actual
raw blobs, index and filesystem state; it never checks out files or moves refs.
The private tree parsers reused below are strictly read-only, framed Git object
and index decoders, not authority or mutation helpers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import stat

from modelark import publication_store as store, publication_tree as git_tree
from modelark.publication_native import QualifiedRepository
from modelark.publication_policy import PublicationRefused, check_replay_state, relative_path
from modelark.slice.transaction import TransferRefusal


@dataclass(frozen=True)
class ObservationLimits:
    max_entries: int = 100_000
    max_depth: int = 128
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if (any(type(value) is not int or value <= 0 for value in asdict(self).values())
                or self.max_entries > 100_000 or self.max_depth > 128
                or self.max_file_bytes > 64 * 1024 * 1024
                or self.max_total_bytes > 256 * 1024 * 1024):
            raise PublicationRefused("PUBLICATION_MAP_OBSERVATION_LIMIT_INVALID")


@dataclass(frozen=True)
class ExpectedFile:
    path: str
    mode: str
    oid: str
    size: int
    sha256: str
    symlink_target_hex: str | None


@dataclass(frozen=True)
class FileObservation:
    path: str
    mode: str
    unix_mode: int
    size: int
    sha256: str
    symlink_target_hex: str | None
    identity: tuple[int, ...]


@dataclass(frozen=True)
class DirectoryObservation:
    path: str
    unix_mode: int
    identity: tuple[int, ...]


@dataclass(frozen=True)
class MapWorktreeObservation:
    root_identity: tuple[int, ...]
    mount_id: int
    profile_digest: str
    old_digest: str
    new_digest: str
    old: tuple[ExpectedFile, ...]
    new: tuple[ExpectedFile, ...]
    index: tuple[git_tree.TreeEntry, ...]
    directories: tuple[DirectoryObservation, ...]
    files: tuple[FileObservation, ...]
    replay_state: str

    def record(self):
        return json.loads(store.canonical({"version": 1, "kind": "map-worktree-observation", **asdict(self)}))


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _inputs(value, repository, limits):
    if type(value) is git_tree.TreeSnapshot:
        if (value.root_identity != tuple(repository.tree.identity(repository.tree.fd))
                or value.mount_id != repository.tree.mount_id):
            raise PublicationRefused("PUBLICATION_MAP_SNAPSHOT_ATTACHMENT_CHANGED")
        entries = value.entries
        seal = store.digest(value.record())
    elif type(value) is tuple and all(type(item) is git_tree.TreeEntry for item in value):
        entries = value
        seal = store.digest([asdict(entry) for entry in entries])
    else:
        raise PublicationRefused("PUBLICATION_MAP_IMMUTABLE_TREE_REQUIRED")
    if len(entries) > limits.max_entries:
        raise PublicationRefused("PUBLICATION_MAP_ENTRY_LIMIT")
    names = {entry.path for entry in entries}
    if len(names) != len(entries) or entries != tuple(sorted(entries)):
        raise PublicationRefused("PUBLICATION_MAP_TREE_INVALID")
    for entry in entries:
        parts = entry.path.split("/")
        if len(parts) > limits.max_depth:
            raise PublicationRefused("PUBLICATION_MAP_DEPTH_LIMIT")
        if any("/".join(parts[:index]) in names for index in range(1, len(parts))):
            raise PublicationRefused("PUBLICATION_MAP_TREE_INVALID")
    return entries, seal


def _expected(repository, entries, limits, cache, budget):
    expected = []
    for entry in entries:
        if entry.oid not in cache:
            # Size gate precedes blob emission; _object independently checks its
            # Git SHA-1 including type/length framing. No filters/smudge execute.
            raw = git_tree._object(repository.read, "blob", entry.oid,
                                   limit=min(limits.max_file_bytes, limits.max_total_bytes - budget[0]))
            budget[0] += len(raw)
            if budget[0] > limits.max_total_bytes:
                raise PublicationRefused("PUBLICATION_MAP_BYTE_LIMIT")
            cache[entry.oid] = (len(raw), hashlib.sha256(raw).hexdigest(), raw.hex() if len(raw) <= 4096 else None)
        size, digest, target = cache[entry.oid]
        if entry.mode == "120000" and (target is None or size == 0):
            raise PublicationRefused("PUBLICATION_MAP_SYMLINK_UNQUALIFIED")
        expected.append(ExpectedFile(entry.path, entry.mode, entry.oid, size, digest,
                                     target if entry.mode == "120000" else None))
    return tuple(expected)


def _index(repository):
    return git_tree._entries(repository.read("ls-files", "--full-name", "--stage", "-v", "-z"), index=True)


def _regular(tree, parent, name, path, before, limits, budget):
    if before.st_size > limits.max_file_bytes:
        raise PublicationRefused("PUBLICATION_MAP_FILE_BYTE_LIMIT", path=path)
    budget[0] += before.st_size
    if budget[0] > limits.max_total_bytes:
        raise PublicationRefused("PUBLICATION_MAP_BYTE_LIMIT")
    fd = tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if _identity(os.fstat(fd)) != _identity(before):
            raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=path)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, min(1024 * 1024, before.st_size - total + 1)):
            total += len(chunk)
            if total > before.st_size:
                raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=path)
            digest.update(chunk)
        if (total != before.st_size or _identity(os.fstat(fd)) != _identity(before)
                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(before)):
            raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=path)
        return FileObservation(path, "100755" if before.st_mode & stat.S_IXUSR else "100644",
                               stat.S_IMODE(before.st_mode), total, digest.hexdigest(), None, _identity(before))
    finally:
        os.close(fd)


def _scan(repository, limits):
    tree = repository.tree
    directories, files = [], []
    root_stat = _identity(os.fstat(tree.fd))
    count, budget = [0], [0]

    def visit(path, depth):
        if depth > limits.max_depth:
            raise PublicationRefused("PUBLICATION_MAP_DEPTH_LIMIT")
        fd = os.dup(tree.fd) if path == "" else tree.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            before = os.fstat(fd)
            if path:
                directories.append(DirectoryObservation(path, stat.S_IMODE(before.st_mode), _identity(before)))
            names = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    if path == "" and entry.name == ".git":
                        continue
                    count[0] += 1
                    if count[0] > limits.max_entries:
                        raise PublicationRefused("PUBLICATION_MAP_ENTRY_LIMIT")
                    names.append(entry.name)
            for name in sorted(names):
                relative = name if not path else path + "/" + name
                relative_path(relative)
                value = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(value.st_mode):
                    visit(relative, depth + 1)
                    if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(value):
                        raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=relative)
                elif stat.S_ISREG(value.st_mode):
                    files.append(_regular(tree, fd, name, relative, value, limits, budget))
                elif stat.S_ISLNK(value.st_mode):
                    link_fd = tree.open(relative, os.O_PATH | os.O_NOFOLLOW)
                    try:
                        if _identity(os.fstat(link_fd)) != _identity(value):
                            raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=relative)
                    finally:
                        os.close(link_fd)
                    target = os.readlink(os.fsencode(name), dir_fd=fd)
                    if len(target) > min(4096, limits.max_file_bytes):
                        raise PublicationRefused("PUBLICATION_MAP_SYMLINK_UNQUALIFIED")
                    budget[0] += len(target)
                    if budget[0] > limits.max_total_bytes:
                        raise PublicationRefused("PUBLICATION_MAP_BYTE_LIMIT")
                    if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(value):
                        raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=relative)
                    files.append(FileObservation(
                        relative, "120000", stat.S_IMODE(value.st_mode), len(target),
                        hashlib.sha256(target).hexdigest(), target.hex(), _identity(value),
                    ))
                else:
                    raise PublicationRefused("PUBLICATION_MAP_FILE_TYPE_UNQUALIFIED", path=relative)
            if _identity(os.fstat(fd)) != _identity(before):
                raise PublicationRefused("PUBLICATION_MAP_DIRECTORY_CHANGED", path=path)
        finally:
            os.close(fd)

    visit("", 0)
    return tuple(sorted(directories, key=lambda item: item.path)), tuple(sorted(files, key=lambda item: item.path)), root_stat


def _recheck(repository, directories, files, root_stat):
    tree = repository.tree
    for node in (*directories, *files):
        with tree.parent(node.path) as (parent, name):
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != node.identity:
                raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=node.path)
            fd = tree.open(node.path, os.O_PATH | os.O_NOFOLLOW)
            try:
                if _identity(os.fstat(fd)) != node.identity:
                    raise PublicationRefused("PUBLICATION_MAP_PATH_CHANGED", path=node.path)
            finally:
                os.close(fd)  # Recheck no mount crossing or symlink substitution.
    if _identity(os.fstat(tree.fd)) != root_stat:
        raise PublicationRefused("PUBLICATION_MAP_DIRECTORY_CHANGED", path="")


def observe(repository, *, old, new, limits=ObservationLimits()):
    """Admit an old-index replay state or a fully published new index/worktree."""
    if type(repository) is not QualifiedRepository:
        raise PublicationRefused("PUBLICATION_QUALIFIED_REPOSITORY_REQUIRED")
    if type(limits) is not ObservationLimits:
        raise PublicationRefused("PUBLICATION_MAP_OBSERVATION_LIMIT_INVALID")
    repository.ensure()
    root = tuple(repository.tree.identity(repository.tree.fd))
    mount_id = repository.tree.mount_id
    profile_digest = repository.profile.digest
    old_entries, old_digest = _inputs(old, repository, limits)
    new_entries, new_digest = _inputs(new, repository, limits)
    cache, budget = {}, [0]
    try:
        expected_old = _expected(repository, old_entries, limits, cache, budget)
        expected_new = _expected(repository, new_entries, limits, cache, budget)
        index = _index(repository)
        if index not in (old_entries, new_entries):
            raise PublicationRefused("PUBLICATION_REPLAY_MIXED_INDEX")
        directories, files, root_stat = _scan(repository, limits)
        old_state = {entry.path: (entry.mode, entry.sha256) for entry in expected_old}
        new_state = {entry.path: (entry.mode, entry.sha256) for entry in expected_new}
        worktree = {entry.path: (entry.mode, entry.sha256) for entry in files}
        directory_paths = frozenset(entry.path for entry in directories)
        check_replay_state(old=old_state, new=new_state,
                           index=new_state if index == new_entries else old_state,
                           worktree=worktree, directories=directory_paths)
        if index == new_entries:
            new_directories = frozenset(str(parent) for entry in new_entries
                                        for parent in relative_path(entry.path).parents if str(parent) != ".")
            if directory_paths != new_directories:
                raise PublicationRefused("PUBLICATION_REPLAY_INCOMPLETE_DIRECTORIES")
        if _index(repository) != index:
            raise PublicationRefused("PUBLICATION_GIT_INDEX_CHANGED")
        _recheck(repository, directories, files, root_stat)
        repository.ensure()
        if (tuple(repository.tree.identity(repository.tree.fd)) != root
                or repository.tree.mount_id != mount_id or repository.profile.digest != profile_digest):
            raise PublicationRefused("PUBLICATION_MAP_ATTACHMENT_CHANGED")
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_MAP_WORKTREE_UNPROVEN", detail=str(exc)) from exc
    return MapWorktreeObservation(root, mount_id, profile_digest, old_digest, new_digest,
                                  expected_old, expected_new, index, directories, files,
                                  "complete" if index == new_entries else "old-index-replay")
