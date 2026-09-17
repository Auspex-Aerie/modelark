"""Read-only committed-tree observations for the unfinished ArchivePublisher.

The coordinator must supply its qualified, fenced native reader and BoundTree.
This primitive does not qualify a command runner, grant catalog authority, prove
worktree/object bytes, or publish/close anything. In particular it never invokes
write-tree (which can mutate the object database/index) to check the index.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Mapping
import hashlib
import json
import re

from modelark import publication_store as store
from modelark.publication_policy import PublicationRefused, check_committed_pointer, relative_path


_OID = re.compile(r"[0-9a-f]{40}\Z")  # Initial qualified Git object format: SHA-1.
_REF = re.compile(r"refs/heads/[A-Za-z0-9_][A-Za-z0-9_./-]*\Z")
_MODES = {"100644", "100755", "120000"}
_MANIFEST_LIMIT = 64 * 1024 * 1024


def _require(value, code):
    if not value:
        raise PublicationRefused(code)


def _oid(value):
    _require(isinstance(value, str) and _OID.fullmatch(value), "PUBLICATION_GIT_OID_UNQUALIFIED")
    return value


def _ref(value):
    _require(isinstance(value, str) and _REF.fullmatch(value) and not any(
        part in {"", ".", ".."} or part.startswith(".") or part.endswith((".", ".lock"))
        for part in value.split("/")) and ".." not in value and value != "refs/heads/git-annex",
        "PUBLICATION_GIT_HEAD_UNQUALIFIED")
    return value


def _record(value):
    return json.loads(store.canonical(asdict(value)))


@dataclass(frozen=True, order=True)
class TreeEntry:
    path: str
    mode: str
    oid: str

    def __post_init__(self):
        relative_path(self.path)
        _require(isinstance(self.mode, str) and self.mode in _MODES, "PUBLICATION_GIT_MODE_UNQUALIFIED")
        _oid(self.oid)


@dataclass(frozen=True)
class TreeSnapshot:
    root_identity: tuple
    mount_id: int
    head_ref: str
    head_oid: str
    tree_oid: str
    parents: tuple[str, ...]
    entries: tuple[TreeEntry, ...]

    def __post_init__(self):
        _require(type(self.root_identity) is tuple and len(self.root_identity) == 3
                 and all(type(value) is int and value >= 0 for value in self.root_identity)
                 and type(self.mount_id) is int and self.mount_id > 0,
                 "PUBLICATION_GIT_SNAPSHOT_INVALID")
        _ref(self.head_ref)
        _oid(self.head_oid)
        _oid(self.tree_oid)
        _require(type(self.parents) is tuple and type(self.entries) is tuple,
                 "PUBLICATION_GIT_SNAPSHOT_INVALID")
        for parent in self.parents:
            _oid(parent)
        _require(all(type(entry) is TreeEntry for entry in self.entries)
                 and self.entries == tuple(sorted(self.entries))
                 and len({entry.path for entry in self.entries}) == len(self.entries),
                 "PUBLICATION_GIT_SNAPSHOT_INVALID")

    def record(self):
        return _record(self)

    @classmethod
    def from_record(cls, value):
        """Decode an already seal-checked intent; decoding alone is not authority."""
        _require(type(value) is dict and set(value) == {
            "root_identity", "mount_id", "head_ref", "head_oid", "tree_oid", "parents", "entries"}
            and all(type(value[name]) is list for name in ("root_identity", "parents", "entries")),
            "PUBLICATION_GIT_SNAPSHOT_INVALID")
        entries = []
        for row in value["entries"]:
            _require(type(row) is dict and set(row) == {"path", "mode", "oid"},
                     "PUBLICATION_GIT_SNAPSHOT_INVALID")
            entries.append(TreeEntry(**row))
        return cls(tuple(value["root_identity"]), value["mount_id"], value["head_ref"], value["head_oid"],
                   value["tree_oid"], tuple(value["parents"]), tuple(entries))


@dataclass(frozen=True)
class CommittedTreeProof:
    binding_digest: str
    profile_digest: str
    before_digest: str
    after: TreeSnapshot
    delta: tuple[tuple[str, TreeEntry | None], ...]
    stored_path: str
    annex_key: str
    qualified_object_path: str
    pointer_oid: str
    representation: str

    def record(self):
        return {"version": 1, "kind": "committed-git-tree", **_record(self)}


def _read(read_git, *args, limit=_MANIFEST_LIMIT):
    # No revision expressions supplied by upstream filenames; raw object reads
    # bypass replacement refs and filters. The future qualified reader also owns
    # command environment/profile checks, bounded output and inherited fence FDs.
    data = read_git("--no-replace-objects", "--literal-pathspecs", *args)
    _require(type(data) is bytes and len(data) <= limit, "PUBLICATION_GIT_OUTPUT_INVALID")
    return data


def _line(data):
    _require(data.endswith(b"\n") and b"\n" not in data[:-1], "PUBLICATION_GIT_FRAMING_INVALID")
    try:
        return data[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PublicationRefused("PUBLICATION_GIT_FRAMING_INVALID") from exc


def _head(read_git):
    ref = _ref(_line(_read(read_git, "symbolic-ref", "HEAD", limit=4096)))
    return ref, _oid(_line(_read(read_git, "rev-parse", "--verify", ref, limit=128)))


def _entries(data, *, index=False):
    _require(not data or data.endswith(b"\0"), "PUBLICATION_GIT_FRAMING_INVALID")
    entries = {}
    try:
        for row in data.split(b"\0")[:-1]:
            header, name = row.split(b"\t", 1)
            fields = header.decode("ascii").split(" ")
            if index:
                _require(len(fields) == 4 and fields[0] == "H" and fields[3] == "0",
                         "PUBLICATION_GIT_INDEX_UNQUALIFIED")
                _, mode, oid, _ = fields
            else:
                _require(len(fields) == 3 and fields[1] == "blob", "PUBLICATION_GIT_TREE_UNQUALIFIED")
                mode, _, oid = fields
            entry = TreeEntry(name.decode("utf-8"), mode, oid)
            _require(entry.path not in entries, "PUBLICATION_GIT_DUPLICATE_PATH")
            entries[entry.path] = entry
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, PublicationRefused):
            raise
        raise PublicationRefused("PUBLICATION_GIT_FRAMING_INVALID") from exc
    for path in entries:
        parts = path.split("/")
        _require(not any("/".join(parts[:i]) in entries for i in range(1, len(parts))),
                 "PUBLICATION_GIT_PATH_COLLISION")
    return tuple(sorted(entries.values()))


def _commit(read_git, oid):
    raw = _object(read_git, "commit", oid, limit=1024 * 1024)
    headers, separator, _ = raw.partition(b"\n\n")
    _require(separator, "PUBLICATION_GIT_COMMIT_INVALID")
    rows = headers.split(b"\n")
    _require(rows[0].startswith(b"tree ") and sum(row.startswith(b"tree ") for row in rows) == 1,
             "PUBLICATION_GIT_COMMIT_INVALID")
    try:
        tree_oid = _oid(rows[0][5:].decode("ascii"))
        parents = tuple(_oid(row[7:].decode("ascii")) for row in rows if row.startswith(b"parent "))
    except UnicodeDecodeError as exc:
        raise PublicationRefused("PUBLICATION_GIT_COMMIT_INVALID") from exc
    return tree_oid, parents


def _object(read_git, kind, oid, *, limit):
    _oid(oid)
    # A non-pointer blob might be a whole model. Refuse its declared size before
    # asking Git to emit its contents; a post-allocation length check is too late.
    size = _line(_read(read_git, "cat-file", "-s", oid, limit=128))
    _require(re.fullmatch(r"0|[1-9][0-9]*", size), "PUBLICATION_GIT_OBJECT_SIZE_INVALID")
    _require(int(size) <= limit, "PUBLICATION_GIT_OBJECT_TOO_LARGE")
    raw = _read(read_git, "cat-file", kind, oid, limit=limit)
    _require(len(raw) == int(size) and hashlib.sha1(
        kind.encode() + b" " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == oid,
        "PUBLICATION_GIT_OBJECT_MISMATCH")
    return raw


def capture(tree, *, read_git, require_scope):
    """Observe a clean index/committed tree, without claiming worktree cleanliness.

    Call before publication mutation to seal its baseline, and again after the
    intended commit. No detached/unborn/annex branch, unmerged stage, assume-unchanged
    or skip-worktree flag, submodule or unqualified path/format is silently accepted.
    Scope must reject IO inside an active write transaction in its owning adapter.
    """
    require_scope()
    tree.check()
    root = tuple(tree.identity(tree.fd))
    mount_id = tree.mount_id
    head_ref, head_oid = _head(read_git)
    tree_oid, parents = _commit(read_git, head_oid)
    entries = _entries(_read(read_git, "ls-tree", "-r", "--full-tree", "-z", tree_oid))
    index = _entries(_read(read_git, "ls-files", "--full-name", "--stage", "-v", "-z"), index=True)
    _require(index == entries, "PUBLICATION_GIT_INDEX_TREE_MISMATCH")
    _require(_head(read_git) == (head_ref, head_oid), "PUBLICATION_GIT_HEAD_CHANGED")
    # Detect an index changed by an overlapping actor during the read interval.
    _require(_entries(_read(read_git, "ls-files", "--full-name", "--stage", "-v", "-z"), index=True) == index,
             "PUBLICATION_GIT_INDEX_CHANGED")
    _require(_head(read_git) == (head_ref, head_oid), "PUBLICATION_GIT_HEAD_CHANGED")
    tree.check()
    require_scope()
    _require(tuple(tree.identity(tree.fd)) == root and tree.mount_id == mount_id,
             "PUBLICATION_GIT_ATTACHMENT_CHANGED")
    return TreeSnapshot(root, mount_id, head_ref, head_oid, tree_oid, parents, entries)


def verify(tree, *, read_git, require_scope, before, allowed_delta, stored_path,
           annex_key, qualified_object_path, binding_digest, profile_digest):
    """Prove the exact allowed file-tree delta and pointer, not payload bytes.

    `before` and `allowed_delta` must come from the coordinator's sealed intent,
    not be inferred from whatever a native command happened to change. An empty
    delta verifies reuse of an existing commitment, not a fabricated new commit.
    """
    store._require_digest(binding_digest)
    store._require_digest(profile_digest)
    relative_path(stored_path)
    _require(type(before) is TreeSnapshot, "PUBLICATION_GIT_BASELINE_REQUIRED")
    _require(isinstance(allowed_delta, Mapping), "PUBLICATION_GIT_DELTA_INVALID")
    expected = {entry.path: entry for entry in before.entries}
    changes = []
    for path, entry in allowed_delta.items():
        relative_path(path)
        _require(entry is None or type(entry) is TreeEntry and entry.path == path,
                 "PUBLICATION_GIT_DELTA_INVALID")
        _require(expected.get(path) != entry, "PUBLICATION_GIT_DELTA_REDUNDANT")
        if entry is None:
            del expected[path]
        else:
            expected[path] = entry
        changes.append((path, entry))
    after = capture(tree, read_git=read_git, require_scope=require_scope)
    _require((after.root_identity, after.mount_id, after.head_ref) ==
             (before.root_identity, before.mount_id, before.head_ref), "PUBLICATION_GIT_BASELINE_CHANGED")
    _require(after.entries == tuple(sorted(expected.values())), "PUBLICATION_GIT_UNEXPECTED_TREE_DELTA")
    if changes:
        _require(after.parents == (before.head_oid,) and after.head_oid != before.head_oid,
                 "PUBLICATION_GIT_COMMIT_LINEAGE_MISMATCH")
    else:
        _require(after.head_oid == before.head_oid, "PUBLICATION_GIT_NOOP_HEAD_CHANGED")
    entry = expected.get(stored_path)
    _require(entry is not None, "PUBLICATION_GIT_POINTER_MISSING")
    blob = _object(read_git, "blob", entry.oid, limit=4096)
    check_committed_pointer(mode=entry.mode, blob=blob, stored_path=stored_path,
                            key=annex_key, qualified_object_path=qualified_object_path)
    # The pointer read must not create a gap in the observed ref/index scope.
    _require(capture(tree, read_git=read_git, require_scope=require_scope) == after,
             "PUBLICATION_GIT_FINAL_STATE_CHANGED")
    return CommittedTreeProof(binding_digest, profile_digest, store.digest(before.record()), after,
                              tuple(sorted(changes)), stored_path, annex_key, qualified_object_path,
                              entry.oid, "locked" if entry.mode == "120000" else "unlocked")
