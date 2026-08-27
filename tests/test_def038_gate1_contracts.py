"""DEF-038 Gate-1 contracts — leftover list CLI (dispose deferred).

Contracts only. Production CLI still has rehearse/publish only, so leftovers
cases stay red until Gate 2. leftovers-dispose must remain absent.
"""
from __future__ import annotations

import builtins
import errno
import importlib.util
import io
import json
import os
import sqlite3
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

    def layout_boom(*_a, **_k):
        raise AssertionError("leftovers must not call _resolve_rehearsal_layout")

    real_lstat = os.lstat
    real_stat = os.stat
    real_open = os.open
    real_unlink = os.unlink
    real_mkdir = os.mkdir

    def _from_dirfd(path, dir_fd=None) -> Path:
        p = Path(path)
        if dir_fd is None:
            return p
        try:
            return Path(os.readlink(f"/proc/self/fd/{int(dir_fd)}")) / path
        except OSError:
            return p

    def _forbid_path(path) -> bool:
        p = Path(path)
        return p in forbidden or p.name in {"outside.sqlite", "decoy.bin"}

    def lstat_hook(path, *a, dir_fd=None, **k):
        p = _from_dirfd(path, dir_fd)
        if _forbid_path(p):
            raise AssertionError(f"must not observe {p}")
        if p.parent == dest and p not in dest_ok and p != dest:
            raise AssertionError(f"live observation limited to reserved dest names, got {p}")
        return real_lstat(path, *a, dir_fd=dir_fd, **k)

    def stat_hook(path, *a, dir_fd=None, **k):
        p = _from_dirfd(path, dir_fd)
        if _forbid_path(p) or p in dest_ok:
            raise AssertionError(f"live dest metadata must use lstat, not stat: {p}")
        return real_stat(path, *a, dir_fd=dir_fd, **k)

    def open_hook(path, flags, *a, dir_fd=None, **k):
        p = _from_dirfd(path, dir_fd)
        if _forbid_path(p):
            raise AssertionError(f"must not open {p}")
        if p in dest_ok:
            raise AssertionError("must not open dest member contents")
        if flags & os.O_CREAT and (p == dest or p.parent == dest):
            raise AssertionError("leftovers must not create/write dest")
        if p.name == _STATE_NAME and not (flags & getattr(os, "O_NOFOLLOW", 0)):
            raise AssertionError("state open must use O_NOFOLLOW")
        return real_open(path, flags, *a, dir_fd=dir_fd, **k)

    def unlink_hook(path, *a, **k):
        p = Path(path)
        if p == dest or p.parent == dest:
            raise AssertionError("leftovers must not unlink dest")
        return real_unlink(path, *a, **k)

    def mkdir_hook(path, *a, **k):
        if Path(path) == dest:
            raise AssertionError("leftovers must not mkdir dest")
        return real_mkdir(path, *a, **k)

    real_ioopen = io.open

    def _high_level_open(file, *a, **k):
        if not isinstance(file, int):
            p = Path(file)
            if _forbid_path(p) or p in dest_ok or p.name == _STATE_NAME:
                raise AssertionError("high-level open forbidden; use os.open O_NOFOLLOW")
        return real_ioopen(file, *a, **k)

    def bopen_hook(file, *a, **k):
        return _high_level_open(file, *a, **k)

    real_connect = sqlite3.connect

    def connect_hook(database, *a, **k):
        if not isinstance(database, int):
            p = Path(str(database).split("?", 1)[0].removeprefix("file:"))
            if p in dest_ok:
                raise AssertionError("must not sqlite3.connect dest members")
        return real_connect(database, *a, **k)

    real_listdir = os.listdir
    real_scandir = os.scandir

    def listdir_hook(path):
        if Path(path) == dest:
            raise AssertionError("must not enumerate dest")
        return real_listdir(path)

    def scandir_hook(path):
        if Path(path) == dest:
            raise AssertionError("must not enumerate dest")
        return real_scandir(path)

    with _no_catalog_bind(), mock.patch.object(
        db, "_resolve_rehearsal_layout", side_effect=layout_boom
    ), mock.patch.object(os, "lstat", side_effect=lstat_hook), mock.patch.object(
        os, "stat", side_effect=stat_hook
    ), mock.patch.object(os, "open", side_effect=open_hook), mock.patch.object(
        os, "unlink", side_effect=unlink_hook
    ), mock.patch.object(os, "mkdir", side_effect=mkdir_hook), mock.patch.object(
        os, "listdir", side_effect=listdir_hook
    ), mock.patch.object(os, "scandir", side_effect=scandir_hook), mock.patch.object(
        builtins, "open", side_effect=bopen_hook
    ), mock.patch.object(io, "open", side_effect=_high_level_open), mock.patch.object(
        sqlite3, "connect", side_effect=connect_hook
    ):
        yield dest_ok


