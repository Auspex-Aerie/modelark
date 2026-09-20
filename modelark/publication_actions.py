"""Immutable mutation intentions; persistence is not native execution authority.

The coordinator and qualified native runner must independently validate the
typed operation and actual result. This journal neither runs commands nor
confers a physical proof because a caller supplied an argv, digest or receipt.
"""
from __future__ import annotations

import json
import sqlite3

from modelark import catalog_write_context as writes
from modelark.publication_policy import PublicationRefused


ACTION_DDL_V9 = (
    """CREATE TABLE publication_actions (
        operation_id TEXT NOT NULL, action_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('policy_setup','payload_install','annex_add',
            'annex_metadata','file_commit','map_stage','map_refs','map_checkout','staging_release','staging_directory')),
        intent_json TEXT NOT NULL, intent_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('PREPARED','VERIFIED')),
        prepared_revision INTEGER, verified_revision INTEGER,
        receipt_json TEXT, receipt_digest TEXT,
        PRIMARY KEY(operation_id,action_id),
        FOREIGN KEY(operation_id) REFERENCES publication_operations(operation_id),
        CHECK((receipt_json IS NULL)=(receipt_digest IS NULL)),
        CHECK((status='PREPARED' AND receipt_json IS NULL AND verified_revision IS NULL)
           OR (status='VERIFIED' AND receipt_json IS NOT NULL)))""",
)

ACTION_DDL = (
    """CREATE TABLE publication_actions (
        operation_id TEXT NOT NULL, action_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('policy_setup','payload_install','annex_add',
            'annex_metadata','file_commit','map_stage','map_refs','map_checkout','staging_release','staging_directory',
            'source_retirement')),
        intent_json TEXT NOT NULL, intent_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('PREPARED','VERIFIED')),
        prepared_revision INTEGER, verified_revision INTEGER,
        receipt_json TEXT, receipt_digest TEXT,
        PRIMARY KEY(operation_id,action_id),
        FOREIGN KEY(operation_id) REFERENCES publication_operations(operation_id),
        CHECK((receipt_json IS NULL)=(receipt_digest IS NULL)),
        CHECK((status='PREPARED' AND receipt_json IS NULL AND verified_revision IS NULL)
           OR (status='VERIFIED' AND receipt_json IS NOT NULL)))""",
)
KINDS = frozenset({"policy_setup", "payload_install", "annex_add", "annex_metadata",
                   "file_commit", "map_stage", "map_refs", "map_checkout", "staging_release", "staging_directory",
                   "source_retirement"})


def _store():
    # Both frozen v9 and current action contracts are imported by the store.
    from modelark import publication_store
    return publication_store


def _result(record, *, noop):
    from modelark.proposal import GraphResult
    return GraphResult(proven_noop=noop, value=record)


def _operation(scope):
    scope.require_transaction(scope.connection)
    if scope.operation_id is None:
        raise PublicationRefused("PUBLICATION_ACTION_OPERATION_REQUIRED")
    return _store().load_owned_operation(scope, scope.operation_id)


def _phase(scope, expected, binding):
    if not isinstance(expected, dict) or set(expected) != {"entity", "id", "phase"}:
        raise PublicationRefused("PUBLICATION_ACTION_PHASE_INVALID")
    entity, identifier, phase = (expected[name] for name in ("entity", "id", "phase"))
    if entity == "operation":
        if identifier != scope.operation_id or phase != "PREPARED":
            raise PublicationRefused("PUBLICATION_ACTION_PHASE_CHANGED")
        return
    if entity not in {"file", "batch"}:
        raise PublicationRefused("PUBLICATION_ACTION_PHASE_INVALID")
    store = _store()
    store.canonical_uuid(identifier)
    if entity == "file":
        selected = any(identifier in files for files in binding["batch_files"].values())
        table, column = "publication_files", "file_id"
    else:
        selected = identifier in binding["batch_files"]
        table, column = "publication_batches", "batch_id"
    if not selected:
        raise PublicationRefused("PUBLICATION_ACTION_PHASE_UNSELECTED")
    row = scope.connection.execute(
        f"SELECT phase FROM {table} WHERE operation_id=? AND {column}=?",
        [scope.operation_id, identifier],
    ).fetchone()
    if row is None or row[0] != phase:
        raise PublicationRefused("PUBLICATION_ACTION_PHASE_CHANGED")


