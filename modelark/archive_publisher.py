"""Owning archive publication coordinator; live activation remains separately gated.

Owns controller/map/drive fences, actual attachment/native/byte/tree factories and
the existing catalog transaction adapter. No public API accepts success flags,
caller-made proof JSON, arbitrary Git commands or catalog mutation callbacks.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
import time
import uuid

from modelark import publication_actions as actions, publication_attachment as attachments
from modelark import publication_catalog as catalog, publication_install as install
from modelark import publication_locks as locks, publication_map_policy as map_policy
from modelark import publication_map_tree as metadata, publication_payload as payload
from modelark import publication_policy_setup as policy_setup, publication_store as store, publication_tree as trees
from modelark import register
from modelark.publication_artifact import VerifiedArtifact
from modelark.publication_native import QualifiedRepository
from modelark.publication_policy import PublicationRefused, _require, payload_relative_path, relative_path, sha256_key


ANNEX_REF = "refs/heads/git-annex"


@dataclass(frozen=True, order=True)
class FileRequest:
    repo_id: str
    rfilename: str
    drive_label: str
    source_drive: str | None = None

    def __post_init__(self):
        if len(relative_path(self.repo_id).parts) != 2:
            raise PublicationRefused("PUBLICATION_REPOSITORY_INVALID")
        payload_relative_path(self.rfilename)
        if not isinstance(self.drive_label, str) or not self.drive_label or self.source_drive == self.drive_label:
            raise PublicationRefused("PUBLICATION_PARTICIPANT_INVALID")

    def record(self):
        return asdict(self)


def _id(parent, name):
    return str(uuid.uuid5(uuid.UUID(parent), name))


def _snapshot(repository):
    return trees.capture(repository.tree, read_git=repository.read, require_scope=repository.scope.require_io)


def _root(profile):
    return {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")}


class ArchivePublisher:
    """One enclosing selected-participant generation and its exact file batches.

    Construction/entry never migrates a catalog or enables legacy conversion.
    A stopped/failed acquisition is not fabricated as a published file; pending
    work remains durable until its explicit owning continuation completes it.
    """

    def __init__(self, con, requests, *, kind="fill", session_id=None, fencing_token=None,
                 retired_paths=None):
        requests = tuple(requests)
        if (not requests or any(type(item) is not FileRequest for item in requests)
                or len(set(requests)) != len(requests) or kind not in {"fill", "replica", "maintenance"}):
            raise PublicationRefused("PUBLICATION_REQUEST_SET_INVALID")
        if any((item.source_drive is not None) != (kind == "replica") for item in requests):
            raise PublicationRefused("PUBLICATION_REQUEST_KIND_MISMATCH")
        retired_paths = {} if retired_paths is None else dict(retired_paths)
        if (kind == "maintenance" and (set(retired_paths) != set(requests)
                                       or any(type(path) is not str for path in retired_paths.values()))
                or kind != "maintenance" and retired_paths):
            raise PublicationRefused("PUBLICATION_RETIREMENT_SET_INVALID")
        retired_paths = {request: relative_path(path).as_posix()
                         for request, path in retired_paths.items()}
        self._connection, self._requests, self._kind = con, tuple(sorted(requests)), kind
        self._retired_paths = retired_paths
        self._session_id, self._fencing_token = session_id, fencing_token
        self.operation_id = str(uuid.uuid4())
        self._stack, self._active, self._finished = ExitStack(), False, False
        self._repositories, self._attachments, self._files = {}, {}, {}
        self._resuming = False

    @classmethod
    def resume(cls, con, operation_id, *, session_id=None, fencing_token=None):
        """Reopen the same owner/selected generation; never adopt another owner."""
        store.canonical_uuid(operation_id)
        row = con.execute("SELECT binding_json,binding_digest,state FROM publication_operations WHERE operation_id=?",
                          [operation_id]).fetchone()
        _require(row is not None and row[2] == "PREPARED", "PUBLICATION_OPERATION_NOT_ACTIVE")
        binding = store._unseal(row[0], row[1])
        rows = list(binding["before_state"]["requests"].values())
        requests = [FileRequest(**{name: item[name] for name in ("repo_id", "rfilename", "drive_label", "source_drive")})
                    for item in rows]
        retired = {request: row.get("retired_path") for request, row in zip(requests, rows)
                   if row.get("retired_path") is not None}
        result = cls(con, requests, kind=binding["kind"], session_id=session_id,
                     fencing_token=fencing_token, retired_paths=retired)
        result.operation_id, result._resuming = operation_id, True
        return result

    def __enter__(self):
        if self._active or self._finished:
            raise PublicationRefused("PUBLICATION_COORDINATOR_REUSED")
        if self._kind == "maintenance":
            from modelark.publication_migrate import _catalog_file, live_catalog_path
            catalog_path = _catalog_file(self._connection)
            if catalog_path is None or catalog_path == live_catalog_path():
                raise PublicationRefused("PUBLICATION_LIVE_CUTOVER_FORBIDDEN")
        if self._resuming:
            return self._resume_enter()
        identity = store.library(self._connection)
        if identity is None:
            raise PublicationRefused("PUBLICATION_MIGRATION_REQUIRED")
        labels = {item.drive_label for item in self._requests} | {
            item.source_drive for item in self._requests if item.source_drive is not None}
        try:
            self.scope = self._stack.enter_context(locks.hold(
                self._connection, labels, map_uuid=identity[1], session_id=self._session_id,
                fencing_token=self._fencing_token))
            revision = store._revision(self._connection)
            for label in sorted(labels):
                attachment = attachments.capture(self.scope, label)
                repository = self._stack.enter_context(QualifiedRepository(self.scope, attachment.root, drive_label=label))
                if (repository.profile.root_identity != attachment.root_identity
                        or repository.profile.mount_id != attachment.mount_id
                        or repository.expected_uuid != attachment.annex_uuid):
                    raise PublicationRefused("PUBLICATION_ATTACHMENT_NATIVE_MISMATCH")
                self._attachments[label], self._repositories[label] = attachment, repository
            self.map = self._stack.enter_context(QualifiedRepository(self.scope, register.library_root()))
            from modelark import publication_map_stage
            self.stage = self._stack.enter_context(publication_map_stage.create(self.map))
            # Reserve both possible stored names; the codec decision is made by
            # acquisition. Only the one actual result becomes a file intent.
            path_sets = {label: set() for label in labels}
            for item in self._requests:
                for suffix in ("", ".znn"):
                    path_sets[item.drive_label].add(item.repo_id + "/" + payload_relative_path(item.rfilename + suffix))
            all_paths = set().union(*path_sets.values())
            repositories = {**self._repositories, "@map": self.map, "@stage": self.stage}
            policies, profiles, before_trees = {}, {}, {}
            from modelark import publication_inventory
            inventories = {label: publication_inventory.capture_baseline(repository).record()
                           for label, repository in self._repositories.items()}
            for label, repository in repositories.items():
                paths = all_paths if label in {"@map", "@stage"} else path_sets[label]
                before_trees[label] = _snapshot(repository).record()
                profiles[label] = repository.profile.record()
                if paths:
                    policies[label] = policy_setup.preview(repository, paths)
                    profiles[label + ":policy"] = policies[label]["after_profile"]
            batch_files, requests = {}, {}
            for item in self._requests:
                file_id = _id(self.operation_id, store.canonical(item.record()))
                batch_id = _id(self.operation_id, "batch:" + item.drive_label)
                batch_files.setdefault(batch_id, []).append(file_id)
                requests[file_id] = {"batch_id": batch_id, **item.record(),
                                     "retired_path": self._retired_paths.get(item)}
                self._files[item] = file_id
            before_state = {"profiles": profiles, "trees": before_trees, "requests": requests,
                            "policies": policies, "inventories": inventories,
                            "roots": {label: str(repository.tree.path) for label, repository in repositories.items()},
                            "attachments": {key: value.record() for key, value in self._attachments.items()}}
            if self._kind == "maintenance":
                before_state["clone_layout"] = [list(row) for row in self._connection.execute(
                    "SELECT annex_uuid,drive_label FROM drives WHERE annex_uuid IS NOT NULL "
                    "ORDER BY annex_uuid")]
            # Recheck physical identity after all preparatory reads, then dirty
            # every participant and freeze operation ownership in one revision.
            for label, proof in self._attachments.items():
                attachments.recheck(self.scope, proof)
            def prepare(con):
                if store._revision(con) != revision:
                    raise PublicationRefused("PUBLICATION_PREPARE_REVISION_CHANGED")
                from modelark.drive_mutation import _advance_one, _drive_facts
                for label in sorted(labels):
                    _advance_one(con, label, "archive-publication:" + self._kind, _drive_facts(con, label),
                                 owner_session_id=self._session_id, owner_fencing_token=self._fencing_token)
                prepare = (store.prepare_maintenance_operation if self._kind == "maintenance"
                           else store.prepare_operation)
                arguments = {"operation_id": self.operation_id,
                             "profile_digest": store.digest(profiles), "batch_files": batch_files,
                             "before_state": before_state}
                if self._kind != "maintenance":
                    arguments["kind"] = self._kind
                return prepare(self.scope, **arguments)
            self.binding_digest = self.scope.write(prepare)
            self._before_state = before_state
            self._active = True
            self._attachments = {label: attachments.capture(self.scope, label) for label in sorted(labels)}
            for label, plan in policies.items():
                repository = repositories[label]
                action_id = _id(self.operation_id, "policy:" + label)
                self.scope.write(lambda _, action_id=action_id, plan=plan, repository=repository: actions.prepare(
                    self.scope, action_id=action_id, kind="policy_setup", intent={
                        "expected_phase": {"entity": "operation", "id": self.operation_id, "phase": "PREPARED"},
                        "profile_digest": repository.profile.digest, "root": _root(repository.profile.record()),
                        "filesystem_plan": plan}))
                policy_setup.apply(repository, action_id)
                if label == "@stage":
                    publication_map_stage.adopt_policy(repository, action_id)
            return self
        except BaseException:
            self._active = False
            self._stack.close()
            raise

    def _resume_enter(self):
        from modelark import publication_map_stage as staging
        identity = store.library(self._connection)
        _require(identity is not None, "PUBLICATION_MIGRATION_REQUIRED")
        labels = {item.drive_label for item in self._requests} | {
            item.source_drive for item in self._requests if item.source_drive is not None}
        try:
            self.scope = self._stack.enter_context(locks.hold(self._connection, labels, map_uuid=identity[1],
                session_id=self._session_id, fencing_token=self._fencing_token, operation_id=self.operation_id))
            binding = staging._saved_operation(self.scope)
            self.binding_digest, self._before_state = store.digest(binding), binding["before_state"]
            state = self._before_state
            for label in sorted(labels):
                proof = attachments.capture(self.scope, label)
                prior = state["attachments"][label]
                _require(proof.root == state["roots"][label] and list(proof.root_identity) == prior["root_identity"]
                         and proof.mount_id == prior["mount_id"], "PUBLICATION_ATTACHMENT_CHANGED")
                repository = self._stack.enter_context(QualifiedRepository(self.scope, proof.root, drive_label=label))
                self._attachments[label], self._repositories[label] = proof, repository
            _require(str(register.library_root()) == state["roots"]["@map"], "PUBLICATION_MAP_ROOT_CHANGED")
            self.map = self._stack.enter_context(QualifiedRepository(self.scope, state["roots"]["@map"]))
            repositories = {**self._repositories, "@map": self.map}
            # A temporary reader can acknowledge exact policy bytes before the
            # stage factory re-issues its private mutation capability.
            stage_reader = self._stack.enter_context(QualifiedRepository(self.scope, state["roots"]["@stage"]))
            repositories["@stage"] = stage_reader
            for label, repository in repositories.items():
                _require(repository.profile.record() in (state["profiles"][label], state["profiles"].get(label + ":policy")),
                         "PUBLICATION_RESUME_PROFILE_CHANGED")
                if label not in state["policies"]:
                    continue
                action_id = _id(self.operation_id, "policy:" + label)
                plan = state["policies"][label]
                exists = self._connection.execute("SELECT 1 FROM publication_actions WHERE operation_id=? AND action_id=?",
                                                 [self.operation_id, action_id]).fetchone()
                if not exists:
                    _require(repository.profile.record() == plan["before_profile"], "PUBLICATION_POLICY_UNJOURNALLED")
                    self.scope.write(lambda _, repository=repository, action_id=action_id, plan=plan: actions.prepare(
                        self.scope, action_id=action_id, kind="policy_setup", intent={
                            "expected_phase": {"entity": "operation", "id": self.operation_id, "phase": "PREPARED"},
                            "profile_digest": repository.profile.digest, "root": _root(repository.profile.record()),
                            "filesystem_plan": plan}))
                policy_setup.apply(repository, action_id)
            self.stage = self._stack.enter_context(staging.resume(self.scope))
            for request in self._requests:
                identifier = _id(self.operation_id, store.canonical(request.record()))
                _require(state["requests"].get(identifier) == {
                    "batch_id": _id(self.operation_id, "batch:" + request.drive_label), **request.record(),
                    "retired_path": self._retired_paths.get(request)},
                    "PUBLICATION_RESUME_WORKSET_CHANGED")
                self._files[request] = identifier
            self._active = True
            return self
        except BaseException:
            self._active = False
            self._stack.close()
            raise

    @property
    def child_fence_fds(self):
        self._require()
        return self.scope.child_fence_fds

    def _require(self):
        if not self._active or self._finished:
            raise PublicationRefused("PUBLICATION_COORDINATOR_INACTIVE")
        self.scope.require_io()

    def write(self, callback):
        self._require()
        return self.scope.write(callback)

    def _recheck(self, label):
        self._require()
        self._attachments[label] = attachments.recheck(self.scope, self._attachments[label])
        self._repositories[label].ensure()

    def _command(self, repository, file_id, phase, kind, argv, name):
        action_id = _id(file_id, name)
        self.scope.write(lambda _: actions.prepare(self.scope, action_id=action_id, kind=kind, intent={
            "expected_phase": {"entity": "file", "id": file_id, "phase": phase},
            "profile_digest": repository.profile.digest, "root": _root(repository.profile.record()),
            "command": {"argv": list(argv), "stdin_digest": hashlib.sha256(b"").hexdigest()}}))
        repository._run_action(action_id, *argv)
        return actions.read(self.scope, action_id)

    def publish(self, request, staged, *, original_bytes, original_sha256, stored_sha256, compressed):
        """Publish verified acquisition bytes through exact tree and catalog CAS.

        The file phase is still not a map receipt or enclosing-drive completion.
        Original hashes are re-proven with the shared bounded decoder, not trusted
        because a caller supplied a digest or a previous compressor returned zero.
        """
        self._require()
        if request not in self._files or request.source_drive is not None:
            raise PublicationRefused("PUBLICATION_FILE_UNSELECTED")
        with VerifiedArtifact(self.scope, staged, compressed=compressed, original_bytes=original_bytes,
                              original_sha256=original_sha256, stored_sha256=stored_sha256) as artifact:
            return self._publish_artifact(request, artifact)

    def _publish_artifact(self, request, artifact, *, source=None):
        """Common publication pipeline; source capability only changes key import."""
        self._require()
        _require(type(artifact) is VerifiedArtifact and artifact.scope is self.scope
                 and request in self._files, "PUBLICATION_ARTIFACT_UNQUALIFIED")
        if source is not None:
            from modelark.publication_replica_pipeline import require_source
            require_source(source, self, request, artifact)
            source.recheck()
        _require((source is not None) == (request.source_drive is not None), "PUBLICATION_SOURCE_REQUIRED")
        original_bytes, original_sha256 = artifact.proof.original_bytes, artifact.proof.original_sha256
        stored_sha256, compressed = artifact.proof.stored_sha256, artifact.proof.compressed
        file_id = self._files[request]
        if self._connection.execute("SELECT 1 FROM publication_files WHERE file_id=?", [file_id]).fetchone():
            from modelark import publication_file_resume
            return publication_file_resume.publish(self, request, artifact, source=source)
        self._recheck(request.drive_label)
        repository = self._repositories[request.drive_label]
        with nullcontext(artifact):
            artifact_proof = artifact.proof
            relative = payload_relative_path(request.rfilename + (".znn" if compressed else ""))
            path = request.repo_id + "/" + relative
            key = source.key if source is not None else sha256_key(artifact_proof.stored_bytes, artifact_proof.stored_sha256)
            object_path = repository.object_path(key)
            before = _snapshot(repository)
            metadata_before = metadata.capture(repository, ref=ANNEX_REF, binding_digest=self.binding_digest)
            install_plan = install.preview(repository, artifact, path)
            pointer = posixpath.relpath(object_path, str(PurePosixPath(path).parent)).encode("ascii")
            entry = trees.TreeEntry(path, "120000", hashlib.sha1(
                b"blob " + str(len(pointer)).encode() + b"\0" + pointer).hexdigest())
            if any(existing.path == path for existing in before.entries):
                raise PublicationRefused("PUBLICATION_TARGET_COMMITTED")
            retired_source = None
            retired_path = self._retired_paths.get(request)
            if retired_path is not None:
                old_entries = {existing.path: existing for existing in before.entries}
                old_entry = old_entries.get(retired_path)
                _require(old_entry is not None and old_entry.mode == "100644" and retired_path != path,
                         "PUBLICATION_RETIREMENT_SOURCE_UNPROVEN")
                raw = trees._object(repository.read, "blob", old_entry.oid, limit=64 * 1024 * 1024)
                _require(len(raw) == original_bytes and hashlib.sha256(raw).hexdigest() == original_sha256,
                         "PUBLICATION_RETIREMENT_SOURCE_UNPROVEN")
                retired_source = {"path": retired_path, "entry": asdict(old_entry),
                                  "bytes": len(raw), "sha256": original_sha256}
            observed_at = datetime.now(timezone.utc).isoformat(sep=" ")
            source_record = source.record() if source is not None else None
            frozen = {}
            def prepare_file(_):
                pair_before = catalog.capture(self.scope, **request.record())
                if source is not None:
                    _require(pair_before == source.pair, "PUBLICATION_REPLICA_SOURCE_CHANGED")
                facts = pair_before["files"]
                if (facts["sha256"] and facts["sha256"].lower() != original_sha256
                        or facts["size_bytes"] is not None and facts["size_bytes"] != original_bytes):
                    raise PublicationRefused("PUBLICATION_ARTIFACT_MANIFEST_MISMATCH")
                if retired_source is not None:
                    legacy = pair_before["archived"]
                    expected_old = (request.repo_id + "/" + legacy["stored_relpath"]
                                    if legacy is not None and legacy.get("stored_relpath") else None)
                    _require(legacy is not None and not legacy.get("annex_key")
                             and expected_old == retired_path
                             and legacy.get("orig_sha256") == original_sha256
                             and legacy.get("orig_bytes") == original_bytes,
                             "PUBLICATION_RETIREMENT_CATALOG_UNPROVEN")
                archived = {**pair_before["key"], "stored_name": PurePosixPath(relative).name,
                            "stored_relpath": relative, "orig_sha256": original_sha256,
                            "znn_sha256": stored_sha256 if compressed else None,
                            "orig_bytes": original_bytes, "stored_bytes": artifact_proof.stored_bytes,
                            "compressed": int(compressed), "annex_key": key, "verified_at": observed_at,
                            "orig_sha256_provenance": "hub_confirmed" if facts["sha256"] else "ingestion_computed"}
                if source is not None:
                    archived["orig_sha256_provenance"] = pair_before["source"]["archived"]["orig_sha256_provenance"]
                old_copy = pair_before["replicas"]
                copy = {**pair_before["key"], "annex_key": key, "present": 1, "verified_at": observed_at,
                        "added_at": old_copy["added_at"] if old_copy is not None else observed_at}
                params = self._connection.execute("SELECT params_b FROM models WHERE repo_id=?", [request.repo_id]).fetchone()
                tags = {"model": request.repo_id, "format": facts["format"], "quant": facts["quant"] or "none"}
                if params and params[0] is not None:
                    tags["params"] = str(params[0])
                frozen.update({"request": request.record(), "catalog_pair": catalog.intended_pair(pair_before, archived=archived, replicas=copy),
                               "stored_path": path, "annex_key": key, "object_path": object_path,
                               "tree_before": before.record(), "entry": asdict(entry), "install": install_plan,
                               "metadata_before": metadata_before.record(), "tags": tags})
                if retired_source is not None:
                    frozen.update({"retired_path": retired_path, "retired_source": retired_source})
                if source_record is not None:
                    frozen["source_proof"] = source_record
                return store.prepare_file(self.scope, operation_id=self.operation_id,
                    batch_id=self._before_state["requests"][file_id]["batch_id"], file_id=file_id, intent=frozen)
            self.scope.write(prepare_file)
            from modelark import publication_file_resume
            return publication_file_resume.publish(self, request, artifact, source=source)

    def retire(self, request):
        """Retire one migration-selected old path after its catalog transition."""
        self._require()
        if self._kind != "maintenance" or request not in self._retired_paths:
            raise PublicationRefused("PUBLICATION_RETIREMENT_FILE_UNSELECTED")
        from modelark import publication_retirement
        return publication_retirement.retire(
            self._repositories[request.drive_label], file_id=self._files[request])

    def _batch_rows(self, batch_id):
        self._require()
        con = self._connection
        con.execute("BEGIN")
        try:
            self.scope.require()
            binding, digest, _ = store._load_bound_operation(self.scope, self.operation_id)
            _require(batch_id in binding["batch_files"], "PUBLICATION_BATCH_UNSELECTED")
            rows = []
            for file_id in binding["batch_files"][batch_id]:
                row, frozen, seal = store._file_chain(con, self.operation_id, file_id, digest)
                _require(row[3] == "CATALOG_PUBLISHED", "PUBLICATION_BATCH_CHILDREN_INCOMPLETE")
                pair = frozen["intent"]["catalog_pair"]
                actual = catalog._capture_rows(self.scope, **frozen["intent"]["request"])
                _require(actual["files"] == pair["before"]["files"]
                         and actual["source"] == pair["before"]["source"]
                         and all(actual[table] == pair["after"][table] for table in ("archived", "replicas")),
                         "PUBLICATION_BATCH_CATALOG_CHANGED")
                rows.append((file_id, row[2], frozen["intent"], store._unseal(row[8], row[9])["proof"], row[10], seal))
            return json.loads(store.canonical(rows))
        finally:
            con.rollback()

    def propagate(self, drive_label):
        """Publish the exact complete target batch to the map, without closure."""
        from modelark import publication_map as publish_map, publication_map_files as map_files
        from modelark import publication_map_stage as staging
        self._require()
        batch_id = _id(self.operation_id, "batch:" + drive_label)
        rows = self._batch_rows(batch_id)
        self._recheck(drive_label)
        repository = self._repositories[drive_label]
        map_action = publish_map._id(self.scope, batch_id, "file-objects")
        source_head = _snapshot(repository)
        source_metadata = metadata.capture(repository, ref=ANNEX_REF, binding_digest=self.binding_digest)
        permissions, writes, selections = {}, {}, []
        # Advisory annotations for a shared key use its latest actual tag write;
        # location permission still requires independently proven local bytes.
        for file_id, file_digest, intent, proof, _revision, _seal in sorted(rows, key=lambda item: item[4]):
            _require(intent["request"]["drive_label"] == drive_label, "PUBLICATION_BATCH_TARGET_CHANGED")
            payload.verify(repository.tree, stored_path=intent["stored_path"], annex_key=intent["annex_key"],
                qualified_object_path=intent["object_path"], binding_digest=file_digest, require_scope=self.scope.require_io)
            key = intent["annex_key"]
            permissions.setdefault(key, frozenset({repository.expected_uuid}))
            path = metadata.metadata_path(key) + ".met"
            before = {entry["path"]: bytes.fromhex(entry["data_hex"]) for entry in intent["metadata_before"]["entries"]}
            after = {entry["path"]: bytes.fromhex(entry["data_hex"]) for entry in proof["metadata"]["entries"]}
            write = map_policy.AnnotationWrite(path, before.get(path, b""), after.get(path, b""),
                                               tuple(sorted(intent["tags"].items())), file_digest)
            _require(write.seal() == proof["annotation_seal"], "PUBLICATION_ANNOTATION_RECEIPT_CHANGED")
            writes[path] = write
            selections.append(map_files.FileSelection(
                file_id, trees.TreeEntry(**intent["entry"]), intent.get("retired_path")))
        _require(_snapshot(repository) == source_head and self._batch_rows(batch_id) == rows,
                 "PUBLICATION_BATCH_SOURCE_CHANGED")
        # A durable map action is resumable authority, but not a substitute for
        # fresh proof that this batch's selected archive bytes still exist.
        if self._connection.execute("SELECT 1 FROM publication_actions WHERE operation_id=? AND action_id=?",
                                    [self.operation_id, map_action]).fetchone():
            proof = publish_map.resume(self.stage, self.map, batch_id=batch_id)
            self._recheck(drive_label)
            _require(self._batch_rows(batch_id) == rows, "PUBLICATION_BATCH_CATALOG_CHANGED")
            receipt = self.scope.write(lambda _: store.propagate_batch(self.scope, operation_id=self.operation_id,
                                                                      batch_id=batch_id, map_proof=proof))
            return {"batch_id": batch_id, "receipt_digest": receipt, "proof": proof}
        ceiling = Decimal(str(time.time()))
        identifiers = self._connection.execute("SELECT action_id FROM publication_actions WHERE operation_id=? AND kind='map_stage'",
                                              [self.operation_id]).fetchall()
        saved_ceilings = set()
        for (identifier,) in identifiers:
            record = actions.read(self.scope, identifier)
            intent = record["intent"]
            if (intent.get("expected_phase", {}).get("id") == batch_id
                    and intent.get("source_profile_digest") == repository.profile.digest
                    and "permissions" in intent):
                _require(intent.get("source_annex_oid") == source_metadata.commit_oid,
                         "PUBLICATION_BATCH_SOURCE_CHANGED")
                saved_ceilings.add(str(intent["permissions"]["clock_ceiling"]))
        _require(len(saved_ceilings) <= 1, "PUBLICATION_BATCH_CLOCK_CHANGED")
        if saved_ceilings:
            ceiling = Decimal(saved_ceilings.pop())
        staged_metadata = staging.candidate(self.stage, repository,
            source_annex_oid=source_metadata.commit_oid, allowed_key_uuids=permissions,
            required_key_uuids=permissions, annotation_writes=writes,
            expected_annotation_seals={path: write.seal() for path, write in writes.items()},
            binding_digest=self.binding_digest, clock_ceiling=ceiling, batch_id=batch_id)
        staged_files = map_files.candidate(self.stage, self.map, sources=(
            map_files.SourceFiles(repository, source_head.head_oid, tuple(selections)),), batch_id=batch_id, resume=True)
        proof = publish_map.apply(self.stage, self.map, file_candidate=staged_files,
                                  metadata_candidates=(staged_metadata,))
        self._recheck(drive_label)
        _require(self._batch_rows(batch_id) == rows, "PUBLICATION_BATCH_CATALOG_CHANGED")
        receipt = self.scope.write(lambda _: store.propagate_batch(self.scope, operation_id=self.operation_id,
                                                                  batch_id=batch_id, map_proof=proof))
        return {"batch_id": batch_id, "receipt_digest": receipt, "proof": proof}

    def __exit__(self, exc_type, exc, traceback):
        unfinished = self._active and not self._finished
        self._active = False
        self._stack.close()
        if exc_type is None and unfinished:
            raise PublicationRefused("PUBLICATION_ENCLOSING_COMPLETION_REQUIRED", operation_id=self.operation_id)

    def finish(self):
        """Complete selected batches, freshly verify inventory, then close once.

        Every selected source and target remains required. A stopped or failed
        file leaves its original operation pending; closure never omits it.
        """
        from modelark import publication_inventory as inventory, publication_map as publish_map
        from modelark import publication_map_stage as staging, publication_map_worktree as worktree
        self._require()
        con = self._connection
        # Check completeness before moving any additional map refs.
        batches = sorted({_id(self.operation_id, "batch:" + item.drive_label): item.drive_label
                          for item in self._requests}.items())
        for batch_id, _ in batches:
            self._batch_rows(batch_id)
        for batch_id, label in batches:
            phase = con.execute("SELECT phase FROM publication_batches WHERE operation_id=? AND batch_id=?",
                                [self.operation_id, batch_id]).fetchone()
            _require(phase is not None, "PUBLICATION_BATCH_UNSELECTED")
            if phase[0] == "PREPARED":
                self.propagate(label)
            else:
                _require(phase[0] == "PROPAGATED", "PUBLICATION_BATCH_PHASE_CHANGED")
        last = con.execute("SELECT batch_id FROM publication_batches WHERE operation_id=? ORDER BY committed_revision DESC LIMIT 1",
                           [self.operation_id]).fetchone()[0]
        publish_map.resume(self.stage, self.map, batch_id=last)
        rows = [row for batch_id, _ in batches for row in self._batch_rows(batch_id)]
        expected = {entry.path: entry for entry in trees.TreeSnapshot.from_record(self._before_state["trees"]["@map"]).entries}
        for _, _, intent, _, _, _ in rows:
            entry = trees.TreeEntry(**intent["entry"])
            _require(entry.path not in expected or expected[entry.path] == entry, "PUBLICATION_MAP_FINAL_CONFLICT")
            expected[entry.path] = entry
            retired = intent.get("retired_path")
            if retired is not None:
                _require(retired in expected and retired != entry.path,
                         "PUBLICATION_MAP_FINAL_RETIREMENT_CHANGED")
                del expected[retired]
        expected = tuple(sorted(expected.values()))
        final_map = _snapshot(self.map)
        _require(final_map.entries == expected, "PUBLICATION_MAP_FINAL_TREE_CHANGED")
        worktree.observe(self.map, old=final_map, new=final_map)
        annex = self.map.ref_oid(ANNEX_REF)
        required = {}
        for _, _, intent, _, _, _ in rows:
            required.setdefault(intent["annex_key"], set()).add(self._repositories[intent["request"]["drive_label"]].expected_uuid)
        for key, identities in required.items():
            observed = self.map.effective_locations(key, annex)
            _require(identities <= observed and self.scope.library[1] not in observed,
                     "PUBLICATION_MAP_FINAL_LOCATIONS_CHANGED")
        proofs, observations = {}, {}
        for label, repository in sorted(self._repositories.items()):
            proof = inventory.verify(repository, self._before_state["inventories"][label],
                file_rows=[row for row in rows if row[2]["request"]["drive_label"] == label])
            proofs[label], observations[label] = proof.record(), proof.observation
        for label in self._repositories:
            self._recheck(label)
        staging._no_payload(self.map)
        _require(_snapshot(self.map) == final_map and self.map.ref_oid(ANNEX_REF) == annex,
                 "PUBLICATION_MAP_FINAL_STATE_CHANGED")
        receipt = self.scope.write(lambda _: store.close_operation(self.scope, operation_id=self.operation_id,
            observations=observations, inventory_proofs=proofs, now=datetime.now(timezone.utc).isoformat(sep=" ")))
        self._finished = True
        return {"operation_id": self.operation_id, "phase": "CLOSED", "receipt_digest": receipt}
