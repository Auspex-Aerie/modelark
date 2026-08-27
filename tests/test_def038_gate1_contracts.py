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
_STATE_NAME = "publication-slot-state.json"
_KW = {"confirm_stopped": "MODELARK-STOPPED", "writers_stopped": True}
_LIVE_NAMES = frozenset({"catalog.sqlite", _RESERVED, *(_RESERVED + s for s in _SIDECARS)})


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
    return _run_root(work) / _STATE_NAME


def _ident(path: Path) -> tuple:
    st = path.lstat()
    blob = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    return (
        path.name,
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_nlink),
        int(st.st_size),
        int(st.st_blocks),
        int(st.st_mtime_ns),
        blob,
    )


def _tree_snap(root: Path) -> dict[str, tuple]:
    if not root.exists():
        return {}
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        try:
            out[rel] = _ident(path)
        except OSError:
            out[rel] = ("missing",)
    return out


def _allowed_dest_paths(dest: Path) -> set[Path]:
    names = ["catalog.sqlite", _RESERVED, *(_RESERVED + s for s in _SIDECARS)]
    return {dest / name for name in names}


def _leftovers(main, work: Path, dest: Path | None, extra_argv: list[str] | None = None):
    argv = ["leftovers", "--work-dir", str(work)]
    if dest is not None:
        argv.extend(["--dest-dir", str(dest)])
    if extra_argv:
        argv.extend(extra_argv)
    buf = StringIO()
    err = StringIO()
    with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    text = buf.getvalue()
    try:
        payload = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = None
    return int(rc or 0), payload, err.getvalue() + text


@contextmanager
def _list_guards(work: Path, dest: Path, extra_forbidden: list[Path] | None = None):
    forbidden = list(extra_forbidden or [])
    dest_ok = _allowed_dest_paths(dest)
    work_ok_prefix = work.resolve()
    dest_res = dest.resolve() if dest.exists() else dest

    def layout_boom(*_a, **_k):
        raise AssertionError("leftovers must not call _resolve_rehearsal_layout")

    real_lstat = os.lstat

    def lstat_hook(path, *a, **k):
        p = Path(path)
        try:
            resolved_parent = p.parent.resolve()
        except OSError:
            resolved_parent = p.parent
        if p in forbidden or p.name == "outside.sqlite" or p.name == "decoy.bin":
            raise AssertionError(f"must not observe {p}")
        if dest.exists() and resolved_parent == dest.resolve() and p.name not in _LIVE_NAMES and p != dest:
            raise AssertionError(f"live observation limited to reserved dest names, got {p}")
        return real_lstat(path, *a, **k)

    with _no_catalog_bind(), mock.patch.object(
        db, "_resolve_rehearsal_layout", side_effect=layout_boom
    ), mock.patch.object(os, "lstat", side_effect=lstat_hook):
        yield


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


def _assert_member_lstat(row: dict, path: Path, *, dest_relation: str) -> None:
    st = path.lstat()
    assert int(row["st_dev"]) == int(st.st_dev)
    assert int(row["st_ino"]) == int(st.st_ino)
    assert int(row["st_nlink"]) == int(st.st_nlink)
    assert int(row["allocated_bytes_estimate"]) == int(st.st_blocks) * 512
    rel = row.get("dest_relation") or row.get("live_dest_relation")
    assert rel == dest_relation


def _valid_state(members: list[dict]) -> str:
    return json.dumps({
        "dest_relation": "absent",
        "staging_path": str(Path("/unused") / _RESERVED),
        "staging_report": {
            "dest_relation": "absent",
            "members": members,
            "staging_path": str(Path("/unused") / _RESERVED),
        },
    })


def test_c01_leftovers_exists_without_catalog_bind_or_dest_create(tmp_path):
    """leftovers must exist, not bind the catalog, and not create dest."""
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest"
    main = _load_main()
    with _no_catalog_bind():
        rc, _payload, err = _leftovers(main, work, dest)
        with pytest.raises(SystemExit) as missing_dest:
            main(["leftovers", "--work-dir", str(work)])
        with pytest.raises(SystemExit) as missing_work:
            main(["leftovers", "--dest-dir", str(dest)])
    assert dest.exists() is False
    assert "invalid choice" not in err.lower(), err
    assert rc in (0, 1)
    assert missing_dest.value.code not in (0, None)
    assert missing_work.value.code not in (0, None)


