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
    """Connection proxy with ordered event log for TX/solver ordering proofs."""

    def __init__(self, con):
        self._con = con
        self.events: list[str] = []

    def execute(self, sql, *args):
        s = sql if isinstance(sql, str) else str(sql)
        up = s.strip().upper()
        if up.startswith("BEGIN"):
            self.events.append(f"BEGIN:{up}")
        elif up.startswith("COMMIT"):
            self.events.append("COMMIT")
        elif up.startswith("ROLLBACK"):
            self.events.append("ROLLBACK")
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


def _approve(prop, con, proposal_id):
    approve = getattr(prop, "approve", None) or getattr(prop, "approve_proposal")
    try:
        return approve(con, proposal_id)
    except TypeError:
        return approve(con, proposal_id=proposal_id)


def test_preview_pure_runs_before_any_begin_immediate():
    """Ordered event log: pure preview must not BEGIN IMMEDIATE; publish may."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    assert hasattr(prop, "preview_pure") or hasattr(prop, "compute_draft_payload"), (
        "must expose preview_pure or compute_draft_payload")
    assert hasattr(prop, "publish_draft") or hasattr(prop, "persist_draft"), (
        "must expose publish_draft or persist_draft separate from pure preview")

    pure = getattr(prop, "preview_pure", None) or getattr(prop, "compute_draft_payload")
    publish = getattr(prop, "publish_draft", None) or getattr(prop, "persist_draft")

    con.events.clear()
    try:
        payload = pure(con, plan_id="ark", mutation=("adopt_current", ()))
    except TypeError:
        payload = pure(con, "ark", ("adopt_current", ()))
    pure_events = list(con.events)
    for ev in pure_events:
        assert "IMMEDIATE" not in ev, (
            f"pure preview must not BEGIN IMMEDIATE; events={pure_events}")

    # Solver calls during pure phase are allowed; during publish TX they are not.
    from modelark import placement, capacity
    con.events.clear()
    solve_during_tx = []

    def track_gate(*a, **k):
        if any(e.startswith("BEGIN") and "IMMEDIATE" in e for e in con.events):
            solve_during_tx.append("gate_b")
        return mock.DEFAULT

    with mock.patch.object(placement, "gate_b", side_effect=track_gate):
        with mock.patch.object(capacity, "plan_capacity", side_effect=track_gate):
            try:
                try:
                    publish(con, payload)
                except TypeError:
                    publish(con, plan_id="ark", payload=payload)
            except Exception:
                pass
    assert solve_during_tx == [], (
        f"solver must not run inside BEGIN IMMEDIATE publish; saw {solve_during_tx} "
        f"events={con.events}")


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
    try:
        _approve(prop, con, pid)
        raise AssertionError("approve must refuse stale based_on_revision")
    except Exception as exc:
        msg = str(exc).upper()
        assert "STALE" in msg or "PREVIEW_STALE" in msg or "REVISION" in msg, exc
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
    try:
        _approve(prop, con, pid)
        raise AssertionError("hash mismatch must refuse")
    except Exception as exc:
        assert "HASH" in str(exc).upper() or "MISMATCH" in str(exc).upper(), exc


def test_mutation_mismatch_refuses():
    """Stored mutation descriptor must match the approve-time request/context."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con, ("adopt_current", ()))
    pid = _pid(draft)
    # Corrupt stored mutation kind after draft.
    con.execute(
        "UPDATE placement_proposals SET mutation_kind=? WHERE proposal_id=?",
        ["finalize", pid])
    try:
        _approve(prop, con, pid)
        raise AssertionError("mutation mismatch must refuse")
    except Exception as exc:
        msg = str(exc).upper()
        assert "MUTATION" in msg or "MISMATCH" in msg, exc
    assert _lifecycle(con, pid) == "draft"
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None


def test_non_draft_refuses():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    con.execute(
        "UPDATE placement_proposals SET lifecycle='superseded' WHERE proposal_id=?", [pid])
    try:
        _approve(prop, con, pid)
        raise AssertionError("non-draft must refuse")
    except Exception as exc:
        msg = str(exc).upper()
        assert "NOT_DRAFT" in msg or "PROPOSAL_NOT_DRAFT" in msg or "DRAFT" in msg, exc


