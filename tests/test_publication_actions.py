"""Real scope/transaction journal tests; all profile/IO receipts are synthetic."""
import hashlib
import json

import pytest

from modelark import publication_actions as actions, publication_locks, publication_store as store
from modelark.catalog_write_context import WriteContextError
from modelark.proposal import GraphResult, graph_write
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import BATCH, FILE, MAP, OP
from test_publication_lifecycle import con as publication_connection  # noqa: F401


ACTION = "77777777-7777-4777-8777-777777777777"
NEXT = "88888888-8888-4888-8888-888888888888"
PROFILE = {"root_identity": [1, 2, 3, 4], "mount_id": 5,
           "annex_uuid": "66666666-6666-4666-8666-666666666660", "fixture": "not IO qualification"}


@pytest.fixture(name="con")
def action_connection(request):
    con = request.getfixturevalue("publication_connection")
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='publication_actions'").fetchone():
        # Transitional fixture only until the parent adds ACTION_DDL to store.DDL.
        for statement in actions.ACTION_DDL:
            con.execute(statement)
    return con


def _start(scope):
    return scope.write(lambda _: store.prepare_operation(
        scope, operation_id=OP, kind="fill", profile_digest=store.digest({"d0": PROFILE}),
        batch_files={BATCH: [FILE]}, before_state={"profiles": {"d0": PROFILE}},
    ))


def _intent(**updates):
    return {
        "expected_phase": {"entity": "operation", "id": OP, "phase": "PREPARED"},
        "profile_digest": store.digest(PROFILE),
        "root": {name: PROFILE[name] for name in ("root_identity", "mount_id", "annex_uuid")},
        "command": {"argv": ["annex", "add", "--force-large", "--", "payload"],
                    "stdin_digest": hashlib.sha256(b"").hexdigest()},
        **updates,
    }


def _prepare(scope, *, action_id=ACTION, intent=None, kind="annex_add"):
    return scope.write(lambda _: actions.prepare(
        scope, action_id=action_id, kind=kind, intent=_intent() if intent is None else intent,
    ))


def _verify(scope, *, action_id=ACTION, receipt=None):
    record = actions.read(scope, action_id)
    return scope.write(lambda _: actions.verify(
        scope, action_id=action_id, intent_digest=record["intent_digest"],
        receipt={"fixture": "independent receipt"} if receipt is None else receipt,
    ))


def _revision(con):
    return con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]


