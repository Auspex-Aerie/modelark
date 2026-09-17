"""Retained staged-byte capability, with independent original/decode verification.

No archive mutation or caller-supplied success receipt. Raw and compressed inputs
share the established bounded original reader and codec RAM policy. The source
descriptor stays open through installation; pathname/inode drift refuses reuse.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import stat

from modelark import artifact_io, artifact_policy, publication_locks, publication_store as store
from modelark.publication_policy import PublicationRefused
from modelark.slice.linux import BoundTree


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@dataclass(frozen=True)
class ArtifactProof:
    root_identity: tuple
    mount_id: int
    name: str
    identity: tuple
    compressed: bool
    stored_bytes: int
    stored_sha256: str
    original_bytes: int
    original_sha256: str
    decode_policy_digest: str

    def record(self):
        return json.loads(store.canonical({"version": 1, "kind": "verified-staged-artifact", **asdict(self)}))


class VerifiedArtifact:
    """Construct by observing bytes; cannot be constructed from a saved JSON proof."""

    def __init__(self, scope, path, *, compressed, original_bytes, original_sha256, stored_sha256):
        if type(scope) is not publication_locks._FenceScope:
            raise PublicationRefused("PUBLICATION_FENCE_AUTHORITY_MISSING")
        if type(compressed) is not bool or type(original_bytes) is not int or original_bytes < 0:
            raise PublicationRefused("PUBLICATION_ARTIFACT_FACTS_INVALID")
        store._require_digest(original_sha256)
        store._require_digest(stored_sha256)
        scope.require_io()
        path = Path(path)
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise PublicationRefused("PUBLICATION_ARTIFACT_PATH_INVALID")
        self._scope, self._closed, self._stack = scope, False, ExitStack()
        self.name = path.name
        try:
            self.tree = self._stack.enter_context(BoundTree(path.parent))
            self.fd = self.tree.open(self.name, os.O_RDONLY | os.O_NONBLOCK)
            self._stack.callback(os.close, self.fd)
            before = os.fstat(self.fd)
            if not stat.S_ISREG(before.st_mode):
                raise PublicationRefused("PUBLICATION_ARTIFACT_TYPE_INVALID")
            self._identity = _identity(before)
            digest = hashlib.sha256()
            while block := self.read(4 << 20):
                digest.update(block)
            if digest.hexdigest() != stored_sha256:
                raise PublicationRefused("PUBLICATION_ARTIFACT_STORED_HASH_MISMATCH")
            self.rewind()
            policy = artifact_policy.qualified_policy()
            original = hashlib.sha256()
            # A duplicated reader shares the retained open-file description. It
            # is closed before rewind; no native helper receives an archive path.
            with os.fdopen(os.dup(self.fd), "rb") as stream, closing(artifact_io.original_stream(
                    stream, compressed=compressed, expected_bytes=original_bytes, limits=policy.limits,
                    policy=policy, check=self.check)) as reader:
                while block := reader.read(policy.limits.read_bytes):
                    original.update(block)
            if original.hexdigest() != original_sha256:
                raise PublicationRefused("PUBLICATION_ARTIFACT_ORIGINAL_HASH_MISMATCH")
            self.check()
            scope.require_io()
            self._proof = ArtifactProof(tuple(self.tree.identity(self.tree.fd)), self.tree.mount_id, self.name,
                                        self._identity, compressed, before.st_size, stored_sha256,
                                        original_bytes, original_sha256, store.digest(policy.to_record()))
            self._proof_seal = store.digest(self._proof.record())
            self.rewind()
        except BaseException:
            self._closed = True
            self._stack.close()
            raise

    @property
    def scope(self):
        return self._scope

    @property
    def proof(self):
        self.check()
        return self._proof

    def check(self):
        if self._closed:
            raise PublicationRefused("PUBLICATION_ARTIFACT_CLOSED")
        if hasattr(self, "_proof_seal") and store.digest(self._proof.record()) != self._proof_seal:
            raise PublicationRefused("PUBLICATION_ARTIFACT_PROOF_CHANGED")
        self.scope.require_io()
        self.tree.check()
        current = self.tree.open(self.name, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if _identity(os.fstat(current)) != self._identity or _identity(os.fstat(self.fd)) != self._identity:
                raise PublicationRefused("PUBLICATION_ARTIFACT_CHANGED")
        finally:
            os.close(current)

    def read(self, count):
        if type(count) is not int or not 0 < count <= 4 << 20:
            raise PublicationRefused("PUBLICATION_ARTIFACT_READ_LIMIT")
        self.check()
        result = os.read(self.fd, count)
        self.check()
        return result

    def rewind(self):
        self.check()
        os.lseek(self.fd, 0, os.SEEK_SET)

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *_):
        self._closed = True
        self._stack.close()
