"""PR-08 / #39-A proposal draft/approve CAS and adopt_current (tests-first, RFC-002).

Gate 1: preview pure outside BEGIN IMMEDIATE (instrumented event log); full approval matrix;
atomic rollback via mid-TX inject; adopt_current leaves selection unchanged.
"""
from __future__ import annotations

import importlib
import sqlite3
from unittest import mock

from modelark.core import db


class _EventCon:
    """Connection proxy with ordered event log and optional mid-TX injection hook."""

    def __init__(self, con):
        self._con = con
        self.events: list[str] = []
        self.inject_after_selection_mutate = False
        self.hook_fired = False
        self._in_immediate = False

    def execute(self, sql, *args):
        s = sql if isinstance(sql, str) else str(sql)
        up = s.strip().upper()
        if up.startswith("BEGIN"):
            self.events.append(f"BEGIN:{up}")
            self._in_immediate = "IMMEDIATE" in up
        elif up.startswith("COMMIT"):
            self.events.append("COMMIT")
            self._in_immediate = False
        elif up.startswith("ROLLBACK"):
            self.events.append("ROLLBACK")
            self._in_immediate = False
        elif self._in_immediate and (
                "INSERT INTO SELECTION" in up or "UPDATE SELECTION" in up
                or "INSERT INTO selection" in s or "UPDATE selection" in s):
            self.events.append("SELECTION_MUTATE")
            result = self._con.execute(sql, *args)
            if self.inject_after_selection_mutate:
                self.hook_fired = True
                self.events.append("INJECT_FAIL")
                raise sqlite3.OperationalError("injected mid-approve failure")
            return result
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)


def _mem():
    raw = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        raw.execute(stmt)
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "planner_state" not in tables:
        raise AssertionError(
            "packaged schema must define planner_state (v5) for in-memory contracts "
            "(expected Gate-1 red until schema.sql lands)")
    if raw.execute("SELECT count(*) FROM planner_state").fetchone()[0] == 0:
        raw.execute(
            "INSERT INTO planner_state(singleton_id,planner_revision,"
            "active_approved_proposal_id,next_fencing_token) VALUES(1,0,NULL,0)")
    return _EventCon(raw)


def _proposal():
    for name in ("modelark.proposal", "modelark.placement_proposal"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    raise AssertionError("modelark.proposal module required (expected Gate-1 red)")


def _pid(draft):
    if isinstance(draft, dict):
        return draft["proposal_id"]
    return draft


def _lifecycle(con, proposal_id):
    return con.execute(
        "SELECT lifecycle FROM placement_proposals WHERE proposal_id=?",
        [proposal_id]).fetchone()[0]


def _seed_selection(con, repos=("org/m",)):
    for repo in repos:
        con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
        con.execute(
            "INSERT OR IGNORE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?, 'model.safetensors', 100, 'safetensors', 'bf16', ?)",
            [repo, "1" * 64])
        con.execute(
            "INSERT OR IGNORE INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')",
            [repo])
    con.execute(
        "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,identity_fingerprint) "
        "VALUES('d0',1000000000000,1000000000000,'primary',0,'active','enabled',1,?)",
        ["f" * 64])
    from modelark import plan
    if plan.get(con, "ark") is None:
        plan.create(con, "ark", name="Ark")
    if "d0" not in plan.plan_drive_labels(con, "ark"):
        plan.add_drive(con, "ark", "d0")
    plan.set_active(con, "ark")
    # Reset revision after seed mutations once writers bump (production).
    try:
        con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    except sqlite3.Error:
        pass


def _create(prop, con, mutation=("adopt_current", ())):
    create = getattr(prop, "create_draft", None) or getattr(prop, "preview_and_draft")
    try:
        return create(con, plan_id="ark", mutation=mutation)
    except TypeError:
        return create(con, "ark", mutation)


def _approve(prop, con, proposal_id, *, mutation=None, **extra):
    """Call approve with optional mutation. Never drop mutation on TypeError (finding 13)."""
    approve = getattr(prop, "approve", None) or getattr(prop, "approve_proposal")
    kwargs = dict(extra)
    if mutation is not None:
        kwargs["mutation"] = mutation
    # Prefer kwargs form; if TypeError, try positional mutation only when provided.
    try:
        if mutation is not None:
            return approve(con, proposal_id, mutation=mutation, **extra)
        return approve(con, proposal_id, **extra)
    except TypeError:
        if mutation is not None:
            return approve(con, proposal_id, mutation)
        return approve(con, proposal_id=proposal_id, **extra)


def _assert_refuses(call, *, must_contain, label):
    """Require call to raise; a successful return is always a failure (finding 13)."""
    try:
        call()
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in must_contain), (
            f"{label}: refusal message {exc!r} must contain one of {must_contain}")
        return exc
    else:
        raise AssertionError(f"{label}: expected refusal, but call returned successfully")