def _validate_intent(scope, value, binding, *, check_phase):
    store = _store()
    if not isinstance(value, dict) or not {
        "expected_phase", "profile_digest", "root",
    } <= value.keys():
        raise PublicationRefused("PUBLICATION_ACTION_INTENT_INVALID")
    store._require_digest(value["profile_digest"])
    before = binding.get("before_state")
    profiles = before.get("profiles") if isinstance(before, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise PublicationRefused("PUBLICATION_ACTION_PROFILES_REQUIRED")
    matching = [profile for profile in profiles.values()
                if isinstance(profile, dict) and store.digest(profile) == value["profile_digest"]]
    if not matching:
        raise PublicationRefused("PUBLICATION_ACTION_PROFILE_UNSELECTED")
    root = value["root"]
    if (not isinstance(root, dict) or set(root) != {"root_identity", "mount_id", "annex_uuid"}
            or any(name not in matching[0] or root[name] != matching[0][name] for name in root)):
        raise PublicationRefused("PUBLICATION_ACTION_ROOT_CHANGED")
    command, filesystem = "command" in value, "filesystem_plan" in value
    if command == filesystem:
        raise PublicationRefused("PUBLICATION_ACTION_MUTATION_PLAN_INVALID")
    if command:
        spec = value["command"]
        if (not isinstance(spec, dict) or set(spec) != {"argv", "stdin_digest"}
                or not isinstance(spec["argv"], list) or not spec["argv"]
                or any(not isinstance(arg, str) or "\0" in arg for arg in spec["argv"])):
            raise PublicationRefused("PUBLICATION_ACTION_COMMAND_INVALID")
        store._require_digest(spec["stdin_digest"])
    elif not isinstance(value["filesystem_plan"], dict) or not value["filesystem_plan"]:
        raise PublicationRefused("PUBLICATION_ACTION_MUTATION_PLAN_INVALID")
    dependencies = value.get("after", [])
    if not isinstance(dependencies, list):
        raise PublicationRefused("PUBLICATION_ACTION_DEPENDENCIES_INVALID")
    seen = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"action_id", "receipt_digest"}:
            raise PublicationRefused("PUBLICATION_ACTION_DEPENDENCIES_INVALID")
        identifier = store.canonical_uuid(dependency["action_id"])
        store._require_digest(dependency["receipt_digest"])
        if identifier in seen:
            raise PublicationRefused("PUBLICATION_ACTION_DEPENDENCIES_INVALID")
        seen.add(identifier)
        prior = _load(scope, identifier, store.digest(binding))
        if prior["status"] != "VERIFIED" or prior["receipt_digest"] != dependency["receipt_digest"]:
            raise PublicationRefused("PUBLICATION_ACTION_DEPENDENCY_UNVERIFIED")
    if check_phase:
        _phase(scope, value["expected_phase"], binding)


