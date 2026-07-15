"""Schema-v2 capacity-mode API plus one-release provisioning compatibility."""
from __future__ import annotations

import sqlite3
from unittest import mock

from modelark import plan
from modelark.core import db
from modelark.web import data, plan_api


def _mem() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    return con


def test_api_emits_canonical_mode_and_deprecated_output_alias():
    con = _mem()
    plan.bootstrap(con)
    with mock.patch.object(data, "conn", return_value=con):
        overview = plan_api.overview()
        row = overview["plans"][0]
        assert row["capacity_mode"] == "guaranteed"
        assert row["provisioning"] == "uncompressed"
        assert row["deprecated_fields"] == ["provisioning"]

        changed = plan_api.set_capacity_mode({
            "plan_id": "ark", "capacity_mode": "compression_aware",
        })
        assert changed["capacity_mode"] == "compression_aware"
        assert changed["provisioning"] == "compressed"
        assert "warnings" not in changed


def test_deprecated_api_inputs_map_and_warn_for_one_release():
    con = _mem()
    plan.bootstrap(con)
    with mock.patch.object(data, "conn", return_value=con):
        created = plan_api.create({
            "plan_id": "legacy", "name": "Legacy", "provisioning": "compressed",
        })
        assert created["ok"] and created["capacity_mode"] == "compression_aware"
        assert created["provisioning"] == "compressed"
        assert created["warnings"]

        changed = plan_api.set_provisioning({"plan_id": "legacy", "mode": "uncompressed"})
        assert changed["capacity_mode"] == "guaranteed"
        assert changed["provisioning"] == "uncompressed"
        assert any("deprecated" in warning for warning in changed["warnings"])

        conflict = plan_api.create({
            "plan_id": "conflict", "capacity_mode": "guaranteed",
            "provisioning": "compressed",
        })
        assert conflict["ok"] is False and "disagree" in conflict["error"]


def test_invalid_capacity_mode_is_a_bounded_api_error():
    con = _mem()
    plan.bootstrap(con)
    with mock.patch.object(data, "conn", return_value=con):
        created = plan_api.create({
            "plan_id": "bad", "capacity_mode": "compressed_storage_codec",
        })
        assert not created["ok"] and "capacity_mode must be" in created["error"]

        changed = plan_api.set_capacity_mode({
            "plan_id": "ark", "capacity_mode": "compressed_storage_codec",
        })
        assert not changed["ok"] and "capacity_mode must be" in changed["error"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
