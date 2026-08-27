"""DEF-038 Gate-1 contracts — leftover list CLI (dispose deferred).

Contracts only. Production CLI still has rehearse/publish only, so leftovers
cases stay red until Gate 2. leftovers-dispose must remain absent.
"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from test_dec053_054_gate2_remediation import _rehearse_ok


_RESERVED = ".catalog.sqlite.publish-staging"
_SIDECARS = ("-wal", "-shm", "-journal")
_KW = {"confirm_stopped": "MODELARK-STOPPED", "writers_stopped": True}
_ALLOWED_LIVE = frozenset(
    {"catalog.sqlite", _RESERVED, *(_RESERVED + s for s in _SIDECARS)}
)


def _load_main():
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_provenance.py"
    spec = importlib.util.spec_from_file_location("modelark_migrate_provenance", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    main = getattr(mod, "main", None)
    assert callable(main)
    return main


@contextmanager
def _no_catalog_bind():
    def boom(*_a, **_k):
        raise AssertionError("leftovers must not call db.configure or db.connect")
    with mock.patch.object(db, "configure", side_effect=boom), mock.patch.object(
        db, "connect", side_effect=boom
    ):
        yield


def _run_root(work: Path) -> Path:
    return work / "pub"


def _state_path(work: Path) -> Path:
    return _run_root(work) / db.PUBLICATION_SLOT_STATE_NAME


def _snapshot(dest: Path) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    if not dest.exists():
        return out
    for path in dest.iterdir():
        st = path.lstat()
        out[path.name] = (st.st_ino, st.st_size, st.st_mtime_ns)
    return out


def _leftovers(main, work: Path, dest: Path | None) -> tuple[int, dict | None, str]:
    argv = ["leftovers", "--work-dir", str(work)]
    if dest is not None:
        argv.extend(["--dest-dir", str(dest)])
    buf = StringIO()
    err = StringIO()
    with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    text = buf.getvalue()
    payload = None
    try:
        payload = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = None
    return int(rc or 0), payload, err.getvalue() + text


def test_c01_leftovers_exists_without_catalog_bind_or_dest_create(tmp_path):
    """leftovers must exist, not bind the catalog, and not create dest."""
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest"
    before = dest.exists()
    main = _load_main()
    with _no_catalog_bind():
        rc, _payload, err = _leftovers(main, work, dest)
    assert dest.exists() is before
    assert "invalid choice" not in err.lower(), err
    assert rc in (0, 1)


def test_c02_occupied_leftover_listed_absent_from_dest(tmp_path):
    """After occupied leftover refuse, list shows the reserved leftover and dest absent."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"def038-occupied")
    try:
        db.publish_provenance_migration(work, dest, **_KW)
    except RuntimeError:
        pass
    main = _load_main()
    with _no_catalog_bind():
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    assert isinstance(payload, dict)
    live = payload.get("live") or payload.get("observations") or []
    names = {Path(item["path"]).name if isinstance(item, dict) else "" for item in live}
    if isinstance(live, dict):
        names = set(live)
    assert _RESERVED in names or any(
        isinstance(item, dict) and Path(item.get("path", "")).name == _RESERVED
        for item in (live if isinstance(live, list) else live.values())
    )
    leftover_row = None
    rows = live if isinstance(live, list) else [
        {"path": k, **(v if isinstance(v, dict) else {})} for k, v in live.items()
    ]
    for item in rows:
        if Path(item.get("path", "")).name == _RESERVED:
            leftover_row = item
            break
    assert leftover_row is not None
    rel = leftover_row.get("dest_relation") or leftover_row.get("live_dest_relation")
    assert rel == "absent"
    assert int(leftover_row.get("allocated_bytes_estimate") or leftover_row.get("st_blocks") or 0) >= 0


def test_c03_success_extra_hardlink_listed(tmp_path):
    """Successful publish leftover is same inode as dest with nlink>=2."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    db.publish_provenance_migration(work, dest, **_KW)
    leftover = dest / _RESERVED
    dest_cat = dest / "catalog.sqlite"
    main = _load_main()
    with _no_catalog_bind():
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    rows = _live_rows(payload)
    left = _row_named(rows, _RESERVED)
    dest_row = _row_named(rows, "catalog.sqlite")
    assert leftover.exists() and dest_cat.exists()
    assert int(left["st_ino"]) == int(dest_cat.lstat().st_ino) == int(dest_row["st_ino"])
    assert int(left["st_nlink"]) >= 2


def test_c04_dest_renamed_away_lists_missing_dest(tmp_path):
    """After dest is renamed away, list shows dest missing and leftover present."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    db.publish_provenance_migration(work, dest, **_KW)
    leftover = dest / _RESERVED
    dest_cat = dest / "catalog.sqlite"
    dest_cat.rename(dest / "catalog.sqlite.moved")
    main = _load_main()
    with _no_catalog_bind():
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    rows = _live_rows(payload)
    dest_row = _row_named(rows, "catalog.sqlite")
    left = _row_named(rows, _RESERVED)
    assert dest_row.get("present") is False or dest_row.get("missing") is True
    assert leftover.exists()
    assert left.get("present") is not False
    assert int(left["st_nlink"]) == int(leftover.lstat().st_nlink)


