"""Metadata-only loss is bounded to never-bootstrapped registration rows."""
import sqlite3

import pytest

import _pr09_gate1_fixtures as f
from modelark import drive_fence, drive_lifecycle, plan, proposal
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.core import db


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    con = f.mem_con()
    plan.create(con, "ark", name="Ark")
    plan.set_active(con, "ark")
    device = {"dev": "/dev/synthetic", "serial": "SERIAL", "model": "test disk"}
    topology = {"requested_dev": device["dev"], "available": True, "nodes": [{
        "dev": "/dev/synthetic1", "fstype": "ext4", "size_bytes": 1000,
        "fs_uuid": "fs", "mountpoints": [str(tmp_path / "mount")],
        "archive_path": str(tmp_path / "mount" / "archive"),
        "archive_state": "absent", "archive_parent_writable": True,
    }]}
    preview = drive_lifecycle.onboarding_preview(con, device, topology)
    result = drive_lifecycle.register_new_identity(
        con, device, topology, expected_binding=preview["registration_binding"],
        confirmation=preview["confirmation"],
        prepare_archive=lambda **kwargs: {
            "archive_path": kwargs["archive_path"], "annex_uuid": "annex", "free_bytes": 800,
        })
    try:
        yield con, result["drive_label"]
    finally:
        con.close()


def declare(con, label, **overrides):
    preview = drive_lifecycle.loss_preview(con, label)
    args = dict(expected_revision=preview["planner_revision"],
                expected_identity_epoch=preview["identity_epoch"],
                expected_identity_fingerprint=preview["identity_fingerprint"],
                confirmation=preview["confirmation"])
    args.update(overrides)
    return drive_lifecycle.declare_lost(con, label, **args)


@pytest.mark.parametrize("fence_spy", [False, True])
def test_actual_registration_can_be_declared_lost_without_bootstrap_or_physical_fence(
        registered, monkeypatch, fence_spy):
    con, label = registered
    original = con.execute("SELECT * FROM drives WHERE drive_label=?", [label]).fetchone()
    revision = drive_lifecycle.planner_revision(con)
    if fence_spy:
        monkeypatch.setattr(proposal, "_fence_keys", lambda *args: pytest.fail("no physical identity exists"))
    result = declare(con, label)
    assert result["changed"] and result["lifecycle"] == "lost" and result["eligibility"] == "excluded"
    assert result["identity_fingerprint"] is None and result["identity_epoch"] == 1
    assert result["planner_revision"] == revision + 1
    after = con.execute("SELECT * FROM drives WHERE drive_label=?", [label]).fetchone()
    assert after[:-2] == original[:-2]  # Only lifecycle/eligibility changed.
    assert con.execute("SELECT * FROM plan_drives WHERE drive_label=?", [label]).fetchall()
    repeated = declare(con, label)
    assert not repeated["changed"] and repeated["planner_revision"] == revision + 1


@pytest.mark.parametrize("field,value", [
    ("identity_epoch", 2), ("write_generation", 1), ("filesystem_capacity_bytes", 0),
    ("filesystem_capacity_bytes", 1000), ("identity_fingerprint", "0" * 64),
    ("write_authority", "dedicated_local"),
])
def test_partial_or_reset_identity_is_not_a_never_bootstrapped_exception(registered, field, value):
    con, label = registered
    con.execute(f"UPDATE drives SET {field}=? WHERE drive_label=?", [value, label])
    before = tuple(con.iterdump())
    with pytest.raises(proposal.Refusal, match="DRIVE_IDENTITY_UNPROVEN"):
        declare(con, label)
    assert tuple(con.iterdump()) == before


def seed_proposal(con, label, *, binding=None, lifecycle="superseded"):
    con.execute(
        "INSERT INTO placement_proposals(proposal_id,plan_id,based_on_revision,lifecycle,"
        "canonical_hash,mutation_kind,serializer_version) VALUES('history','ark',0,?,?,'adopt_current','1')",
        [lifecycle, "0" * 64])
    if binding:
        con.execute(
            "INSERT INTO proposal_tasks(proposal_id,requirement_id,row_kind,repo_id,"
            f"{binding},full_manifest_hash) VALUES('history','task','executable','org/model',?,?)",
            [label, "0" * 64])


def seed_history(con, label, kind):
    if kind == "dirty":
        con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                    "VALUES(?,2,1,'historical')", [label])
    elif kind == "anchor":
        # An orphan anchor is damaged history, not proof of never being used.
        con.execute(
            "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
            "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
            "VALUES(?,2,1,800,1000,?,'dedicated_local','proof','fence','test')", [label, "0" * 64])
    elif kind == "hash_repair":
        con.execute("INSERT INTO drive_hash_repair_state(drive_label,identity_epoch,status) "
                    "VALUES(?,2,'complete')", [label])
    elif kind == "archived":
        con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,compressed) "
                    "VALUES('org/model','file',?,0)", [label])
    elif kind == "replicas":
        con.execute("INSERT INTO replicas(repo_id,rfilename,drive_label) VALUES('org/model','file',?)", [label])
    else:
        seed_proposal(con, label, binding=kind)


@pytest.mark.parametrize("history", ["dirty", "anchor", "hash_repair", "archived", "replicas",
                                     "target_drive", "source_drive", "satisfying_drive"])
