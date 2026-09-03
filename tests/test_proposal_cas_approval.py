"""PR-08 / #39-A proposal draft/approve CAS and adopt_current (tests-first, RFC-002).

Gate 1: preview pure outside BEGIN IMMEDIATE (instrumented event log); full approval matrix;
atomic rollback via mid-TX inject; adopt_current leaves selection unchanged.
"""
from __future__ import annotations

import importlib
import sqlite3
from contextlib import contextmanager
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
        "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
        "write_authority,filesystem_capacity_bytes) "
        "VALUES('d0',1000000000000,1000000000000,'primary',0,'active','enabled',1,1,?,"
        "'dedicated_local',1000000000000)",
        ["f" * 64])
    # Clean anchor so default A6 evidence uses offline admission (not catalog free→live).
    con.execute(
        "INSERT OR IGNORE INTO drive_dirty_generations"
        "(drive_label,identity_epoch,generation,operation_code) VALUES('d0',1,1,'seed')")
    con.execute(
        "INSERT OR IGNORE INTO drive_clean_anchors"
        "(drive_label,identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
        "identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
        "VALUES('d0',1,1,1000000000000,1000000000000,?,'dedicated_local','seed','seed',"
        "'2026-01-01T00:00:00Z')",
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
    """Call approve. No silent TypeError retries that drop kwargs (finding 18)."""
    approve = getattr(prop, "approve", None) or getattr(prop, "approve_proposal")
    if mutation is not None:
        return approve(con, proposal_id, mutation=mutation, **extra)
    return approve(con, proposal_id, **extra)


def _refusal_code(result) -> str | None:
    """Extract typed refusal code from a returned Refusal/dict or raised exception."""
    if result is None:
        return None
    if isinstance(result, dict):
        return (result.get("code") or result.get("error") or "").upper() or None
    code = getattr(result, "code", None)
    if code is not None:
        return str(getattr(code, "value", code)).upper()
    return None


def _assert_refuses(call, *, code: str, label: str):
    """Accept returned typed refusal OR raised exception carrying the exact code (finding 18).

    A bare successful return with no refusal is always a failure. Does not treat AssertionError
    sentinels as production refusals.
    """
    want = code.upper()
    try:
        out = call()
    except AssertionError:
        raise
    except Exception as exc:
        msg = str(exc).upper()
        got = _refusal_code(exc)
        if got == want or want in msg or f"CODE={want}" in msg or f"'{want}'" in msg:
            return exc
        raise AssertionError(
            f"{label}: expected refusal code {want}, got exception {type(exc).__name__}: {exc}"
        ) from exc
    else:
        got = _refusal_code(out)
        if got == want:
            return out
        raise AssertionError(
            f"{label}: expected refusal code {want}, but call returned successfully: {out!r}")


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


def test_approve_refuses_ambiguous_current_drafts_without_orphaning_intent():
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    columns = [
        row[1] for row in con.execute("PRAGMA table_info(placement_proposals)").fetchall()
        if row[1] != "proposal_id"
    ]
    names = ",".join(columns)
    con.execute(
        f"INSERT INTO placement_proposals(proposal_id,{names}) "
        f"SELECT ?,{names} FROM placement_proposals WHERE proposal_id=?",
        ["ambiguous-sibling", pid],
    )

    _assert_refuses(
        lambda: _approve(prop, con, pid),
        code="MULTIPLE_CURRENT_DRAFTS",
        label="approval with ambiguous current drafts",
    )
    assert con.execute(
        "SELECT planner_revision,active_approved_proposal_id FROM planner_state "
        "WHERE singleton_id=1"
    ).fetchone() == (0, None)
    assert {
        row[0] for row in con.execute(
            "SELECT proposal_id FROM placement_proposals WHERE lifecycle='draft'"
        ).fetchall()
    } == {"ambiguous-sibling", pid}


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
        code="PREVIEW_STALE",
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
        code="PROPOSAL_HASH_MISMATCH",
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
        code="MUTATION_MISMATCH",
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
        code="PROPOSAL_NOT_DRAFT",
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
        code="APPROVED_INPUT_CHANGED",
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
        code="PROPOSAL_NOT_FEASIBLE",
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


def _proposal_relevant_drive_labels(con, proposal_id) -> set[str]:
    """RFC-002 proposal_drive_ids: exact target/source labels from stored assignment rows."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(proposal_tasks)").fetchall()}
    drive_cols = [c for c in ("target_drive", "source_drive", "satisfying_drive") if c in cols]
    assert drive_cols, (
        "proposal_tasks must expose target_drive/source_drive (or satisfying_drive) "
        "so A6 can derive proposal-relevant fence keys")
    labels: set[str] = set()
    for col in drive_cols:
        for (val,) in con.execute(
                f"SELECT DISTINCT {col} FROM proposal_tasks "
                f"WHERE proposal_id=? AND {col} IS NOT NULL",
                [proposal_id]):
            labels.add(val)
    return labels


def _fence_keys_for_labels(con, labels: set[str]) -> list[tuple]:
    """Join drive labels to identity (fingerprint, epoch) keys used by hold_drives_sorted."""
    keys = []
    for label in labels:
        row = con.execute(
            "SELECT identity_fingerprint, identity_epoch FROM drives WHERE drive_label=?",
            [label]).fetchone()
        assert row is not None and row[0], f"drive {label} must have identity_fingerprint"
        keys.append((row[0], int(row[1])))
    return sorted(keys)


def test_approval_acquires_controller_then_sorted_drives_then_evidence_before_tx():
    """A6: fence proposal-relevant drives only (RFC proposal_drive_ids), not every plan member.

    Fixture forces a two-copy assignment that genuinely references both d0 and d1 in stored
    proposal_tasks. Expected fence keys are derived from those target/source labels joined to
    identity/epoch — never assumed from plan membership alone (finding 37).

    Order: controller → exact sorted proposal fence keys → fresh clock-tied evidence → BEGIN IMMEDIATE.
    """
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    # Two-copy requirement so the assignment must place work on both plan members.
    con.execute("UPDATE models SET numcopies=2 WHERE repo_id='org/m'")
    fp_d0 = "f" * 64  # _seed_selection d0 fingerprint, epoch 1
    fp_d1 = "e" * 64
    con.execute(
        "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
        "write_authority,filesystem_capacity_bytes) "
        "VALUES('d1',1000000000000,1000000000000,'replica',0,'active','enabled',2,1,?,"
        "'dedicated_local',1000000000000)",
        [fp_d1])
    con.execute(
        "INSERT OR IGNORE INTO drive_dirty_generations"
        "(drive_label,identity_epoch,generation,operation_code) VALUES('d1',2,1,'seed')")
    con.execute(
        "INSERT OR IGNORE INTO drive_clean_anchors"
        "(drive_label,identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
        "identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
        "VALUES('d1',2,1,1000000000000,1000000000000,?,'dedicated_local','seed','seed',"
        "'2026-01-01T00:00:00Z')",
        [fp_d1])
    from modelark import plan
    plan.add_drive(con, "ark", "d1")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    draft = _create(prop, con)
    pid = _pid(draft)

    # Derive relevant drives from the stored assignment — not from plan membership.
    relevant_labels = _proposal_relevant_drive_labels(con, pid)
    assert {"d0", "d1"} <= relevant_labels, (
        f"two-copy fixture must store both d0 and d1 in proposal_tasks target/source; "
        f"got {relevant_labels}")
    expected_keys = _fence_keys_for_labels(con, relevant_labels)
    assert expected_keys == sorted([(fp_d0, 1), (fp_d1, 2)]), expected_keys
    # Epoch lookup for evidence provenance per label.
    label_epochs = {
        label: int(con.execute(
            "SELECT identity_epoch FROM drives WHERE drive_label=?", [label]
        ).fetchone()[0])
        for label in relevant_labels
    }

    import modelark.drive_fence as df
    from modelark import capacity_evidence

    approval_now = "2026-07-25T12:34:56Z"
    order: list[str] = []
    acquire_order: list[tuple] = []

    @contextmanager
    def fake_controller(catalog_path, *, blocking=True):
        order.append("CONTROLLER")
        assert not con._in_immediate, "controller fence before BEGIN IMMEDIATE"
        yield object()

    @contextmanager
    def fake_drives_sorted(keyed_drives, *, blocking=True):
        # Model the real helper: sort inside; do not require pre-sorted input.
        keys = list(keyed_drives)
        for identity, epoch in sorted(keys):
            acquire_order.append((identity, int(epoch)))
            order.append(f"DRIVE:{identity}:{int(epoch)}")
        assert not con._in_immediate, "drive fences before BEGIN IMMEDIATE"
        yield [object() for _ in sorted(keys)]

    class _TestClock:
        def now(self):
            return approval_now

    class _TestServices:
        """Tests-only inject (A6): clock + evidence capture observed during approve."""

        def __init__(self):
            self.clock = _TestClock()
            self.evidence_calls = 0
            self.last_evidence = None

        def observe_exact_capacity(self, *a, **k):
            """RFC-shaped capture: after fences, before BEGIN IMMEDIATE; observed_at from clock."""
            self.evidence_calls += 1
            order.append("EVIDENCE")
            assert not con._in_immediate, "evidence capture must precede BEGIN IMMEDIATE"
            assert "CONTROLLER" in order, "evidence after controller fence"
            assert any(x.startswith("DRIVE:") for x in order), "evidence after drive fences"
            now = self.clock.now()
            assert now == approval_now
            # Evidence only for proposal-relevant drives (not every plan member).
            self.last_evidence = {
                label: capacity_evidence.Evidence(
                    kind="live", executable=True, admissible_free=10**12,
                    optimistic_usable_max=10**12, observed_free=10**12,
                    observed_at=now, identity_epoch=label_epochs[label])
                for label in relevant_labels
            }
            return self.last_evidence

    services = _TestServices()

    con.events.clear()
    with mock.patch.object(df, "hold_controller", side_effect=fake_controller):
        with mock.patch.object(df, "hold_drives_sorted", side_effect=fake_drives_sorted):
            # Production approve accepts services= (RFC). Do not pre-build evidence_by_drive
            # before fence acquisition — capture must run inside the services seam.
            try:
                _approve(prop, con, pid, services=services)
            except TypeError:
                # Positional request + services per RFC approve_proposal(con, id, request, services).
                approve = getattr(prop, "approve", None) or prop.approve_proposal
                try:
                    approve(con, pid, {}, services)
                except TypeError as exc:
                    raise AssertionError(
                        "approve must accept services= (or request, services) for the "
                        "tests-only A6 clock/evidence seam"
                    ) from exc

    assert order and order[0] == "CONTROLLER", f"controller first; order={order}"
    drive_events = [x for x in order if x.startswith("DRIVE:")]
    assert drive_events == [f"DRIVE:{i}:{e}" for i, e in expected_keys], (
        f"exact sorted proposal fence keys required; expected {expected_keys}, "
        f"acquire_order={acquire_order}, drive_events={drive_events}")
    assert acquire_order == expected_keys, acquire_order
    assert "EVIDENCE" in order, (
        f"services.observe_exact_capacity must run during approve; order={order}")
    assert services.evidence_calls >= 1
    assert services.last_evidence is not None
    assert set(services.last_evidence) == relevant_labels
    for label, epoch in label_epochs.items():
        ev = services.last_evidence[label]
        assert ev.observed_at == approval_now, (
            f"{label} observed_at must come from approval clock, got {ev.observed_at!r}")
        assert ev.identity_epoch == epoch
    # Full authority sequence: controller → proposal drives (sorted) → evidence.
    assert order.index("CONTROLLER") < order.index(drive_events[0]), order
    assert order.index(drive_events[-1]) < order.index("EVIDENCE"), order
    if len(drive_events) > 1:
        assert order.index(drive_events[0]) < order.index(drive_events[-1]), order
    assert any(e.startswith("BEGIN:") and "IMMEDIATE" in e for e in con.events), con.events
    # Evidence observed before IMMEDIATE: callback asserted !_in_immediate; TX still opened.
    begin_i = next(
        i for i, e in enumerate(con.events) if e.startswith("BEGIN:") and "IMMEDIATE" in e)
    assert begin_i >= 0


def test_approval_routes_through_fill_worker_guarded_mutation():
    """A8: approval must execute inside FillWorker.guarded_mutation callback (atomic guard)."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    draft = _create(prop, con)
    pid = _pid(draft)
    from modelark.web import fill_worker

    calls: list[str] = []

    def tracking_gm(mutate):
        calls.append("ENTER")
        # Live path: refuse without running mutate.
        if fill_worker.WORKER.running():
            calls.append("REFUSE")
            return None
        calls.append("RUN")
        return mutate()

    # (1) Happy path: guarded_mutation runs the callback.
    with mock.patch.object(fill_worker.WORKER, "running", return_value=False):
        with mock.patch.object(fill_worker.WORKER, "guarded_mutation", side_effect=tracking_gm):
            _approve(prop, con, pid)
    assert "ENTER" in calls and "RUN" in calls, (
        f"approve must call WORKER.guarded_mutation and run its callback; calls={calls}")

    # (2) Live Fill: guarded_mutation returns None → FILL_SESSION_ACTIVE.
    d2 = _create(prop, con, ("adopt_current", ()))
    p2 = _pid(d2)
    calls.clear()
    with mock.patch.object(fill_worker.WORKER, "running", return_value=True):
        with mock.patch.object(fill_worker.WORKER, "guarded_mutation", side_effect=tracking_gm):
            _assert_refuses(
                lambda: _approve(prop, con, p2),
                code="FILL_SESSION_ACTIVE",
                label="approve while guarded_mutation refuses",
            )
    assert "ENTER" in calls and "REFUSE" in calls, calls


def test_second_approval_supersedes_prior_and_moves_pointer():
    """Second approve supersedes prior approved proposal and moves singleton pointer."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    d1 = _create(prop, con, ("adopt_current", ()))
    p1 = _pid(d1)
    _approve(prop, con, p1)
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == p1
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
    """A7: real switch supersedes approval, clears pointer, switches plan, bumps once."""
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
    plan.set_active(con, "other")
    state = con.execute(
        "SELECT planner_revision, active_approved_proposal_id FROM planner_state "
        "WHERE singleton_id=1").fetchone()
    assert state[1] is None, "switch must clear active_approved_proposal_id"
    assert _lifecycle(con, p1) == "superseded"
    assert plan.active(con)["plan_id"] == "other"
    assert state[0] == before_rev + 1, "exactly one revision bump"


def test_active_plan_switch_mid_failure_rolls_back_all_axes():
    """A7: inject failure mid-switch → plan, proposal lifecycle, pointer, revision all roll back."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    d1 = _create(prop, con)
    p1 = _pid(d1)
    _approve(prop, con, p1)
    from modelark import plan
    plan.create(con, "other", name="Other")
    before = {
        "rev": con.execute(
            "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0],
        "ptr": con.execute(
            "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
        ).fetchone()[0],
        "life": _lifecycle(con, p1),
        "active": plan.active(con)["plan_id"],
    }
    # Inject after first UPDATE in set_active (is_active=false for all).
    real_execute = con.execute
    fired = {"n": 0}

    def boom(sql, *args):
        s = sql if isinstance(sql, str) else str(sql)
        if "UPDATE plans SET is_active" in s or "is_active=false" in s.lower():
            fired["n"] += 1
            if fired["n"] == 1:
                real_execute(sql, *args)
                raise sqlite3.OperationalError("injected mid-switch failure")
        return real_execute(sql, *args)

    con.execute = boom  # type: ignore[method-assign]
    try:
        try:
            plan.set_active(con, "other")
        except Exception:
            pass
    finally:
        con.execute = real_execute  # type: ignore[method-assign]

    assert fired["n"] >= 1, "injection must fire mid-switch"
    assert plan.active(con)["plan_id"] == before["active"], "plan must roll back"
    assert _lifecycle(con, p1) == before["life"], "proposal lifecycle must roll back"
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == before["ptr"], "pointer must roll back"
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == before["rev"], "revision must roll back"


def test_noop_set_active_preserves_existing_approval_and_pointer():
    """A7: no-op switch changes nothing — including an existing approval pointer."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    d1 = _create(prop, con)
    p1 = _pid(d1)
    _approve(prop, con, p1)
    from modelark import plan
    before = (
        con.execute(
            "SELECT planner_revision, active_approved_proposal_id FROM planner_state "
            "WHERE singleton_id=1").fetchone(),
        plan.active(con)["plan_id"],
        _lifecycle(con, p1),
    )
    plan.set_active(con, before[1])  # already active
    after = (
        con.execute(
            "SELECT planner_revision, active_approved_proposal_id FROM planner_state "
            "WHERE singleton_id=1").fetchone(),
        plan.active(con)["plan_id"],
        _lifecycle(con, p1),
    )
    assert before == after, "no-op must preserve revision, pointer, plan, and approval lifecycle"


def test_fence_keys_refuse_missing_identity_fingerprint():
    """Finding 38: missing fingerprints refuse — never silently omit fence keys."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    con.execute(
        "UPDATE drives SET identity_fingerprint=NULL WHERE drive_label='d0'")
    try:
        prop._fence_keys(con, ["d0"])
        raise AssertionError("must refuse missing fingerprint")
    except Exception as exc:
        msg = str(exc).upper()
        assert "IDENTITY" in msg or "FINGERPRINT" in msg or "DRIVE_IDENTITY" in msg, exc


def test_two_copy_executable_sets_source_drive_on_replica():
    """Finding 40: replica tasks name a durable source_drive, not None."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    con.execute("UPDATE models SET numcopies=2 WHERE repo_id='org/m'")
    fp_d1 = "e" * 64
    con.execute(
        "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
        "write_authority,filesystem_capacity_bytes) "
        "VALUES('d1',1000000000000,1000000000000,'replica',0,'active','enabled',2,1,?,"
        "'dedicated_local',1000000000000)",
        [fp_d1])
    con.execute(
        "INSERT OR IGNORE INTO drive_dirty_generations"
        "(drive_label,identity_epoch,generation,operation_code) VALUES('d1',2,1,'seed')")
    con.execute(
        "INSERT OR IGNORE INTO drive_clean_anchors"
        "(drive_label,identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
        "identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
        "VALUES('d1',2,1,1000000000000,1000000000000,?,'dedicated_local','seed','seed',"
        "'2026-01-01T00:00:00Z')",
        [fp_d1])
    from modelark import plan
    plan.add_drive(con, "ark", "d1")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    draft = _create(prop, con)
    pid = _pid(draft)
    rows = con.execute(
        "SELECT requirement_id, target_drive, source_drive, row_kind FROM proposal_tasks "
        "WHERE proposal_id=? ORDER BY order_key", [pid]).fetchall()
    assert len(rows) >= 2, rows
    replica = [r for r in rows if str(r[0]).startswith("replica")]
    assert replica, f"expected replica task; got {rows}"
    for rid, target, source, kind in replica:
        assert kind == "executable"
        assert source is not None, f"{rid} must set source_drive; target={target}"
        assert source != target, f"{rid} source must differ from target for multi-copy"


def test_baseline_certificate_persisted_and_revalidated():
    """Finding 39/A10: baseline certificate is stored and content revalidation refuses drift."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    # Complete archive on d0 → baseline_satisfied with certificate.
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes,"
        "orig_sha256) VALUES('org/m','model.safetensors','d0',0,100,100,?)",
        ["1" * 64])
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    draft = _create(prop, con)
    pid = _pid(draft)
    row = con.execute(
        "SELECT row_kind, baseline_certificate, satisfying_drive FROM proposal_tasks "
        "WHERE proposal_id=?", [pid]).fetchone()
    assert row is not None
    assert row[0] == "baseline_satisfied", row
    assert row[1] and len(row[1]) == 64, f"baseline_certificate must be stored; got {row[1]!r}"
    # Change current file content without revision bump → approve must refuse (finding 39).
    con.execute(
        "UPDATE files SET sha256=? WHERE repo_id='org/m' AND rfilename='model.safetensors'",
        ["2" * 64])
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        code="APPROVED_INPUT_CHANGED",
        label="baseline manifest content drift",
    )


