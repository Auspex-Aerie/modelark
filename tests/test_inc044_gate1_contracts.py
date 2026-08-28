"""INC-044 Gate-1: unknown-capacity diagnostics use canonical placement candidates.

Lost/excluded plan members remain visible in historical drive ledgers, but they are not placement
targets.  A fail-closed capacity projection must therefore never label them ``eligible_drives`` or
attach a mount/reconcile recovery action to their retired placement authority.
"""
from __future__ import annotations

import sqlite3

from modelark import librarian
from modelark.core import db


def _catalog_with_lost_and_placeable_unknown_drives() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute(
        "INSERT INTO plans(plan_id,name,capacity_mode,is_active) "
        "VALUES('ark','Ark','guaranteed',1)"
    )
    con.executemany(
        "INSERT INTO drives("
        "drive_label,role,capacity_bytes,free_bytes,lifecycle,eligibility"
        ") VALUES(?,?,?,?,?,?)",
        [
            ("drive-active", "primary", 1_000_000, 1_000_000, "active", "enabled"),
            ("drive-lost", "primary", 2_000_000, 2_000_000, "lost", "excluded"),
        ],
    )
    con.executemany(
        "INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)",
        [("drive-active",), ("drive-lost",)],
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


def test_inc044_unknown_failure_rows_exclude_lost_historical_membership():
    con = _catalog_with_lost_and_placeable_unknown_drives()
    plan = librarian.plan_view(con, plan_id="ark", capacity_mode="guaranteed")

    assert plan["feasible"] is False
    assert plan["blocking_diagnostics"] == ["CAPACITY_EVIDENCE_UNKNOWN"]
    assert not any(item["planned_bytes"] for item in plan["drives"])

    by_label = {item["label"]: item for item in plan["drives"]}
    assert by_label["drive-lost"]["lifecycle"] == "lost"
    assert by_label["drive-lost"]["eligibility"] == "excluded"
    assert by_label["drive-lost"]["usable"] == 0

    failure_labels = {
        label
        for failure in plan["capacity_failures"]
        for label in failure["eligible_drives"]
    }
    assert failure_labels == {"drive-active"}, plan["capacity_failures"]
    assert all(
        "drive-lost" not in failure["eligible_drives"]
        for failure in plan["capacity_failures"]
    )
    capacity_advisory = next(
        item for item in plan["advisories"]
        if item["code"] == "CAPACITY_EVIDENCE_UNKNOWN"
    )
    assert capacity_advisory["count"] == 1
    con.close()
