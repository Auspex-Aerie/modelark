"""Durable archive-publication records and the shared fail-closed read guard.

Schema 9 is readable when the exact publication contract is present.
Installation is an explicit migration primitive, never an import/connect side
effect. Supporting the reader floor does not enable conversion or activate
writer/closure integrations (DEC-157).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextvars import ContextVar
from typing import Iterable
import uuid

from modelark import catalog_write_context as writes
from modelark.publication_actions import ACTION_DDL
from modelark.publication_policy import PublicationRefused

VERSION = 9
PROTOCOL = 1
_CLOSING = ContextVar("modelark_publication_closing", default=None)
_CATALOG_WRITE = ContextVar("modelark_publication_catalog_write", default=None)
TABLES = frozenset({"publication_library", "publication_operations", "publication_participants",
                    "publication_files", "publication_batches", "publication_clone_obligations", "publication_actions"})

DDL = (
    """CREATE TABLE publication_library (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
        library_id TEXT NOT NULL UNIQUE, map_uuid TEXT NOT NULL UNIQUE,
        protocol_version INTEGER NOT NULL CHECK(protocol_version=1))""",
    """CREATE TABLE publication_operations (
        operation_id TEXT PRIMARY KEY, library_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('fill','replica','registration','maintenance','returning_clone')),
        binding_json TEXT NOT NULL, binding_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PREPARED','CLOSED')),
        created_revision INTEGER, closed_revision INTEGER, closure_json TEXT,
        last_revision INTEGER,
        FOREIGN KEY(library_id) REFERENCES publication_library(library_id),
        CHECK((state='PREPARED' AND closed_revision IS NULL AND closure_json IS NULL)
           OR (state='CLOSED' AND closure_json IS NOT NULL)))""",
    """CREATE TABLE publication_participants (
        operation_id TEXT NOT NULL, drive_label TEXT NOT NULL,
        annex_uuid TEXT NOT NULL, identity_epoch INTEGER NOT NULL CHECK(identity_epoch>0),
        generation INTEGER NOT NULL CHECK(generation>0), identity_fingerprint TEXT NOT NULL,
        owner_session_id TEXT, owner_fencing_token INTEGER,
        PRIMARY KEY(operation_id,drive_label),
        FOREIGN KEY(operation_id) REFERENCES publication_operations(operation_id),
        CHECK((owner_session_id IS NULL)=(owner_fencing_token IS NULL)))""",
    """CREATE TABLE publication_files (
        file_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        intent_json TEXT NOT NULL, intent_digest TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('PREPARED','LOCAL_VERIFIED','TREE_VERIFIED','CATALOG_PUBLISHED')),
        local_proof_json TEXT, local_proof_digest TEXT,
        tree_proof_json TEXT, tree_proof_digest TEXT,
        catalog_proof_json TEXT, catalog_proof_digest TEXT, committed_revision INTEGER,
        FOREIGN KEY(operation_id,batch_id) REFERENCES publication_batches(operation_id,batch_id),
        CHECK((local_proof_json IS NULL)=(local_proof_digest IS NULL)),
        CHECK((tree_proof_json IS NULL)=(tree_proof_digest IS NULL)),
        CHECK((catalog_proof_json IS NULL)=(catalog_proof_digest IS NULL)),
        CHECK(phase='PREPARED' OR local_proof_json IS NOT NULL),
        CHECK(phase IN ('PREPARED','LOCAL_VERIFIED') OR tree_proof_json IS NOT NULL),
        CHECK(phase!='CATALOG_PUBLISHED' OR catalog_proof_json IS NOT NULL))""",
    """CREATE TABLE publication_batches (
        operation_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        intent_json TEXT NOT NULL, intent_digest TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('PREPARED','PROPAGATED')),
        map_proof_json TEXT, map_proof_digest TEXT, committed_revision INTEGER,
        PRIMARY KEY(operation_id,batch_id),
        FOREIGN KEY(operation_id) REFERENCES publication_operations(operation_id),
        CHECK((map_proof_json IS NULL)=(map_proof_digest IS NULL)),
        CHECK(phase!='PROPAGATED' OR map_proof_json IS NOT NULL))""",
    """CREATE TABLE publication_clone_obligations (
        annex_uuid TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
        old_layout INTEGER NOT NULL, new_layout INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PENDING','CLOSED')),
        receipt_json TEXT, committed_revision INTEGER,
        FOREIGN KEY(operation_id) REFERENCES publication_operations(operation_id),
        CHECK(new_layout>old_layout), CHECK(state!='CLOSED' OR receipt_json IS NOT NULL))""",
    "CREATE INDEX publication_participant_drive ON publication_participants(drive_label,operation_id)",
    "CREATE INDEX publication_open_operations ON publication_operations(state) WHERE state!='CLOSED'",
    *ACTION_DDL,
)


def canonical(value) -> str:
    """Stable binding encoding; reject non-finite numbers and non-JSON objects."""
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PublicationRefused("PUBLICATION_BINDING_INVALID") from exc


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def canonical_uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise PublicationRefused("PUBLICATION_UUID_INVALID") from exc
    return value


def _require_digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise PublicationRefused("PUBLICATION_DIGEST_INVALID")


def _unseal(raw, expected):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PublicationRefused("PUBLICATION_RECORD_UNPROVEN") from exc
    if not isinstance(value, dict) or canonical(value) != raw or digest(value) != expected:
        raise PublicationRefused("PUBLICATION_RECORD_UNPROVEN")
    return value


def _participants(scope):
    """Bind the already-owned dirty generation, not a new competing generation."""
    con = scope.connection
    result = []
    for label in sorted(scope.identities):
        row = con.execute(
            "SELECT d.annex_uuid,d.identity_epoch,d.write_generation,d.identity_fingerprint,"
            "g.owner_session_id,g.owner_fencing_token FROM drives d JOIN drive_dirty_generations g "
            "ON g.drive_label=d.drive_label AND g.identity_epoch=d.identity_epoch "
            "AND g.generation=d.write_generation WHERE d.drive_label=?", [label]).fetchone()
        if row is None or con.execute(
                "SELECT 1 FROM drive_clean_anchors WHERE drive_label=? AND identity_epoch=? AND generation=?",
                [label, row[1], row[2]]).fetchone():
            raise PublicationRefused("PUBLICATION_DIRTY_GENERATION_REQUIRED", drive=label)
        canonical_uuid(row[0])
        if scope.writer.kind == "session":
            if (row[4], row[5]) != (scope.writer.session_id, scope.writer.fencing_token):
                raise PublicationRefused("PUBLICATION_GENERATION_OWNER_MISMATCH", drive=label)
        elif row[4] is not None or row[5] is not None:
            # Terminal-owned maintenance adoption needs its own later reviewed
            # adapter; graph_write's no-live-session permission alone is NOT enough.
            raise PublicationRefused("PUBLICATION_MAINTENANCE_ADAPTER_REQUIRED", drive=label)
        result.append([label, *row])
    return result


def _revision(con):
    row = con.execute("SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()
    if row is None:
        raise PublicationRefused("PUBLICATION_REVISION_UNPROVEN")
    return int(row[0])


def _stamp_operation(scope, operation_id, previous, *, created=False):
    def finalize(revision):
        sql = ("UPDATE publication_operations SET created_revision=?,last_revision=? "
               "WHERE operation_id=? AND created_revision IS NULL AND last_revision IS NULL") if created else (
                   "UPDATE publication_operations SET last_revision=? WHERE operation_id=? AND last_revision=?")
        args = [revision, revision, operation_id] if created else [revision, operation_id, previous]
        if scope.connection.execute(sql, args).rowcount != 1:
            raise PublicationRefused("PUBLICATION_REVISION_CAS_FAILED", operation_id=operation_id)
    writes.defer_revision_once(scope.connection, scope.writer, key=("publication-operation", operation_id),
                               binding=(previous, created), finalize=finalize)


def prepare_operation(scope, *, operation_id, kind, profile_digest, batch_files, before_state):
    """Persistence half of PREPARED, called only after publisher profile/IO preflight.

    Requires the actual held lock scope and the existing owning adapter's TX.
    Does not qualify a profile or verify supplied before-state filesystem evidence.
    The public publisher must supply those proofs; no conversion interface is enabled.
    """
    scope.require_transaction(scope.connection)
    canonical_uuid(operation_id)
    _require_digest(profile_digest)
    if kind not in {"fill", "replica", "registration"}:
        raise PublicationRefused("PUBLICATION_CONVERSION_DISABLED")
    if not isinstance(batch_files, dict) or not batch_files:
        raise PublicationRefused("PUBLICATION_BATCHES_REQUIRED")
    children = []
    for batch_id, files in batch_files.items():
        canonical_uuid(batch_id)
        if not isinstance(files, (list, tuple)) or not files:
            raise PublicationRefused("PUBLICATION_FILES_REQUIRED")
        children.extend(canonical_uuid(file_id) for file_id in files)
    if len(children) != len(set(children)):
        raise PublicationRefused("PUBLICATION_CHILD_ID_REUSED")
    con = scope.connection
    require_clear(con, scope.identities, tree_change=True)
    participants = _participants(scope)
    binding = {"version": PROTOCOL, "operation_id": operation_id, "library_id": scope.library[0],
               "map_uuid": scope.library[1], "kind": kind, "profile_digest": profile_digest,
               "participants": participants, "before_state": before_state,
               "batch_files": {key: sorted(value) for key, value in sorted(batch_files.items())}}
    seal = digest(binding)
    try:
        con.execute("INSERT INTO publication_operations(operation_id,library_id,kind,binding_json,"
                    "binding_digest,state) VALUES(?,?,?,?,?,'PREPARED')",
                    [operation_id, scope.library[0], kind, canonical(binding), seal])
        for participant in participants:
            con.execute("INSERT INTO publication_participants VALUES(?,?,?,?,?,?,?,?)", [operation_id, *participant])
        for batch_id, files in binding["batch_files"].items():
            intent = {"version": PROTOCOL, "operation_digest": seal, "files": files}
            con.execute("INSERT INTO publication_batches(operation_id,batch_id,intent_json,intent_digest,phase) "
                        "VALUES(?,?,?,?,'PREPARED')", [operation_id, batch_id, canonical(intent), digest(intent)])
    except sqlite3.IntegrityError as exc:
        raise PublicationRefused("PUBLICATION_PREPARE_CONFLICT") from exc
    _stamp_operation(scope, operation_id, None, created=True)
    scope.operation_id = operation_id
    return seal


def load_owned_operation(scope, operation_id):
    """Revalidate authority, participants and expected revision for each continuation."""
    scope.require_transaction(scope.connection)
    return _load_bound_operation(scope, operation_id)


def _load_bound_operation(scope, operation_id):
    """Read-only binding check; owning write/IO adapter establishes authority first."""
    con = scope.connection
    row = con.execute("SELECT binding_json,binding_digest,state,last_revision FROM publication_operations "
                      "WHERE operation_id=?", [operation_id]).fetchone()
    if row is None or row[2] != "PREPARED":
        raise PublicationRefused("PUBLICATION_OPERATION_NOT_ACTIVE", operation_id=operation_id)
    binding = _unseal(row[0], row[1])
    if (binding.get("version") != PROTOCOL or not isinstance(binding.get("batch_files"), dict)
            or not binding["batch_files"] or binding.get("operation_id") != operation_id
            or binding.get("library_id") != scope.library[0]
            or binding.get("map_uuid") != scope.library[1] or binding.get("participants") != _participants(scope)):
        raise PublicationRefused("PUBLICATION_OPERATION_BINDING_CHANGED", operation_id=operation_id)
    stored = [list(r) for r in con.execute(
        "SELECT drive_label,annex_uuid,identity_epoch,generation,identity_fingerprint,owner_session_id,"
        "owner_fencing_token FROM publication_participants WHERE operation_id=? ORDER BY drive_label", [operation_id])]
    if stored != binding["participants"] or row[3] != _revision(con):
        raise PublicationRefused("PUBLICATION_OPERATION_STALE", operation_id=operation_id)
    return binding, row[1], row[3]


def prepare_file(scope, *, operation_id, batch_id, file_id, intent):
    """Persist immutable full before/intended-after rows before any payload mutation."""
    binding, operation_digest, revision = load_owned_operation(scope, operation_id)
    if file_id not in binding["batch_files"].get(batch_id, ()):
        raise PublicationRefused("PUBLICATION_FILE_UNSELECTED")
    frozen = {"version": PROTOCOL, "operation_digest": operation_digest,
              "batch_id": batch_id, "file_id": file_id, "intent": intent}
    try:
        scope.connection.execute("INSERT INTO publication_files(file_id,operation_id,batch_id,intent_json,"
                                 "intent_digest,phase) VALUES(?,?,?,?,?,'PREPARED')",
                                 [file_id, operation_id, batch_id, canonical(frozen), digest(frozen)])
    except sqlite3.IntegrityError as exc:
        raise PublicationRefused("PUBLICATION_FILE_PREPARE_CONFLICT") from exc
    _stamp_operation(scope, operation_id, revision)
    return digest(frozen)


_FILE_PHASES = {"LOCAL_VERIFIED": ("PREPARED", "local"),
                "TREE_VERIFIED": ("LOCAL_VERIFIED", "tree"),
                "CATALOG_PUBLISHED": ("TREE_VERIFIED", "catalog")}


def _file_chain(con, operation_id, file_id, operation_digest):
    row = con.execute(
        "SELECT batch_id,intent_json,intent_digest,phase,local_proof_json,local_proof_digest,"
        "tree_proof_json,tree_proof_digest,catalog_proof_json,catalog_proof_digest,committed_revision "
        "FROM publication_files WHERE operation_id=? AND file_id=?", [operation_id, file_id]).fetchone()
    if row is None:
        raise PublicationRefused("PUBLICATION_FILE_UNSELECTED")
    intent = _unseal(row[1], row[2])
    if (intent.get("operation_digest") != operation_digest or intent.get("file_id") != file_id
            or intent.get("batch_id") != row[0]):
        raise PublicationRefused("PUBLICATION_FILE_BINDING_CHANGED")
    phases = ["PREPARED", *_FILE_PHASES]
    if row[3] not in phases:
        raise PublicationRefused("PUBLICATION_PHASE_INVALID")
    if row[3] == "CATALOG_PUBLISHED":
        if type(row[10]) is not int or not 0 < row[10] <= _revision(con):
            raise PublicationRefused("PUBLICATION_FILE_REVISION_UNPROVEN")
    elif row[10] is not None:
        raise PublicationRefused("PUBLICATION_FILE_REVISION_UNPROVEN")
    previous = row[2]
    for index, phase in enumerate(phases[1:], 1):
        raw, seal = row[2 + index * 2:4 + index * 2]
        if index > phases.index(row[3]):
            if raw is not None or seal is not None:
                raise PublicationRefused("PUBLICATION_PHASE_PROOF_MISMATCH")
            continue
        receipt = _unseal(raw, seal)
        if (receipt.get("operation_digest") != operation_digest or receipt.get("file_digest") != row[2]
                or receipt.get("file_id") != file_id or receipt.get("batch_id") != row[0]
                or receipt.get("phase") != phase or receipt.get("previous_proof_digest") != previous):
            raise PublicationRefused("PUBLICATION_FILE_PROOF_CHAIN_CHANGED")
        previous = seal
    return row, intent, previous


def advance_file(scope, *, operation_id, file_id, phase, proof, catalog_cas=None):
    """Persist an independently verified publisher proof; never infer IO from JSON.

    This storage layer owns phase/binding/revision CAS only. ArchivePublisher's
    private proof factories own actual byte/tree checks. Catalog CAS is a DB-only
    callback in this same transaction; it must compare complete captured row pairs.
    """
    binding, operation_digest, revision = load_owned_operation(scope, operation_id)
    if phase not in _FILE_PHASES:
        raise PublicationRefused("PUBLICATION_PHASE_INVALID")
    expected, column = _FILE_PHASES[phase]
    con = scope.connection
    row, intent, previous_proof_digest = _file_chain(con, operation_id, file_id, operation_digest)
    if row[3] != expected or file_id not in binding["batch_files"].get(row[0], ()):
        raise PublicationRefused("PUBLICATION_PHASE_CAS_FAILED")
    if (phase == "CATALOG_PUBLISHED") != callable(catalog_cas):
        raise PublicationRefused("PUBLICATION_CATALOG_CAS_REQUIRED")
    if catalog_cas is not None:
        token = _CATALOG_WRITE.set((scope, operation_id, file_id, row[2]))
        try:
            catalog_cas(con, intent["intent"])
        finally:
            _CATALOG_WRITE.reset(token)
    receipt = {"version": PROTOCOL, "operation_digest": operation_digest, "file_digest": row[2],
               "phase": phase, "proof": proof, "file_id": file_id, "batch_id": row[0],
               "previous_proof_digest": previous_proof_digest}
    con.execute(f"UPDATE publication_files SET phase=?,{column}_proof_json=?,{column}_proof_digest=? "
                "WHERE operation_id=? AND file_id=?", [phase, canonical(receipt), digest(receipt), operation_id, file_id])
    if phase == "CATALOG_PUBLISHED":
        def stamp(new_revision):
            if con.execute("UPDATE publication_files SET committed_revision=? WHERE file_id=? "
                           "AND committed_revision IS NULL AND catalog_proof_digest=?",
                           [new_revision, file_id, digest(receipt)]).rowcount != 1:
                raise PublicationRefused("PUBLICATION_FILE_REVISION_CAS_FAILED")
        writes.defer_revision(con, scope.writer, stamp)
    _stamp_operation(scope, operation_id, revision)
    return digest(receipt)


def require_catalog_transition(scope, pair):
    """Only the bound file's CATALOG_PUBLISHED callback may perform its pair CAS."""
    scope.require_transaction(scope.connection)
    current = _CATALOG_WRITE.get()
    if current is None or current[0] is not scope:
        raise PublicationRefused("PUBLICATION_CATALOG_TRANSITION_REQUIRED")
    _, operation_id, file_id, expected_intent = current
    _, operation_digest, _ = load_owned_operation(scope, operation_id)
    row, intent, _ = _file_chain(scope.connection, operation_id, file_id, operation_digest)
    if (row[3] != "TREE_VERIFIED" or row[2] != expected_intent
            or canonical(intent["intent"].get("catalog_pair")) != canonical(pair)):
        raise PublicationRefused("PUBLICATION_CATALOG_INTENT_MISMATCH")