def test_history_at_any_epoch_excludes_metadata_only_loss(registered, history):
    con, label = registered
    seed_history(con, label, history)
    before = tuple(con.iterdump())
    with pytest.raises(proposal.Refusal, match="DRIVE_IDENTITY_UNPROVEN"):
        declare(con, label)
    assert tuple(con.iterdump()) == before


@pytest.mark.parametrize("alias", [0, 1])
def test_proven_identity_still_requires_every_compatible_physical_lock(registered, alias):
    con, label = registered
    fingerprint = identity_fingerprint_v1(
        fs_uuid="fs", annex_uuid="annex", serial="SERIAL", filesystem_capacity_bytes=1000)
    con.execute("UPDATE drives SET filesystem_capacity_bytes=1000,identity_fingerprint=?,"
                "write_authority='dedicated_local' WHERE drive_label=?", [fingerprint, label])
    keys = proposal._fence_keys(con, [label])
    assert len(keys) == 2
    before = tuple(con.iterdump())
    with drive_fence.hold_drives_sorted([keys[alias]], blocking=False):
        with pytest.raises(proposal.Refusal, match="DRIVE_BUSY"):
            declare(con, label)
    assert tuple(con.iterdump()) == before
    assert declare(con, label)["lifecycle"] == "lost"


@pytest.mark.parametrize("override", [
    {"confirmation": "yes"}, {"expected_revision": -1},
    {"expected_identity_epoch": 2}, {"expected_identity_fingerprint": "0" * 64},
])
def test_registration_loss_keeps_confirmation_and_preview_cas(registered, override):
    con, label = registered
    before = tuple(con.iterdump())
    with pytest.raises(proposal.Refusal, match="DRIVE_LOSS_(CONFIRMATION_MISMATCH|PREVIEW_STALE)"):
        declare(con, label, **override)
    assert tuple(con.iterdump()) == before


def test_registration_loss_refuses_live_fill(registered):
    con, label = registered
    seed_proposal(con, label, lifecycle="approved")
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,controller_identity,"
        "state,bound_planner_revision,fencing_token) VALUES('live','ark','history','controller','starting',0,1)")
    before = tuple(con.iterdump())
    with pytest.raises(proposal.Refusal, match="FILL_SESSION_ACTIVE"):
        declare(con, label)
    assert tuple(con.iterdump()) == before


def test_registration_loss_rechecks_history_inside_graph_transaction(registered, monkeypatch):
    con, label = registered
    original = proposal.graph_write

    def raced(c, op):
        def inside(tx):
            assert tx.in_transaction
            seed_history(tx, label, "dirty")
            return op(tx)
        return original(c, inside)

    monkeypatch.setattr(proposal, "graph_write", raced)
    before = tuple(con.iterdump())
    with pytest.raises(proposal.Refusal, match="DRIVE_IDENTITY_UNPROVEN"):
        declare(con, label)
    assert tuple(con.iterdump()) == before


def test_registration_loss_rolls_back_approval_and_lifecycle_with_revision(registered, monkeypatch):
    con, label = registered
    seed_proposal(con, label, lifecycle="approved")
    con.execute("UPDATE planner_state SET active_approved_proposal_id='history'")

    def fail(_):
        raise RuntimeError("revision failure")

    monkeypatch.setattr(proposal, "bump_revision", fail)
    before = tuple(con.iterdump())
    with pytest.raises(RuntimeError, match="revision failure"):
        declare(con, label)
    assert tuple(con.iterdump()) == before


def test_registration_loss_invalidates_approval_without_rewriting_history(registered):
    con, label = registered
    seed_proposal(con, label, lifecycle="approved")
    con.execute("UPDATE planner_state SET active_approved_proposal_id='history'")
    before = con.execute("SELECT canonical_hash,mutation_kind,created_at FROM placement_proposals").fetchone()
    result = declare(con, label)
    assert result["approval_invalidated"]
    assert con.execute("SELECT lifecycle FROM placement_proposals").fetchone() == ("superseded",)
    assert con.execute("SELECT active_approved_proposal_id FROM planner_state").fetchone() == (None,)
    assert con.execute("SELECT canonical_hash,mutation_kind,created_at FROM placement_proposals").fetchone() == before


def test_competing_bootstrap_writer_excludes_classification_until_transaction_ends(registered, tmp_path, monkeypatch):
    source, label = registered
    path = tmp_path / "competing.sqlite"
    writer = sqlite3.connect(path, isolation_level=None)
    source.backup(writer)
    contender = sqlite3.connect(path, isolation_level=None, timeout=0)
    original = drive_lifecycle._never_bootstrapped_for_loss
    classified = []

    def classify(con, name):
        classified.append(name)
        return original(con, name)

    monkeypatch.setattr(drive_lifecycle, "_never_bootstrapped_for_loss", classify)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE drives SET write_generation=1 WHERE drive_label=?", [label])
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            declare(contender, label)
        assert classified == []
        # A partially completed bootstrap cannot be mistaken for pristine once visible.
        writer.execute("COMMIT")
        with pytest.raises(proposal.Refusal, match="DRIVE_IDENTITY_UNPROVEN"):
            declare(contender, label)
        assert classified == [label]
        assert writer.execute("SELECT lifecycle FROM drives WHERE drive_label=?", [label]).fetchone() == ("active",)
    finally:
        contender.close()
        writer.close()