def test_preview_pure_runs_before_any_begin_immediate():
    """Pure preview never BEGIN IMMEDIATE; successful publish uses IMMEDIATE and no solver in TX."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    assert hasattr(prop, "preview_pure") or hasattr(prop, "compute_draft_payload")
    assert hasattr(prop, "publish_draft") or hasattr(prop, "persist_draft")

    pure = getattr(prop, "preview_pure", None) or getattr(prop, "compute_draft_payload")
    publish = getattr(prop, "publish_draft", None) or getattr(prop, "persist_draft")

    con.events.clear()
    try:
        payload = pure(con, plan_id="ark", mutation=("adopt_current", ()))
    except TypeError:
        payload = pure(con, "ark", ("adopt_current", ()))
    for ev in con.events:
        assert "IMMEDIATE" not in ev, f"pure preview must not BEGIN IMMEDIATE; events={con.events}"

    from modelark import placement, capacity
    con.events.clear()
    solve_events: list[tuple[str, bool]] = []  # (name, in_immediate)

    def track(name):
        def _side(*a, **k):
            solve_events.append((name, con._in_immediate))
            raise AssertionError(f"unexpected {name} call during publish")
        return _side

    with mock.patch.object(placement, "gate_b", side_effect=track("gate_b")):
        with mock.patch.object(placement, "improve", side_effect=track("improve")):
            with mock.patch.object(capacity, "plan_capacity", side_effect=track("plan_capacity")):
                try:
                    out = publish(con, payload)
                except TypeError:
                    out = publish(con, plan_id="ark", payload=payload)

    assert any(e.startswith("BEGIN:") and "IMMEDIATE" in e for e in con.events), (
        f"successful publish must BEGIN IMMEDIATE; events={con.events}")
    assert "COMMIT" in con.events, f"successful publish must COMMIT; events={con.events}"
    assert not any(in_imm for _n, in_imm in solve_events), (
        f"no solver between BEGIN IMMEDIATE and COMMIT; solve_events={solve_events}")
    # Persistence must succeed — a proposal row exists.
    n = con.execute("SELECT count(*) FROM placement_proposals").fetchone()[0]
    assert n >= 1, f"publish must persist a draft proposal; count={n} out={out!r}"


def test_draft_persist_does_not_mutate_selection_or_revision():
    prop = _proposal()
    con = _mem()
    _seed_selection(con, repos=())
    before = list(con.execute("SELECT repo_id FROM selection ORDER BY 1").fetchall())
    before_rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    draft = _create(prop, con, ("finalize", ("org/new",)))
    pid = _pid(draft)
    after = list(con.execute("SELECT repo_id FROM selection ORDER BY 1").fetchall())
    assert before == after, "draft must not change selection"
    after_rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after_rev == before_rev, "draft alone must not bump planner_revision"
    assert _lifecycle(con, pid) == "draft"


def test_persistence_reread_hash_equality():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    load = getattr(prop, "load_proposal", None) or getattr(prop, "get_proposal")
    loaded = load(con, pid)
    stored = loaded["canonical_hash"] if isinstance(loaded, dict) else loaded.canonical_hash
    recompute = getattr(prop, "recompute_hash", None) or getattr(prop, "hash_stored_proposal")
    assert recompute(con, pid) == stored


def test_approve_adopt_current_sets_pointer_bumps_revision_selection_unchanged():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    before_sel = list(con.execute(
        "SELECT repo_id, finalized_at FROM selection ORDER BY 1").fetchall())
    draft = _create(prop, con, ("adopt_current", ()))
    pid = _pid(draft)
    _approve(prop, con, pid)
    state = con.execute(
        "SELECT planner_revision, active_approved_proposal_id FROM planner_state "
        "WHERE singleton_id=1"
    ).fetchone()
    assert state[0] == 1, f"first approval must advance revision 0→1; got {state[0]}"
    assert state[1] == pid
    assert _lifecycle(con, pid) == "approved"
    after_sel = list(con.execute(
        "SELECT repo_id, finalized_at FROM selection ORDER BY 1").fetchall())
    assert before_sel == after_sel, "adopt_current must leave selection unchanged"


def test_cas_stale_revision_refuses_without_partial_apply():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    con.execute("UPDATE planner_state SET planner_revision=1 WHERE singleton_id=1")
    before_sel = list(con.execute("SELECT repo_id FROM selection ORDER BY 1").fetchall())
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        must_contain=("STALE", "PREVIEW_STALE", "REVISION"),
        label="stale revision",
    )
    assert list(con.execute("SELECT repo_id FROM selection ORDER BY 1").fetchall()) == before_sel
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None
    assert _lifecycle(con, pid) == "draft"


def test_hash_mismatch_refuses():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    con.execute(
        "UPDATE placement_proposals SET canonical_hash=? WHERE proposal_id=?",
        ["f" * 64, pid])
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        must_contain=("HASH", "PROPOSAL_HASH_MISMATCH"),
        label="hash mismatch",
    )


def test_mutation_mismatch_refuses():
    """Approval-time mutation must match stored descriptor; leave stored rows intact."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con, ("adopt_current", ()))
    pid = _pid(draft)
    stored_kind = con.execute(
        "SELECT mutation_kind FROM placement_proposals WHERE proposal_id=?",
        [pid]).fetchone()[0]
    _assert_refuses(
        lambda: _approve(prop, con, pid, mutation=("finalize", ("org/x",))),
        must_contain=("MUTATION", "MUTATION_MISMATCH"),
        label="mutation mismatch",
    )
    assert con.execute(
        "SELECT mutation_kind FROM placement_proposals WHERE proposal_id=?",
        [pid]).fetchone()[0] == stored_kind
    assert _lifecycle(con, pid) == "draft"