def _load(scope, action_id, operation_digest):
    store = _store()
    row = scope.connection.execute(
        "SELECT kind,intent_json,intent_digest,status,prepared_revision,verified_revision,"
        "receipt_json,receipt_digest FROM publication_actions WHERE operation_id=? AND action_id=?",
        [scope.operation_id, action_id],
    ).fetchone()
    if row is None:
        raise PublicationRefused("PUBLICATION_ACTION_MISSING", action_id=action_id)
    frozen = store._unseal(row[1], row[2])
    if (row[0] not in KINDS or frozen.get("version") != 1
            or frozen.get("operation_digest") != operation_digest
            or frozen.get("operation_id") != scope.operation_id
            or frozen.get("action_id") != action_id or frozen.get("kind") != row[0]
            or not isinstance(frozen.get("intent"), dict)):
        raise PublicationRefused("PUBLICATION_ACTION_BINDING_CHANGED")
    revision = store._revision(scope.connection)
    if type(row[4]) is not int or not 0 < row[4] <= revision:
        raise PublicationRefused("PUBLICATION_ACTION_REVISION_UNPROVEN")
    receipt = None
    if row[3] == "PREPARED":
        if any(item is not None for item in row[5:]):
            raise PublicationRefused("PUBLICATION_ACTION_STATUS_UNPROVEN")
    elif row[3] == "VERIFIED":
        receipt = store._unseal(row[6], row[7])
        if (type(row[5]) is not int or not row[4] < row[5] <= revision
                or receipt.get("version") != 1 or receipt.get("operation_digest") != operation_digest
                or receipt.get("action_id") != action_id or receipt.get("intent_digest") != row[2]
                or not isinstance(receipt.get("receipt"), dict) or not receipt["receipt"]):
            raise PublicationRefused("PUBLICATION_ACTION_RECEIPT_UNPROVEN")
    else:
        raise PublicationRefused("PUBLICATION_ACTION_STATUS_UNPROVEN")
    return {"action_id": action_id, "operation_id": scope.operation_id, "kind": row[0],
            "intent": frozen["intent"], "intent_digest": row[2], "status": row[3],
            "prepared_revision": row[4], "verified_revision": row[5],
            "receipt": receipt, "receipt_digest": row[7]}


def _stamp(scope, action_id, seal, status, revision):
    store = _store()
    column = "prepared_revision" if status == "PREPARED" else "verified_revision"

    def finalize(new_revision):
        if scope.connection.execute(
            f"UPDATE publication_actions SET {column}=? WHERE operation_id=? AND action_id=? "
            f"AND intent_digest=? AND status=? AND {column} IS NULL",
            [new_revision, scope.operation_id, action_id, seal, status],
        ).rowcount != 1:
            raise PublicationRefused("PUBLICATION_ACTION_REVISION_CAS_FAILED")

    writes.defer_revision_once(scope.connection, scope.writer,
                               key=("publication-action", scope.operation_id, action_id, status),
                               binding=seal, finalize=finalize)
    store._stamp_operation(scope, scope.operation_id, revision)


def prepare(scope, *, action_id, kind, intent):
    """Persist one immutable, phase-bound mutation plan before any physical IO."""
    store = _store()
    binding, operation_digest, revision = _operation(scope)
    store.canonical_uuid(action_id)
    if not isinstance(kind, str) or kind not in KINDS:
        raise PublicationRefused("PUBLICATION_ACTION_KIND_INVALID")
    if kind == "source_retirement" and scope.connection.execute(
            "PRAGMA user_version").fetchone()[0] < store.VERSION:
        raise PublicationRefused("PUBLICATION_SCHEMA_UPGRADE_REQUIRED")
    # Normalize caller-owned dictionaries before persisting or returning them.
    intent = json.loads(store.canonical(intent))
    frozen = {"version": 1, "operation_id": scope.operation_id, "operation_digest": operation_digest,
              "action_id": action_id, "kind": kind, "intent": intent}
    seal = store.digest(frozen)
    exists = scope.connection.execute(
        "SELECT 1 FROM publication_actions WHERE operation_id=? AND action_id=?",
        [scope.operation_id, action_id],
    ).fetchone()
    if exists:
        record = _load(scope, action_id, operation_digest)
        if record["intent_digest"] != seal:
            raise PublicationRefused("PUBLICATION_ACTION_PREPARE_CONFLICT")
        _validate_intent(scope, intent, binding, check_phase=record["status"] == "PREPARED")
        return _result(record, noop=True)
    _validate_intent(scope, intent, binding, check_phase=True)
    try:
        scope.connection.execute(
            "INSERT INTO publication_actions(operation_id,action_id,kind,intent_json,intent_digest,status) "
            "VALUES(?,?,?,?,?,'PREPARED')",
            [scope.operation_id, action_id, kind, store.canonical(frozen), seal],
        )
    except sqlite3.IntegrityError as exc:
        raise PublicationRefused("PUBLICATION_ACTION_PREPARE_CONFLICT") from exc
    _stamp(scope, action_id, seal, "PREPARED", revision)
    return _result({"action_id": action_id, "intent_digest": seal, "status": "PREPARED"}, noop=False)