def test_c05_sidecar_neighbor_listed(tmp_path):
    """A reserved sidecar on dest is listed as a neighbor."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    sidecar = dest / (_RESERVED + "-wal")
    sidecar.write_bytes(b"def038-wal")
    try:
        db.publish_provenance_migration(work, dest, **_KW)
    except RuntimeError:
        pass
    main = _load_main()
    with _no_catalog_bind():
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    rows = _live_rows(payload)
    row = _row_named(rows, sidecar.name)
    assert row is not None
    assert row.get("present") is not False


def test_c06_no_leftovers_dispose_parser():
    """This cycle has no leftovers-dispose subcommand."""
    main = _load_main()
    with pytest.raises(SystemExit) as exc:
        main(["leftovers-dispose", "--work-dir", ".", "--dest-dir", ".", "--member", "x"])
    assert exc.value.code not in (0, None)


def test_c07_out_of_bundle_recorded_member_is_unbound(tmp_path):
    """Recorded paths outside dest reserved names are flagged, never live-stated."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"def038-c07")
    try:
        db.publish_provenance_migration(work, dest, **_KW)
    except RuntimeError:
        pass
    state = json.loads(_state_path(work).read_text())
    report = state.get("staging_report") if isinstance(state.get("staging_report"), dict) else state
    report.setdefault("members", []).append({
        "path": str(tmp_path / "outside.sqlite"),
        "st_dev": 0,
        "st_ino": 0,
        "st_nlink": 1,
        "allocated_bytes_estimate": 512,
    })
    _state_path(work).write_text(json.dumps(state))
    main = _load_main()
    with _no_catalog_bind():
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    recorded = payload.get("recorded") or payload.get("recorded_report") or {}
    rec_members = recorded.get("members") if isinstance(recorded, dict) else payload.get("recorded_members")
    assert rec_members
    flagged = [m for m in rec_members if "outside.sqlite" in str(m.get("path"))]
    assert flagged and flagged[0].get("unbound") or flagged[0].get("invalid")
    rows = _live_rows(payload)
    for item in rows:
        assert Path(item["path"]).name in _ALLOWED_LIVE
        assert "outside.sqlite" not in str(item.get("path"))


def test_c08_slot_state_symlink_refused(tmp_path):
    """Symlink at publication-slot-state.json must be refused."""
    work = tmp_path / "work"
    run = work / "pub"
    run.mkdir(parents=True)
    target = tmp_path / "other.json"
    target.write_text("{}")
    _state_path(work).symlink_to(target)
    dest = tmp_path / "dest"
    dest.mkdir()
    main = _load_main()
    with _no_catalog_bind():
        rc, payload, err = _leftovers(main, work, dest)
    assert rc != 0
    assert payload is None or payload.get("status") != "ok"
    assert "symlink" in err.lower() or "nofollow" in err.lower() or rc == 1


def test_c09_run_dir_symlink_race_refused(tmp_path):
    """A child run dir swapped to a symlink must not leak an escaped state file."""
    work = tmp_path / "work"
    work.mkdir()
    real_run = work / "pub"
    real_run.mkdir()
    (real_run / db.PUBLICATION_SLOT_STATE_NAME).write_text("{}")
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (escaped / db.PUBLICATION_SLOT_STATE_NAME).write_text('{"evil": true}')
    dest = tmp_path / "dest"
    dest.mkdir()
    real_scandir = os.scandir
    swapped = {"yes": False}

    def scandir_hook(path):
        it = real_scandir(path)
        for entry in it:
            yield entry
        if Path(path).resolve() == work.resolve() and not swapped["yes"]:
            swapped["yes"] = True
            os.rename(real_run, tmp_path / "pub-aside")
            os.symlink(escaped, real_run)

    main = _load_main()
    with _no_catalog_bind(), mock.patch.object(os, "scandir", side_effect=scandir_hook):
        rc, payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert rc != 0
    assert payload is None or payload.get("evil") is not True
    assert "evil" not in (err + (json.dumps(payload) if payload else ""))


def test_c10_non_enoent_lstat_is_refuse_not_missing(tmp_path):
    """EACCES/EIO on dest catalog must refuse, not look like a missing dest."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    db.publish_provenance_migration(work, dest, **_KW)
    before = _snapshot(dest)
    real_lstat = os.lstat

    def lstat_hook(path):
        if Path(path).name == "catalog.sqlite" and Path(path).parent.resolve() == dest.resolve():
            raise OSError(errno.EACCES, "denied")
        return real_lstat(path)

    main = _load_main()
    with _no_catalog_bind(), mock.patch.object(os, "lstat", side_effect=lstat_hook):
        rc, payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert rc != 0
    text = (err + json.dumps(payload) if payload else err).lower()
    assert "missing" not in text or "eacces" in text or "denied" in text or "observe" in text
    if payload:
        rows = _live_rows(payload)
        dest_row = _row_named(rows, "catalog.sqlite")
        assert dest_row is None or dest_row.get("missing") is not True
    assert _snapshot(dest) == before


def _live_rows(payload: dict) -> list[dict]:
    live = payload.get("live") or payload.get("observations") or []
    if isinstance(live, dict):
        rows = []
        for key, val in live.items():
            if isinstance(val, dict):
                rows.append({"path": val.get("path", key), **val})
            else:
                rows.append({"path": key})
        return rows
    return list(live)


def _row_named(rows: list[dict], name: str) -> dict:
    for item in rows:
        if Path(item.get("path", "")).name == name:
            return item
    raise AssertionError(f"live rows missing {name}: {rows!r}")
