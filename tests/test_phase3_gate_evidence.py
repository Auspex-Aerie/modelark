"""Sanitized, non-mutating evidence harness for the DEC-045 Phase 3 gates."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from modelark.core import db

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import phase3_gate_evidence as evidence


def _safetensors(path, dtype="BF16", payload_bytes=256 * 1024):
    header = json.dumps({
        "weight": {
            "dtype": dtype,
            "shape": [payload_bytes // 2],
            "data_offsets": [0, payload_bytes],
        }
    }, separators=(",", ":")).encode()
    path.write_bytes(len(header).to_bytes(8, "little") + header + (b"\x01\x3f" * (payload_bytes // 2)))
    return path


def _catalog(path):
    con = sqlite3.connect(path, isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) "
        "VALUES('primary','primary',0,10000000000,10000000000)"
    )
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','primary')")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/model',1)")
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/model','2026-01-01')")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/model','model.safetensors',100,'safetensors','bf16')"
    )
    con.close()
    return path


def test_real_bf16_measurement_is_bounded_sanitized_and_removes_temp(tmp_path):
    shard = _safetensors(tmp_path / "private-name.safetensors")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = evidence.measure_streamznn(shard, scratch, chunk_bytes=64 * 1024)
    assert result["passed"] is True
    assert result["roundtrip_sha256_verified"] is True
    assert result["filesystem_high_water_bytes"] <= result["enforced_cap_bytes"]
    assert "private-name" not in json.dumps(result)
    assert list(scratch.iterdir()) == []


def test_real_bf16_measurement_rejects_non_bf16_input(tmp_path):
    shard = _safetensors(tmp_path / "fp16.safetensors", dtype="F16")
    with pytest.raises(evidence.GateEvidenceError, match="no BF16"):
        evidence.measure_streamznn(shard, tmp_path)


def test_catalog_replay_is_sanitized_bounded_and_leaves_source_unchanged(tmp_path):
    catalog = _catalog(tmp_path / "catalog.sqlite")
    before = catalog.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = evidence.measure_catalog_replay(catalog, scratch, samples=2)
    assert result["passed"] is True
    assert result["source_opened_uri_mode_ro"] is True
    assert result["concurrent_writer_read_succeeded"] is True
    assert result["selected_repositories"] == 1
    assert "org/model" not in json.dumps(result)
    assert catalog.read_bytes() == before
    assert list(scratch.iterdir()) == []
