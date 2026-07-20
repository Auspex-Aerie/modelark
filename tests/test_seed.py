"""Catalog seed import (onboarding): the bundled starter catalog hydrates a fresh install offline."""
from __future__ import annotations

import json
from functools import wraps
from pathlib import Path
from unittest import mock

from modelark import seed
from modelark.core import db


def _isolated_catalog(fn):
    @wraps(fn)
    def wrapped(tmp_path):
        old_data, old_state = db.CATALOG_DIR, db.STATE_DIR
        try:
            db.configure(tmp_path / "data", tmp_path / "state")
            return fn(tmp_path)
        finally:
            db.configure(old_data, old_state)
    return wrapped


def _export(tmp_path, rows) -> Path:
    p = tmp_path / "models.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


@_isolated_catalog
def test_import_seeds_fresh_catalog(tmp_path):
    src = _export(tmp_path, [
        {"repo_id": "org/a", "category": "generative-llm", "total_size_bytes": 10},
        {"repo_id": "org/b", "category": "embedding", "total_size_bytes": 20},
    ])
    con = db.connect()
    try:
        result = seed.import_catalog(con, source=src)
        assert result == {"imported": 2, "skipped": 0, "source": str(src), "total_models": 2}
        rows = dict(con.execute("SELECT repo_id, category FROM models ORDER BY repo_id").fetchall())
        assert rows == {"org/a": "generative-llm", "org/b": "embedding"}
    finally:
        con.close()


@_isolated_catalog
def test_import_is_insert_only_by_default(tmp_path):
    # A locally richer row must NOT be downgraded by a re-import; missing rows are still added.
    con = db.connect()
    try:
        con.execute("INSERT INTO models (repo_id, category, notes) VALUES ('org/a','generative-llm','local')")
        src = _export(tmp_path, [
            {"repo_id": "org/a", "category": "generative-llm", "notes": "seed"},
            {"repo_id": "org/c", "category": "encoder"},
        ])
        result = seed.import_catalog(con, source=src)
        assert result["imported"] == 1 and result["skipped"] == 1 and result["total_models"] == 2
        assert con.execute("SELECT notes FROM models WHERE repo_id='org/a'").fetchone()[0] == "local"
    finally:
        con.close()


@_isolated_catalog
def test_import_refresh_overwrites_existing(tmp_path):
    con = db.connect()
    try:
        con.execute("INSERT INTO models (repo_id, category, notes) VALUES ('org/a','generative-llm','local')")
        src = _export(tmp_path, [{"repo_id": "org/a", "category": "generative-llm", "notes": "seed"}])
        result = seed.import_catalog(con, source=src, overwrite=True)
        assert result["imported"] == 1 and result["skipped"] == 0
        assert con.execute("SELECT notes FROM models WHERE repo_id='org/a'").fetchone()[0] == "seed"
    finally:
        con.close()


@_isolated_catalog
def test_import_ignores_unknown_columns(tmp_path):
    # Forward/backward-compatible: an export column the current schema lacks is dropped, not fatal.
    src = _export(tmp_path, [{"repo_id": "org/a", "category": "encoder", "future_column": "x"}])
    con = db.connect()
    try:
        result = seed.import_catalog(con, source=src)
        assert result["imported"] == 1
    finally:
        con.close()


def test_seed_source_prefers_checkout_then_packaged(tmp_path):
    # With no source checkout, the packaged wheel resource is used so installed users can seed offline.
    with mock.patch.object(db, "REPO_ROOT", tmp_path):     # tmp_path has no catalog/export/models.jsonl
        src = seed.seed_source()
    assert str(src).endswith("catalog_seed.jsonl")
    assert src.read_text(encoding="utf-8").strip(), "packaged seed must be present and non-empty"


def test_packaged_seed_matches_committed_export():
    # Guard against drift: the wheel seed is a copy of the committed export. Only meaningful in a
    # source checkout (installed wheels have no repo-root export); skip cleanly otherwise.
    checkout = db.REPO_ROOT / "catalog" / "export" / "models.jsonl"
    packaged = db.REPO_ROOT / "modelark" / "data" / "catalog_seed.jsonl"
    if not (checkout.is_file() and packaged.is_file()):
        return
    assert packaged.read_bytes() == checkout.read_bytes(), (
        "modelark/data/catalog_seed.jsonl is stale — regenerate it: "
        "cp catalog/export/models.jsonl modelark/data/catalog_seed.jsonl"
    )