def test_c02_occupied_leftover_listed_absent_from_dest(tmp_path):
    """After occupied leftover refuse, list shows reserved leftover dest_relation absent."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"def038-occupied")
    decoy = dest / "decoy.bin"
    decoy.write_bytes(b"nope")
    try:
        db.publish_provenance_migration(work, dest, **_KW)
    except RuntimeError:
        pass
    before_dest = _tree_snap(dest)
    before_state = _ident(_state_path(work))
    main = _load_main()
    with _list_guards(work, dest, extra_forbidden=[decoy]):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    assert _tree_snap(dest) == before_dest
    assert _ident(_state_path(work)) == before_state
    row = _row_named(_live_rows(payload), _RESERVED)
    _assert_member_lstat(row, leftover, dest_relation="absent")


def test_c03_success_extra_hardlink_listed(tmp_path):
    """Successful publish leftover is same inode as dest with nlink>=2."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    db.publish_provenance_migration(work, dest, **_KW)
    leftover = dest / _RESERVED
    dest_cat = dest / "catalog.sqlite"
    before_dest = _tree_snap(dest)
    before_state = _ident(_state_path(work))
    main = _load_main()
    with _list_guards(work, dest):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    assert _tree_snap(dest) == before_dest
    assert _ident(_state_path(work)) == before_state
    left = _row_named(_live_rows(payload), _RESERVED)
    dest_row = _row_named(_live_rows(payload), "catalog.sqlite")
    st_l = leftover.lstat()
    st_d = dest_cat.lstat()
    assert (st_l.st_dev, st_l.st_ino) == (st_d.st_dev, st_d.st_ino)
    _assert_member_lstat(left, leftover, dest_relation="same_attempt_inode")
    assert int(dest_row["st_dev"]) == int(st_d.st_dev)
    assert int(dest_row["st_ino"]) == int(st_d.st_ino)
    assert int(left["st_nlink"]) >= 2


def test_c04_dest_renamed_away_lists_missing_dest(tmp_path):
    """After dest is renamed away, list shows dest missing and leftover present."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    db.publish_provenance_migration(work, dest, **_KW)
    leftover = dest / _RESERVED
    dest_cat = dest / "catalog.sqlite"
    dest_cat.rename(dest / "catalog.sqlite.moved")
    before_dest = _tree_snap(dest)
    before_state = _ident(_state_path(work))
    main = _load_main()
    with _list_guards(work, dest):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    assert _tree_snap(dest) == before_dest
    assert _ident(_state_path(work)) == before_state
    dest_row = _row_named(_live_rows(payload), "catalog.sqlite")
    left = _row_named(_live_rows(payload), _RESERVED)
    assert dest_row.get("present") is False or dest_row.get("missing") is True
    _assert_member_lstat(left, leftover, dest_relation="absent")


@pytest.mark.parametrize("suffix", _SIDECARS)
def test_c05_sidecar_neighbor_listed(tmp_path, suffix):
    """Each reserved sidecar on dest is listed as a neighbor."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    sidecar = dest / (_RESERVED + suffix)
    sidecar.write_bytes(f"def038{suffix}".encode())
    try:
        db.publish_provenance_migration(work, dest, **_KW)
    except RuntimeError:
        pass
    before_dest = _tree_snap(dest)
    before_state = _ident(_state_path(work))
    main = _load_main()
    with _list_guards(work, dest):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    assert _tree_snap(dest) == before_dest
    assert _ident(_state_path(work)) == before_state
    row = _row_named(_live_rows(payload), sidecar.name)
    _assert_member_lstat(row, sidecar, dest_relation="absent")


def test_c06_no_leftovers_dispose_parser():
    """This cycle has no leftovers-dispose subcommand."""
    main = _load_main()
    buf = StringIO()
    with mock.patch("sys.stderr", buf), pytest.raises(SystemExit) as exc:
        main(["leftovers-dispose", "--work-dir", ".", "--dest-dir", ".", "--member", "x"])
    assert exc.value.code not in (0, None)
    assert "invalid choice" in buf.getvalue().lower()
    assert "leftovers-dispose" in buf.getvalue()