def propagate_batch(scope, *, operation_id, batch_id, map_proof):
    """Persist exact child digests and independently checked map/no-op evidence."""
    binding, operation_digest, revision = load_owned_operation(scope, operation_id)
    con = scope.connection
    batch = con.execute("SELECT intent_json,intent_digest,phase FROM publication_batches "
                        "WHERE operation_id=? AND batch_id=?", [operation_id, batch_id]).fetchone()
    if batch is None or batch[2] != "PREPARED":
        raise PublicationRefused("PUBLICATION_BATCH_CAS_FAILED")
    intent = _unseal(batch[0], batch[1])
    if (intent.get("operation_digest") != operation_digest
            or intent.get("files") != binding["batch_files"].get(batch_id)):
        raise PublicationRefused("PUBLICATION_BATCH_BINDING_CHANGED")
    files = con.execute("SELECT file_id,phase,catalog_proof_json,catalog_proof_digest,committed_revision "
                        "FROM publication_files WHERE operation_id=? AND batch_id=? ORDER BY file_id",
                        [operation_id, batch_id]).fetchall()
    if [r[0] for r in files] != intent["files"]:
        raise PublicationRefused("PUBLICATION_BATCH_CHILDREN_INCOMPLETE")
    children = {}
    for file_id, phase, raw, seal, committed in files:
        if phase != "CATALOG_PUBLISHED" or type(committed) is not int or not 0 < committed <= revision:
            raise PublicationRefused("PUBLICATION_BATCH_CHILDREN_INCOMPLETE")
        _file_chain(con, operation_id, file_id, operation_digest)
        receipt = _unseal(raw, seal)
        if receipt.get("operation_digest") != operation_digest or receipt.get("phase") != phase:
            raise PublicationRefused("PUBLICATION_FILE_BINDING_CHANGED")
        children[file_id] = seal
    receipt = {"version": PROTOCOL, "operation_digest": operation_digest, "batch_digest": batch[1],
               "children": children, "map_proof": map_proof}
    con.execute("UPDATE publication_batches SET phase='PROPAGATED',map_proof_json=?,map_proof_digest=? "
                "WHERE operation_id=? AND batch_id=?", [canonical(receipt), digest(receipt), operation_id, batch_id])

    def stamp(new_revision):
        if con.execute("UPDATE publication_batches SET committed_revision=? "
                       "WHERE operation_id=? AND batch_id=? AND committed_revision IS NULL AND map_proof_digest=?",
                       [new_revision, operation_id, batch_id, digest(receipt)]).rowcount != 1:
            raise PublicationRefused("PUBLICATION_BATCH_REVISION_CAS_FAILED")
    writes.defer_revision(con, scope.writer, stamp)
    _stamp_operation(scope, operation_id, revision)
    return digest(receipt)