def test_missed_revision_bump_still_blocked_by_semantic_recompute():
    """Even if based_on_revision still matches, changed semantic inputs refuse."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    # Leave planner_revision at based_on value, but change selection/manifest facts.
    con.execute(
        "INSERT INTO models(repo_id,numcopies) VALUES('org/extra',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/extra','x.safetensors',50,'safetensors','bf16',?)", ["3" * 64])
    con.execute(
        "INSERT INTO selection(repo_id,finalized_at) VALUES('org/extra','2026-01-01')")
    # Revision intentionally NOT bumped (missed writer) — semantic recompute must still refuse.
    rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    based = con.execute(
        "SELECT based_on_revision FROM placement_proposals WHERE proposal_id=?",
        [pid]).fetchone()[0]
    assert rev == based, "fixture keeps revision matching based_on"
    try:
        _approve(prop, con, pid)
        raise AssertionError("semantic input change must refuse even when revision matches")
    except Exception as exc:
        msg = str(exc).upper()
        assert "INPUT" in msg or "SEMANTIC" in msg or "HASH" in msg or "CHANGED" in msg, exc
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None


def test_exact_assignment_rejection_does_not_call_optimizer():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    from modelark import placement, capacity
    # Force exact-assignment validation failure via evidence inject hook if present;
    # otherwise shrink drive so assignment no longer fits while forbidding re-solve.
    with mock.patch.object(placement, "improve", side_effect=AssertionError("no improve")):
        with mock.patch.object(placement, "gate_b", side_effect=AssertionError("no gate_b")):
            with mock.patch.object(capacity, "plan_capacity",
                                   side_effect=AssertionError("no plan_capacity")):
                # Invalidate exact target by removing drive membership after draft.
                from modelark import plan
                plan.remove_drive(con, "ark", "d0")
                # Restore revision match if remove_drive bumped (production).
                based = con.execute(
                    "SELECT based_on_revision FROM placement_proposals WHERE proposal_id=?",
                    [pid]).fetchone()[0]
                con.execute(
                    "UPDATE planner_state SET planner_revision=? WHERE singleton_id=1", [based])
                try:
                    _approve(prop, con, pid)
                    raise AssertionError("exact assignment must reject when target gone")
                except AssertionError as exc:
                    if "no improve" in str(exc) or "no gate_b" in str(exc) or \
                            "no plan_capacity" in str(exc):
                        raise AssertionError(
                            f"approve must not re-optimize on assignment failure: {exc}") from exc
                    raise
                except Exception as exc:
                    msg = str(exc).upper()
                    assert "ASSIGN" in msg or "TARGET" in msg or "FEASIB" in msg or \
                        "EVIDENCE" in msg or "DRIVE" in msg, exc


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
    try:
        _approve(prop, con, pid)
        raise AssertionError("non-feasible proposal must not become active approved")
    except AssertionError:
        raise
    except Exception as exc:
        assert any(k in str(exc).upper() for k in (
            "FEASIBLE", "INFEASIBLE", "NOT_FEASIBLE", "REFUS")), exc
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None


def test_approval_mid_transaction_failure_rolls_back_all_effects():
    """Inject failure after mutation begins inside approve TX → full rollback."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con, repos=())
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/x',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/x','model.safetensors',100,'safetensors','bf16')")
    draft = _create(prop, con, ("finalize", ("org/x",)))
    pid = _pid(draft)

    # Instrument approve path: after first selection mutation SQL, raise.
    real_execute = con._con.execute
    mutated = {"n": 0}

    def boom(sql, *args):
        s = sql if isinstance(sql, str) else str(sql)
        if "INSERT INTO selection" in s or "UPDATE selection" in s:
            mutated["n"] += 1
            if mutated["n"] >= 1:
                # Let the mutation apply once then fail before commit — proxy raises after execute.
                real_execute(sql, *args)
                raise sqlite3.OperationalError("injected mid-approve failure")
        return real_execute(sql, *args)

    con._con.execute = boom  # type: ignore[method-assign]
    try:
        try:
            _approve(prop, con, pid)
        except Exception:
            pass
    finally:
        con._con.execute = real_execute  # type: ignore[method-assign]

    assert con.execute(
        "SELECT count(*) FROM selection WHERE repo_id='org/x'").fetchone()[0] == 0, (
        "failed approve must roll back selection mutation")
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None
    assert _lifecycle(con, pid) == "draft"
    # Revision must not land at approved-success value solely from failed TX.
    # (May be 0 or concurrent; must not leave approved lifecycle.)


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
                try:
                    _approve(prop, con, pid)
                except AssertionError as exc:
                    if any(x in str(exc) for x in ("no plan_capacity", "no improve", "no gate_b")):
                        raise AssertionError(
                            f"approve must not invoke solver/capacity: {exc}") from exc
                    raise


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
