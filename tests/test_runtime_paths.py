"""Installed/runtime path contract: writable state is injectable and packaged defaults are usable."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import yaml

from modelark import wishlist
from modelark.core import db


def test_configure_isolates_data_state_and_logs(tmp_path):
    data, state = tmp_path / "data", tmp_path / "state"
    db.configure(data, state)
    assert db.DB_PATH == data.resolve() / "catalog.sqlite"
    assert db.STATE_DIR == state.resolve()
    cfg = wishlist.logging_config()
    assert Path(cfg["file_path"]).is_relative_to(state.resolve())
    con = db.connect(_bootstrapping=True)
    con.close()
    assert db.DB_PATH.is_file()


def test_explicit_config_wins(tmp_path):
    config = tmp_path / "custom.yaml"
    config.write_text("download:\n  max_24h_gb: 7\n")
    wishlist.configure(config)
    assert wishlist.download()["max_24h_gb"] == 7
    wishlist.configure(None)


def test_packaged_default_works_without_source_checkout(tmp_path):
    wishlist.configure(None)
    with mock.patch.object(db, "REPO_ROOT", tmp_path), \
            mock.patch.object(wishlist, "_user_config_path", return_value=tmp_path / "missing.yaml"):
        source = wishlist.config_source()
        assert source.name == "default_wishlist.yaml"
        assert wishlist.download()["max_24h_gb"] == 1000
        assert wishlist.compression()["threads"] == 1


def test_packaged_default_matches_source_policy(tmp_path):
    packaged = wishlist.config_source()
    source = db.REPO_ROOT / "wishlist.yaml"
    assert yaml.safe_load(packaged.read_text()) == yaml.safe_load(source.read_text())


def test_legacy_repo_catalog_is_never_silently_replaced(tmp_path):
    legacy_root, new_data = tmp_path / "checkout", tmp_path / "xdg"
    legacy = legacy_root / "catalog" / "catalog.sqlite"
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    db.configure(new_data)
    with mock.patch.object(db, "REPO_ROOT", legacy_root):
        try:
            db.connect()
            raise AssertionError("legacy catalog must require an explicit migration choice")
        except RuntimeError as e:
            assert "Legacy repo-local catalog" in str(e) and "--data-dir" in str(e), e
    assert not db.DB_PATH.exists()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))
            print(f"ok  {name}")
    print("all passed")
