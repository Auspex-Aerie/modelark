"""Catalog reader-floor v8 preserves logical v7 Slice facts, not stale identities."""
from contextlib import contextmanager
from dataclasses import replace
import io
import sqlite3

import pytest

from modelark import drive_fence
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.slice import catalog, domain
from modelark.slice.sources import FencedSources
from modelark.slice.transaction import TransferRefusal
from test_slice_catalog import seed
from test_slice_domain import spec


def test_reader_floor_only_change_preserves_existing_plan_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    path = tmp_path / "catalog ? #.sqlite"
    con = seed(path, domain)
    try:
        before = catalog.read_catalog(path, spec(domain))
        proposal = domain.preview(spec(domain), before)
        approval = domain.approve(proposal, expected_seal=proposal.seal, current_snapshot=before)
        rows = tuple(con.iterdump())
        con.execute("PRAGMA user_version=8")
        bytes_before_read = path.read_bytes()
        after = catalog.read_catalog(path, spec(domain))
        fresh_proposal = domain.preview(spec(domain), after)
        assert after.schema_version == 7
        assert after == before and after.snapshot_id == before.snapshot_id
        assert fresh_proposal == proposal
        assert fresh_proposal.canonical_bytes() == proposal.canonical_bytes()
        assert domain.approve(proposal, expected_seal=proposal.seal, current_snapshot=after) == approval
        assert path.read_bytes() == bytes_before_read
        assert tuple(con.iterdump()) == rows
        assert con.execute("PRAGMA user_version").fetchone()[0] == 8

        # The already sealed source remains usable through the production fence
        # and fresh catalog gate; the injected byte reader performs no archive IO.
        class Reader:
            @contextmanager
            def open(self, candidate):
                assert candidate == proposal.closure[0].sources[0]
                yield io.BytesIO(b"source bytes")

        with FencedSources(path, Reader()).open(proposal.closure[0].sources[0]) as (snapshot, stream):
            assert snapshot == before and stream.read() == b"source bytes"
    finally:
        con.close()


@pytest.mark.parametrize("physical_version", [7, 8])
def test_pre_change_golden_snapshot_and_proposal_seals(physical_version, tmp_path):
    path = tmp_path / "catalog.sqlite"
    con = seed(path, domain)
    try:
        con.execute(f"PRAGMA user_version={physical_version}")
        snapshot = replace(catalog.read_catalog(path, spec(domain)),
                           catalog_id="file:///catalog-compatibility.sqlite")
        # Captured from the physical-v7 adapter before introducing the v8 mapping.
        assert snapshot.snapshot_id == "47d4a4568a76fe936a449a17e36633cc6f802c257910bcd288a864ecda8c81e4"
        assert domain.preview(spec(domain), snapshot).seal == (
            "894bd08d16592d93f08de1a9024c829f94c8770988ae0622f0ec4b63a9930e99")
    finally:
        con.close()


def test_physical_v8_is_not_a_new_logical_snapshot_layout(tmp_path):
    path = tmp_path / "catalog.sqlite"
    con = seed(path, domain)
    try:
        con.execute("PRAGMA user_version=8")
        snapshot = catalog.read_catalog(path, spec(domain))
        assert snapshot.schema_version == 7
        with pytest.raises(domain.SliceRefusal, match="INVALID_SNAPSHOT"):
            domain.preview(spec(domain), replace(snapshot, schema_version=8))
    finally:
        con.close()


@pytest.mark.parametrize("physical_version", [0, 1, 6, 9, 99])
def test_unsupported_physical_versions_refuse_without_touching_catalog(physical_version, tmp_path):
    path = tmp_path / "catalog.sqlite"
    con = seed(path, domain)
    con.execute(f"PRAGMA user_version={physical_version}")
    con.close()
    before = path.read_bytes()
    with pytest.raises(domain.SliceRefusal, match="CATALOG_VERSION_UNSUPPORTED"):
        catalog.read_catalog(path, spec(domain))
    assert path.read_bytes() == before
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == physical_version


@pytest.mark.parametrize("changed_field,value", [
    ("identity_fingerprint", "b" * 64),
    ("serial", "changed-serial"),
    ("write_authority", "unknown"),
])
def test_reader_floor_mapping_does_not_hide_changed_source_evidence(changed_field, value, tmp_path):
    path = tmp_path / "catalog.sqlite"
    con = seed(path, domain)
    try:
        before = catalog.read_catalog(path, spec(domain))
        proposal = domain.preview(spec(domain), before)
        con.execute("PRAGMA user_version=8")
        con.execute(f"UPDATE drives SET {changed_field}=? WHERE drive_label='drive-a'", [value])
        after = catalog.read_catalog(path, spec(domain))
        assert after.snapshot_id != before.snapshot_id
        assert domain.preview(spec(domain), after).seal != proposal.seal
        with pytest.raises(domain.SliceRefusal, match="PREVIEW_STALE"):
            domain.approve(proposal, expected_seal=proposal.seal, current_snapshot=after)
    finally:
        con.close()


def test_old_candidate_is_not_rebound_when_catalog_v8_identity_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    path = tmp_path / "catalog.sqlite"
    con = seed(path, domain)
    try:
        proposal = domain.preview(spec(domain), catalog.read_catalog(path, spec(domain)))
        con.execute("PRAGMA user_version=8")
        con.execute("UPDATE drives SET serial='changed-serial'")

        class Reader:
            def open(self, candidate):
                pytest.fail("changed source identity must refuse before byte reader opens")

        with pytest.raises(TransferRefusal, match="SOURCE_EVIDENCE_UNAVAILABLE"):
            with FencedSources(path, Reader()).open(proposal.closure[0].sources[0]):
                pytest.fail("stale sealed candidate must not be rebound")
    finally:
        con.close()


def test_new_valid_identity_generation_and_anchor_still_require_new_preview(tmp_path):
    path = tmp_path / "catalog.sqlite"
    con = seed(path, domain)
    try:
        before = catalog.read_catalog(path, spec(domain))
        proposal = domain.preview(spec(domain), before)
        new_fingerprint = identity_fingerprint_v1(
            fs_uuid="fs-a", annex_uuid="annex-a", serial="new-serial", filesystem_capacity_bytes=1000)
        con.execute("BEGIN IMMEDIATE")
        con.execute("PRAGMA user_version=8")
        con.execute("INSERT INTO drive_dirty_generations"
                    "(drive_label,identity_epoch,generation,operation_code) VALUES('drive-a',1,3,'fixture')")
        con.execute("UPDATE drives SET serial='new-serial',identity_fingerprint=?,write_generation=3",
                    [new_fingerprint])
        con.execute("INSERT INTO drive_clean_anchors"
                    "(drive_label,identity_epoch,generation,identity_fingerprint,filesystem_capacity_bytes,"
                    "anchor_free_bytes,write_authority,identity_proof,fence_proof,observed_at) "
                    "VALUES('drive-a',1,3,?,1000,900,'dedicated_local','fixture','fixture','2026-09-09')",
                    [new_fingerprint])
        con.execute("COMMIT")
        after = catalog.read_catalog(path, spec(domain))
        fresh_proposal = domain.preview(spec(domain), after)
        assert fresh_proposal.source_ready
        assert fresh_proposal.seal != proposal.seal
        assert before.anchors[0].anchor_id != after.anchors[0].anchor_id
        assert before.drives[0].write_generation != after.drives[0].write_generation
        with pytest.raises(domain.SliceRefusal, match="PREVIEW_STALE"):
            domain.approve(proposal, expected_seal=proposal.seal, current_snapshot=after)
    finally:
        con.close()
