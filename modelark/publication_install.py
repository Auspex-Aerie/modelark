"""Exclusive staged-payload installation from an immutable durable file intent.

Only the exact recorded missing path/parent namespace may be created. Native
annex admission, committed-tree verification and catalog publication are later
steps; installing bytes never supplies their receipts or a clean anchor.
"""
from __future__ import annotations

import hashlib
import os
import stat
import uuid

from modelark import publication_actions as actions, publication_store as store
from modelark.publication_artifact import VerifiedArtifact
from modelark.publication_native import QualifiedRepository
from modelark.publication_policy import PublicationRefused, relative_path
from modelark.slice.linux import rename_noreplace
from modelark.slice.transaction import TransferRefusal


def _identity(value):
    return [value.st_dev, value.st_ino, value.st_mode]


def _require(repository, artifact):
    if type(repository) is not QualifiedRepository or type(artifact) is not VerifiedArtifact:
        raise PublicationRefused("PUBLICATION_INSTALL_CAPABILITY_REQUIRED")
    if repository.scope is not artifact.scope or repository.drive_label is None:
        raise PublicationRefused("PUBLICATION_INSTALL_SCOPE_MISMATCH")
    repository.ensure()
    artifact.check()


def preview(repository, artifact, stored_path):
    """Observe absence/parents; coordinator freezes this before payload mutation."""
    try:
        return _preview(repository, artifact, stored_path)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_INSTALL_NAMESPACE_UNPROVEN", detail=str(exc)) from exc