def close_operation(scope, *, operation_id, observations, inventory_proofs, now):
    """Enclosing coordinator closure, never a per-file or ordinary recovery action.

    Coordinator must freshly reconcile inventory and observe every selected drive
    under this same scope BEFORE opening this transaction. Proofs/observations are
    not synthesized here. Required child and batch receipts plus actual anchors
    are committed together; missing/detached participants never become optional.
    """
    binding, operation_digest, revision = load_owned_operation(scope, operation_id)
    labels = set(scope.identities)
    if set(observations) != labels or set(inventory_proofs) != labels:
        raise PublicationRefused("PUBLICATION_CLOSURE_PARTICIPANTS_INCOMPLETE")
    con = scope.connection
    from modelark import publication_actions
    action_receipts = publication_actions.verified_receipts(scope)
    batches = con.execute("SELECT batch_id,phase,map_proof_json,map_proof_digest,committed_revision,intent_json,intent_digest "
                          "FROM publication_batches WHERE operation_id=? ORDER BY batch_id", [operation_id]).fetchall()
    if [r[0] for r in batches] != sorted(binding["batch_files"]):
        raise PublicationRefused("PUBLICATION_CLOSURE_BATCHES_INCOMPLETE")
    seals = {}
    for batch_id, phase, raw, seal, committed, intent_json, intent_digest in batches:
        if phase != "PROPAGATED" or type(committed) is not int or not 0 < committed <= revision:
            raise PublicationRefused("PUBLICATION_CLOSURE_BATCHES_INCOMPLETE")
        receipt = _unseal(raw, seal)
        intent = _unseal(intent_json, intent_digest)
        if (intent.get("operation_digest") != operation_digest
                or intent.get("files") != binding["batch_files"][batch_id]
                or receipt.get("batch_digest") != intent_digest):
            raise PublicationRefused("PUBLICATION_BATCH_BINDING_CHANGED")
        current_children = dict(con.execute(
            "SELECT file_id,catalog_proof_digest FROM publication_files WHERE operation_id=? AND batch_id=? "
            "AND phase='CATALOG_PUBLISHED' AND committed_revision IS NOT NULL", [operation_id, batch_id]))
        if (receipt.get("operation_digest") != operation_digest
                or receipt.get("children") != current_children
                or sorted(current_children) != binding["batch_files"][batch_id]):
            raise PublicationRefused("PUBLICATION_BATCH_CHILDREN_CHANGED")
        for file_id in current_children:
            _file_chain(con, operation_id, file_id, operation_digest)
        seals[batch_id] = seal
    closure = {"version": PROTOCOL, "operation_digest": operation_digest,
               "batches": seals, "inventory_proofs": inventory_proofs, "actions": action_receipts}
    # Only this coordinator may temporarily satisfy the shared anchor guard. It
    # revalidates the complete record first and publishes every selected anchor
    # in the SAME owning transaction. Any exception keeps the durable obligation.
    from modelark.drive_mutation import _publish_anchor_locked
    token = _CLOSING.set((con, operation_id))
    try:
        for label, _annex_uuid, epoch, generation, *_ in binding["participants"]:
            _publish_anchor_locked(con, label, epoch, generation, observations[label], now)
        con.execute("UPDATE publication_operations SET state='CLOSED',closure_json=? WHERE operation_id=?",
                    [canonical(closure), operation_id])
    finally:
        _CLOSING.reset(token)

    def stamp(new_revision):
        if con.execute("UPDATE publication_operations SET closed_revision=?,last_revision=? "
                       "WHERE operation_id=? AND last_revision=? AND state='CLOSED' AND closed_revision IS NULL",
                       [new_revision, new_revision, operation_id, revision]).rowcount != 1:
            raise PublicationRefused("PUBLICATION_CLOSURE_CAS_FAILED")
    writes.defer_revision(con, scope.writer, stamp)
    return digest(closure)