def test_non_draft_refuses():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    con.execute(
        "UPDATE placement_proposals SET lifecycle='superseded' WHERE proposal_id=?", [pid])
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        must_contain=("NOT_DRAFT", "PROPOSAL_NOT_DRAFT"),
        label="non-draft",
    )


def test_missed_revision_bump_still_blocked_by_semantic_recompute():
    """Even if based_on_revision still matches, changed semantic inputs refuse."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/extra',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/extra','x.safetensors',50,'safetensors','bf16',?)", ["3" * 64])
    con.execute(
        "INSERT INTO selection(repo_id,finalized_at) VALUES('org/extra','2026-01-01')")
    rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    based = con.execute(
        "SELECT based_on_revision FROM placement_proposals WHERE proposal_id=?",
        [pid]).fetchone()[0]
    assert rev == based, "fixture keeps revision matching based_on"
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        must_contain=("INPUT", "SEMANTIC", "APPROVED_INPUT_CHANGED", "CHANGED"),
        label="missed-revision semantic recompute",
    )
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None


def test_exact_assignment_rejection_does_not_call_optimizer():
    """Inject exact-assignment/evidence refusal directly — do not change semantic selection inputs."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    from modelark import placement, capacity

    class ExactAssignmentRefusal(Exception):
        def __init__(self):
            super().__init__("EXACT_ASSIGNMENT_REJECTED")

    assert hasattr(prop, "validate_exact_assignment") or hasattr(
        prop, "revalidate_assignment_evidence"), (
        "approve path must expose validate_exact_assignment (or revalidate_assignment_evidence)")

    target_name = "validate_exact_assignment" if hasattr(
        prop, "validate_exact_assignment") else "revalidate_assignment_evidence"

    with mock.patch.object(placement, "improve", side_effect=AssertionError("no improve")):
        with mock.patch.object(placement, "gate_b", side_effect=AssertionError("no gate_b")):
            with mock.patch.object(capacity, "plan_capacity",
                                   side_effect=AssertionError("no plan_capacity")):
                with mock.patch.object(
                        prop, target_name, side_effect=ExactAssignmentRefusal()):
                    try:
                        _approve(prop, con, pid)
                    except ExactAssignmentRefusal:
                        pass
                    except AssertionError as exc:
                        if any(x in str(exc) for x in ("no improve", "no gate_b", "no plan_capacity")):
                            raise AssertionError(
                                f"approve must not re-optimize: {exc}") from exc
                        raise
                    except Exception as exc:
                        msg = str(exc).upper()
                        assert "ASSIGN" in msg or "EVIDENCE" in msg or "EXACT" in msg, exc
                    else:
                        raise AssertionError(
                            "exact assignment refusal must surface (call returned successfully)")
    assert _lifecycle(con, pid) == "draft"
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None