def test_prepare_and_verify_finalize_in_exact_owning_revision(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        before = _revision(con)
        result = _prepare(scope)
        record = actions.read(scope, ACTION)
        assert not result.proven_noop
        assert record["prepared_revision"] == before + 1 == _revision(con)
        assert record["verified_revision"] is None and record["receipt"] is None
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone()[0] == _revision(con)
        _verify(scope)
        record = actions.read(scope, ACTION)
        assert record["status"] == "VERIFIED"
        assert record["verified_revision"] == before + 2 == _revision(con)
        assert record["receipt"]["intent_digest"] == record["intent_digest"]
        assert con.execute("SELECT state FROM publication_operations").fetchone()[0] == "PREPARED"


def test_exact_prepare_and_verified_repeat_are_real_noops_without_finalizers(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        revision = _revision(con)
        # Use the actual graph adapter; no synthetic context or raw BEGIN authority.
        duplicate = graph_write(con, lambda _: actions.prepare(
            scope, action_id=ACTION, kind="annex_add", intent=_intent(),
        ))
        assert duplicate.proven_noop and _revision(con) == revision
        _verify(scope)
        revision = _revision(con)
        record = actions.read(scope, ACTION)
        duplicate = graph_write(con, lambda _: actions.verify(
            scope, action_id=ACTION, intent_digest=record["intent_digest"],
            receipt={"fixture": "independent receipt"},
        ))
        assert duplicate.proven_noop and _revision(con) == revision


def test_scope_write_exact_retry_preserves_revision(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        revision = _revision(con)
        assert _prepare(scope).proven_noop
        assert _revision(con) == revision


@pytest.mark.parametrize("changes", [
    {"profile_digest": "f" * 64},
    {"root": {"root_identity": [9], "mount_id": 5, "annex_uuid": PROFILE["annex_uuid"]}},
    {"expected_phase": {"entity": "operation", "id": OP, "phase": "CLOSED"}},
    {"command": {"argv": ["bad\0argument"], "stdin_digest": "a" * 64}},
    {"filesystem_plan": {"action": "second conflicting plan"}},
])
def test_invalid_or_unselected_intent_never_creates_action(con, changes):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        revision = _revision(con)
        with pytest.raises(PublicationRefused):
            _prepare(scope, intent=_intent(**changes))
        assert con.execute("SELECT count(*) FROM publication_actions").fetchone()[0] == 0
        assert _revision(con) == revision


def test_changed_intent_or_receipt_refuses_reusing_action_id(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        with pytest.raises(PublicationRefused, match="PREPARE_CONFLICT"):
            _prepare(scope, intent=_intent(note="different sealed intention"))
        _verify(scope)
        with pytest.raises(PublicationRefused, match="VERIFY_CONFLICT"):
            _verify(scope, receipt={"fixture": "different receipt"})


def test_dependency_requires_committed_verified_exact_receipt(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        dependent = _intent(after=[{"action_id": ACTION, "receipt_digest": "a" * 64}])
        with pytest.raises(PublicationRefused, match="DEPENDENCY_UNVERIFIED"):
            _prepare(scope, action_id=NEXT, intent=dependent)
        _verify(scope)
        dependent["after"][0]["receipt_digest"] = actions.read(scope, ACTION)["receipt_digest"]
        _prepare(scope, action_id=NEXT, intent=dependent)
        assert actions.read(scope, NEXT)["status"] == "PREPARED"


def test_file_phase_change_refuses_outstanding_action(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        scope.write(lambda _: store.prepare_file(
            scope, operation_id=OP, batch_id=BATCH, file_id=FILE, intent={"fixture": "pair"},
        ))
        _prepare(scope, intent=_intent(expected_phase={"entity": "file", "id": FILE, "phase": "PREPARED"}))
        scope.write(lambda _: store.advance_file(
            scope, operation_id=OP, file_id=FILE, phase="LOCAL_VERIFIED", proof={"fixture": "proof"},
        ))
        with pytest.raises(PublicationRefused, match="PHASE_CHANGED"):
            actions.read(scope, ACTION)


def test_raw_transaction_and_no_operation_cannot_prepare(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="OPERATION_REQUIRED"):
            _prepare(scope)
        _start(scope)
        con.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(WriteContextError, match="AUTHORITY_MISSING"):
                actions.prepare(scope, action_id=ACTION, kind="annex_add", intent=_intent())
        finally:
            con.rollback()


def test_read_refuses_transaction_expired_scope_and_external_revision(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        with pytest.raises(PublicationRefused, match="IO_TRANSACTION_ACTIVE"):
            scope.write(lambda _: actions.read(scope, ACTION))
        graph_write(con, lambda _: GraphResult(proven_noop=False))
        with pytest.raises(PublicationRefused, match="OPERATION_STALE"):
            actions.read(scope, ACTION)
    with pytest.raises(PublicationRefused, match="FENCE_AUTHORITY_MISSING"):
        actions.read(scope, ACTION)


def test_action_seal_tampering_and_terminal_operation_refuse(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        original = con.execute("SELECT intent_json FROM publication_actions").fetchone()[0]
        changed = json.loads(original)
        changed["intent"]["command"]["argv"].append("other")
        con.execute("UPDATE publication_actions SET intent_json=?", [store.canonical(changed)])
        with pytest.raises(PublicationRefused, match="RECORD_UNPROVEN"):
            actions.read(scope, ACTION)
        con.execute("UPDATE publication_actions SET intent_json=?", [original])
        con.execute("UPDATE publication_operations SET state='CLOSED',closure_json='{}'")
        with pytest.raises(PublicationRefused, match="OPERATION_NOT_ACTIVE"):
            actions.read(scope, ACTION)


def test_failed_revision_finalizer_rolls_back_receipt_status_and_operation_marker(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        revision = _revision(con)
        con.execute("CREATE TRIGGER fail_action_revision BEFORE UPDATE OF verified_revision "
                    "ON publication_actions BEGIN SELECT RAISE(ABORT, 'injected marker failure'); END")
        with pytest.raises(Exception, match="injected marker failure"):
            _verify(scope)
        assert _revision(con) == revision
        record = actions.read(scope, ACTION)
        assert record["status"] == "PREPARED" and record["receipt"] is None
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone()[0] == revision


def test_filesystem_plan_is_recorded_but_does_not_touch_files(con, tmp_path):
    path = tmp_path / "must-not-exist"
    intent = _intent()
    intent.pop("command")
    intent["filesystem_plan"] = {"action": "install", "path": str(path), "sha256": "a" * 64}
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope, kind="payload_install", intent=intent)
        _verify(scope)
    assert not path.exists()


def test_closure_receipts_refuse_pending_actions_and_return_exact_verified_set(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        _start(scope)
        _prepare(scope)
        with pytest.raises(PublicationRefused, match="ACTIONS_INCOMPLETE"):
            scope.write(lambda _: GraphResult(proven_noop=True, value=actions.verified_receipts(scope)))
        _verify(scope)
        record = actions.read(scope, ACTION)
        revision = _revision(con)
        result = scope.write(lambda _: GraphResult(proven_noop=True, value=actions.verified_receipts(scope)))
        assert result.value == [{key: record[key] for key in (
            "action_id", "kind", "intent_digest", "receipt_digest", "verified_revision",
        )}]
        assert _revision(con) == revision