def _install_schema(con, *, library_id: str, map_uuid: str) -> None:
    """Explicit clone-migration callback; no transaction ownership or live command.

    Caller must already have made/rehearsed a backup and established the actual map
    identity. This callback joins graph_write so schema/floor/revision are atomic.
    Do not invoke on a live catalog; deployment orchestration is a later stage.
    """
    writes.require_write(con, writes.WriteIdentity("graph"))
    canonical_uuid(library_id)
    canonical_uuid(map_uuid)
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version not in (7, 8):
        raise PublicationRefused("PUBLICATION_MIGRATION_SOURCE_INVALID", version=version)
    existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if TABLES & existing:
        raise PublicationRefused("PUBLICATION_SCHEMA_AMBIGUOUS")
    for statement in DDL:
        con.execute(statement)
    con.execute("INSERT INTO publication_library VALUES(1,?,?,?)", [library_id, map_uuid, PROTOCOL])
    con.execute(f"PRAGMA user_version={VERSION}")


def library(con) -> tuple[str, str] | None:
    """Read-only validation. Older catalogs have no publication feature/obligations.

    A partial or unexpected publication schema cannot be treated as an empty set.
    Guard checks apply even when using raw sqlite (as Slice does), not just db.connect.
    """
    try:
        version = int(con.execute("PRAGMA user_version").fetchone()[0])
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version < VERSION:
            if TABLES & tables:
                raise PublicationRefused("PUBLICATION_SCHEMA_FLOOR_INVALID")
            return None
        if version != VERSION or not TABLES <= tables:
            raise PublicationRefused("PUBLICATION_SCHEMA_UNSUPPORTED", version=version)
        definitions = dict(con.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))
        for statement in DDL:
            name = statement.split()[2]
            if " ".join(definitions.get(name, "").split()) != " ".join(statement.split()):
                raise PublicationRefused("PUBLICATION_SCHEMA_UNQUALIFIED", object=name)
        rows = con.execute("SELECT singleton_id,library_id,map_uuid,protocol_version FROM publication_library").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or rows[0][3] != PROTOCOL:
            raise PublicationRefused("PUBLICATION_LIBRARY_UNPROVEN")
        return canonical_uuid(rows[0][1]), canonical_uuid(rows[0][2])
    except sqlite3.Error as exc:
        raise PublicationRefused("PUBLICATION_SCHEMA_UNPROVEN") from exc