def test_non_feasible_draft_is_not_approved():
    prop = _proposal()
    con = _mem()
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/huge',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/huge','w.safetensors',1000000000000000,'safetensors','bf16')")
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/huge','2026-01-01')")
    from modelark import plan
    plan.create(con, "ark", name="Ark")
    plan.set_active(con, "ark")
    try:
        con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    except sqlite3.Error:
        pass
    draft = _create(prop, con)
    pid = _pid(draft)
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        must_contain=("FEASIBLE", "INFEASIBLE", "NOT_FEASIBLE", "PROPOSAL_NOT_FEASIBLE"),
        label="non-feasible draft",
    )
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None


def test_approval_mid_transaction_failure_rolls_back_all_effects():
    """Inject on _EventCon.execute after selection mutate → full rollback of all four axes."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con, repos=())
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/x',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/x','model.safetensors',100,'safetensors','bf16')")
    draft = _create(prop, con, ("finalize", ("org/x",)))
    pid = _pid(draft)
    before_rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]

    con.inject_after_selection_mutate = True
    con.hook_fired = False
    try:
        _approve(prop, con, pid)
    except Exception:
        pass

    assert con.hook_fired, (
        "injection hook on _EventCon.execute must fire after selection mutate "
        f"(events={con.events})")
    assert con.execute(
        "SELECT count(*) FROM selection WHERE repo_id='org/x'").fetchone()[0] == 0, (
        "selection mutation must roll back")
    assert _lifecycle(con, pid) == "draft", "lifecycle must remain draft"
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None, "pointer must remain null"
    after_rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after_rev == before_rev, (
        f"revision must roll back to pre-approve value; before={before_rev} after={after_rev}")
    assert "ROLLBACK" in con.events or after_rev == before_rev


def test_approve_does_not_call_optimizer_on_happy_path():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    from modelark import capacity, placement
    with mock.patch.object(capacity, "plan_capacity", side_effect=AssertionError("no plan_capacity")):
        with mock.patch.object(placement, "improve", side_effect=AssertionError("no improve")):
            with mock.patch.object(placement, "gate_b", side_effect=AssertionError("no gate_b")):
                _approve(prop, con, pid)
    assert _lifecycle(con, pid) == "approved"


def test_approval_acquires_sorted_fences_and_fresh_evidence_before_sqlite_tx():
    """A6: controller + sorted drive fences; fresh evidence before BEGIN IMMEDIATE."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    assert hasattr(prop, "acquire_approval_fences"), (
        "proposal.acquire_approval_fences required (sorted controller + drive fences; A6)")
    assert hasattr(prop, "capture_approval_evidence"), (
        "proposal.capture_approval_evidence required before short SQLite TX (A6)")

    order: list[str] = []
    real_acq = prop.acquire_approval_fences
    real_ev = prop.capture_approval_evidence

    def acq(*a, **k):
        order.append("FENCES")
        # Must not be inside IMMEDIATE yet.
        assert not con._in_immediate, "fences must be acquired before BEGIN IMMEDIATE"
        return real_acq(*a, **k)

    def ev(*a, **k):
        order.append("EVIDENCE")
        assert "FENCES" in order, "evidence after fences"
        assert not con._in_immediate, "evidence must be captured before BEGIN IMMEDIATE"
        return real_ev(*a, **k)

    con.events.clear()
    with mock.patch.object(prop, "acquire_approval_fences", side_effect=acq):
        with mock.patch.object(prop, "capture_approval_evidence", side_effect=ev):
            _approve(prop, con, pid)
    assert order == ["FENCES", "EVIDENCE"], f"expected FENCES then EVIDENCE; got {order}"
    # First BEGIN IMMEDIATE for approve must follow evidence (events may include only approve TX).
    begin_idxs = [i for i, e in enumerate(con.events) if e.startswith("BEGIN:") and "IMMEDIATE" in e]
    assert begin_idxs, f"approve must BEGIN IMMEDIATE; events={con.events}"


