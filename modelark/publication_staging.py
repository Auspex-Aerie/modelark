"""Journalled release of one operation-owned, durably archived Fill download.

Never a general cleanup helper: replica inputs, shared caches, operator files,
unbound names and directory removal are excluded. An absent source is a proven
post-state only after this exact release intent was durably prepared.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import secrets
import stat
import uuid

from modelark import publication_actions as actions, publication_catalog as catalog
from modelark import publication_payload as payload, publication_store as store, publication_tree as trees
from modelark.publication_artifact import VerifiedArtifact
from modelark.publication_policy import PublicationRefused, parse_sha256_key
from modelark.slice.transaction import TransferRefusal


def _directory_id(owner, label, relative):
    return str(uuid.uuid5(uuid.UUID(owner.operation_id), "staging-directory:" + label + ":" + relative))


def _directory_request(owner, repo_id):
    from modelark.archive_publisher import ArchivePublisher
    if type(owner) is not ArchivePublisher:
        raise PublicationRefused("PUBLICATION_STAGING_OWNER_REQUIRED")
    owner._require()
    if owner._kind != "fill":
        raise PublicationRefused("PUBLICATION_STAGING_FILL_ONLY")
    selected = [request for request in owner._requests if request.repo_id == repo_id]
    labels = {request.drive_label for request in selected}
    if not selected or len(labels) != 1 or any(request.source_drive is not None for request in selected):
        raise PublicationRefused("PUBLICATION_STAGING_REPOSITORY_UNSELECTED")
    return selected[0]


def _directory_paths(owner, repo_id):
    container = ".git/annex/tmp/modelark-downloads"
    operation = container + "/" + owner.operation_id
    return (".git/annex/tmp", container, operation,
            operation + "/" + hashlib.sha256(repo_id.encode("utf-8")).hexdigest())


_OWNER_MARKER = ".modelark-staging-owner"


def _marker(repository, relative, content, *, finish=False):
    """Complete only the journal-bound nonce marker, never arbitrary file bytes."""
    with repository.tree.parent(relative) as (parent, name):
        if finish:
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
            except FileExistsError:
                fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_uid != os.geteuid() or before.st_nlink != 1 or before.st_size > len(content)):
                raise PublicationRefused("PUBLICATION_STAGING_MARKER_CHANGED")
            actual = os.read(fd, len(content) + 1)
            if actual != content:
                if not finish or not content.startswith(actual):
                    raise PublicationRefused("PUBLICATION_STAGING_MARKER_CHANGED")
                remaining = memoryview(content)[len(actual):]
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise PublicationRefused("PUBLICATION_STAGING_MARKER_INCOMPLETE")
                    remaining = remaining[count:]
            os.fsync(fd)
            os.fsync(parent)
            identity = payload._identity(os.fstat(fd))
            if payload._regular(repository.tree, relative, len(content), hashlib.sha256(content).hexdigest()) != identity:
                raise PublicationRefused("PUBLICATION_STAGING_MARKER_CHANGED")
            return list(identity)
        finally:
            os.close(fd)


def _reservation(owner, repository, relative, parent_identity, dependency):
    identifier = str(uuid.uuid5(uuid.UUID(_directory_id(owner, repository.drive_label, relative)), "reservation"))
    try:
        record = actions.read(owner.scope, identifier)
    except PublicationRefused as exc:
        if exc.code != "PUBLICATION_ACTION_MISSING":
            raise
        record = None
    if record is None:
        nonce = secrets.token_hex(32)
        parent, name = relative.rsplit("/", 1)
        marker = parent + "/." + name + ".modelark-reservation-" + nonce
        content = store.canonical({"version": 1, "operation": owner.operation_id, "directory": relative,
                                   "parent_identity": parent_identity, "nonce": nonce}).encode()
        plan = {"version": 1, "kind": "staging-reservation", "directory": relative,
                "parent_identity": parent_identity, "marker": marker, "content_hex": content.hex()}
        profile = repository.profile.record()
        intent = {"expected_phase": {"entity": "operation", "id": owner.operation_id, "phase": "PREPARED"},
                  "profile_digest": repository.profile.digest,
                  "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
                  "filesystem_plan": plan,
                  "after": [] if dependency is None else [{"action_id": dependency["action_id"],
                                                            "receipt_digest": dependency["receipt_digest"]}]}
        owner.scope.write(lambda _: actions.prepare(owner.scope, action_id=identifier,
                                                   kind="staging_directory", intent=intent))
        record = actions.read(owner.scope, identifier)
    plan = record["intent"]["filesystem_plan"]
    if plan.get("directory") != relative or plan.get("parent_identity") != parent_identity:
        raise PublicationRefused("PUBLICATION_STAGING_RESERVATION_CHANGED")
    content = bytes.fromhex(plan["content_hex"])
    identity = _marker(repository, plan["marker"], content, finish=record["status"] == "PREPARED")
    receipt = {"version": 1, "marker": plan["marker"], "identity": identity,
               "content_sha256": hashlib.sha256(content).hexdigest()}
    owner.scope.write(lambda _: actions.verify(owner.scope, action_id=identifier,
                                               intent_digest=record["intent_digest"], receipt=receipt))
    return actions.read(owner.scope, identifier), content


def _mkdir(owner, repository, relative, *, container, dependency):
    """Existing shared containers are not claimed; operation children are exclusive."""
    scope = owner.scope
    identifier = _directory_id(owner, repository.drive_label, relative)
    try:
        prior = actions.read(scope, identifier)
    except PublicationRefused as exc:
        if exc.code != "PUBLICATION_ACTION_MISSING":
            raise
        prior = None
    with repository.tree.parent(relative) as (parent, name):
        parent_identity = list(repository.tree.identity(parent))
        observed = _present(parent, name)
        before = None
        if observed is not None:
            fd = repository.tree.open(relative, os.O_RDONLY | os.O_DIRECTORY)
            try:
                before = list(repository.tree.identity(fd))
            finally:
                os.close(fd)
        if prior is None:
            if before is not None and not container:
                raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_UNOWNED")
            plan = {"version": 1, "kind": "staging-directory", "path": relative,
                    "parent_identity": parent_identity, "before": before, "shared_container": container}
        else:
            plan = prior["intent"]["filesystem_plan"]
            if (plan.get("kind") != "staging-directory" or plan.get("path") != relative
                    or plan.get("shared_container") is not container or plan.get("parent_identity") != parent_identity):
                raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_CHANGED")
        profile = repository.profile.record()
        intent = {"expected_phase": {"entity": "operation", "id": owner.operation_id, "phase": "PREPARED"},
                  "profile_digest": repository.profile.digest,
                  "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
                  "filesystem_plan": plan,
                  "after": [] if dependency is None else [{"action_id": dependency["action_id"],
                                                            "receipt_digest": dependency["receipt_digest"]}]}
        scope.write(lambda _: actions.prepare(scope, action_id=identifier, kind="staging_directory", intent=intent))
        record = actions.read(scope, identifier)
        if record["status"] == "VERIFIED":
            if before != record["receipt"]["receipt"].get("identity"):
                raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_CHANGED")
            return record
        if plan["before"] is None:
            if before is not None:
                # No saved post-create inode means a crash/collision is ambiguous.
                # Never adopt an existing operation subtree by pathname alone.
                raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_AMBIGUOUS")
            with repository.tree.parent(relative) as (current, _):
                if list(repository.tree.identity(current)) != parent_identity:
                    raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_CHANGED")
            os.mkdir(name, 0o700, dir_fd=parent)
        elif before != plan["before"]:
            raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_CHANGED")
        fd = repository.tree.open(relative, os.O_RDONLY | os.O_DIRECTORY)
        try:
            identity = list(repository.tree.identity(fd))
            os.fsync(fd)
            os.fsync(parent)
            with repository.tree.parent(relative) as (current, _):
                if list(repository.tree.identity(current)) != parent_identity:
                    raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_CHANGED")
            receipt = {"version": 1, "path": relative, "identity": identity,
                       "mount_id": repository.tree.mount_id, "parent_identity": parent_identity}
            scope.write(lambda _: actions.verify(scope, action_id=identifier,
                                                 intent_digest=record["intent_digest"], receipt=receipt))
        finally:
            os.close(fd)
        return actions.read(scope, identifier)


class StagingDirectory:
    """Retained operation/repository directory identities, owned by publisher lifetime."""

    def __init__(self, owner, request):
        self._owner, self._request = owner, request
        self._repository = owner._repositories[request.drive_label]
        self._stack, self._closed, self._directories = ExitStack(), False, []
        try:
            dependency = None
            paths = _directory_paths(owner, request.repo_id)
            for offset, relative in enumerate(paths):
                dependency = _mkdir(owner, self._repository, relative, container=offset < 2, dependency=dependency)
                fd = self._repository.tree.open(relative, os.O_RDONLY | os.O_DIRECTORY)
                self._stack.callback(os.close, fd)
                identity = tuple(dependency["receipt"]["receipt"]["identity"])
                self._directories.append((relative, fd, identity))
            self._path = self._repository.tree.path / paths[-1]
            self.check()
        except BaseException:
            self.close()
            raise

    @property
    def path(self):
        self.check()
        return self._path

    def __fspath__(self):
        return str(self.path)

    def check(self):
        if self._closed:
            raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_CLOSED")
        _directory_request(self._owner, self._request.repo_id)
        self._owner._recheck(self._request.drive_label)
        for relative, retained, identity in self._directories:
            current = self._repository.tree.open(relative, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if (self._repository.tree.identity(current) != identity
                        or self._repository.tree.identity(retained) != identity):
                    raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_CHANGED")
            finally:
                os.close(current)

    def close(self):
        self._closed = True
        self._stack.close()

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *_):
        self.close()


def directory(owner, repo_id):
    """Return a retained private staging directory; never adopt unjournaled children."""
    request = _directory_request(owner, repo_id)
    cache = getattr(owner, "_staging_directories", None)
    if cache is None:
        cache = owner._staging_directories = {}
    key = (request.drive_label, repo_id)
    try:
        if key not in cache:
            cache[key] = owner._stack.enter_context(StagingDirectory(owner, request))
        cache[key].check()
        return cache[key]
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_STAGING_NAMESPACE_UNPROVEN") from exc


def _identifier(file_id):
    return str(uuid.uuid5(uuid.UUID(file_id), "staging-release"))


def _file(owner, file_id):
    from modelark.archive_publisher import ArchivePublisher
    if type(owner) is not ArchivePublisher:
        raise PublicationRefused("PUBLICATION_STAGING_OWNER_REQUIRED")
    owner._require()
    if owner._kind != "fill":
        raise PublicationRefused("PUBLICATION_STAGING_FILL_ONLY")
    store.canonical_uuid(file_id)
    selected = [request for request, value in owner._files.items() if value == file_id]
    if len(selected) != 1 or selected[0].source_drive is not None:
        raise PublicationRefused("PUBLICATION_STAGING_FILE_UNSELECTED")
    request = selected[0]
    con = owner._connection
    con.execute("BEGIN")
    try:
        owner.scope.require()
        _, digest, _ = store._load_bound_operation(owner.scope, owner.operation_id)
        row, frozen, _ = store._file_chain(con, owner.operation_id, file_id, digest)
        if row[3] != "CATALOG_PUBLISHED":
            raise PublicationRefused("PUBLICATION_STAGING_FILE_UNPUBLISHED")
        value = frozen["intent"]
        if value.get("request") != request.record() or value["catalog_pair"]["before"]["source"] is not None:
            raise PublicationRefused("PUBLICATION_STAGING_FILE_MISMATCH")
        actual = catalog._capture_rows(owner.scope, **request.record())
        expected = {**value["catalog_pair"]["before"], **value["catalog_pair"]["after"]}
        if actual != expected:
            raise PublicationRefused("PUBLICATION_STAGING_CATALOG_CHANGED")
        return request, value, row[2], row[9]
    finally:
        con.rollback()


def _path(owner, repository, request, path):
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise PublicationRefused("PUBLICATION_STAGING_PATH_UNOWNED") from exc
    if (not isinstance(raw, str) or not raw or "\0" in raw or "\\" in raw
            or not Path(raw).is_absolute() or Path(raw).as_posix() != raw):
        raise PublicationRefused("PUBLICATION_STAGING_PATH_UNOWNED")
    try:
        relative = Path(raw).relative_to(repository.tree.path).as_posix()
    except ValueError as exc:
        raise PublicationRefused("PUBLICATION_STAGING_PATH_UNOWNED") from exc
    prefix = (".git", "annex", "tmp", "modelark-downloads", owner.operation_id,
              hashlib.sha256(request.repo_id.encode("utf-8")).hexdigest())
    parts = tuple(relative.split("/"))
    if (len(parts) <= len(prefix) or parts[:len(prefix)] != prefix
            or any(part in {"", ".", "..", ".git"} for part in parts[len(prefix):])):
        raise PublicationRefused("PUBLICATION_STAGING_PATH_UNOWNED")
    return relative


def _parent(repository, parent, artifact, relative):
    if (list(repository.tree.identity(parent)) != artifact["root_identity"]
            or repository.tree.mount_id != artifact["mount_id"]):
        raise PublicationRefused("PUBLICATION_STAGING_PARENT_CHANGED")
    with repository.tree.parent(relative) as (current, _):
        if repository.tree.identity(current) != repository.tree.identity(parent):
            raise PublicationRefused("PUBLICATION_STAGING_PARENT_CHANGED")


def _present(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _input(repository, path, artifact):
    expected = artifact["identity"]
    if (type(expected) is not list or len(expected) != 6
            or any(type(value) is not int for value in expected)):
        raise PublicationRefused("PUBLICATION_STAGING_ARTIFACT_INVALID")
    actual = payload._regular(repository.tree, path, artifact["stored_bytes"], artifact["stored_sha256"])
    payload._recheck_regular(repository.tree, path, actual)
    if list(actual) != expected:
        raise PublicationRefused("PUBLICATION_STAGING_INPUT_CHANGED")


def _archive(owner, request, repository, intent, seal):
    owner._recheck(request.drive_label)
    source = intent["install"]["artifact"]
    key, obj, path = (intent[name] for name in ("annex_key", "object_path", "stored_path"))
    archived = intent["catalog_pair"]["after"]["archived"]
    size, digest = parse_sha256_key(key)
    if (repository.object_path(key) != obj or (size, digest) != (source["stored_bytes"], source["stored_sha256"])
            or archived["annex_key"] != key or archived["orig_sha256"] != source["original_sha256"]
            or archived["orig_bytes"] != source["original_bytes"]
            or archived["stored_bytes"] != size or archived["compressed"] != int(source["compressed"])
            or (archived["znn_sha256"] if source["compressed"] else archived["orig_sha256"]) != digest
            or path != request.repo_id + "/" + archived["stored_relpath"]):
        raise PublicationRefused("PUBLICATION_STAGING_ARCHIVE_MISMATCH")
    local = payload.verify(repository.tree, stored_path=path, annex_key=key, qualified_object_path=obj,
                           binding_digest=seal, require_scope=owner.scope.require_io)
    baseline = trees.capture(repository.tree, read_git=repository.read, require_scope=owner.scope.require_io)
    if trees.TreeEntry(**intent["entry"]) not in baseline.entries:
        raise PublicationRefused("PUBLICATION_STAGING_COMMITTED_POINTER_CHANGED")
    tree = trees.verify(repository.tree, read_git=repository.read, require_scope=owner.scope.require_io,
        before=baseline, allowed_delta={}, stored_path=path, annex_key=key, qualified_object_path=obj,
        binding_digest=seal, profile_digest=repository.profile.digest)
    return local, tree


def resume_file(owner, request):
    """Skip re-acquisition only for an already published, released Fill file.

    Returns a receipt when this exact request is CATALOG_PUBLISHED and its
    staging duplicate is gone (or is released here). Returns None when bytes
    still need to be acquired. Never fabricates a published file.
    """
    try:
        return _resume_file(owner, request)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_STAGING_NAMESPACE_UNPROVEN") from exc


def _resume_file(owner, request):
    from modelark.archive_publisher import ArchivePublisher, FileRequest
    if type(owner) is not ArchivePublisher:
        raise PublicationRefused("PUBLICATION_STAGING_OWNER_REQUIRED")
    owner._require()
    if owner._kind != "fill":
        raise PublicationRefused("PUBLICATION_STAGING_FILL_ONLY")
    if type(request) is not FileRequest or request not in owner._files or request.source_drive is not None:
        raise PublicationRefused("PUBLICATION_STAGING_FILE_UNSELECTED")
    file_id = owner._files[request]
    con = owner._connection
    con.execute("BEGIN")
    try:
        owner.scope.require()
        _, digest, _ = store._load_bound_operation(owner.scope, owner.operation_id)
        try:
            row, _frozen, _ = store._file_chain(con, owner.operation_id, file_id, digest)
        except PublicationRefused as exc:
            if exc.code != "PUBLICATION_FILE_UNSELECTED":
                raise
            return None
        if row[3] != "CATALOG_PUBLISHED":
            return None
    finally:
        con.rollback()
    identifier = _identifier(file_id)
    try:
        existing = actions.read(owner.scope, identifier)
    except PublicationRefused as exc:
        if exc.code != "PUBLICATION_ACTION_MISSING":
            raise
        existing = None
    directory(owner, request.repo_id).check()
    _request, intent, seal, _catalog_receipt = _file(owner, file_id)
    archived = intent["catalog_pair"]["after"]["archived"]
    receipt = {"file_id": file_id, "stored_path": intent["stored_path"], "annex_key": intent["annex_key"],
               "stored_bytes": archived["stored_bytes"], "phase": "CATALOG_PUBLISHED"}
    if existing is not None and existing["status"] == "VERIFIED":
        _archive(owner, request, owner._repositories[request.drive_label], intent, seal)
        return receipt
    artifact = intent["install"]["artifact"]
    name = artifact.get("name")
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
        raise PublicationRefused("PUBLICATION_STAGING_ARTIFACT_MISMATCH")
    relative = _directory_paths(owner, request.repo_id)[-1] + "/" + name
    path = (owner._repositories[request.drive_label].tree.path / relative).as_posix()
    release(owner, file_id=file_id, path=path)
    return receipt


def release(owner, *, file_id, path):
    """Unlink only the exact committed Fill input, retaining a durable receipt."""
    try:
        return _release(owner, file_id, path)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_STAGING_NAMESPACE_UNPROVEN") from exc


def _release(owner, file_id, path):
    request, intent, seal, catalog_receipt = _file(owner, file_id)
    scope = owner.scope
    repository = owner._repositories[request.drive_label]
    relative = _path(owner, repository, request, path)
    # Namespace spelling is not ownership. The private operation/repository
    # directories must already have verified creation/identity receipts.
    for owned in _directory_paths(owner, request.repo_id)[2:]:
        record = actions.read(scope, _directory_id(owner, request.drive_label, owned))
        if (record["status"] != "VERIFIED"
                or record["intent"]["filesystem_plan"].get("path") != owned):
            raise PublicationRefused("PUBLICATION_STAGING_DIRECTORY_UNOWNED")
    admitted = directory(owner, request.repo_id)
    admitted.check()
    artifact = intent["install"]["artifact"]
    if (artifact.get("version") != 1 or artifact.get("kind") != "verified-staged-artifact"
            or artifact.get("name") != relative.rsplit("/", 1)[-1]):
        raise PublicationRefused("PUBLICATION_STAGING_ARTIFACT_MISMATCH")
    identifier = _identifier(file_id)
    existing = None
    try:
        existing = actions.read(scope, identifier)
    except PublicationRefused as exc:
        if exc.code != "PUBLICATION_ACTION_MISSING":
            raise
    local, tree = _archive(owner, request, repository, intent, seal)
    with VerifiedArtifact(scope, repository.tree.path / intent["object_path"],
            compressed=artifact["compressed"], original_bytes=artifact["original_bytes"],
            original_sha256=artifact["original_sha256"], stored_sha256=artifact["stored_sha256"]) as archived:
        if archived.proof.identity != local.object_identity:
            raise PublicationRefused("PUBLICATION_STAGING_ARCHIVE_CHANGED")
        profile = repository.profile.record()
        root = {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")}
        plan = {"version": 1, "kind": "staging-release", "path": relative, "artifact": artifact,
                "operation_id": owner.operation_id, "file_id": file_id, "repo_id": request.repo_id,
                "root": root, "file_intent_digest": seal, "catalog_receipt_digest": catalog_receipt}
        action_intent = {"expected_phase": {"entity": "file", "id": file_id, "phase": "CATALOG_PUBLISHED"},
                         "profile_digest": repository.profile.digest, "root": root, "filesystem_plan": plan}
        with repository.tree.parent(relative) as (parent, name):
            _parent(repository, parent, artifact, relative)
            actual = _present(parent, name)
            if actual is None and existing is None:
                raise PublicationRefused("PUBLICATION_STAGING_ABSENCE_UNJOURNALLED")
            if actual is not None:
                if existing is not None and existing["status"] == "VERIFIED":
                    raise PublicationRefused("PUBLICATION_STAGING_RELEASED_PATH_REAPPEARED")
                if not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.geteuid():
                    raise PublicationRefused("PUBLICATION_STAGING_INPUT_CHANGED")
                _input(repository, relative, artifact)
            scope.write(lambda _: actions.prepare(scope, action_id=identifier, kind="staging_release", intent=action_intent))
            record = actions.read(scope, identifier)
            if record["status"] == "VERIFIED":
                archived.check()
                return record
            archived.check()
            owner._recheck(request.drive_label)
            admitted.check()
            # Re-observe catalog and the source's bound parent after preparing
            # the journal. Native/profile/fence IO remains outside SQL.
            _file(owner, file_id)
            latest_local, latest_tree = _archive(owner, request, repository, intent, seal)
            if latest_local != local or latest_tree != tree:
                raise PublicationRefused("PUBLICATION_STAGING_ARCHIVE_CHANGED")
            _file(owner, file_id)
            repository.tree.check()
            _parent(repository, parent, artifact, relative)
            actual = _present(parent, name)
            if actual is not None:
                _input(repository, relative, artifact)
                _parent(repository, parent, artifact, relative)
                os.unlink(name, dir_fd=parent)
            os.fsync(parent)
            if _present(parent, name) is not None:
                raise PublicationRefused("PUBLICATION_STAGING_RELEASE_INCOMPLETE")
            archived.check()
            admitted.check()
            repository.tree.check()
            _parent(repository, parent, artifact, relative)
            receipt = {"version": 1, "path": relative, "absent": True,
                       "artifact_digest": store.digest(artifact), "catalog_receipt_digest": catalog_receipt,
                       "archive_local": local.record(), "archive_tree": tree.record()}
            scope.write(lambda _: actions.verify(scope, action_id=identifier,
                intent_digest=record["intent_digest"], receipt=receipt))
            return actions.read(scope, identifier)
