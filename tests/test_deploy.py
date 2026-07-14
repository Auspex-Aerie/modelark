#!/usr/bin/env python3
"""Minimal deploy surface: deterministic unit rendering and no-write dry runs."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import SkipTest, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import deploy

def test_unit_is_unprivileged_explicit_and_resume_is_opt_in(tmp_path):
    source = tmp_path / "source with space"
    executable = source / ".venv/bin/modelark"
    data = tmp_path / "data"
    state = tmp_path / "state"
    unit = deploy.render_unit(source, executable, data, state, None, 8077, False)
    assert "User=" not in unit and "Group=" not in unit
    assert "WantedBy=default.target" in unit
    assert "WorkingDirectory=" in unit and "source\\x20with\\x20space" in unit
    assert f'"{data}"' in unit and f'"{state}"' in unit
    assert " --resume" not in unit and '"--resume"' not in unit
    assert "@WORKING_DIRECTORY@" not in unit and "@EXEC_START@" not in unit

    resumed = deploy.render_unit(source, executable, data, state, None, 8077, True)
    assert '"--resume"' in resumed


def test_unit_quotes_percent_and_explicit_config(tmp_path):
    source = tmp_path / "source%tree"
    config = tmp_path / 'config "public".yaml'
    unit = deploy.render_unit(
        source, source / ".venv/bin/modelark", tmp_path / "data", tmp_path / "state",
        config, 18077, False)
    assert "source%%tree" in unit
    assert "config \\\"public\\\".yaml" in unit
    assert '"--config"' in unit and '"18077"' in unit


def test_acceptance_validates_unit_identity_and_runtime_paths(tmp_path):
    source = tmp_path / "checkout"
    executable = source / ".venv/bin/modelark"
    data = tmp_path / "data"
    state = tmp_path / "state"
    config = tmp_path / "wishlist.yaml"
    unit_path = tmp_path / "modelark.service"
    unit_path.write_text(deploy.render_unit(
        source, executable, data, state, config, 18077, False))
    deploy._validate_unit(unit_path, source, executable, data, state, config, 18077)
    try:
        deploy._validate_unit(unit_path, source, executable, tmp_path / "wrong-data",
                              state, config, 18077)
        raise AssertionError("mismatched runtime paths must fail deployment acceptance")
    except RuntimeError as exc:
        assert "does not match" in str(exc)


def test_atomic_unit_write_and_private_runtime_dirs(tmp_path):
    unit_path = tmp_path / "config/systemd/user/modelark.service"
    deploy._mkdir_private(tmp_path / "data", False)
    deploy._mkdir_private(tmp_path / "state", False)
    deploy._write_unit(unit_path, "first\n", False)
    deploy._write_unit(unit_path, "second\n", False)
    assert unit_path.read_text() == "second\n"
    assert unit_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700


def test_legacy_catalog_cannot_be_silently_ignored(tmp_path):
    source = tmp_path / "checkout"
    legacy = source / "catalog"
    legacy.mkdir(parents=True)
    (legacy / "catalog.sqlite").touch()
    destination = tmp_path / "xdg-data"
    try:
        deploy._guard_legacy_catalog(source, destination)
        raise AssertionError("an empty destination must not hide checkout-local data")
    except RuntimeError as exc:
        assert "has no valid migrated catalog.sqlite" in str(exc)

    deploy._guard_legacy_catalog(source, legacy)
    destination.mkdir()
    with sqlite3.connect(destination / "catalog.sqlite") as con:
        con.execute("CREATE TABLE models(repo_id TEXT PRIMARY KEY)")
    deploy._guard_legacy_catalog(source, destination)


def test_dry_run_creates_nothing(tmp_path, capsys):
    home = tmp_path / "home"
    source = ROOT
    venv = tmp_path / "new-venv"
    data = tmp_path / "new-data"
    state = tmp_path / "new-state"
    with mock.patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
    }, clear=False), mock.patch.object(Path, "home", return_value=home):
        deploy.main([
            "--source", str(source), "--venv", str(venv),
            "--data-dir", str(data), "--state-dir", str(state),
            "--enable", "--start", "--resume-fill", "--dry-run",
        ])
    out = capsys.readouterr().out
    assert "resume:  enabled" in out
    assert "systemctl --user enable modelark.service" in out
    assert "systemctl --user restart modelark.service" in out
    assert not venv.exists() and not data.exists() and not state.exists() and not home.exists()


def test_standalone_dry_run(tmp_path):
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "home/.config"),
        "XDG_DATA_HOME": str(tmp_path / "home/.local/share"),
        "XDG_STATE_HOME": str(tmp_path / "home/.local/state"),
    })
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/deploy.py"), "--source", str(ROOT), "--dry-run"],
        env=env, text=True, capture_output=True, check=True)
    assert "ModelArk deployment plan" in result.stdout
    assert not (tmp_path / "home").exists()


def test_setup_wrapper_delegates_without_writes(tmp_path):
    env = dict(os.environ)
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "home/.config"),
        "XDG_DATA_HOME": str(tmp_path / "home/.local/share"),
        "XDG_STATE_HOME": str(tmp_path / "home/.local/state"),
    })
    result = subprocess.run(
        [str(ROOT / "scripts/setup.sh"), "--venv", str(tmp_path / "venv"), "--dry-run"],
        env=env, text=True, capture_output=True, check=True)
    assert "delegates to the unprivileged deploy surface" in result.stderr
    assert "ModelArk deployment plan" in result.stdout
    assert not (tmp_path / "home").exists() and not (tmp_path / "venv").exists()


def test_rendered_unit_passes_systemd_verify(tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        raise SkipTest("systemd-analyze is not installed")
    unit_path = tmp_path / "modelark.service"
    unit_path.write_text(deploy.render_unit(
        ROOT, Path(sys.executable), tmp_path / "data", tmp_path / "state",
        None, 18077, False))
    result = subprocess.run(
        [analyzer, "verify", str(unit_path)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            if "capsys" in fn.__code__.co_varnames:
                # The pytest-only output assertion has equivalent coverage in the subprocess test.
                print(f"skip {name} (pytest capture fixture)")
                continue
            try:
                fn(path)
                print(f"ok  {name}")
            except SkipTest as exc:
                print(f"skip {name}: {exc}")
    print("all passed")