def test_executable_manifest_content_drift_refuses_approve():
    """A10: executable tasks also revalidate current full_manifest_hash (not baseline-only)."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    # No complete archive → executable placement task.
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    draft = _create(prop, con)
    pid = _pid(draft)
    kind = con.execute(
        "SELECT row_kind FROM proposal_tasks WHERE proposal_id=?", [pid]).fetchone()[0]
    assert kind == "executable", kind
    con.execute(
        "UPDATE files SET sha256=? WHERE repo_id='org/m' AND rfilename='model.safetensors'",
        ["2" * 64])
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        code="APPROVED_INPUT_CHANGED",
        label="executable manifest content drift",
    )


def test_baseline_archived_evidence_drift_refuses_approve():
    """A10: archived per-file evidence drift invalidates baseline certificate (manifest stable)."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes,"
        "orig_sha256) VALUES('org/m','model.safetensors','d0',0,100,100,?)",
        ["1" * 64])
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    draft = _create(prop, con)
    pid = _pid(draft)
    kind = con.execute(
        "SELECT row_kind FROM proposal_tasks WHERE proposal_id=?", [pid]).fetchone()[0]
    assert kind == "baseline_satisfied", kind
    # Keep files.sha256 (manifest hash) stable; tamper durable archive evidence only.
    con.execute(
        "UPDATE archived SET orig_sha256=? WHERE repo_id='org/m' "
        "AND rfilename='model.safetensors' AND drive_label='d0'",
        ["9" * 64])
    _assert_refuses(
        lambda: _approve(prop, con, pid),
        code="EXACT_ASSIGNMENT_REJECTED",
        label="baseline archived per-file evidence drift",
    )