def verify(scope, *, action_id, intent_digest, receipt):
    """Record independently checked completion; receipt JSON itself proves no IO."""
    store = _store()
    binding, operation_digest, revision = _operation(scope)
    store.canonical_uuid(action_id)
    store._require_digest(intent_digest)
    record = _load(scope, action_id, operation_digest)
    if record["intent_digest"] != intent_digest:
        raise PublicationRefused("PUBLICATION_ACTION_BINDING_CHANGED")
    if not isinstance(receipt, dict) or not receipt:
        raise PublicationRefused("PUBLICATION_ACTION_RECEIPT_INVALID")
    frozen = {"version": 1, "operation_digest": operation_digest, "action_id": action_id,
              "intent_digest": intent_digest, "receipt": receipt}
    seal = store.digest(frozen)
    _validate_intent(scope, record["intent"], binding, check_phase=record["status"] == "PREPARED")
    if record["status"] == "VERIFIED":
        if record["receipt_digest"] != seal:
            raise PublicationRefused("PUBLICATION_ACTION_VERIFY_CONFLICT")
        return _result(record, noop=True)
    if scope.connection.execute(
        "UPDATE publication_actions SET status='VERIFIED',receipt_json=?,receipt_digest=? "
        "WHERE operation_id=? AND action_id=? AND status='PREPARED' AND intent_digest=?",
        [store.canonical(frozen), seal, scope.operation_id, action_id, intent_digest],
    ).rowcount != 1:
        raise PublicationRefused("PUBLICATION_ACTION_VERIFY_CONFLICT")
    _stamp(scope, action_id, intent_digest, "VERIFIED", revision)
    return _result({**record, "status": "VERIFIED", "receipt": frozen, "receipt_digest": seal}, noop=False)


def read(scope, action_id):
    """Read durable intent only through a live IO scope; never while a TX is held."""
    store = _store()
    store.canonical_uuid(action_id)
    scope.require_io()
    if scope.operation_id is None:
        raise PublicationRefused("PUBLICATION_ACTION_OPERATION_REQUIRED")
    scope.connection.execute("BEGIN")
    try:
        scope.require()
        binding, operation_digest, _ = store._load_bound_operation(scope, scope.operation_id)
        record = _load(scope, action_id, operation_digest)
        _validate_intent(scope, record["intent"], binding, check_phase=record["status"] == "PREPARED")
    finally:
        scope.connection.rollback()
    scope.require_io()
    return record


def verified_receipts(scope):
    """Return exact action seals for enclosing closure, under its owning TX.

    No pending action may disappear behind operation closure. An empty set
    makes no statement that the coordinator's required action set was complete;
    that separate plan/workset check remains the coordinator's responsibility.
    """
    binding, operation_digest, _ = _operation(scope)
    identifiers = [row[0] for row in scope.connection.execute(
        "SELECT action_id FROM publication_actions WHERE operation_id=? ORDER BY action_id",
        [scope.operation_id],
    )]
    receipts = []
    for identifier in identifiers:
        record = _load(scope, identifier, operation_digest)
        if record["status"] != "VERIFIED":
            raise PublicationRefused("PUBLICATION_ACTIONS_INCOMPLETE", action_id=identifier)
        _validate_intent(scope, record["intent"], binding, check_phase=False)
        receipts.append({key: record[key] for key in (
            "action_id", "kind", "intent_digest", "receipt_digest", "verified_revision",
        )})
    return receipts
