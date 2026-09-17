"""Shared forward/retry phase engine for one immutable file publication.

No new file intent, source assumption, dirty generation or original-byte claim is
created here. Ambiguous native-add effects remain pending instead of being run
again as if a failure implied that nothing happened.
"""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import time

from modelark import publication_actions as actions, publication_catalog as catalog
from modelark import publication_install as install, publication_map_policy as policy
from modelark import publication_map_tree as metadata, publication_payload as payload
from modelark import publication_store as store, publication_tree as trees
from modelark.publication_policy import _require


def publish(owner, request, artifact, *, source=None):
    from modelark.archive_publisher import ArchivePublisher, _id, _root, _snapshot
    from modelark.publication_artifact import VerifiedArtifact
    _require(type(owner) is ArchivePublisher and type(artifact) is VerifiedArtifact
             and artifact.scope is owner.scope and request in owner._files,
             "PUBLICATION_FILE_RESUME_UNQUALIFIED")
    owner._require()
    scope, con, file_id = owner.scope, owner._connection, owner._files[request]
    repository = owner._repositories[request.drive_label]

    def saved():
        scope.require_io()
        con.execute("BEGIN")
        try:
            binding, digest, _ = store._load_bound_operation(scope, owner.operation_id)
            row, frozen, _ = store._file_chain(con, owner.operation_id, file_id, digest)
            return row, frozen["intent"]
        finally:
            con.rollback()

    row, intent = saved()
    _require(intent["request"] == request.record(), "PUBLICATION_FILE_RESUME_BINDING_CHANGED")
    if source is not None:
        from modelark.publication_replica_pipeline import require_source
        require_source(source, owner, request, artifact)
        source.recheck()
        expected_pair = intent["catalog_pair"]["before"]
        if row[3] == "CATALOG_PUBLISHED":
            expected_pair = {**expected_pair, **intent["catalog_pair"]["after"]}
        _require(expected_pair == source.pair, "PUBLICATION_REPLICA_SOURCE_CHANGED")
        current_source = source.record()
        _require({**current_source, "pair": intent["catalog_pair"]["before"]} == intent.get("source_proof"),
                 "PUBLICATION_REPLICA_SOURCE_PROOF_CHANGED")
    pair = intent["catalog_pair"]
    expected_artifact = pair["after"]["archived"]
    actual = artifact.proof
    _require(actual.original_sha256 == expected_artifact["orig_sha256"]
             and actual.original_bytes == expected_artifact["orig_bytes"]
             and actual.stored_bytes == expected_artifact["stored_bytes"]
             and actual.compressed == bool(expected_artifact["compressed"])
             and actual.stored_sha256 == (expected_artifact["znn_sha256"] if actual.compressed
                                         else expected_artifact["orig_sha256"]),
             "PUBLICATION_FILE_RESUME_ARTIFACT_CHANGED")
    before = trees.TreeSnapshot.from_record(intent["tree_before"])
    entry = trees.TreeEntry(**intent["entry"])
    path, key, object_path = intent["stored_path"], intent["annex_key"], intent["object_path"]
    _require(repository.object_path(key) == object_path, "PUBLICATION_FILE_RESUME_OBJECT_CHANGED")
    expected_index = tuple(sorted((*before.entries, entry)))
    file_digest = row[2]

    def recheck():
        owner._recheck(request.drive_label)
        artifact.check()
        if source is not None:
            source.recheck()

    def local():
        recheck()
        return payload.verify(repository.tree, stored_path=path, annex_key=key,
            qualified_object_path=object_path, binding_digest=file_digest, require_scope=scope.require_io)

    def index():
        return trees._entries(repository.read("ls-files", "--full-name", "--stage", "-v", "-z"), index=True)

    def committed():
        recheck()
        return trees.verify(repository.tree, read_git=repository.read, require_scope=scope.require_io,
            before=before, allowed_delta={path: entry}, stored_path=path, annex_key=key,
            qualified_object_path=object_path, binding_digest=file_digest, profile_digest=repository.profile.digest)

    def prepare(name, kind, phase, argv):
        identifier = _id(file_id, name)
        present = con.execute("SELECT 1 FROM publication_actions WHERE operation_id=? AND action_id=?",
                              [owner.operation_id, identifier]).fetchone()
        if not present:
            scope.write(lambda _: actions.prepare(scope, action_id=identifier, kind=kind, intent={
                "expected_phase": {"entity": "file", "id": file_id, "phase": phase},
                "profile_digest": repository.profile.digest, "root": _root(repository.profile.record()),
                "command": {"argv": list(argv), "stdin_digest": hashlib.sha256(b"").hexdigest()}}))
        record = actions.read(scope, identifier)
        _require(record["kind"] == kind and record["intent"]["command"]["argv"] == list(argv),
                 "PUBLICATION_FILE_RESUME_ACTION_CHANGED")
        return record, bool(present)

    if row[3] == "PREPARED":
        identifier = install.prepare(repository, artifact, file_id=file_id, plan=intent["install"])
        installed = actions.read(scope, identifier)
        if installed["status"] != "VERIFIED":
            install.apply(repository, artifact, identifier)
        recheck()
        add = None
        if source is None:
            argv = ("annex", "add", "--force", "--force-large", "--backend=SHA256", "--", path)
            add, existed = prepare("annex-add", "annex_add", "PREPARED", argv)
            if not existed:
                repository._run_action(add["action_id"], *argv)
            # Existing PREPARED native add is observation-only: a consumed or
            # partially transformed input does not authorize another invocation.
        else:
            from modelark import publication_replica
            publication_replica.apply(repository, artifact, file_id=file_id)
        proof = local()
        _require(index() == expected_index, "PUBLICATION_FILE_RESUME_ADD_INCOMPLETE")
        def advance_local(_):
            if add is not None:
                actions.verify(scope, action_id=add["action_id"], intent_digest=add["intent_digest"],
                    receipt={"local": proof.record(), "index": [asdict(item) for item in expected_index]})
            store.advance_file(scope, operation_id=owner.operation_id, file_id=file_id,
                               phase="LOCAL_VERIFIED", proof=proof.record())
        scope.write(advance_local)
        row, _ = saved()

    if row[3] == "LOCAL_VERIFIED":
        local()
        _require(index() == expected_index, "PUBLICATION_FILE_RESUME_INDEX_CHANGED")
        argv = ("commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m", "ModelArk publication " + owner.operation_id)
        action, _ = prepare("file-commit", "file_commit", "LOCAL_VERIFIED", argv)
        current_head = trees._head(repository.read)
        if current_head == (before.head_ref, before.head_oid):
            _require(action["status"] == "PREPARED", "PUBLICATION_FILE_VERIFIED_COMMIT_CHANGED")
            repository._run_action(action["action_id"], *argv)
        tree = committed()
        def advance_tree(_):
            actions.verify(scope, action_id=action["action_id"], intent_digest=action["intent_digest"], receipt=tree.record())
            store.advance_file(scope, operation_id=owner.operation_id, file_id=file_id,
                               phase="TREE_VERIFIED", proof=tree.record())
        scope.write(advance_tree)
        row, _ = saved()

    if row[3] == "TREE_VERIFIED":
        tree = committed()
        argv = ("modelark-tag-and-flush", key, store.canonical(intent["tags"]))
        action, existed = prepare("metadata", "annex_metadata", "TREE_VERIFIED", argv)
        baseline_oid = intent["metadata_before"]["commit_oid"]
        if not existed or repository.ref_oid("refs/heads/git-annex") == baseline_oid:
            _require(action["status"] == "PREPARED", "PUBLICATION_FILE_VERIFIED_METADATA_CHANGED")
            repository._run_action(action["action_id"], *argv)
        after = metadata.capture(repository, ref="refs/heads/git-annex", binding_digest=file_digest)
        baseline = {entry["path"]: bytes.fromhex(entry["data_hex"]) for entry in intent["metadata_before"]["entries"]}
        tag_path = metadata.metadata_path(key) + ".met"
        write = policy.AnnotationWrite(tag_path, baseline.get(tag_path, b""), after.metadata().get(tag_path, b""),
                                       tuple(sorted(intent["tags"].items())), file_digest)
        allowed = {key: frozenset({repository.expected_uuid})}
        validation = policy.validate_candidate(baseline=baseline, incoming=after.metadata(), candidate=after.metadata(),
            allowed_key_uuids=allowed, required_key_uuids=allowed, map_uuid=scope.library[1],
            clock_ceiling=Decimal(str(time.time())), annotation_writes={tag_path: write},
            expected_annotation_seals={tag_path: write.seal()})
        locations = repository.effective_locations(key, after.commit_oid)
        _require(repository.expected_uuid in locations and scope.library[1] not in locations,
                 "PUBLICATION_NATIVE_COPY_UNPROVEN")
        final_local = local()
        _require(_snapshot(repository) == tree.after, "PUBLICATION_FILE_RESUME_TREE_CHANGED")
        def advance_catalog(_):
            actions.verify(scope, action_id=action["action_id"], intent_digest=action["intent_digest"],
                receipt={"metadata": after.record(), "admission": validation,
                         "annotation_seal": write.seal(), "locations": sorted(locations)})
            store.advance_file(scope, operation_id=owner.operation_id, file_id=file_id, phase="CATALOG_PUBLISHED",
                proof={"local": final_local.record(), "tree": tree.record(), "metadata": after.record(),
                       "annotation_seal": write.seal()}, catalog_cas=lambda con, frozen: catalog.compare_and_swap(scope, frozen["catalog_pair"]))
        scope.write(advance_catalog)
        row, _ = saved()

    _require(row[3] == "CATALOG_PUBLISHED", "PUBLICATION_FILE_RESUME_PHASE_CHANGED")
    local()
    # Another file may have since added a descendant commit: require this exact
    # entry in the current committed/index tree rather than rewind that history.
    current = _snapshot(repository)
    _require(entry in current.entries, "PUBLICATION_FILE_RESUME_TREE_CHANGED")
    con.execute("BEGIN")
    try:
        current_pair = catalog._capture_rows(scope, **request.record())
        _require(current_pair["files"] == pair["before"]["files"] and current_pair["source"] == pair["before"]["source"]
                 and all(current_pair[name] == pair["after"][name] for name in ("archived", "replicas")),
                 "PUBLICATION_FILE_RESUME_CATALOG_CHANGED")
    finally:
        con.rollback()
    return {"file_id": file_id, "stored_path": path, "annex_key": key,
            "stored_bytes": actual.stored_bytes, "phase": "CATALOG_PUBLISHED"}