def _live_rows(payload: dict, dest: Path) -> list[dict]:
    live = payload["live"]
    assert isinstance(live, list) and len(live) == 5
    names = {Path(item["path"]).name for item in live}
    assert names == _LIVE_NAMES
    for item in live:
        assert Path(item["path"]) == dest / Path(item["path"]).name
    return live


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
    assert row["dest_relation"] == dest_relation
    assert row["present"] is True
    assert row.get("missing") is not True


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
    before_work = _tree_snap(work)
    main = _load_main()
    with _list_guards(work, dest):
        rc, _payload, err = _leftovers(main, work, dest)
        with pytest.raises(SystemExit) as missing_dest:
            main(["leftovers", "--work-dir", str(work)])
        with pytest.raises(SystemExit) as missing_work:
            main(["leftovers", "--dest-dir", str(dest)])
    assert dest.exists() is False
    assert _tree_snap(work) == before_work
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
    row = _row_named(_live_rows(payload, dest), _RESERVED)
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
    left = _row_named(_live_rows(payload, dest), _RESERVED)
    dest_row = _row_named(_live_rows(payload, dest), "catalog.sqlite")
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
    dest_row = _row_named(_live_rows(payload, dest), "catalog.sqlite")
    left = _row_named(_live_rows(payload, dest), _RESERVED)
    assert dest_row["present"] is False
    assert dest_row["missing"] is True
    for name in (_RESERVED + s for s in _SIDECARS):
        row = _row_named(_live_rows(payload, dest), name)
        assert row["present"] is False
        assert row["missing"] is True
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
    row = _row_named(_live_rows(payload, dest), sidecar.name)
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
    recorded = payload["recorded"]
    rec_members = recorded["members"]
    flagged = [m for m in rec_members if "outside.sqlite" in str(m.get("path"))]
    assert flagged and flagged[0].get("unbound") is True
    for item in _live_rows(payload, dest):
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
    with _list_guards(work, dest, extra_forbidden=[target]):
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
    (escaped / _STATE_NAME).write_text(payload_txt)
    escaped_state = escaped / _STATE_NAME
    before_escaped = _ident(escaped_state)
    before_dest = _tree_snap(dest)
    real_open = os.open
    swapped = {"yes": False}
    work_id = os.stat(work)

    def open_hook(path, flags, *a, dir_fd=None, **k):
        target = Path(path)
        if dir_fd is not None:
            try:
                base = Path(os.readlink(f"/proc/self/fd/{int(dir_fd)}"))
                target = base / path
            except OSError:
                pass
        if target == escaped_state or Path(path) == escaped_state:
            raise AssertionError("must not read escaped state file")
        if target.name == "pub" and flags & os.O_DIRECTORY:
            assert dir_fd is not None, "child run dir must open relative to work dirfd"
            assert not os.path.isabs(str(path)), "child path must be relative to work dirfd"
            wst = os.fstat(dir_fd)
            assert (int(wst.st_dev), int(wst.st_ino)) == (
                int(work_id.st_dev),
                int(work_id.st_ino),
            )
            assert flags & getattr(os, "O_NOFOLLOW", 0)
            if not swapped["yes"]:
                swapped["yes"] = True
                os.rename(real_run, tmp_path / "pub-aside")
                os.symlink(escaped, real_run)
        return real_open(path, flags, *a, dir_fd=dir_fd, **k)

    main = _load_main()
    with _no_catalog_bind(), mock.patch.object(
        db, "_resolve_rehearsal_layout", side_effect=AssertionError("no layout")
    ), mock.patch.object(os, "open", side_effect=open_hook):
        rc, payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert swapped["yes"] is True
    assert rc != 0
    assert _ident(escaped_state) == before_escaped
    assert _tree_snap(dest) == before_dest