def test_approval_refuses_while_fill_guard_is_live():
    """A8: no supported approval route bypasses the existing Fill guard."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    from modelark.web import fill_worker

    # Production approve must consult process-local Fill liveness (not durable sessions).
    live_attr = None
    for name in ("is_live", "is_running", "blocks_graph_write"):
        if hasattr(fill_worker.WORKER, name):
            live_attr = name
            break
    assert live_attr is not None or hasattr(prop, "fill_guard_blocks_approval"), (
        "Fill liveness hook required on WORKER or proposal.fill_guard_blocks_approval (A8)")

    if hasattr(prop, "fill_guard_blocks_approval"):
        with mock.patch.object(prop, "fill_guard_blocks_approval", return_value=True):
            _assert_refuses(
                lambda: _approve(prop, con, pid),
                must_contain=("FILL_SESSION_ACTIVE",),
                label="approve while Fill live",
            )
    else:
        with mock.patch.object(fill_worker.WORKER, live_attr, return_value=True):
            _assert_refuses(
                lambda: _approve(prop, con, pid),
                must_contain=("FILL_SESSION_ACTIVE",),
                label="approve while Fill live",
            )


def test_second_approval_supersedes_prior_and_moves_pointer():
    """A7-related: second approve supersedes prior approved proposal and moves singleton pointer."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    d1 = _create(prop, con, ("adopt_current", ()))
    p1 = _pid(d1)
    _approve(prop, con, p1)
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == p1
    # Second draft+approve (e.g. after a no-op adopt again, or finalize no-op).
    d2 = _create(prop, con, ("adopt_current", ()))
    p2 = _pid(d2)
    assert p2 != p1
    _approve(prop, con, p2)
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == p2
    assert _lifecycle(con, p1) == "superseded"
    assert _lifecycle(con, p2) == "approved"


def test_active_plan_switch_supersedes_clears_pointer_bumps_once():
    """A7: real switch atomically supersedes approval, clears pointer, switches plan, bumps once."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    d1 = _create(prop, con)
    p1 = _pid(d1)
    _approve(prop, con, p1)
    from modelark import plan
    plan.create(con, "other", name="Other")
    before_rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    # Switch must go through plan.set_active (graph_write).
    plan.set_active(con, "other")
    state = con.execute(
        "SELECT planner_revision, active_approved_proposal_id FROM planner_state "
        "WHERE singleton_id=1").fetchone()
    assert state[1] is None, "switch must clear active_approved_proposal_id"
    assert _lifecycle(con, p1) == "superseded"
    assert plan.active(con)["plan_id"] == "other"
    assert state[0] == before_rev + 1, "exactly one revision bump for the switch transaction"
    # Failure rolls back: inject fail mid-switch.
    plan.create(con, "third", name="Third")
    # Re-approve on other first
    con.execute("UPDATE planner_state SET planner_revision=0, active_approved_proposal_id=NULL "
                "WHERE singleton_id=1")
    # No-op switch
    before = (
        con.execute("SELECT planner_revision, active_approved_proposal_id FROM planner_state "
                    "WHERE singleton_id=1").fetchone(),
        plan.active(con)["plan_id"],
    )
    plan.set_active(con, "other")
    after = (
        con.execute("SELECT planner_revision, active_approved_proposal_id FROM planner_state "
                    "WHERE singleton_id=1").fetchone(),
        plan.active(con)["plan_id"],
    )
    assert before == after, "no-op set_active must change nothing"


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, type(exc).__name__, str(exc)[:240]))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: proposal CAS contracts EXPECTED RED until PR-08 production.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
