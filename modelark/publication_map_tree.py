"""Git-only metadata observations through the real qualified publication reader.

Safe to use on quarantine refs before admitting them to a native annex merge:
this module never invokes annex, parses a configured transport, updates a ref or
touches the index. Native annex-8 hashdirlower correspondence is qualified by the
disposable native tests, not inferred from the object-store mixed-case buckets.
The result proves a bounded immutable Git snapshot, not metadata admission,
physical presence, native whereis agreement, or publication completion.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from modelark import publication_native as native, publication_store as store
from modelark.publication_policy import PublicationRefused, _require, parse_sha256_key, relative_path

_OID = re.compile(r"[0-9a-f]{40}\Z")
_REF = re.compile(r"refs/[A-Za-z0-9_./-]+\Z")
_COMMIT_LIMIT = 1024 * 1024
_TREE_LIMIT = 1024 * 1024
_TREE_TOTAL_LIMIT = 8 * 1024 * 1024
_BLOB_LIMIT = 1024 * 1024
_BLOB_TOTAL_LIMIT = 16 * 1024 * 1024
_ENTRY_LIMIT = 65536
_DEPTH_LIMIT = 16


def metadata_path(key: str) -> str:
    """Pinned annex-8 SHA256-family key location-log path; no native authority.

    MD5 is the native directory-distribution convention, not a content digest or
    security proof. The full qualified SHA256/SHA256E key, including its exact
    suffix, determines the bucket; unqualified key grammars remain refused.
    """
    parse_sha256_key(key)
    bucket = hashlib.md5(key.encode("ascii"), usedforsecurity=False).hexdigest()
    return f"{bucket[:3]}/{bucket[3:6]}/{key}.log"


def _oid(value):
    _require(isinstance(value, str) and _OID.fullmatch(value) is not None,
             "PUBLICATION_GIT_OID_UNQUALIFIED")
    return value


def _reference(value):
    _require(isinstance(value, str), "PUBLICATION_METADATA_REF_INVALID")
    if _OID.fullmatch(value):
        return value
    _require(_REF.fullmatch(value) is not None and ".." not in value
             and all(part and part not in {".", ".."} and not part.startswith(".")
                     and not part.endswith((".", ".lock")) for part in value.split("/")),
             "PUBLICATION_METADATA_REF_INVALID")
    return value


def _line(data):
    _require(type(data) is bytes and data.endswith(b"\n") and b"\n" not in data[:-1]
             and len(data) <= 128, "PUBLICATION_METADATA_NATIVE_FRAMING")
    try:
        return data[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PublicationRefused("PUBLICATION_METADATA_NATIVE_FRAMING") from exc


def _object(repository, kind, oid, *, limit):
    _oid(oid)
    size_text = _line(repository.read("cat-file", "-s", oid))
    _require(re.fullmatch(r"0|[1-9][0-9]*", size_text) is not None,
             "PUBLICATION_METADATA_OBJECT_SIZE_INVALID")
    size = int(size_text)
    _require(size <= limit, "PUBLICATION_METADATA_OBJECT_TOO_LARGE", kind=kind, size=size)
    raw = repository.read("cat-file", kind, oid)
    _require(type(raw) is bytes and len(raw) == size,
             "PUBLICATION_METADATA_OBJECT_MISMATCH")
    actual = hashlib.sha1(kind.encode("ascii") + b" " + str(size).encode("ascii") + b"\0" + raw).hexdigest()
    _require(actual == oid, "PUBLICATION_METADATA_OBJECT_MISMATCH")
    return raw


def _commit(repository, oid):
    raw = _object(repository, "commit", oid, limit=_COMMIT_LIMIT)
    headers, separator, _ = raw.partition(b"\n\n")
    _require(bool(separator), "PUBLICATION_METADATA_COMMIT_INVALID")
    lines = headers.split(b"\n")
    _require(lines[0].startswith(b"tree ") and sum(line.startswith(b"tree ") for line in lines) == 1,
             "PUBLICATION_METADATA_COMMIT_INVALID")
    try:
        tree_oid = _oid(lines[0][5:].decode("ascii"))
        parents = tuple(_oid(line[7:].decode("ascii")) for line in lines if line.startswith(b"parent "))
    except UnicodeDecodeError as exc:
        raise PublicationRefused("PUBLICATION_METADATA_COMMIT_INVALID") from exc
    return tree_oid, parents


def _key_path(path):
    relative_path(path)
    base = path.removesuffix(".met")
    if not base.endswith(".log"):
        return
    name = base.rsplit("/", 1)[-1][:-4]
    # Root policy logs (uuid.log, trust.log, etc.) are observed without interpreting
    # them. Key-shaped names and all sharded logs must have the qualified layout.
    if "/" not in base and not name.startswith("SHA256"):
        return
    parse_sha256_key(name)
    _require(base == metadata_path(name), "PUBLICATION_METADATA_KEY_PATH_MISMATCH", path=path)


@dataclass(frozen=True, order=True)
class MetadataBlob:
    path: str
    oid: str
    data: bytes

    def __post_init__(self):
        _key_path(self.path)
        _oid(self.oid)
        _require(type(self.data) is bytes and len(self.data) <= _BLOB_LIMIT,
                 "PUBLICATION_METADATA_BLOB_INVALID")
        actual = hashlib.sha1(b"blob " + str(len(self.data)).encode() + b"\0" + self.data).hexdigest()
        _require(actual == self.oid, "PUBLICATION_METADATA_OBJECT_MISMATCH")

    def record(self):
        return {"path": self.path, "mode": "100644", "oid": self.oid,
                "size": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest(),
                "data_hex": self.data.hex()}


@dataclass(frozen=True)
class MetadataSnapshot:
    binding_digest: str
    profile_digest: str
    root_identity: tuple[int, int, int]
    mount_id: int
    ref: str
    commit_oid: str
    tree_oid: str
    parents: tuple[str, ...]
    entries: tuple[MetadataBlob, ...]

    def __post_init__(self):
        store._require_digest(self.binding_digest)
        store._require_digest(self.profile_digest)
        _require(type(self.root_identity) is tuple and len(self.root_identity) == 3
                 and all(type(value) is int and value >= 0 for value in self.root_identity)
                 and type(self.mount_id) is int and self.mount_id > 0,
                 "PUBLICATION_METADATA_SNAPSHOT_INVALID")
        _reference(self.ref)
        _oid(self.commit_oid)
        _oid(self.tree_oid)
        _require(type(self.parents) is tuple and type(self.entries) is tuple
                 and all(type(value) is MetadataBlob for value in self.entries)
                 and self.entries == tuple(sorted(self.entries))
                 and len({entry.path for entry in self.entries}) == len(self.entries)
                 and len(self.entries) <= _ENTRY_LIMIT
                 and sum(len(entry.data) for entry in self.entries) <= _BLOB_TOTAL_LIMIT,
                 "PUBLICATION_METADATA_SNAPSHOT_INVALID")
        for parent in self.parents:
            _oid(parent)
        paths = {entry.path for entry in self.entries}
        _require(all(not any("/".join(path.split("/")[:index]) in paths
                             for index in range(1, len(path.split("/")))) for path in paths),
                 "PUBLICATION_METADATA_DUPLICATE_PATH")

    def metadata(self) -> dict[str, bytes]:
        """A detached byte mapping for pure policy; snapshot internals stay frozen."""
        return {entry.path: entry.data for entry in self.entries}

    def record(self) -> dict:
        return {"version": 1, "kind": "native-metadata-tree", "binding_digest": self.binding_digest,
                "profile_digest": self.profile_digest, "root_identity": list(self.root_identity),
                "mount_id": self.mount_id, "ref": self.ref, "commit_oid": self.commit_oid,
                "tree_oid": self.tree_oid, "parents": list(self.parents),
                "entries": [entry.record() for entry in self.entries]}

    @property
    def digest(self) -> str:
        return store.digest(self.record())


def capture(repository: native.QualifiedRepository, *, ref: str, binding_digest: str,
            expected_oid: str | None = None) -> MetadataSnapshot:
    """Capture real immutable commit/tree/blob objects and recheck the named ref.

    Every object is size-checked before reading, then independently SHA1-checked;
    raw tree traversal verifies nested object hashes rather than trusting ls-tree
    expansion of possibly corrupted loose objects. Aggregate limits bound memory
    and tree fanout. Missing refs and unexpected races refuse, never empty-success.
    """
    _require(type(repository) is native.QualifiedRepository, "PUBLICATION_METADATA_READER_UNQUALIFIED")
    _reference(ref)
    store._require_digest(binding_digest)
    if expected_oid is not None:
        _oid(expected_oid)
    repository.ensure()
    root, mount = tuple(repository.tree.identity(repository.tree.fd)), repository.tree.mount_id
    profile_digest = repository.profile.digest
    oid = _oid(_line(repository.read("rev-parse", "--verify", ref)))
    _require(expected_oid is None or oid == expected_oid, "PUBLICATION_METADATA_REF_CHANGED")
    tree_oid, parents = _commit(repository, oid)
    entries, total = [], {"tree": 0, "blob": 0}

    def walk(current_oid, prefix, depth):
        _require(depth <= _DEPTH_LIMIT, "PUBLICATION_METADATA_TREE_LIMIT")
        raw = _object(repository, "tree", current_oid,
                      limit=min(_TREE_LIMIT, _TREE_TOTAL_LIMIT - total["tree"]))
        # Pure admission compares complete leaf maps. Reject invisible empty
        # subtrees so a hostile tree cannot change without a corresponding leaf.
        _require(not prefix or bool(raw), "PUBLICATION_METADATA_EMPTY_DIRECTORY")
        total["tree"] += len(raw)
        offset, last_order, names = 0, None, set()
        while offset < len(raw):
            end = raw.find(b"\0", offset)
            _require(end >= 0 and end + 21 <= len(raw), "PUBLICATION_METADATA_TREE_INVALID")
            header = raw[offset:end]
            mode, separator, name = header.partition(b" ")
            _require(separator and mode in {b"40000", b"100644"} and name and b"/" not in name,
                     "PUBLICATION_METADATA_MODE_UNQUALIFIED")
            _require(name not in names, "PUBLICATION_METADATA_DUPLICATE_PATH")
            names.add(name)
            order = name + (b"/" if mode == b"40000" else b"")
            _require(last_order is None or last_order < order, "PUBLICATION_METADATA_TREE_ORDER_INVALID")
            last_order = order
            try:
                path = prefix + name.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PublicationRefused("PUBLICATION_METADATA_PATH_INVALID") from exc
            relative_path(path)
            object_oid = raw[end + 1:end + 21].hex()
            offset = end + 21
            if mode == b"40000":
                walk(object_oid, path + "/", depth + 1)
                continue
            _require(len(entries) < _ENTRY_LIMIT, "PUBLICATION_METADATA_TREE_LIMIT")
            _key_path(path)
            blob = _object(repository, "blob", object_oid,
                           limit=min(_BLOB_LIMIT, _BLOB_TOTAL_LIMIT - total["blob"]))
            total["blob"] += len(blob)
            entries.append(MetadataBlob(path, object_oid, blob))

    walk(tree_oid, "", 0)
    _require(_line(repository.read("rev-parse", "--verify", ref)) == oid,
             "PUBLICATION_METADATA_REF_CHANGED")
    repository.ensure()
    _require(tuple(repository.tree.identity(repository.tree.fd)) == root
             and repository.tree.mount_id == mount and repository.profile.digest == profile_digest,
             "PUBLICATION_METADATA_ATTACHMENT_CHANGED")
    return MetadataSnapshot(binding_digest, profile_digest, root, mount, ref, oid,
                            tree_oid, parents, tuple(sorted(entries)))
