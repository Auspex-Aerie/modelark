"""Journalled local replica-object import and exclusive pointer staging.

Only consumes the installer's verified disposable copy, never the retained
source artifact. File/catalog phases, commits, map publication and clean anchors
belong to the enclosing publisher. Native success alone produces no receipt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import PurePosixPath
import posixpath
import stat
import uuid

from modelark import publication_actions as actions, publication_catalog as catalog
from modelark import publication_payload as payload, publication_store as store, publication_tree as trees
from modelark.publication_artifact import VerifiedArtifact
from modelark.publication_native import QualifiedRepository
from modelark.publication_policy import PublicationRefused, parse_sha256_key, relative_path
from modelark.slice.transaction import TransferRefusal


@dataclass(frozen=True)
class ReplicaResult:
    local: payload.LocalPayloadProof
    actions: tuple[dict, ...]


def _id(file_id, name):
    return str(uuid.uuid5(uuid.UUID(file_id), name))


def _require(repository, artifact):
    if type(repository) is not QualifiedRepository or type(artifact) is not VerifiedArtifact:
        raise PublicationRefused("PUBLICATION_REPLICA_CAPABILITY_REQUIRED")
    if repository.scope is not artifact.scope or repository.drive_label is None:
        raise PublicationRefused("PUBLICATION_REPLICA_SCOPE_MISMATCH")
    repository.ensure()
    artifact.check()


def _file(repository, artifact, file_id):
    _require(repository, artifact)
    scope = repository.scope
    store.canonical_uuid(file_id)
    scope.connection.execute("BEGIN")
    try:
        scope.require()
        _, operation_digest, _ = store._load_bound_operation(scope, scope.operation_id)
        row, frozen, _ = store._file_chain(scope.connection, scope.operation_id, file_id, operation_digest)
        if row[3] != "PREPARED":
            raise PublicationRefused("PUBLICATION_REPLICA_PHASE_CHANGED")
        value, seal = frozen["intent"], row[2]
    finally:
        scope.connection.rollback()
    scope.require_io()
    try:
        path, key, obj = value["stored_path"], value["annex_key"], value["object_path"]
        relative_path(path)
        size, digest = parse_sha256_key(key)
        proof = artifact.proof
        if ((size, digest) != (proof.stored_bytes, proof.stored_sha256)
                or value["install"]["stored_path"] != path
                or value["install"]["artifact"] != proof.record()):
            raise PublicationRefused("PUBLICATION_REPLICA_ARTIFACT_MISMATCH")
        pair = value["catalog_pair"]
        catalog._validate(pair)
        source = pair["before"]["source"]
        after = pair["after"]["archived"]
        if (source is None or source["key"]["drive_label"] not in scope.identities
                or after["drive_label"] != repository.drive_label
                or path != after["repo_id"] + "/" + after["stored_relpath"]
                or after["annex_key"] != key or source["archived"]["annex_key"] != key):
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_MISMATCH")
        for name in ("orig_sha256", "orig_bytes", "stored_bytes", "compressed", "znn_sha256",
                     "orig_sha256_provenance"):
            if source["archived"][name] != after[name]:
                raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_MISMATCH")
        if (after["orig_sha256"] != proof.original_sha256 or after["orig_bytes"] != proof.original_bytes
                or after["stored_bytes"] != size or after["compressed"] != int(proof.compressed)
                or (after["znn_sha256"] if proof.compressed else after["orig_sha256"]) != digest):
            raise PublicationRefused("PUBLICATION_REPLICA_ARTIFACT_MISMATCH")
        if repository.object_path(key) != obj:
            raise PublicationRefused("PUBLICATION_REPLICA_OBJECT_PATH_CHANGED")
        before = trees.TreeSnapshot.from_record(value["tree_before"])
        entry = trees.TreeEntry(**value["entry"])
        link = posixpath.relpath(obj, str(PurePosixPath(path).parent))
        blob = link.encode("ascii")
        expected = trees.TreeEntry(path, "120000", hashlib.sha1(
            b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest())
        if (entry != expected or any(item.path == path for item in before.entries)
                or before.root_identity != tuple(repository.tree.identity(repository.tree.fd))
                or before.mount_id != repository.tree.mount_id):
            raise PublicationRefused("PUBLICATION_REPLICA_TREE_INTENT_MISMATCH")
    except (KeyError, TypeError, UnicodeError) as exc:
        raise PublicationRefused("PUBLICATION_REPLICA_FILE_INTENT_INVALID") from exc
    return value, seal, before, entry, link


def _parents(repository, path):
    result = []
    parts = relative_path(path).parts
    for offset in range(1, len(parts)):
        name = "/".join(parts[:offset])
        fd = repository.tree.open(name, os.O_RDONLY | os.O_DIRECTORY)
        try:
            value = os.fstat(fd)
            result.append({"path": name, "identity": [value.st_dev, value.st_ino, value.st_mode]})
        finally:
            os.close(fd)
    return result


def _target(repository, path):
    with repository.tree.parent(path) as (parent, name):
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None


def _regular(repository, path, size, digest):
    try:
        identity = payload._regular(repository.tree, path, size, digest)
        payload._recheck_regular(repository.tree, path, identity)
    except FileNotFoundError as exc:
        raise PublicationRefused("PUBLICATION_PAYLOAD_UNAVAILABLE", path=path) from exc
    return list(identity)


def _index(repository, before, entry, *, allow_staged):
    repository.ensure()
    if trees._head(repository.read) != (before.head_ref, before.head_oid):
        raise PublicationRefused("PUBLICATION_REPLICA_HEAD_CHANGED")
    actual = trees._entries(repository.read("ls-files", "--full-name", "--stage", "-v", "-z"), index=True)
    expected = tuple(sorted((*before.entries, entry)))
    if actual != before.entries and (not allow_staged or actual != expected):
        raise PublicationRefused("PUBLICATION_REPLICA_INDEX_CHANGED")
    return actual == expected, expected


def _maybe(scope, identifier):
    try:
        return actions.read(scope, identifier)
    except PublicationRefused as exc:
        if exc.code != "PUBLICATION_ACTION_MISSING":
            raise
        return None


def _base(repository, file_id, dependencies):
    profile = repository.profile.record()
    return {"expected_phase": {"entity": "file", "id": file_id, "phase": "PREPARED"},
            "profile_digest": repository.profile.digest,
            "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
            "after": [{"action_id": row["action_id"], "receipt_digest": row["receipt_digest"]}
                      for row in dependencies]}


def _prepare(repository, identifier, kind, intent):
    scope = repository.scope
    existing = _maybe(scope, identifier)
    scope.write(lambda _: actions.prepare(scope, action_id=identifier, kind=kind, intent=intent))
    return actions.read(scope, identifier), existing is None


def _receipt(repository, record, receipt):
    scope = repository.scope
    scope.write(lambda _: actions.verify(scope, action_id=record["action_id"],
                                         intent_digest=record["intent_digest"], receipt=receipt))
    return actions.read(scope, record["action_id"])


def _flush_object(repository, obj):
    fd = repository.tree.open(obj, os.O_RDONLY | os.O_NONBLOCK)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    parts = obj.split("/")
    for offset in range(len(parts) - 1, 0, -1):
        fd = repository.tree.open("/".join(parts[:offset]), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def apply(repository, artifact, *, file_id):
    """Import one prepared replica, returning proofs without advancing file phase.

    A PREPARED native action is never blindly executed again. Exact already
    completed object or index state can close its receipt without native replay;
    ambiguous untouched/failed native states require explicit continuation.
    """
    try:
        return _apply(repository, artifact, file_id)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_REPLICA_NAMESPACE_UNPROVEN", detail=str(exc)) from exc


def _apply(repository, artifact, file_id):
    value, seal, before, entry, link = _file(repository, artifact, file_id)
    scope = repository.scope
    path, key, obj = (value[name] for name in ("stored_path", "annex_key", "object_path"))
    size, digest = parse_sha256_key(key)
    installed = actions.read(scope, _id(file_id, "payload-install"))
    if (installed["kind"] != "payload_install" or installed["status"] != "VERIFIED"
            or installed["intent"].get("filesystem_plan") != value["install"]):
        raise PublicationRefused("PUBLICATION_REPLICA_INSTALL_UNVERIFIED")
    install_receipt = installed["receipt"]["receipt"]
    identity = install_receipt.get("target_identity")
    if (type(identity) is not list or len(identity) != 6 or any(type(item) is not int for item in identity)
            or identity[:2] == list(artifact.proof.identity[:2])
            or install_receipt.get("artifact_digest") != store.digest(artifact.proof.record())
            or install_receipt.get("stored_path") != path
            or (install_receipt.get("stored_bytes"), install_receipt.get("stored_sha256")) != (size, digest)):
        raise PublicationRefused("PUBLICATION_REPLICA_INSTALL_IDENTITY_UNPROVEN")
    parents = _parents(repository, path)
    if len(value["install"]["parents"]) != len(parents):
        raise PublicationRefused("PUBLICATION_REPLICA_PARENT_CHANGED")
    for frozen, current in zip(value["install"]["parents"], parents):
        if frozen["identity"] is not None and frozen != current:
            raise PublicationRefused("PUBLICATION_REPLICA_PARENT_CHANGED")
    setkey_id = _id(file_id, "replica-setkey")
    prior_setkey = _maybe(scope, setkey_id)
    if prior_setkey is None:
        _index(repository, before, entry, allow_staged=False)
        actual = _regular(repository, path, size, digest)
        target = _target(repository, path)
        if (actual != identity or target is None or target.st_nlink != 1 or target.st_uid != os.geteuid()
                or not stat.S_ISREG(target.st_mode)):
            raise PublicationRefused("PUBLICATION_REPLICA_INPUT_CHANGED")
    argv = ("annex", "setkey", "--", key, path)
    setkey_intent = {**_base(repository, file_id, [installed]), "file_digest": seal, "parents": parents,
                     "content_input": {"path": path, "identity": identity,
                                       "stored_bytes": size, "stored_sha256": digest},
                     "command": {"argv": list(argv), "stdin_digest": hashlib.sha256(b"").hexdigest()}}
    setkey, first = _prepare(repository, setkey_id, "annex_add", setkey_intent)
    if first:
        # Pre-existing objects are allowed only when independently valid. Native
        # setkey preserves such an object and consumes only this disposable copy.
        try:
            _regular(repository, obj, size, digest)
        except PublicationRefused as exc:
            if exc.code != "PUBLICATION_PAYLOAD_UNAVAILABLE":
                raise
        _require(repository, artifact)
        if _parents(repository, path) != parents:
            raise PublicationRefused("PUBLICATION_REPLICA_PARENT_CHANGED")
        repository._run_action(setkey_id, *argv)
    if setkey["status"] == "PREPARED":
        if _target(repository, path) is not None:
            raise PublicationRefused("PUBLICATION_REPLICA_NATIVE_CONTINUATION_REQUIRED", action_id=setkey_id)
        obj_identity = _regular(repository, obj, size, digest)
        _flush_object(repository, obj)
        if _regular(repository, obj, size, digest) != obj_identity:
            raise PublicationRefused("PUBLICATION_REPLICA_OBJECT_CHANGED")
        if _parents(repository, path) != parents:
            raise PublicationRefused("PUBLICATION_REPLICA_PARENT_CHANGED")
        with repository.tree.parent(path) as (parent, _):
            os.fsync(parent)
        _require(repository, artifact)
        setkey = _receipt(repository, setkey, {"version": 1, "annex_key": key, "object_path": obj,
            "object_identity": obj_identity, "consumed_path": path, "consumed_identity": identity})
    if _regular(repository, obj, size, digest) != setkey["receipt"]["receipt"]["object_identity"]:
        raise PublicationRefused("PUBLICATION_REPLICA_OBJECT_CHANGED")

    pointer_id = _id(file_id, "replica-pointer")
    existing_pointer = _maybe(scope, pointer_id)
    if existing_pointer is None and _target(repository, path) is not None:
        raise PublicationRefused("PUBLICATION_TARGET_OCCUPIED", path=path)
    pointer_plan = {"version": 1, "kind": "replica-pointer", "file_digest": seal,
                    "stored_path": path, "link": link, "parents": parents, "target_before": None,
                    "object_identity": setkey["receipt"]["receipt"]["object_identity"]}
    pointer, _ = _prepare(repository, pointer_id, "payload_install",
        {**_base(repository, file_id, [setkey]), "filesystem_plan": pointer_plan})
    _require(repository, artifact)
    if _parents(repository, path) != parents:
        raise PublicationRefused("PUBLICATION_REPLICA_PARENT_CHANGED")
    with repository.tree.parent(path) as (parent, name):
        target = _target(repository, path)
        if target is None:
            if pointer["status"] == "VERIFIED":
                raise PublicationRefused("PUBLICATION_REPLICA_POINTER_CHANGED")
            os.symlink(link, name, dir_fd=parent)
        elif not stat.S_ISLNK(target.st_mode) or os.readlink(name, dir_fd=parent) != link:
            raise PublicationRefused("PUBLICATION_TARGET_OCCUPIED", path=path)
        os.fsync(parent)
    local = payload.verify(repository.tree, stored_path=path, annex_key=key, qualified_object_path=obj,
                           binding_digest=seal, require_scope=scope.require_io)
    if _parents(repository, path) != parents:
        raise PublicationRefused("PUBLICATION_REPLICA_PARENT_CHANGED")
    _require(repository, artifact)
    pointer = _receipt(repository, pointer, {"version": 1, "local": local.record(), "parents": parents})

    stage_id = _id(file_id, "replica-stage")
    existing_stage = _maybe(scope, stage_id)
    staged, expected = _index(repository, before, entry, allow_staged=existing_stage is not None)
    argv = ("add", "-f", "--", path)
    stage, first = _prepare(repository, stage_id, "annex_add",
        {**_base(repository, file_id, [pointer]), "file_digest": seal,
         "command": {"argv": list(argv), "stdin_digest": hashlib.sha256(b"").hexdigest()}})
    if first:
        _require(repository, artifact)
        repository._run_action(stage_id, *argv)
    elif stage["status"] == "PREPARED" and not staged:
        raise PublicationRefused("PUBLICATION_REPLICA_NATIVE_CONTINUATION_REQUIRED", action_id=stage_id)
    staged, _ = _index(repository, before, entry, allow_staged=True)
    if not staged:
        raise PublicationRefused("PUBLICATION_REPLICA_INDEX_CHANGED")
    local = payload.verify(repository.tree, stored_path=path, annex_key=key, qualified_object_path=obj,
                           binding_digest=seal, require_scope=scope.require_io)
    if _parents(repository, path) != parents:
        raise PublicationRefused("PUBLICATION_REPLICA_PARENT_CHANGED")
    _require(repository, artifact)
    stage = _receipt(repository, stage, {"version": 1, "local": local.record(),
                                        "index": [asdict(item) for item in expected]})
    return ReplicaResult(local, (setkey, pointer, stage))
