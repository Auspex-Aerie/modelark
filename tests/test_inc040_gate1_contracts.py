"""INC-040 Gate-1: proposal and library previews share one fail-closed admission authority.

The proposal preview must not call legacy ``drives.free_bytes`` feasible when canonical
reconciliation has no identity-bound capacity evidence for the same immutable catalog snapshot.
"""
from __future__ import annotations

import sqlite3

from modelark import librarian, proposal
from modelark.core import db


def _unknown_capacity_catalog() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute(
        "INSERT INTO plans(plan_id,name,capacity_mode,is_active) "
        "VALUES('ark','Ark','guaranteed',1)"
    )
    # Deliberately tempting legacy scalar, but no identity fingerprint, live observation,
    # dedicated-local authority, or clean anchor. Canonical admission must call this unknown.
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,lifecycle,eligibility) "
        "VALUES('drive-unknown',1000000,1000000,'active','enabled')"
    )
    con.execute(
        "INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','drive-unknown')"
    )
    con.execute(
        "INSERT INTO models(repo_id,category,numcopies) "
        "VALUES('org/model','generative-llm',1)"
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/model','model.safetensors',100,'safetensors','bf16',?)",
        ["a" * 64],
    )
    con.execute(
        "INSERT INTO selection(repo_id,finalized_at) "
        "VALUES('org/model','2026-01-01 00:00:00')"
    )
    return con


def test_inc040_proposal_preview_cannot_greenlight_unknown_canonical_capacity():
    con = _unknown_capacity_catalog()
    canonical = librarian.plan_view(con, plan_id="ark", capacity_mode="guaranteed")
    proposed = proposal.preview_pure(con, "ark", ("adopt_current", ()))

    assert canonical["feasible"] is False
    assert "CAPACITY_EVIDENCE_UNKNOWN" in canonical["blocking_diagnostics"]
    assert canonical["totals"]["n_planned"] == 0

    # Expected red for INC-040: current proposal.preview_pure separately subtracts a
    # safety floor from free_bytes and returns FEASIBLE with an executable task.
    assert proposed["header"]["gate_b_code"] != "FEASIBLE", proposed["header"]
    assert not any(task["row_kind"] == "executable" for task in proposed["tasks"]), proposed["tasks"]
    con.close()