def test_missing_catalog_identity_does_not_satisfy_baseline():
    """Files without sha256 and size never satisfy or become executable placement work."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    # Strip catalog identity so only the filename remains.
    con.execute(
        "UPDATE files SET sha256=NULL, size_bytes=NULL "
        "WHERE repo_id='org/m' AND rfilename='model.safetensors'")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes,"
        "orig_sha256) VALUES('org/m','model.safetensors','d0',0,100,100,?)",
        ["1" * 64])
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    draft = _create(prop, con)
    pid = _pid(draft)
    kinds = [r[0] for r in con.execute(
        "SELECT row_kind FROM proposal_tasks WHERE proposal_id=?", [pid]).fetchall()]
    assert "baseline_satisfied" not in kinds, (
        f"filename-only archive must not satisfy baseline; kinds={kinds}")
    # DEC-067: the proposal consumes canonical provenance decisions.  An existing same-name row
    # cannot be safely overwritten or budgeted when catalog identity is absent, so this is a typed
    # fail-closed plan rather than executable work from the retired proposal-only planner.
    assert "executable" not in kinds, kinds
    loaded = prop.load_proposal(con, pid)
    assert loaded["gate_b_code"] == "UNPROVEN_PROVENANCE"


def test_default_evidence_is_not_catalog_free_as_live():
    """Finding 38: default services must not invent live free from drives.free_bytes."""
    prop = _proposal()
    con = _mem()
    # Dedicated-local drive with free_bytes but no clean anchor and generation 0 (unproven offline).
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
        "write_authority,filesystem_capacity_bytes) "
        "VALUES('dx',1000,900,'primary',0,'active','enabled',1,0,?,'dedicated_local',1000)",
        ["c" * 64])
    svc = prop._DefaultServices()
    ev = svc.observe_exact_capacity(con, ["dx"])["dx"]
    assert not (getattr(ev, "kind", None) == "live" and getattr(ev, "executable", False)), (
        f"catalog free must not become live evidence; got kind={ev.kind} executable={ev.executable}")
    assert getattr(ev, "kind", None) == "unknown" or not getattr(ev, "executable", True), (
        f"expected unknown/non-executable without live observe or clean anchor; got {ev}")


def test_graph_write_rolls_back_when_revision_bump_fails():
    """Finding 44: revision-update failure must roll back the graph mutation."""
    prop = _proposal()
    con = _mem()
    _seed_selection(con)
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    before_models = con.execute(
        "SELECT count(*) FROM models WHERE repo_id='org/tx'").fetchone()[0]
    before_rev = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]

    class _Inject:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a):
            text = sql if isinstance(sql, str) else str(sql)
            if "UPDATE planner_state SET planner_revision" in text:
                raise sqlite3.OperationalError("injected revision bump failure")
            return self._inner.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self._inner, n)

    spy = _Inject(con)

    class Result:
        proven_noop = False
        value = None

    def op(c):
        c.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/tx',1)")
        return Result()

    try:
        prop.graph_write(spy, op)
        raise AssertionError("graph_write must fail when revision bump fails")
    except Exception:
        pass
    assert con.execute(
        "SELECT count(*) FROM models WHERE repo_id='org/tx'").fetchone()[0] == before_models
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == before_rev


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