@pytest.mark.parametrize("target_name", sorted(_LIVE_NAMES))
@pytest.mark.parametrize("errnum", (errno.EACCES, errno.EIO))
def test_c10_non_enoent_lstat_is_refuse_not_missing(tmp_path, errnum, target_name):
    """Non-ENOENT lstat on dest catalog must refuse, not look like missing."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    db.publish_provenance_migration(work, dest, **_KW)
    before_dest = _tree_snap(dest)
    before_state = _ident(_state_path(work))
    real_lstat = os.lstat

    fired = {"yes": False}

    def lstat_hook(path, *a, dir_fd=None, **k):
        p = Path(path)
        if dir_fd is not None:
            try:
                p = Path(os.readlink(f"/proc/self/fd/{int(dir_fd)}")) / path
            except OSError:
                p = Path(path)
        if p.parent == dest and p.name == target_name:
            fired["yes"] = True
            raise OSError(errnum, os.strerror(errnum))
        return real_lstat(path, *a, dir_fd=dir_fd, **k)

    main = _load_main()
    with _no_catalog_bind(), mock.patch.object(
        db, "_resolve_rehearsal_layout", side_effect=AssertionError("no layout")
    ), mock.patch.object(os, "lstat", side_effect=lstat_hook):
        rc, payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert rc != 0
    assert fired["yes"] is True
    if payload:
        rows = _live_rows(payload, dest)
        try:
            dest_row = _row_named(rows, "catalog.sqlite")
        except AssertionError:
            dest_row = None
        assert dest_row is None or dest_row.get("missing") is not True
    assert _tree_snap(dest) == before_dest
    assert _ident(_state_path(work)) == before_state


def test_c11_root_slot_state_is_accepted(tmp_path):
    """A regular publication-slot-state.json directly under work-dir must list."""
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"root-state")
    st = leftover.lstat()
    (work / _STATE_NAME).write_text(_valid_state([{
        "path": str(leftover),
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_nlink": int(st.st_nlink),
        "allocated_bytes_estimate": int(st.st_blocks) * 512,
    }]))
    main = _load_main()
    with _list_guards(work, dest):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    _assert_member_lstat(
        _row_named(_live_rows(payload, dest), _RESERVED), leftover, dest_relation="absent"
    )


def test_c12_duplicate_recorded_members_refused(tmp_path):
    """Duplicate member paths in slot-state must refuse."""
    work = tmp_path / "work"
    run = work / "pub"
    run.mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"dup")
    st = leftover.lstat()
    member = {
        "path": str(leftover),
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_nlink": int(st.st_nlink),
        "allocated_bytes_estimate": int(st.st_blocks) * 512,
    }
    (run / _STATE_NAME).write_text(_valid_state([member, dict(member)]))
    main = _load_main()
    with _list_guards(work, dest):
        rc, _payload, err = _leftovers(main, work, dest)
    assert "invalid choice" not in err.lower(), err
    assert rc != 0


def test_c13_live_different_inode_when_dest_and_leftover_are_distinct(tmp_path):
    """Dest catalog present with a different inode from leftover is different_inode."""
    work = tmp_path / "work"
    run = work / "pub"
    run.mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"leftover-copy")
    dest_cat = dest / "catalog.sqlite"
    dest_cat.write_bytes(b"other-catalog-bytes")
    st = leftover.lstat()
    (run / _STATE_NAME).write_text(_valid_state([{
        "path": str(leftover),
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_nlink": int(st.st_nlink),
        "allocated_bytes_estimate": int(st.st_blocks) * 512,
    }]))
    main = _load_main()
    with _list_guards(work, dest):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    _assert_member_lstat(
        _row_named(_live_rows(payload, dest), _RESERVED), leftover, dest_relation="different_inode"
    )


def test_c14_dangling_symlink_leftover_uses_lstat(tmp_path):
    """A dangling reserved symlink is listed by lstat identity, not followed."""
    work = tmp_path / "work"
    run = work / "pub"
    run.mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.symlink_to(dest / "missing-target")
    st = leftover.lstat()
    (run / _STATE_NAME).write_text(_valid_state([{
        "path": str(leftover),
        "st_dev": int(st.st_dev),
        "st_ino": int(st.st_ino),
        "st_nlink": int(st.st_nlink),
        "allocated_bytes_estimate": int(st.st_blocks) * 512,
    }]))
    main = _load_main()
    with _list_guards(work, dest):
        rc, payload, err = _leftovers(main, work, dest)
    assert rc == 0, err
    row = _row_named(_live_rows(payload, dest), _RESERVED)
    _assert_member_lstat(row, leftover, dest_relation="absent")
