"""Serial-less registrations retain their identity when physical serial becomes observable."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bs, drive_mutation as dm, fetch, register
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.serial_identity import serial_for_identity, SerialIdentityUnproven
from modelark.slice import domain
from modelark.slice.local_source import LocalArchiveReader
from modelark.slice.transaction import TransferRefusal
from test_archive_serial_observation import archive as _archive_fixture
from test_slice_local_source import attachment as _attachment_fixture
from test_slice_domain import facts, spec

archive = _archive_fixture
attachment = _attachment_fixture
from test_block_identity import observed as _observed_fixture
observed = _observed_fixture


@pytest.mark.parametrize("baseline_kind", ["skip_smart", "raid"])
def test_serialless_registration_bootstraps_and_fills_without_silent_enrichment(archive, monkeypatch, baseline_kind):
    con, _ = archive
    with monkeypatch.context() as mocks:
        mocks.setattr(register, "_run", lambda *a, **k: SimpleNamespace(stdout="test disk"))
        baseline = (register._unchecked_baseline("/dev/mock", "skip SMART") if baseline_kind == "skip_smart"
                    else register._raid_baseline("/dev/mock"))
    assert baseline["serial"] == ""
    # This is the exact serial persistence policy of register_drive for these baselines.
    con.execute("UPDATE drives SET serial=?,identity_fingerprint=NULL,filesystem_capacity_bytes=NULL,"
                "write_authority='unknown',raid_backed=?",
                [baseline["serial"] or None, int(baseline_kind == "raid")])
    inventory = []
    monkeypatch.setattr(bs, "_require_complete_inventory",
                        lambda *a, **k: inventory.append("scanned") or bs.Inventory([], []))
    outcome = bs.reconcile_drive(con, "drive-a", dedicated=True, now="test")
    assert outcome.outcome == "bootstrapped" and inventory == ["scanned"]
    expected = identity_fingerprint_v1(fs_uuid="archive-fs", annex_uuid="archive-annex", serial=None,
                                       filesystem_capacity_bytes=1000)
    assert con.execute("SELECT serial,identity_fingerprint FROM drives").fetchone() == (None, expected)
    first = con.execute("SELECT identity_proof,fence_proof FROM drive_clean_anchors").fetchone()
    assert json.loads(first[0])["serial"] is None
    assert json.loads(first[1])["serial"] == "ZR16L100"
    transport = fetch._observe_drive(con, "drive-a")
    assert transport.identity_proven and transport.refusal_code is None
    assert transport == bs._live_evidence(con, "drive-a").observation()
    ran = []
    with dm.drive_mutation(con, ["drive-a"], "fill-test", now="test",
                           observe=lambda label: fetch._observe_drive(con, label),
                           reconcile=lambda *a: ran.append("reconciled")):
        ran.append("wrote")
    assert ran == ["wrote", "reconciled"]
    assert con.execute("SELECT serial,identity_fingerprint,write_generation FROM drives").fetchone() == (
        None, expected, 2)
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 2


def test_serialless_inventory_still_refuses_changed_actual_disk_observation(archive, monkeypatch):
    con, (_, disk, _) = archive
    con.execute("UPDATE drives SET serial=NULL,identity_fingerprint=NULL,filesystem_capacity_bytes=NULL,"
                "write_authority='unknown'")
    before = tuple(con.iterdump())

    def inventory(*args, **kwargs):
        disk["serial"] = "different-after-inventory"
        return bs.Inventory([], [])

    monkeypatch.setattr(bs, "_require_complete_inventory", inventory)
    with pytest.raises(dm.DriveMutationRefused, match="DRIVE_IDENTITY_UNPROVEN"):
        bs.reconcile_drive(con, "drive-a", dedicated=True, now="test")
    assert tuple(con.iterdump()) == before


def _serialless(candidate):
    fingerprint = identity_fingerprint_v1(
        fs_uuid=candidate.drive.fs_uuid, annex_uuid=candidate.drive.annex_uuid, serial=None,
        filesystem_capacity_bytes=candidate.drive.filesystem_capacity_bytes)
    return replace(candidate, drive=replace(candidate.drive, serial=None, identity_fingerprint=fingerprint),
                   anchor=replace(candidate.anchor, identity_fingerprint=fingerprint))


def test_serialless_slice_preview_and_reader_keep_canonical_identity(attachment):
    root, original, observer, evidence = attachment
    candidate = _serialless(original)
    snapshot = replace(facts(domain), drives=(candidate.drive,), anchors=(candidate.anchor,))
    assert domain.preview(spec(domain), snapshot).source_ready
    assert evidence.serial == "serial-a"
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(20) == b"originaldata"
    assert candidate.drive.serial is None


def test_serialless_slice_still_refuses_mid_open_actual_serial_change(attachment, monkeypatch):
    root, candidate, observer, evidence = attachment
    calls = []

    def observe(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            evidence.serial = "replacement"
        return evidence

    monkeypatch.setattr(observer, "observe", observe)
    with pytest.raises(TransferRefusal, match="SOURCE_CHANGED"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(_serialless(candidate)):
            pytest.fail("changed device must not yield source bytes")
    assert len(calls) == 2, "test must reach the second physical observation"


def test_known_serial_slice_still_refuses_mismatch(attachment):
    root, candidate, observer, _ = attachment
    candidate = replace(candidate, drive=replace(candidate.drive, serial="different-canonical"))
    with pytest.raises(TransferRefusal, match="SOURCE_CHANGED"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            pytest.fail("known serial must remain mandatory")


@pytest.mark.parametrize("canonical", [None, ""])
@pytest.mark.parametrize("observed_serial", [None, "physical-serial"])
def test_optional_serial_selector_does_not_enrich(canonical, observed_serial):
    assert serial_for_identity(canonical, observed_serial) is None


@pytest.mark.parametrize("canonical,observed_serial", [
    (False, "physical-serial"), (1, None), (" whitespace ", "physical-serial"),
    (None, []), (None, " bad "), (None, False),
])
def test_invalid_serial_evidence_is_not_treated_as_optional(canonical, observed_serial):
    with pytest.raises(SerialIdentityUnproven):
        serial_for_identity(canonical, observed_serial)


def test_serialless_registration_still_requires_successful_physical_observation(archive, monkeypatch):
    from modelark import block_identity
    con, _ = archive
    con.execute("UPDATE drives SET serial=NULL")

    def fail(*args, **kwargs):
        raise PermissionError("no inventory access")

    monkeypatch.setattr(block_identity.subprocess, "run", fail)
    assert not bs._live_evidence(con, "drive-a").proven
    assert not fetch._observe_drive(con, "drive-a").identity_proven
