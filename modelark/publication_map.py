"""Journalled map publication: quarantine objects, atomic refs, checked replay.

Only independently verified scratch results enter this adapter. Advancing refs
alone is not completion; index/worktree, raw metadata and native locations must
all agree before the exact batch receipt can be published to the catalog.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from uuid import UUID, uuid5

from modelark import publication_actions as actions, publication_map_files as files
from modelark import publication_map_stage as staging, publication_map_tree as metadata
from modelark import publication_map_worktree as worktree, publication_store as store
from modelark import publication_tree as trees
from modelark.publication_policy import _require


def _id(scope, batch_id, step):
    return str(uuid5(UUID(scope.operation_id), "map-publish:" + batch_id + ":" + step))


def _binding(scope):
    return staging._saved_operation(scope)


def _dependencies(scope, values):
    _require(type(values) is list and bool(values), "PUBLICATION_MAP_DEPENDENCIES_MISSING")
    seen = set()
    records = []
    for item in values:
        _require(type(item) is dict and set(item) == {"action_id", "receipt_digest"}
                 and item["action_id"] not in seen, "PUBLICATION_MAP_DEPENDENCIES_INVALID")
        record = actions.read(scope, item["action_id"])
        _require(record["status"] == "VERIFIED" and record["receipt_digest"] == item["receipt_digest"],
                 "PUBLICATION_MAP_DEPENDENCY_CHANGED")
        seen.add(item["action_id"])
        records.append(record)
    return records


def _map(repository):
    staging._actual(repository)
    binding = _binding(repository.scope)
    state = binding["before_state"]
    _require(repository.drive_label is None
             and str(repository.tree.path) == state["roots"]["@map"]
             and repository.profile.record() in (state["profiles"]["@map"], state["profiles"].get("@map:policy")),
             "PUBLICATION_MAP_ROOT_UNSELECTED")
    staging._no_payload(repository)
    return binding


def ref_transaction(plan):
    """Exactly two expected-old ref updates in one native reference transaction."""
    before = trees.TreeSnapshot.from_record(plan["files"]["before"])
    new_head = trees._oid(plan["files"]["new_head"])
    old_annex = metadata._oid(plan["old_annex"])
    new_annex = metadata._oid(plan["new_annex"])
    return ("start\nupdate " + before.head_ref + " " + new_head + " " + before.head_oid
            + "\nupdate " + staging.ANNEX_REF + " " + new_annex + " " + old_annex
            + "\nprepare\ncommit\n").encode("ascii")


def _ref_state(repository, plan):
    before = trees.TreeSnapshot.from_record(plan["files"]["before"])
    head = staging._head(repository)
    _require(head[0] == before.head_ref, "PUBLICATION_MAP_HEAD_CHANGED")
    pair = (head[1], repository.ref_oid(staging.ANNEX_REF))
    old = (before.head_oid, plan["old_annex"])
    new = (plan["files"]["new_head"], plan["new_annex"])
    _require(pair in (old, new), "PUBLICATION_MAP_REF_CONFLICT")
    return "new" if pair == new else "old"


def require_map_action(repository, record):
    """Native command admission, bound to the exact real map and frozen plan."""
    binding = _map(repository)
    plan = record["intent"].get("map_plan")
    _require(type(plan) is dict and plan.get("operation_id") == repository.scope.operation_id
             and plan.get("binding_digest") == store.digest(binding)
             and plan.get("batch_id") in binding["batch_files"]
             and record["intent"].get("map_plan_digest") == store.digest(plan),
             "PUBLICATION_MAP_PLAN_CHANGED")
    _dependencies(repository.scope, record["intent"]["after"])
    state = _ref_state(repository, plan)
    old = trees.TreeSnapshot.from_record(plan["files"]["before"])
    new = tuple(trees.TreeEntry(**item) for item in plan["files"]["entries"])
    observation = worktree.observe(repository, old=old, new=new)
    if record["kind"] == "map_refs":
        _require(state == "old", "PUBLICATION_MAP_REF_CAS_REQUIRED")
        # New files cannot pre-exist before the reference transaction. That is
        # not an interrupted checkout: no such checkout was authorized yet.
        worktree.observe(repository, old=old, new=old)
    else:
        _require(record["kind"] == "map_checkout" and state == "new",
                 "PUBLICATION_MAP_REFS_NOT_PUBLISHED")
        _require(observation.replay_state in {"complete", "old-index-replay"},
                 "PUBLICATION_MAP_REPLAY_UNPROVEN")
    return plan


def _candidate_plan(stage, repository, file_candidate, metadata_candidates):
    staging.require_stage(stage)
    binding = _map(repository)
    scope = repository.scope
    _require(stage.scope is scope and type(file_candidate) is files.FileCandidate
             and type(metadata_candidates) is tuple and bool(metadata_candidates)
             and all(type(item) is staging.StagedCandidate for item in metadata_candidates),
             "PUBLICATION_MAP_CANDIDATES_UNQUALIFIED")
    batch_id = file_candidate.batch_id
    _require(file_candidate.operation_id == scope.operation_id
             and file_candidate.binding_digest == store.digest(binding)
             and batch_id in binding["batch_files"]
             and file_candidate.stage_profile_digest == stage.profile.digest,
             "PUBLICATION_MAP_CANDIDATE_BINDING_CHANGED")
    dependencies = list(file_candidate.record()["action_receipts"])
    previous = None
    for item in metadata_candidates:
        _require(item.operation_id == scope.operation_id and item.batch_id == batch_id
                 and item.binding_digest == file_candidate.binding_digest
                 and item.stage_profile_digest == stage.profile.digest
                 and (previous is None or previous == item.before.commit_oid),
                 "PUBLICATION_MAP_METADATA_CHAIN_CHANGED")
        previous = item.after.commit_oid
        dependencies.extend(item.record()["action_receipts"])
    records = _dependencies(scope, dependencies)
    # Candidate dataclasses are convenient immutable values, not capabilities.
    # Rebind their assertions to the actual independently verified action rows.
    verified = [record["receipt"]["receipt"] for record in records]
    for item in metadata_candidates:
        _require(any(row.get("candidate_snapshot") == item.after.record()
                     and row.get("validation") == json.loads(item.validation_json)
                     and row.get("native_locations") == item.record()["native_locations"]
                     for row in verified), "PUBLICATION_MAP_METADATA_RECEIPT_CHANGED")
    _require(any(record["intent"].get("selection") == json.loads(file_candidate.selection_json)
                 and record["intent"].get("before") == file_candidate.before.record()
                 for record in records), "PUBLICATION_MAP_FILE_RECEIPT_CHANGED")
    _require(tuple(trees.TreeEntry(**entry) for entry in file_candidate.record()["entries"])
             == trees._entries(stage.read("ls-tree", "-r", "--full-tree", "-z", file_candidate.new_tree)),
             "PUBLICATION_MAP_FILE_CANDIDATE_CHANGED")
    tree, parents = trees._commit(stage.read, file_candidate.new_head)
    _require(tree == file_candidate.new_tree
             and (file_candidate.new_head == file_candidate.before.head_oid and not file_candidate.delta
                  or parents == (file_candidate.before.head_oid,)), "PUBLICATION_MAP_FILE_CANDIDATE_CHANGED")
    if file_candidate.delta:
        _require(any(row.get("head") == file_candidate.new_head and row.get("tree") == tree
                     and row.get("parent") == file_candidate.before.head_oid for row in verified),
                 "PUBLICATION_MAP_FILE_RECEIPT_CHANGED")
    final = metadata_candidates[-1].after
    _require(metadata.capture(stage, ref=staging.ANNEX_REF, binding_digest=final.binding_digest) == final,
             "PUBLICATION_MAP_METADATA_CANDIDATE_CHANGED")
    return {"version": 1, "operation_id": scope.operation_id, "batch_id": batch_id,
            "binding_digest": file_candidate.binding_digest,
            "stage_profile_digest": stage.profile.digest,
            "files": file_candidate.record(), "metadata": [item.record() for item in metadata_candidates],
            "old_annex": metadata_candidates[0].before.commit_oid, "new_annex": final.commit_oid,
            "dependencies": dependencies}


def apply(stage, repository, *, file_candidate, metadata_candidates):
    """Publish/replay one exact prepared batch, never close its enclosing drive."""
    plan = _candidate_plan(stage, repository, file_candidate, metadata_candidates)
    return _apply_plan(stage, repository, plan)


def resume(stage, repository, *, batch_id):
    """Resume only the plan sealed before the first map-object import."""
    staging.require_stage(stage)
    _map(repository)
    record = actions.read(repository.scope, _id(repository.scope, batch_id, "file-objects"))
    plan = record["intent"].get("map_plan")
    _require(type(plan) is dict and plan.get("batch_id") == batch_id
             and record["intent"].get("map_plan_digest") == store.digest(plan),
             "PUBLICATION_MAP_PLAN_CHANGED")
    return _apply_plan(stage, repository, plan)


def _apply_plan(stage, repository, plan):
    staging.require_stage(stage)
    binding = _map(repository)
    scope, batch_id = repository.scope, plan["batch_id"]
    _require(stage.scope is scope and plan["binding_digest"] == store.digest(binding)
             and plan["stage_profile_digest"] == stage.profile.digest, "PUBLICATION_MAP_PLAN_CHANGED")
    _dependencies(scope, plan["dependencies"])
    old = trees.TreeSnapshot.from_record(plan["files"]["before"])
    new = tuple(trees.TreeEntry(**item) for item in plan["files"]["entries"])
    profile = repository.profile.record()
    dependencies = list(plan["dependencies"])

    def prepare(step, kind, argv, data=b""):
        identifier = _id(scope, batch_id, step)
        intent = {"expected_phase": {"entity": "batch", "id": batch_id, "phase": "PREPARED"},
                  "profile_digest": repository.profile.digest,
                  "root": {key: profile[key] for key in ("root_identity", "mount_id", "annex_uuid")},
                  "map_plan": plan, "map_plan_digest": store.digest(plan), "after": list(dependencies),
                  "command": {"argv": list(argv), "stdin_digest": hashlib.sha256(data).hexdigest()}}
        scope.write(lambda _: actions.prepare(scope, action_id=identifier, kind=kind, intent=intent))
        return actions.read(scope, identifier)

    def verified(record, receipt):
        scope.write(lambda _: actions.verify(scope, action_id=record["action_id"],
                    intent_digest=record["intent_digest"], receipt=receipt))
        result = actions.read(scope, record["action_id"])
        dependencies.append({"action_id": result["action_id"], "receipt_digest": result["receipt_digest"]})

    for step, oid in (("file-objects", plan["files"]["new_head"]), ("annex-objects", plan["new_annex"])):
        ref = "refs/modelark/map-candidate/" + _id(scope, batch_id, step)
        record = prepare(step, "map_stage", ("fetch-local-object", stage.profile.digest, oid, ref))
        current = repository.ref_oid(ref)
        if current is None:
            _require(record["status"] == "PREPARED" and _ref_state(repository, plan) == "old",
                     "PUBLICATION_MAP_IMPORTED_OBJECTS_MISSING")
            repository._fetch_action(record["action_id"], stage, oid, ref)
        else:
            _require(current == oid, "PUBLICATION_MAP_QUARANTINE_CHANGED")
        if step == "file-objects":
            tree, parents = trees._commit(repository.read, oid)
            _require(tree == plan["files"]["new_tree"]
                     and trees._entries(repository.read("ls-tree", "-r", "--full-tree", "-z", tree)) == new,
                     "PUBLICATION_MAP_IMPORTED_TREE_CHANGED")
            proof = {"ref": ref, "head": oid, "tree": tree, "parents": list(parents)}
        else:
            snapshot = metadata.capture(repository, ref=ref, binding_digest=plan["binding_digest"], expected_oid=oid)
            expected = plan["metadata"][-1]["after"]
            _require(all(snapshot.record()[name] == expected[name] for name in ("commit_oid", "tree_oid", "parents", "entries")),
                     "PUBLICATION_MAP_IMPORTED_METADATA_CHANGED")
            proof = {"metadata": snapshot.record()}
        verified(record, proof)

    transaction = ref_transaction(plan)
    refs = prepare("refs", "map_refs", ("update-ref", "--stdin"), transaction)
    if _ref_state(repository, plan) == "old":
        _require(refs["status"] == "PREPARED", "PUBLICATION_MAP_VERIFIED_REFS_CHANGED")
        repository._run_action(refs["action_id"], "update-ref", "--stdin", input_data=transaction)
    _require(_ref_state(repository, plan) == "new", "PUBLICATION_MAP_REF_PUBLICATION_FAILED")
    verified(refs, {"head_ref": old.head_ref, "head": plan["files"]["new_head"], "annex": plan["new_annex"]})

    checkout = prepare("checkout", "map_checkout", ("read-tree", "--reset", "-u", plan["files"]["new_head"]))
    observed = worktree.observe(repository, old=old, new=new)
    if observed.replay_state != "complete":
        _require(checkout["status"] == "PREPARED", "PUBLICATION_MAP_VERIFIED_WORKTREE_CHANGED")
        repository._run_action(checkout["action_id"], "read-tree", "--reset", "-u", plan["files"]["new_head"])
        observed = worktree.observe(repository, old=old, new=new)
    _require(observed.replay_state == "complete", "PUBLICATION_MAP_CHECKOUT_INCOMPLETE")
    final_tree = trees.capture(repository.tree, read_git=repository.read, require_scope=scope.require_io)
    _require(final_tree.head_oid == plan["files"]["new_head"] and final_tree.entries == new,
             "PUBLICATION_MAP_FINAL_TREE_CHANGED")
    final_metadata = metadata.capture(repository, ref=staging.ANNEX_REF,
                                      binding_digest=plan["binding_digest"], expected_oid=plan["new_annex"])
    locations = plan["metadata"][-1]["native_locations"]
    for key, identities in locations.items():
        _require(sorted(repository.effective_locations(key, plan["new_annex"])) == identities,
                 "PUBLICATION_MAP_NATIVE_LOCATIONS_CHANGED")
    staging._no_payload(repository)
    _require(_ref_state(repository, plan) == "new", "PUBLICATION_MAP_FINAL_REFS_CHANGED")
    # Stable exact logical postconditions: inode/mtime observations are fresh
    # safety checks, not a reason to invent a new receipt on an exact retry.
    proof = {"version": 1, "kind": "published-map", "plan_digest": store.digest(plan),
             "tree": final_tree.record(), "metadata": final_metadata.record(),
             "native_locations": locations, "no_annex_payload": True,
             "entries": [asdict(entry) for entry in observed.index]}
    verified(checkout, proof)
    _require(worktree.observe(repository, old=old, new=new).replay_state == "complete",
             "PUBLICATION_MAP_FINAL_WORKTREE_CHANGED")
    return {**proof, "actions": dependencies}
