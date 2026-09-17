"""Read-only, descriptor-confined stored-payload verification for ArchivePublisher.

Proves actual object bytes AND readable mapped path, not original decompression,
Git commitment, location claims, catalog authority or clean closure. The caller
holds the publication scope and separately qualifies the native object path. A
receipt records checks, not cryptographic authority; it is not reusable across a
different file intent, attachment or publication scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import posixpath
import stat

from modelark import publication_store as store
from modelark.publication_policy import PublicationRefused, check_committed_pointer, parse_sha256_key, relative_path
from modelark.slice.transaction import TransferRefusal


_CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class LocalPayloadProof:
    """Observed stored bytes only; codec/original-byte evidence stays separate."""
    binding_digest: str
    root_identity: tuple
    mount_id: int
    stored_path: str
    object_path: str
    annex_key: str
    stored_sha256: str
    stored_bytes: int
    object_identity: tuple
    mapped_identity: tuple
    representation: str

    def record(self):
        from dataclasses import asdict
        # Durable value is canonical JSON-shaped data, including identity arrays.
        # A fresh receipt and its saved/resumed form must compare identically.
        return json.loads(store.canonical({"version": 1, "kind": "descriptor-stored-payload", **asdict(self)}))


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _regular(tree, path, expected_size, expected_sha):
    # NONBLOCK keeps an unexpected FIFO from hanging before fstat can reject it.
    fd = tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise PublicationRefused("PUBLICATION_PAYLOAD_SIZE_OR_TYPE_MISMATCH", path=path)
        digest = hashlib.sha256()
        remaining = expected_size
        while remaining:
            block = os.read(fd, min(_CHUNK, remaining))
            if not block:
                raise PublicationRefused("PUBLICATION_PAYLOAD_TRUNCATED", path=path)
            digest.update(block)
            remaining -= len(block)
        if os.read(fd, 1) or _identity(os.fstat(fd)) != _identity(before):
            raise PublicationRefused("PUBLICATION_PAYLOAD_CHANGED", path=path)
        if digest.hexdigest() != expected_sha:
            raise PublicationRefused("PUBLICATION_PAYLOAD_HASH_MISMATCH", path=path)
        return _identity(before)
    finally:
        os.close(fd)


def _recheck_regular(tree, path, expected):
    fd = tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if _identity(os.fstat(fd)) != expected:
            raise PublicationRefused("PUBLICATION_PAYLOAD_CHANGED", path=path)
    finally:
        os.close(fd)


def verify(tree, *, stored_path, annex_key, qualified_object_path, binding_digest, require_scope):
    """Hash physical object and mapped reader under the caller's held IO scope.

    `require_scope` is the actual scope's require method, not a success flag. The
    higher-level coordinator owns selecting that capability and binding the
    qualified profile/file intent; this lower IO primitive cannot establish it.
    Reject write transactions in that coordinator before calling this function.
    No native command, symlink following, retrieval, rewriting or decompression.
    """
    store._require_digest(binding_digest)
    relative_path(stored_path)
    size, sha = parse_sha256_key(annex_key)
    # Reuse the same native-qualified path grammar as committed-tree validation.
    expected_link = posixpath.relpath(qualified_object_path, str(PurePosixPath(stored_path).parent)).encode()
    check_committed_pointer(mode="120000", blob=expected_link, stored_path=stored_path,
                            key=annex_key, qualified_object_path=qualified_object_path)
    require_scope()
    try:
        tree.check()
        object_identity = _regular(tree, qualified_object_path, size, sha)
        with tree.parent(stored_path) as (parent, name):
            mapped_stat = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(mapped_stat.st_mode):
                link = os.readlink(os.fsencode(name), dir_fd=parent)
                if link != expected_link:
                    raise PublicationRefused("PUBLICATION_MAPPED_POINTER_MISMATCH", path=stored_path)
                mapped_identity = _identity(mapped_stat)
                representation = "locked"
            elif stat.S_ISREG(mapped_stat.st_mode):
                representation = "unlocked"
                # An unlocked worktree may be a distinct file. Its bytes must be
                # hashed too; a populated object alone is not mapped readability.
                mapped_identity = _regular(tree, stored_path, size, sha)
            else:
                raise PublicationRefused("PUBLICATION_MAPPED_TYPE_INVALID", path=stored_path)
        _recheck_regular(tree, qualified_object_path, object_identity)
        if representation == "locked":
            with tree.parent(stored_path) as (parent, name):
                if (_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != mapped_identity
                        or os.readlink(os.fsencode(name), dir_fd=parent) != expected_link):
                    raise PublicationRefused("PUBLICATION_MAPPED_POINTER_CHANGED", path=stored_path)
        else:
            _recheck_regular(tree, stored_path, mapped_identity)
        tree.check()
        require_scope()
        return LocalPayloadProof(binding_digest, tuple(tree.identity(tree.fd)), tree.mount_id, stored_path,
                                 qualified_object_path, annex_key, sha, size, object_identity,
                                 mapped_identity, representation)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_PAYLOAD_UNAVAILABLE", path=stored_path) from exc
