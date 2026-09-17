"""Replica source admission into the shared owning publication pipeline.

The source is selected by catalog identity, not a caller-supplied staging path or
proof record. Its retained original object is independently decoded/hashed and
its exact mapped and committed pointer are rechecked through the final CAS.
No source representation, provenance or copy-row repair occurs here.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import PurePosixPath

from modelark import publication_catalog as catalog, publication_payload as payload
from modelark import publication_store as store, publication_tree as trees
from modelark.publication_artifact import VerifiedArtifact
from modelark.publication_policy import PublicationRefused, parse_sha256_key, relative_path
from modelark.slice.transaction import TransferRefusal


def _require_owner(owner, request):
    from modelark.archive_publisher import ArchivePublisher, FileRequest
    if type(owner) is not ArchivePublisher or type(request) is not FileRequest:
        raise PublicationRefused("PUBLICATION_REPLICA_OWNER_REQUIRED")
    owner._require()
    if (owner._kind != "replica" or request not in owner._files or request.source_drive is None
            or request.source_drive not in owner._repositories
            or request.source_drive == request.drive_label):
        raise PublicationRefused("PUBLICATION_REPLICA_REQUEST_UNSELECTED")


def _capture(owner, request, *, with_published_pair=False):
    owner._require()
    con = owner._connection
    con.execute("BEGIN")
    try:
        owner.scope.require()
        _, operation_digest, _ = store._load_bound_operation(owner.scope, owner.operation_id)
        pair = catalog._capture_rows(owner.scope, **request.record())
        if not with_published_pair:
            return pair
        file_id = owner._files[request]
        exists = con.execute("SELECT 1 FROM publication_files WHERE operation_id=? AND file_id=?",
                             [owner.operation_id, file_id]).fetchone()
        published = None
        if exists:
            row, frozen, _ = store._file_chain(con, owner.operation_id, file_id, operation_digest)
            if frozen["intent"].get("request") != request.record():
                raise PublicationRefused("PUBLICATION_REPLICA_REQUEST_UNSELECTED")
            if row[3] == "CATALOG_PUBLISHED":
                published = frozen["intent"]["catalog_pair"]
                catalog._validate(published)
        return pair, published
    finally:
        con.rollback()


class _ReplicaSource:
    """Live observed capability; never reconstructed from a saved receipt."""

    def __init__(self, owner, request):
        _require_owner(owner, request)
        self._owner, self._request = owner, request
        self._stack, self._closed = ExitStack(), False
        self._repository = owner._repositories[request.source_drive]
        try:
            owner._recheck(request.source_drive)
            self._pair = _capture(owner, request)
            self._pair_seal = store.digest(self._pair)
            facts = self._pair["source"]["archived"]
            self._key = facts["annex_key"]
            size, digest = parse_sha256_key(self._key)
            relative = facts["stored_relpath"]
            relative_path(relative)
            if PurePosixPath(relative).name != facts["stored_name"]:
                raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_PATH_INVALID")
            self._path = request.repo_id + "/" + relative
            if (type(facts["compressed"]) is not int or facts["compressed"] not in (0, 1)
                    or type(facts["stored_bytes"]) is not int or facts["stored_bytes"] != size
                    or type(facts["orig_bytes"]) is not int or facts["orig_bytes"] < 0
                    or not facts["compressed"] and facts["znn_sha256"] is not None
                    or (facts["znn_sha256"] if facts["compressed"] else facts["orig_sha256"]) != digest):
                raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_FACTS_INVALID")
            if facts["orig_sha256_provenance"] not in {
                    "hub_confirmed", "ingestion_computed", "annex_key", "archive-head-blob"}:
                raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_PROVENANCE_UNPROVEN")
            store._require_digest(facts["orig_sha256"])
            replica = self._pair["source"]["replicas"]
            if replica is not None and (replica["annex_key"] != self._key or replica["present"] != 1):
                raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_COPY_CONFLICT")
            self._object = self._repository.object_path(self._key)
            self._baseline = trees.capture(self._repository.tree, read_git=self._repository.read,
                                          require_scope=owner.scope.require_io)
            self._binding = store.digest({"operation_id": owner.operation_id,
                                         "request": request.record(), "source": self._pair["source"],
                                         "files": self._pair["files"]})
            self._local, self._committed = self._observe()
            self._artifact = self._stack.enter_context(VerifiedArtifact(
                owner.scope, self._repository.tree.path / self._object,
                compressed=bool(facts["compressed"]), original_bytes=facts["orig_bytes"],
                original_sha256=facts["orig_sha256"], stored_sha256=digest))
            if self._artifact.proof.identity != self._local.object_identity:
                raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_OBJECT_CHANGED")
            self._artifact_record = self._artifact.proof.record()
            self._seal = store.digest(self._record())
            self.recheck()
        except BaseException:
            self._closed = True
            self._stack.close()
            raise

    @property
    def key(self):
        self._check()
        return self._key

    @property
    def pair(self):
        # The shared pipeline compares this value inside its short owned SQL
        # snapshot. Physical checks belong before that transaction, never in it.
        if self._closed or store.digest(self._pair) != self._pair_seal:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CAPABILITY_CHANGED")
        return json.loads(store.canonical(self._pair))

    def _record(self):
        return {"version": 1, "kind": "observed-replica-source",
                "key": self._key, "pair": self._pair, "path": self._path, "object": self._object,
                "binding": self._binding, "baseline": self._baseline.record(),
                "local": self._local.record(), "tree": self._committed.record(),
                "artifact": self._artifact_record}

    def record(self):
        """Detached sealed observation for the journal; grants no live authority."""
        if self._closed or store.digest(self._record()) != self._seal:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CAPABILITY_CHANGED")
        return json.loads(store.canonical(self._record()))

    def _check(self):
        if self._closed:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CLOSED")
        _require_owner(self._owner, self._request)
        if (self._repository is not self._owner._repositories[self._request.source_drive]
                or self._artifact.scope is not self._owner.scope
                or self._artifact.proof.record() != self._artifact_record
                or store.digest(self._record()) != self._seal):
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CAPABILITY_CHANGED")

    def _observe(self):
        repo, owner = self._repository, self._owner
        if repo.object_path(self._key) != self._object:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_OBJECT_CHANGED")
        local = payload.verify(repo.tree, stored_path=self._path, annex_key=self._key,
            qualified_object_path=self._object, binding_digest=self._binding,
            require_scope=owner.scope.require_io)
        committed = trees.verify(repo.tree, read_git=repo.read, require_scope=owner.scope.require_io,
            before=self._baseline, allowed_delta={}, stored_path=self._path, annex_key=self._key,
            qualified_object_path=self._object, binding_digest=self._binding,
            profile_digest=repo.profile.digest)
        return local, committed

    def recheck(self):
        """Fresh physical, attachment, index/HEAD and source/target catalog proof."""
        self._check()
        self._owner._recheck(self._request.source_drive)
        self._artifact.check()
        current_pair, published = _capture(self._owner, self._request, with_published_pair=True)
        expected = self._pair
        if published is not None:
            after = {**published["before"], **published["after"]}
            # Only the same file's verified durable CAS authorizes its exact
            # target transition. A fresh continuation may capture that after
            # state; the original source record remains unchanged either way.
            if self._pair not in (published["before"], after):
                raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CHANGED")
            expected = after
        if current_pair != expected:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CHANGED")
        local, committed = self._observe()
        if local != self._local or committed != self._committed:
            raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CHANGED")
        self._artifact.check()
        self._owner._recheck(self._request.source_drive)

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_):
        self._closed = True
        self._stack.close()


def require_source(source, owner, request, artifact):
    """Only the observed, still-open source capability enters shared machinery."""
    if (type(source) is not _ReplicaSource or source._owner is not owner
            or source._request != request or source._artifact is not artifact):
        raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_CAPABILITY_REQUIRED")
    source._check()


def publish(owner, request):
    """Resolve and verify the selected source, then reuse the common pipeline."""
    _require_owner(owner, request)
    file_id = owner._files[request]
    existing = owner._connection.execute(
        "SELECT 1 FROM publication_files WHERE operation_id=? AND file_id=?",
        [owner.operation_id, file_id]).fetchone()
    try:
        with _ReplicaSource(owner, request) as source:
            if existing:
                from modelark import publication_file_resume
                return publication_file_resume.publish(owner, request, source._artifact, source=source)
            return owner._publish_artifact(request, source._artifact, source=source)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_REPLICA_SOURCE_UNAVAILABLE") from exc
