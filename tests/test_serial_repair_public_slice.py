"""Real repair and public Slice workflow over disposable bytes/synthetic host inputs.

The production catalog, repair, observers, descriptor readers, fences, transaction,
destination and receipts run unchanged. Only host inventory/provider inputs are
synthetic; this does not qualify physical archive or destination backing.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bootstrap, drive_fence
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.core import db
from modelark.slice import catalog, domain, folder_operator, operator, state
from modelark.slice.local_source import LocalArchiveReader
from modelark.slice.sources import FencedSources
from modelark.slice.transaction import TransferRefusal
from test_serial_repair_adversarial import _paused_owner
from test_slice_folder_observation import native as _native_fixture
from test_slice_hardware import hardware as _hardware_fixture

native = _native_fixture
hardware = _hardware_fixture
PAYLOAD = b"tiny original source for serial repair qualification\n"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
REPO = "qualification/tiny"
NAME = "model.safetensors"
ANNEX = "qualification-annex"
_REAL_GETXATTR = os.getxattr


@pytest.fixture
def workflow(tmp_path, monkeypatch, hardware, native, request):
    source_observer, mount, hardware_state, disk, leaf, _ = hardware
    destination_observer, parent, native_state, *_ = native
    # The observation-only fixture simulates absent xattrs. The public writer
    # must instead set/read/authenticate real markers on the disposable output.
    monkeypatch.setattr(os, "getxattr", _REAL_GETXATTR)
    archive = mount / "modelark"
    archive.mkdir()
    subprocess.run(["git", "init", "-q", str(archive)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(archive), "config", "annex.uuid", ANNEX],
                   capture_output=True, check=True)
    source = archive / REPO / NAME
    source.parent.mkdir(parents=True)
    source.write_bytes(PAYLOAD)
    (parent / "unrelated").write_bytes(b"unrelated sibling")
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "private-slice")
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "STATE_DIR", tmp_path / "catalog-state")
    monkeypatch.setattr(folder_operator, "NativeFolderObserver", lambda: destination_observer)
    monkeypatch.setattr(folder_operator, "LinuxObserver", lambda: source_observer)
    monkeypatch.setattr(folder_operator, "read_fs_text", lambda _: native_state["mounts"])
    commands = []
    real_run = subprocess.run

    def run(argv, **kwargs):
        commands.append(tuple(argv))
        if argv[0] == "git":
            return real_run(argv, **kwargs)
        if argv[:2] == ["blkid", "-U"]:
            assert argv[2] == leaf["uuid"]
            output = leaf["path"]
        elif argv[:3] == ["lsblk", "-nro", "MOUNTPOINT"]:
            assert argv[3] == leaf["path"]
            output = str(mount)
        elif argv[:3] == ["findmnt", "-fno", "UUID"]:
            assert argv[-1] == str(archive)
            output = leaf["uuid"]
        elif argv[0] == "findmnt":
            assert argv == ["findmnt", "--json", "--first-only", "--output", "MAJ:MIN",
                            "--target", str(archive)]
            output = json.dumps({"filesystems": [{"maj:min": leaf["maj:min"]}]})
        elif argv[0] == "lsblk":
            assert "--json" in argv
            output = json.dumps(hardware_state["inventory"])
        else:
            pytest.fail(f"unexpected host command: {argv!r}")
        return subprocess.CompletedProcess(argv, 0, output + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    space = os.statvfs(archive)
    capacity = space.f_blocks * space.f_frsize
    old = identity_fingerprint_v1(fs_uuid=leaf["uuid"], annex_uuid=ANNEX,
                                  serial=None, filesystem_capacity_bytes=capacity)
    canonical = identity_fingerprint_v1(fs_uuid=leaf["uuid"], annex_uuid=ANNEX,
                                        serial=disk["serial"], filesystem_capacity_bytes=capacity)
    mode = getattr(request, "param", "legacy")
    stored_serial = None if mode in {"serialless", "observed_serial"} else disk["serial"]
    stored_fp = canonical if mode in {"canonical", "observed_serial"} else old
    proof = json.dumps({"v": 1, "fs_uuid": leaf["uuid"], "annex_uuid": ANNEX,
                        "serial": disk["serial"] if mode in {"canonical", "observed_serial"} else None})
    con = sqlite3.connect(db.DB_PATH, isolation_level=None)
    con.executescript(db.SCHEMA_PATH.read_text())
    con.execute("PRAGMA user_version=7")
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,identity_epoch,write_generation,"
                "identity_fingerprint,filesystem_capacity_bytes,write_authority) "
                "VALUES('drive-00',?,?,?,1,1,?,?,'dedicated_local')",
                [leaf["uuid"], ANNEX, stored_serial, stored_fp, capacity])
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES('drive-00',1,1,'fixture')")
    con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
                "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,"
                "fence_proof,observed_at) VALUES('drive-00',1,1,?,?,?,'dedicated_local',?,?,'2026-09-11')",
                [space.f_bavail * space.f_frsize, capacity, stored_fp, proof, proof])
    con.execute("INSERT INTO models(repo_id) VALUES(?)", [REPO])
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) "
                "VALUES(?,?,?,?,'safetensors','bf16')", [REPO, NAME, len(PAYLOAD), DIGEST])
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,stored_relpath,orig_bytes,stored_bytes,"
                "orig_sha256,orig_sha256_provenance,compressed) "
                "VALUES(?,?,'drive-00',?,?,?,?,'ingestion_computed',0)",
                [REPO, NAME, NAME, len(PAYLOAD), len(PAYLOAD), DIGEST])
    try:
        yield SimpleNamespace(con=con, path=db.DB_PATH, archive=archive, source=source,
                              parent=parent, observer=source_observer, old=old, canonical=canonical,
                              commands=commands, serial=disk["serial"],
                              archive_bytes={str(p.relative_to(archive)): p.read_bytes()
                                             for p in archive.rglob("*") if p.is_file()})
    finally:
        con.close()


def _candidate(case):
    spec = domain.SliceSpec((REPO,), "qualification", "delivery")
    snapshot = catalog.read_catalog(case.path, spec)
    preview = domain.preview(spec, snapshot)
    assert preview.source_ready, preview.gaps
    return preview.closure[0].sources[0]


@pytest.mark.parametrize('workflow', ['observed_serial'], indirect=True)
def test_serialless_old_observed_anchor_repair_unblocks_public_slice(workflow):
    case = workflow
    blocked = operator.preview(case.path, case.parent / 'blocked-output', (REPO,), None)
    assert blocked['state'] == 'blocked'
    assert not (case.parent / 'blocked-output').exists()
    intent = bootstrap.inspect_serial_identity(case.con, 'drive-00')
    assert intent['status'] == 'observed_serial_clean'
    report = bootstrap.repair_serial_identity(
        case.con, 'drive-00', expected_binding=intent['binding'],
        now='2026-09-11', writers_stopped=True)
    assert report['canonical_fingerprint'] == case.old
    assert case.con.execute('SELECT serial FROM drives').fetchone() == (None,)
    preview = _preview(case, 'repaired-output')
    _complete(case, preview, case.parent / 'repaired-output')
    assert {str(p.relative_to(case.archive)): p.read_bytes()
            for p in case.archive.rglob('*') if p.is_file()} == case.archive_bytes


def _preview(case, name):
    result = operator.preview(case.path, case.parent / name, (REPO,), None)
    assert result["state"] == "ready", result
    return result


def _sources(case):
    return FencedSources(case.path, LocalArchiveReader({"drive-00": case.archive}, observer=case.observer))


def _complete(case, preview, destination):
    operator.approve(preview["transaction_id"], preview["seal"])
    outcome = operator.start(preview["transaction_id"], destination, {"drive-00": case.archive})
    assert outcome["state"] == "complete", outcome
    output = destination / REPO / NAME
    assert output.read_bytes() == PAYLOAD
    receipt_path = destination / ".modelark-slice-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["seal"] == preview["seal"]
    assert receipt["transaction"] == preview["transaction_id"]
    assert len(receipt["files"]) == 1
    assert receipt["files"][0]["sha256"] == DIGEST
    assert receipt["files"][0]["size"] == len(PAYLOAD)
    assert receipt["files"][0]["source"]["drive"]["identity_fingerprint"] == (
        case.con.execute("SELECT identity_fingerprint FROM drives").fetchone()[0])
    before = receipt_path.read_bytes()
    assert operator.start(preview["transaction_id"], "/unattached", {})["state"] == "complete"
    assert receipt_path.read_bytes() == before
    assert case.source.read_bytes() == PAYLOAD
    assert {str(p.relative_to(case.archive)): p.read_bytes()
            for p in case.archive.rglob("*") if p.is_file()} == case.archive_bytes
    assert (case.parent / "unrelated").read_bytes() == b"unrelated sibling"


@pytest.mark.parametrize("dirty", [False, True])
def test_actual_repair_unblocks_public_slice_without_rebinding_old_source(workflow, dirty):
    case = workflow
    spec = domain.SliceSpec((REPO,), "qualification", "delivery")
    before_snapshot = catalog.read_catalog(case.path, spec)
    stale_candidate = domain.SourceEvidence(before_snapshot.copies[0], before_snapshot.drives[0],
                                            before_snapshot.anchors[0], "ingestion_computed")
    refused = operator.preview(case.path, case.parent / "old-delivery", (REPO,), None)
    assert refused["state"] == "blocked", refused
    assert {gap["code"] for gap in refused["gaps"]} == {"SOURCE_RECONCILIATION_REQUIRED"}
    with pytest.raises(TransferRefusal, match="SOURCE_CHANGED"):
        with _sources(case).open(stale_candidate):
            pytest.fail("legacy null-serial fingerprint must not yield source bytes")
    assert not (case.parent / "old-delivery").exists()
    if dirty:
        case.con.execute("INSERT INTO drive_dirty_generations"
                         "(drive_label,identity_epoch,generation,operation_code) "
                         "VALUES('drive-00',1,2,'interrupted_fill')")
        case.con.execute("UPDATE drives SET write_generation=2")
        _paused_owner(case.con)
    tables = ("models", "files", "archived", "replicas", "execution_sessions")
    history = {table: case.con.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    anchors = case.con.execute("SELECT * FROM drive_clean_anchors").fetchall()
    generations = case.con.execute("SELECT * FROM drive_dirty_generations ORDER BY generation").fetchall()
    intent = bootstrap.inspect_serial_identity(case.con, "drive-00")
    assert intent["status"] == ("legacy_dirty" if dirty else "legacy_clean")
    result = bootstrap.repair_serial_identity(case.con, "drive-00", expected_binding=intent["binding"],
                                              now="2026-09-11", writers_stopped=True)
    assert result["status"] == "repaired" and result["legacy_recovered"] is dirty
    assert result["inventory_present"] == 1
    assert case.con.execute("PRAGMA user_version").fetchone() == (8,)
    assert case.con.execute("SELECT identity_epoch,write_generation,identity_fingerprint FROM drives").fetchone() == (
        1, 3 if dirty else 2, case.canonical)
    assert {table: case.con.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == history
    assert case.con.execute("SELECT * FROM drive_clean_anchors ORDER BY anchor_id").fetchall()[:1] == anchors
    assert case.con.execute("SELECT * FROM drive_dirty_generations ORDER BY generation").fetchall()[:-1] == generations
    if dirty:
        assert case.con.execute("SELECT identity_fingerprint FROM drive_clean_anchors WHERE generation=2").fetchone() == (
            case.old,)
    with pytest.raises(TransferRefusal, match="SOURCE_EVIDENCE_UNAVAILABLE"):
        with _sources(case).open(stale_candidate):
            pytest.fail("old sealed source must not be rebound to repaired identity")
    fresh = _preview(case, "new-delivery")
    _complete(case, fresh, case.parent / "new-delivery")
    assert {table: case.con.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == history
    assert any(command[0] == "findmnt" for command in case.commands)
    assert any(command[:2] == ("lsblk", "--json") for command in case.commands)


@pytest.mark.parametrize("workflow", ["serialless"], indirect=True)
def test_historic_serialless_seal_stales_but_completed_output_and_receipt_survive_repair(workflow):
    case = workflow
    old_candidate = _candidate(case)
    completed = _preview(case, "completed-before-repair")
    _complete(case, completed, case.parent / "completed-before-repair")
    old = _preview(case, "approved-before-repair")
    operator.approve(old["transaction_id"], old["seal"])
    old_plan = state.Store().load(old["transaction_id"])
    receipt_path = case.parent / "completed-before-repair/.modelark-slice-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    # Reproduce the historical descriptive-serial restoration without changing
    # the old null-serial fingerprint/anchor or the already sealed plan.
    case.con.execute("UPDATE drives SET serial=?", [case.serial])
    intent = bootstrap.inspect_serial_identity(case.con, "drive-00")
    result = bootstrap.repair_serial_identity(case.con, "drive-00", expected_binding=intent["binding"],
                                              now="2026-09-11", writers_stopped=True)
    assert result["status"] == "repaired"
    with pytest.raises(domain.SliceRefusal, match="PREVIEW_STALE"):
        operator.approve(old["transaction_id"], old["seal"])
    with pytest.raises(TransferRefusal, match="SOURCE_EVIDENCE_UNAVAILABLE"):
        with _sources(case).open(old_candidate):
            pytest.fail("historic source must not adopt the repaired fingerprint")
    stale_start = operator.start(old["transaction_id"], case.parent / "approved-before-repair",
                                 {"drive-00": case.archive})
    assert stale_start["state"] == "blocked_source", stale_start
    assert "SOURCE_EVIDENCE_UNAVAILABLE" in stale_start["reason"]
    assert not (case.parent / "approved-before-repair").exists()
    assert state.Store().events(old["transaction_id"]) == []
    assert not (case.parent / "approved-before-repair" / REPO / NAME).exists()
    assert not (case.parent / "approved-before-repair/.modelark-slice-receipt.json").exists()
    assert state.Store().load(old["transaction_id"]) == old_plan
    assert operator.start(completed["transaction_id"], "/unattached", {})["state"] == "complete"
    assert receipt_path.read_bytes() == receipt_bytes
    assert (case.parent / "completed-before-repair" / REPO / NAME).read_bytes() == PAYLOAD
    assert case.source.read_bytes() == PAYLOAD


@pytest.mark.parametrize("workflow", ["canonical"], indirect=True)
def test_unchanged_source_old_public_seal_executes_after_reader_floor_only_change(workflow):
    case = workflow
    old = _preview(case, "unaffected-delivery")
    operator.approve(old["transaction_id"], old["seal"])
    plan = state.Store().load(old["transaction_id"])
    before = catalog.read_catalog(case.path, plan.proposal.spec)
    rows = tuple(case.con.iterdump())
    case.con.execute("PRAGMA user_version=8")
    after = catalog.read_catalog(case.path, plan.proposal.spec)
    assert after == before
    assert domain.preview(plan.proposal.spec, after) == plan.proposal
    assert state.Store().load(old["transaction_id"]) == plan
    _complete(case, old, case.parent / "unaffected-delivery")
    assert tuple(case.con.iterdump()) == rows
