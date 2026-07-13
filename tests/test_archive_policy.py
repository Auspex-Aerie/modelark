"""Archive planning must never silently turn an unsupported/pickle-only repo into aux files."""
from __future__ import annotations

import sqlite3
from unittest import mock

from modelark import fetch, plan
from modelark.core import db


def _catalog():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO models(repo_id) VALUES('org/model')")
    return con


def _file(con, name, size, fmt, quant=None):
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES('org/model',?,?,?,?)",
        [name, size, fmt, quant],
    )


def test_pickle_only_repo_is_blocked_instead_of_planning_aux_files():
    con = _catalog()
    _file(con, "pytorch_model.bin", 100, "pytorch", "fp16")
    _file(con, "config.json", 10, "aux")
    with mock.patch.object(fetch.wishlist, "exclude_pickle_only", return_value=True):
        try:
            fetch.plan(con, "org/model")
            raise AssertionError("pickle-only archive planning should be blocked")
        except fetch.ArchivePolicyError as exc:
            assert "pickle-only weights are blocked" in str(exc)


def test_explicit_pickle_opt_in_archives_weights_as_inert_raw_bytes():
    con = _catalog()
    _file(con, "pytorch_model.bin", 100, "pytorch", "fp16")
    _file(con, "config.json", 10, "aux")
    with mock.patch.object(fetch.wishlist, "exclude_pickle_only", return_value=False):
        files = fetch.plan(con, "org/model")
    assert {f["rfilename"]: f["mode"] for f in files} == {
        "pytorch_model.bin": "raw", "config.json": "raw"
    }


def test_safe_weights_take_precedence_over_pickle_copies():
    con = _catalog()
    _file(con, "model.safetensors", 80, "safetensors", "bf16")
    _file(con, "pytorch_model.bin", 100, "pytorch", "fp16")
    _file(con, "config.json", 10, "aux")
    with mock.patch.object(fetch.wishlist, "exclude_pickle_only", return_value=True):
        files = fetch.plan(con, "org/model")
    assert {f["rfilename"]: f["mode"] for f in files} == {
        "model.safetensors": "compress", "config.json": "raw"
    }


def test_restore_planning_can_recover_an_existing_pickle_archive_after_policy_tightens():
    con = _catalog()
    _file(con, "pytorch_model.bin", 100, "pytorch", "fp16")
    _file(con, "config.json", 10, "aux")
    with mock.patch.object(fetch.wishlist, "exclude_pickle_only", return_value=True):
        files = fetch.plan(con, "org/model", allow_pickle=True)
    assert {f["rfilename"] for f in files} == {"pytorch_model.bin", "config.json"}


def test_unsupported_weight_repo_fails_loudly():
    con = _catalog()
    _file(con, "model.onnx", 100, "onnx")
    _file(con, "config.json", 10, "aux")
    try:
        fetch.plan(con, "org/model")
        raise AssertionError("an unsupported weight format must not produce an aux-only plan")
    except fetch.ArchivePolicyError as exc:
        assert "no supported archive weights" in str(exc) and "onnx" in str(exc)


def test_capacity_footprint_matches_pickle_policy():
    con = _catalog()
    _file(con, "pytorch_model.bin", 100, "pytorch", "fp16")
    _file(con, "config.json", 10, "aux")
    with mock.patch.object(plan.wishlist, "exclude_pickle_only", return_value=True):
        assert plan._footprint_by_repo(con, ["org/model"]) == {}
    with mock.patch.object(plan.wishlist, "exclude_pickle_only", return_value=False):
        assert plan._footprint_by_repo(con, ["org/model"])["org/model"] == (110, 0, 110)


if __name__ == "__main__":
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
            print(f"ok  {name}")
    print("all passed")