def test_c07_out_of_bundle_recorded_member_is_unbound(tmp_path):
    """Recorded paths outside dest reserved names are flagged, never live-stated."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"def038-c07")
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"secret")
    decoy = dest / "decoy.bin"
    decoy.write_bytes(b"nope")
    try:
        db.publish_provenance_migration(work, dest, **_KW)
    except RuntimeError:
        pass
    state = json.loads(_state_path(work).read_text())
    report = state.get("staging_report") if isinstance(state.get("staging_report"), dict) else state
    report.setdefault("members", []).append({
        "path": str(outside),
        "st_dev": 0,
        "st_ino": 0,
        "st_nlink": 1,
        "allocated_bytes_estimate": 512,
    })
    _state_path(work).write_text(json.dumps(state))
    before_dest = _tree_snap(dest)
    before_state = _ident(_state_path(work))
    main = _load_main()
    with _list_guards(work, dest, extra_forbidden=[outside, decoy]):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    assert _tree_snap(dest) == before_dest
    assert _ident(_state_path(work)) == before_state
    recorded = payload.get("recorded") or payload.get("recorded_report") or {}
    rec_members = recorded.get("members") if isinstance(recorded, dict) else payload.get("recorded_members")
    flagged = [m for m in rec_members if "outside.sqlite" in str(m.get("path"))]
    assert flagged and (flagged[0].get("unbound") or flagged[0].get("invalid"))
    for item in _live_rows(payload):
        assert Path(item["path"]).name in _LIVE_NAMES


def test_c08_slot_state_symlink_refused(tmp_path):
    """Symlink at publication-slot-state.json must be refused without following."""
    work = tmp_path / "work"
    run = work / "pub"
    run.mkdir(parents=True)
    target = tmp_path / "other.json"
    leftover = tmp_path / "dest-placeholder"
    leftover.mkdir()
    member = leftover / _RESERVED
    member.write_bytes(b"x")
    st = member.lstat()
    target.write_text(_valid_state([{
        "path": str(member),
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_nlink": int(st.st_nlink),
        "allocated_bytes_estimate": int(st.st_blocks) * 512,
    }]))
    state = run / _STATE_NAME
    state.symlink_to(target)
    dest = tmp_path / "dest"
    dest.mkdir()
    main = _load_main()
    with _no_catalog_bind(), mock.patch.object(
        db, "_resolve_rehearsal_layout", side_effect=AssertionError("no layout")
    ):
        rc, payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert rc != 0
    assert payload is None or payload.get("status") != "ok"
    low = err.lower()
    assert "symlink" in low or "nofollow" in low or "eloop" in low or "loop" in low


def test_c09_run_dir_symlink_race_refused(tmp_path):
    """A child run dir swapped to a symlink must not leak an escaped state file."""
    work = tmp_path / "work"
    work.mkdir()
    real_run = work / "pub"
    real_run.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"ok")
    st = leftover.lstat()
    payload_txt = _valid_state([{
        "path": str(leftover),
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_nlink": int(st.st_nlink),
        "allocated_bytes_estimate": int(st.st_blocks) * 512,
    }])
    (real_run / _STATE_NAME).write_text(payload_txt)
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (escaped / _STATE_NAME).write_text('{"evil": true}')
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
    with _no_catalog_bind(), mock.patch.object(
        db, "_resolve_rehearsal_layout", side_effect=AssertionError("no layout")
    ), mock.patch.object(os, "scandir", side_effect=scandir_hook):
        rc, payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert swapped["yes"] is True
    assert rc != 0
    blob = err + (json.dumps(payload) if payload else "")
    assert "evil" not in blob


@pytest.mark.parametrize("errnum", (errno.EACCES, errno.EIO))
def test_c10_non_enoent_lstat_is_refuse_not_missing(tmp_path, errnum):
    """Non-ENOENT lstat on dest catalog must refuse, not look like missing."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    db.publish_provenance_migration(work, dest, **_KW)
    before_dest = _tree_snap(dest)
    before_state = _ident(_state_path(work))
    real_lstat = os.lstat

    def lstat_hook(path, *a, **k):
        if Path(path).name == "catalog.sqlite" and Path(path).parent.resolve() == dest.resolve():
            raise OSError(errnum, os.strerror(errnum))
        return real_lstat(path, *a, **k)

    main = _load_main()
    with _no_catalog_bind(), mock.patch.object(
        db, "_resolve_rehearsal_layout", side_effect=AssertionError("no layout")
    ), mock.patch.object(os, "lstat", side_effect=lstat_hook):
        rc, payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert rc != 0
    if payload:
        rows = _live_rows(payload)
        try:
            dest_row = _row_named(rows, "catalog.sqlite")
        except AssertionError:
            dest_row = None
        assert dest_row is None or dest_row.get("missing") is not True
    assert _tree_snap(dest) == before_dest
    assert _ident(_state_path(work)) == before_state
