"""Serial repair diagnostics never promote unknown evidence to write authority."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import _pr09_gate1_fixtures as f
from modelark import admission, drive_fence, execution_projection, execution_session, fetch, planning, proposal, register
from modelark.core import db
from modelark.execution_service import production_services


REPAIR = "DRIVE_SERIAL_REPAIR_REQUIRED"


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(register, "archive_path", lambda *args: None)
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE drives SET serial='SERIAL'")
    try:
        yield con
    finally:
        con.close()


def live(con, monkeypatch):
    """Inject only hardware reads, retaining the real fetch/admission boundary."""
    monkeypatch.setattr(register, "archive_path", lambda *args: Path("/disposable/archive"))
    monkeypatch.setattr(fetch, "_live_drive_evidence", lambda _, label: {
        "fs_uuid": label + "-fs", "annex_uuid": None, "serial": "SERIAL",
        "filesystem_capacity_bytes": 10**12, "free_bytes": 10**12,
    })
    return lambda label: fetch.observe_for_admission(con, label)


def assert_unknown(evidence, code=REPAIR):
    assert evidence.kind == "unknown"
    assert evidence.executable is False and evidence.admissible_free == 0
    assert evidence.code == code


def assert_guidance(actions):
    assert "inspect_serial_identity" in actions
    assert "repair_serial_identity" in actions
    assert any(action in actions for action in ("retry_preview", "preview_again"))
    assert not {"mount_or_reconcile_drive", "mount_and_reconcile", "resume_same_approval"} & set(actions)


def test_real_fetch_preview_preserves_closed_diagnostic_and_guidance(catalog, monkeypatch):
    observe = live(catalog, monkeypatch)
    assert observe("d0").refusal_code == REPAIR
    before = tuple(catalog.iterdump())
    result = planning.preview(catalog, "ark", observe=observe)
    assert not result.feasible and result.root_code == "CAPACITY_EVIDENCE_UNKNOWN"
    for evidence in dict(result.evidence_by_drive).values():
        assert_unknown(evidence)
    serialized = result.capacity.to_dict()
    assert_guidance(serialized["gate_b_actions"])
    failures = serialized["failures"]
    assert failures
    for failure in failures:
        assert failure["code"] == "CAPACITY_EVIDENCE_UNKNOWN"
        assert failure["evidence_code"] == REPAIR
        assert_guidance(failure["actions"])
    assert tuple(catalog.iterdump()) == before


def test_real_fetch_approval_refuses_without_mutation_and_keeps_diagnostic(catalog, monkeypatch):
    pid = proposal.create_draft(catalog, plan_id="ark")["proposal_id"]
    observe = live(catalog, monkeypatch)
    before = tuple(catalog.iterdump())
    with pytest.raises(proposal.Refusal) as refusal:
        proposal.approve(catalog, pid, services=proposal._DefaultServices(observe_live=observe))
    assert refusal.value.code == "EXACT_ASSIGNMENT_REJECTED"
    assert refusal.value.evidence["evidence_code"] == REPAIR
    assert_guidance(refusal.value.actions)
    assert tuple(catalog.iterdump()) == before


def test_mixed_preview_keeps_other_drives_reconciliation_guidance(catalog, monkeypatch):
    catalog.execute("UPDATE drives SET role='primary' WHERE drive_label='d1'")
    observe = live(catalog, monkeypatch)

    def mixed(label):
        observation = observe(label)
        return replace(observation, refusal_code=None) if label == "d1" else observation

    result = planning.preview(catalog, "ark", observe=mixed)
    assert result.root_code == "CAPACITY_EVIDENCE_UNKNOWN"
    failures = {item.eligible_drives: item for item in result.capacity.failures}
    assert_guidance(failures[("d0",)].actions)
    assert "mount_or_reconcile_drive" in failures[("d1",)].actions
    assert "inspect_serial_identity" not in failures[("d1",)].actions
    assert {"inspect_serial_identity", "repair_serial_identity", "mount_or_reconcile_drive"} <= set(
        result.capacity.gate_b_actions)


@pytest.mark.parametrize("resume", [False, True])
def test_real_fetch_start_resume_refuses_before_session_write(catalog, monkeypatch, resume):
    _, pid, _ = f.create_and_approve(catalog)
    predecessor = None
    if resume:
        predecessor = "paused"
        catalog.execute(
            "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
            "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
            "VALUES('paused','ark',?,'controller','worker','paused',0,1)", [pid])
    observe = live(catalog, monkeypatch)
    services = f.default_services()
    physical = production_services(catalog, catalog_path=db.DB_PATH)
    services.controller_flock = physical.controller_flock
    services.drive_fences = physical.drive_fences
    services.observe_exact_capacity = proposal._DefaultServices(observe_live=observe).observe_exact_capacity
    before = tuple(catalog.iterdump())
    result = execution_session.start_session(catalog, pid, predecessor, services)
    assert result.code == "CAPACITY_EVIDENCE_UNKNOWN"
    assert result.evidence["evidence_code"] == REPAIR
    assert_guidance(result.actions)
    assert tuple(catalog.iterdump()) == before


@pytest.mark.parametrize("change", ["arbitrary_code", "no_attribute", "live_fingerprint", "live_capacity",
                                  "fs_uuid", "annex_uuid", "serial", "fingerprint", "authority"])
def test_closed_diagnostic_requires_independent_exact_saved_and_live_facts(catalog, monkeypatch, change):
    observation = live(catalog, monkeypatch)("d0")
    if change == "arbitrary_code":
        observation = replace(observation, refusal_code="PLEASE_TRUST_ME")
    elif change == "no_attribute":
        observation = SimpleNamespace(**{k: v for k, v in vars(observation).items() if k != "refusal_code"})
    elif change == "live_fingerprint":
        observation = replace(observation, fingerprint="invented")
    elif change == "live_capacity":
        observation = replace(observation, filesystem_capacity=10**12 + 1)
    else:
        field = {"fingerprint": "identity_fingerprint", "authority": "write_authority"}.get(change, change)
        value = {"fingerprint": "0" * 64, "authority": "unknown"}.get(change, "different")
        catalog.execute(f"UPDATE drives SET {field}=? WHERE drive_label='d0'", [value])
    evidence = admission.execution_evidence(catalog, "d0", observation, now="test")
    assert_unknown(evidence, "DRIVE_IDENTITY_UNPROVEN")


def test_offline_clean_anchor_remains_executable_without_new_probe(catalog):
    evidence = admission.preview_by_drive(catalog, ["d0"], observe=lambda _: None, now="test")["d0"]
    assert evidence.executable and evidence.admissible_free > 0 and evidence.code is None
    default = proposal._DefaultServices().observe_exact_capacity(catalog, ["d0"])["d0"]
    assert default == replace(evidence, observed_at=default.observed_at)


def test_fence_contention_overrides_recognized_serial_diagnostic(catalog, monkeypatch):
    observe = live(catalog, monkeypatch)
    keys = admission._facts(catalog, "d0").fence_identity().lock_keys()
    with drive_fence.hold_drives_sorted(keys, blocking=False):
        evidence = admission.preview_by_drive(catalog, ["d0"], observe=observe, now="test")["d0"]
    assert_unknown(evidence, "DRIVE_FENCE_UNAVAILABLE")


def test_snapshot_fact_race_overrides_recognized_serial_diagnostic(catalog, monkeypatch):
    observe = live(catalog, monkeypatch)

    def racing(label):
        observation = observe(label)
        # New epoch still forms the same exact hash mismatch, but is not the held binding.
        catalog.execute("UPDATE drives SET identity_epoch=identity_epoch+1 WHERE drive_label=?", [label])
        return observation

    evidence = admission.preview_by_drive(catalog, ["d0"], observe=racing, now="test")["d0"]
    assert_unknown(evidence, "DRIVE_IDENTITY_UNPROVEN")


def test_approval_fact_cas_keeps_precedence_over_serial_diagnostic(catalog, monkeypatch):
    pid = proposal.create_draft(catalog, plan_id="ark")["proposal_id"]
    observe = live(catalog, monkeypatch)
    services = proposal._DefaultServices(observe_live=observe)
    original = services.observe_exact_capacity

    def racing(con, labels):
        result = original(con, labels)
        con.execute("UPDATE drives SET identity_epoch=identity_epoch+1 WHERE drive_label='d0'")
        return result

    services.observe_exact_capacity = racing
    before = catalog.execute("SELECT * FROM placement_proposals").fetchall()
    with pytest.raises(proposal.Refusal, match="APPROVED_INPUT_CHANGED") as refusal:
        proposal.approve(catalog, pid, services=services)
    assert refusal.value.evidence == {"reason": "drive_fence_identity_changed"}
    assert catalog.execute("SELECT * FROM placement_proposals").fetchall() == before
    assert not catalog.in_transaction


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("code", [REPAIR, "UNRECOGNIZED", None])
def test_execution_diagnostic_actions_are_closed_and_keep_error_category(offline, code):
    evidence = SimpleNamespace(kind="unknown", executable=False, code=code)
    refusal = execution_projection._unknown_capacity_refusal("d0", evidence, offline=offline)
    assert refusal.code == "CAPACITY_EVIDENCE_UNKNOWN"
    assert refusal.evidence["evidence_code"] == code
    assert refusal.evidence.get("offline", False) == offline
    if code == REPAIR:
        assert_guidance(refusal.actions)
    else:
        assert refusal.actions == ("mount_and_reconcile", "resume_same_approval")
