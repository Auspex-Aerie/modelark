"""DEF-035 Gate-1 contracts — installed provenance migrate command.

Contracts only. Production is unchanged, so c01/c02/c03/c04/c04b/c06 stay
red until Gate 2 adds ``modelark-provenance-migrate`` and drops the
``writers_stopped`` default.
"""
from __future__ import annotations

import importlib.util
import inspect
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db


_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
_TOKEN = "MODELARK-STOPPED"


def _project_scripts() -> dict[str, str]:
    lines = _PYPROJECT.read_text(encoding="utf-8").splitlines()
    in_sec = False
    out: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_sec = stripped == "[project.scripts]"
            continue
        if in_sec and "=" in stripped and not stripped.startswith("#"):
            key, value = stripped.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _load_main():
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_provenance.py"
    assert path.is_file(), f"need {path} for modelark-provenance-migrate"
    spec = importlib.util.spec_from_file_location("modelark_migrate_provenance", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    main = getattr(mod, "main", None)
    assert callable(main), "scripts/migrate_provenance.py must export main"
    return main


@contextmanager
def _no_catalog_bind():
    def boom(*_a, **_k):
        raise AssertionError(
            "provenance-migrate must not call db.configure or db.connect"
        )
    with mock.patch.object(db, "configure", side_effect=boom), mock.patch.object(
        db, "connect", side_effect=boom
    ):
        yield


def test_c01_console_script_maps_provenance_migrate():
    """Installed script must wrap scripts.migrate_provenance:main."""
    scripts = _project_scripts()
    assert "modelark-provenance-migrate" in scripts, (
        f"pyproject.toml [project.scripts] missing modelark-provenance-migrate; "
        f"got {sorted(scripts)}"
    )
    assert scripts["modelark-provenance-migrate"] == "scripts.migrate_provenance:main"


def test_c02_argparse_subcommands_and_confirm_token(tmp_path):
    """rehearse needs no token; publish without token is refused; rehearse rejects token."""
    src, work, dest = tmp_path / "src", tmp_path / "work", tmp_path / "dest"
    src.mkdir()
    work.mkdir()
    dest.mkdir()

    with _no_catalog_bind():
        main = _load_main()
        with mock.patch.object(
            db, "rehearse_provenance_migration", return_value={"status": "ok"}
        ):
            rc = main([
                "rehearse",
                "--source-data-dir", str(src),
                "--work-dir", str(work),
                "--run-id", "g1",
            ])
        assert rc == 0

        with mock.patch.object(db, "publish_provenance_migration") as pub:
            with pytest.raises(SystemExit) as missing:
                main([
                    "publish",
                    "--work-dir", str(work),
                    "--dest-dir", str(dest),
                ])
            assert missing.value.code not in (0, None)
            pub.assert_not_called()
            with pytest.raises(SystemExit) as wrong:
                main([
                    "publish",
                    "--work-dir", str(work),
                    "--dest-dir", str(dest),
                    "--confirm-stopped", "WRONG",
                ])
            assert wrong.value.code not in (0, None)
            pub.assert_not_called()

        with pytest.raises(SystemExit) as rehearse_token:
            main([
                "rehearse",
                "--source-data-dir", str(src),
                "--work-dir", str(work),
                "--run-id", "g1",
                "--confirm-stopped", _TOKEN,
            ])
        assert rehearse_token.value.code not in (0, None)


def test_c03_rehearse_calls_helper_without_configure(tmp_path):
    """Rehearse passes explicit paths and must not call db.configure."""
    src, work = tmp_path / "src", tmp_path / "work"
    src.mkdir()
    work.mkdir()
    with _no_catalog_bind():
        main = _load_main()
        with mock.patch.object(
            db, "rehearse_provenance_migration", return_value={"status": "ok"}
        ) as reh:
            rc = main([
                "rehearse",
                "--source-data-dir", str(src),
                "--work-dir", str(work),
                "--run-id", "g1",
            ])
        assert rc == 0
        reh.assert_called_once()
        args, kwargs = reh.call_args
        assert Path(args[0]) == src
        assert Path(args[1]) == work
        assert kwargs.get("run_id") == "g1"


def test_c04_publish_calls_helper_with_token_without_configure(tmp_path):
    """Publish passes MODELARK-STOPPED and writers_stopped=True; no configure."""
    work, dest = tmp_path / "work", tmp_path / "dest"
    work.mkdir()
    dest.mkdir()
    with _no_catalog_bind():
        main = _load_main()
        with mock.patch.object(
            db, "publish_provenance_migration", return_value={"status": "ok"}
        ) as pub:
            rc = main([
                "publish",
                "--work-dir", str(work),
                "--dest-dir", str(dest),
                "--confirm-stopped", _TOKEN,
            ])
            assert rc == 0
            pub.assert_called_once()
            args, kwargs = pub.call_args
            assert Path(args[0]) == work
            assert Path(args[1]) == dest
            assert kwargs.get("confirm_stopped") == _TOKEN
            assert kwargs.get("writers_stopped") is True
        with mock.patch.object(db, "publish_provenance_migration") as pub2:
            with pytest.raises(SystemExit) as extra:
                main([
                    "publish",
                    "--work-dir", str(work),
                    "--dest-dir", str(dest),
                    "--confirm-stopped", _TOKEN,
                    "--source-data-dir", str(tmp_path / "src"),
                ])
            assert extra.value.code not in (0, None)
            pub2.assert_not_called()


def test_c04b_dest_symlink_to_source_parent_is_refused_before_staging(tmp_path):
    """Dest that resolve()s to the reported source parent must not occupancy-refuse."""
    src = tmp_path / "src"
    src.mkdir()
    source_cat = src / "catalog.sqlite"
    source_cat.write_bytes(b"def035-src")
    dest = tmp_path / "dest-link"
    dest.symlink_to(src)
    work = tmp_path / "work"
    work.mkdir()
    fake_layout = (
        work.resolve(),
        {"status": "ok", "manifest_status": "validated"},
        work / "manifest.json",
        work / "snapshot" / "catalog.sqlite",
        work / "clone" / "catalog.sqlite",
        source_cat,
    )
    error = None
    with mock.patch.object(db, "_resolve_rehearsal_layout", return_value=fake_layout), \
            mock.patch.object(db, "_publish_staging_no_clobber") as stage:
        try:
            db.publish_provenance_migration(
                work, dest, confirm_stopped=_TOKEN, writers_stopped=True
            )
        except RuntimeError as exc:
            error = exc
    assert error is not None, "dest identity equal to source parent must be refused"
    assert "destination resolves to the rehearsal source" in str(error).lower(), error
    stage.assert_not_called()


def test_c05_legacy_migrate_script_unchanged():
    """modelark-migrate remains the legacy-runtime cutover."""
    scripts = _project_scripts()
    assert scripts.get("modelark-migrate") == "scripts.migrate_legacy_runtime:main"


def test_c06_writers_stopped_has_no_default():
    """publish_provenance_migration must require writers_stopped."""
    sig = inspect.signature(db.publish_provenance_migration)
    param = sig.parameters["writers_stopped"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, param.kind
    assert param.default is inspect.Parameter.empty, (
        "writers_stopped must be a required keyword (no default); "
        f"got default={param.default!r}"
    )
    sig.bind(Path("."), Path("."), confirm_stopped=_TOKEN, writers_stopped=True)
    with pytest.raises(TypeError, match="writers_stopped"):
        sig.bind(Path("."), Path("."), confirm_stopped=_TOKEN)