def require_clear(con, drive_labels: Iterable[str] | None = None, *, tree_change: bool = False) -> None:
    """Shared admission/ordinary-clean guard; never clears or recovers an obligation.

    None checks the whole library (approval/control-plane admission). A concrete
    selected-drive set excludes unrelated offline clone obligations from that set.
    No caller boolean or session token bypasses this guard. Explicit publication
    replay/closure requires the separate owning coordinator, not ordinary recovery.
    """
    identity = library(con)
    if identity is None:
        return
    labels = None if drive_labels is None else frozenset(drive_labels)
    try:
        operations = con.execute(
            "SELECT o.operation_id,o.state,o.library_id,o.kind,p.drive_label "
            "FROM publication_operations o LEFT JOIN publication_participants p "
            "ON p.operation_id=o.operation_id WHERE o.state!='CLOSED'").fetchall()
        blocked = set()
        for operation_id, state, library_id, kind, label in operations:
            if library_id != identity[0] or state not in {"PREPARED", "CLOSED"}:
                raise PublicationRefused("PUBLICATION_RECORD_UNPROVEN", operation_id=operation_id)
            if label is None:
                if kind != "registration" or state != "PREPARED":
                    raise PublicationRefused("PUBLICATION_RECORD_UNPROVEN", operation_id=operation_id)
                if labels is None or tree_change:
                    blocked.add(operation_id)
                continue
            closing = _CLOSING.get()
            explicitly_closing = closing is not None and closing[0] is con and closing[1] == operation_id
            if state != "CLOSED" and not explicitly_closing and (labels is None or label in labels):
                blocked.add(operation_id)
        if tree_change:
            clones = con.execute(
                "SELECT c.operation_id,c.state,d.drive_label FROM publication_clone_obligations c "
                "LEFT JOIN drives d ON d.annex_uuid=c.annex_uuid").fetchall()
            for operation_id, state, label in clones:
                if state not in {"PENDING", "CLOSED"}:
                    raise PublicationRefused("PUBLICATION_RECORD_UNPROVEN", operation_id=operation_id)
                if state != "CLOSED" and (labels is None or label in labels):
                    blocked.add(operation_id)
        if blocked:
            raise PublicationRefused("MAINTENANCE_REQUIRED", operation_ids=sorted(blocked),
                                     drive_labels=None if labels is None else sorted(labels))
    except sqlite3.Error as exc:
        raise PublicationRefused("PUBLICATION_RECORD_UNPROVEN") from exc