def _preview(repository, artifact, stored_path):
    _require(repository, artifact)
    parts = relative_path(stored_path).parts
    parents, missing = [], False
    for offset in range(1, len(parts)):
        path = "/".join(parts[:offset])
        identity = None
        if not missing:
            try:
                fd = repository.tree.open(path, os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                missing = True
            else:
                try:
                    identity = _identity(os.fstat(fd))
                finally:
                    os.close(fd)
        parents.append({"path": path, "identity": identity})
    if not missing:
        with repository.tree.parent(stored_path) as (parent, name):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PublicationRefused("PUBLICATION_TARGET_OCCUPIED", path=stored_path)
    return {"version": 1, "stored_path": stored_path, "parents": parents,
            "artifact": artifact.proof.record(), "target_before": None}


def prepare(repository, artifact, *, file_id, plan):
    """Bind install plan to this already-prepared file, not an arbitrary pathname."""
    _require(repository, artifact)
    scope = repository.scope
    store.canonical_uuid(file_id)
    action_id = str(uuid.uuid5(uuid.UUID(file_id), "payload-install"))
    profile = repository.profile.record()
    intent = {"expected_phase": {"entity": "file", "id": file_id, "phase": "PREPARED"},
              "profile_digest": repository.profile.digest,
              "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
              "filesystem_plan": plan}
    def write(_):
        binding, operation_digest, _revision = store.load_owned_operation(scope, scope.operation_id)
        _row, frozen, _previous = store._file_chain(scope.connection, scope.operation_id, file_id, operation_digest)
        if frozen["intent"].get("install") != plan:
            raise PublicationRefused("PUBLICATION_INSTALL_FILE_INTENT_MISMATCH")
        return actions.prepare(scope, action_id=action_id, kind="payload_install", intent=intent)
    scope.write(write)
    return action_id


def _regular(repository, path, size, digest):
    from modelark.publication_payload import _regular as verify_regular, _recheck_regular
    identity = verify_regular(repository.tree, path, size, digest)
    _recheck_regular(repository.tree, path, identity)
    return identity


def apply(repository, artifact, action_id):
    """Copy into the owned temporary, fsync and no-replace publish; exact replay.

    Source bytes are retained. Interrupted writes resume only if the existing
    temporary is an exact prefix of that same verified source; a conflicting
    target or namespace never gets overwritten or deleted.
    """
    try:
        return _apply(repository, artifact, action_id)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_INSTALL_NAMESPACE_UNPROVEN", detail=str(exc)) from exc


def _apply(repository, artifact, action_id):
    _require(repository, artifact)
    scope = repository.scope
    record = actions.read(scope, action_id)
    # After annex-add the path is a committed symlink/unlocked representation,
    # not an installer-owned regular target. The coordinator skips this phase
    # and independently proves that representation; never re-run creation here.
    if record["status"] != "PREPARED":
        raise PublicationRefused("PUBLICATION_INSTALL_ALREADY_VERIFIED")
    intent = record["intent"]
    plan = intent.get("filesystem_plan")
    profile = repository.profile.record()
    if (record["kind"] != "payload_install" or type(plan) is not dict
            or set(plan) != {"version", "stored_path", "parents", "artifact", "target_before"}
            or plan["version"] != 1 or plan["artifact"] != artifact.proof.record()
            or plan["target_before"] is not None or intent["profile_digest"] != repository.profile.digest
            or intent["root"] != {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")}):
        raise PublicationRefused("PUBLICATION_INSTALL_INTENT_MISMATCH")
    parts = relative_path(plan["stored_path"]).parts
    expected_parents = ["/".join(parts[:i]) for i in range(1, len(parts))]
    if (type(plan["parents"]) is not list or any(type(row) is not dict for row in plan["parents"])
            or [row.get("path") for row in plan["parents"]] != expected_parents
            or any(set(row) != {"path", "identity"} for row in plan["parents"])):
        raise PublicationRefused("PUBLICATION_INSTALL_INTENT_MISMATCH")
    phase = intent["expected_phase"]
    if phase["entity"] != "file" or phase["phase"] != "PREPARED":
        raise PublicationRefused("PUBLICATION_INSTALL_FILE_INTENT_MISMATCH")
    file_id = store.canonical_uuid(phase["id"])
    if action_id != str(uuid.uuid5(uuid.UUID(file_id), "payload-install")):
        raise PublicationRefused("PUBLICATION_INSTALL_FILE_INTENT_MISMATCH")
    # A journal row is persistence, not permission for an arbitrary filesystem
    # plan. Rebind it to the actual immutable file intent before any mkdir/open.
    scope.connection.execute("BEGIN")
    try:
        scope.require()
        _, operation_digest, _ = store._load_bound_operation(scope, scope.operation_id)
        _, frozen, _ = store._file_chain(scope.connection, scope.operation_id, file_id, operation_digest)
        if frozen["intent"].get("install") != plan:
            raise PublicationRefused("PUBLICATION_INSTALL_FILE_INTENT_MISMATCH")
    finally:
        scope.connection.rollback()
    scope.require_io()
    temporary = ".modelark-publication-" + store.canonical_uuid(action_id) + ".partial"
    for offset, row in enumerate(plan["parents"]):
        path = row["path"]
        with repository.tree.parent(path) as (parent, name):
            if row["identity"] is None:
                try:
                    os.mkdir(name, mode=0o755, dir_fd=parent)
                except FileExistsError:
                    pass
            fd = repository.tree.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if row["identity"] is not None:
                    if _identity(os.fstat(fd)) != row["identity"]:
                        raise PublicationRefused("PUBLICATION_INSTALL_PARENT_CHANGED", path=path)
                else:
                    allowed = {parts[offset + 1]}
                    if offset == len(plan["parents"]) - 1:
                        allowed.add(temporary)
                    with os.scandir(fd) as children:
                        for child in children:
                            if child.name not in allowed:
                                raise PublicationRefused("PUBLICATION_INSTALL_NAMESPACE_CHANGED", path=path)
                os.fsync(fd)
                if row["identity"] is None:
                    # This includes replay after a crash between mkdir and the
                    # parent flush; fsync(child) cannot persist its parent entry.
                    os.fsync(parent)
            finally:
                os.close(fd)
        _require(repository, artifact)
    size, digest = artifact.proof.stored_bytes, artifact.proof.stored_sha256
    target = plan["stored_path"]
    with repository.tree.parent(target) as (parent, name):
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            present = False
        else:
            _regular(repository, target, size, digest)
            present = True
            try:
                os.stat(temporary, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                # Our exclusive rename consumes this exact temporary name.
                # Both names surviving is not one of its replay states, even
                # if their bytes coincide. Preserve both for explicit recovery.
                raise PublicationRefused("PUBLICATION_INSTALL_TEMPORARY_CONFLICT")
        if not present:
            try:
                fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
            except FileExistsError:
                relative = "/".join((*parts[:-1], temporary))
                fd = repository.tree.open(relative, os.O_RDWR | os.O_NONBLOCK)
            try:
                value = os.fstat(fd)
                if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_uid != os.geteuid()
                        or stat.S_IMODE(value.st_mode) != 0o600 or value.st_size > size):
                    raise PublicationRefused("PUBLICATION_INSTALL_TEMPORARY_UNPROVEN")
                artifact.rewind()
                checked = hashlib.sha256()
                existing = value.st_size
                capacity = os.fstatvfs(fd)
                available = capacity.f_bavail * capacity.f_frsize
                required = size - existing
                if available < required:
                    raise PublicationRefused("PUBLICATION_INSTALL_SPACE_INSUFFICIENT",
                                             required_bytes=required, available_bytes=available)
                while existing:
                    part = os.read(fd, min(4 << 20, existing))
                    source = artifact.read(len(part)) if part else b""
                    if not part or source != part:
                        raise PublicationRefused("PUBLICATION_INSTALL_TEMPORARY_UNPROVEN")
                    checked.update(part)
                    existing -= len(part)
                while block := artifact.read(4 << 20):
                    pending = memoryview(block)
                    while pending:
                        written = os.write(fd, pending)
                        if written <= 0:
                            raise PublicationRefused("PUBLICATION_INSTALL_WRITE_FAILED")
                        pending = pending[written:]
                    checked.update(block)
                if checked.hexdigest() != digest or os.fstat(fd).st_size != size:
                    raise PublicationRefused("PUBLICATION_INSTALL_SOURCE_CHANGED")
                os.fsync(fd)
                _require(repository, artifact)
                relative = "/".join((*parts[:-1], temporary))
                verified = _regular(repository, relative, size, digest)
                named = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                current = os.fstat(fd)
                if (_identity(named) != _identity(current) or named.st_nlink != 1
                        or verified != (current.st_dev, current.st_ino, current.st_mode, current.st_size,
                                        current.st_mtime_ns, current.st_ctime_ns)):
                    raise PublicationRefused("PUBLICATION_INSTALL_TEMPORARY_CHANGED")
                rename_noreplace(parent, temporary, parent, name)
                os.fsync(parent)
            finally:
                os.close(fd)
    target_identity = _regular(repository, target, size, digest)
    _require(repository, artifact)
    receipt = {"version": 1, "stored_path": target, "stored_bytes": size, "stored_sha256": digest,
               "artifact_digest": store.digest(artifact.proof.record()), "root": intent["root"],
               "target_identity": list(target_identity)}
    return scope.write(lambda _: actions.verify(scope, action_id=action_id,
                                                intent_digest=record["intent_digest"], receipt=receipt))
