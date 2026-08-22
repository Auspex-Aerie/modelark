"""INC-036 Gate-1 contracts — leftover staging must not block retry.

Contracts only. Production is unchanged, so c01/c02/c04/c05 stay red until
Gate 2 uses unique per-attempt staging names and never unlinks leftovers.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from test_dec053_054_gate2_remediation import _rehearse_ok


_WELL_KNOWN = ".catalog.sqlite.publish-staging"
_SIDECARS = ("-wal", "-shm", "-journal")
_SENTINEL = b"inc036-staging-name-sentinel"
_KW = {"confirm_stopped": "MODELARK-STOPPED", "writers_stopped": True}


def _identity(path: Path) -> tuple[int, int, bytes]:
    st = path.stat()
    return (int(st.st_dev), int(st.st_ino), path.read_bytes())


def _staging_mains(dest: Path) -> list[Path]:
    if not dest.is_dir():
        return []
    found = []
    for path in dest.iterdir():
        name = path.name
        if not name.startswith(_WELL_KNOWN):
            continue
        if name.endswith(_SIDECARS):
            continue
        if path.is_file():
            found.append(path)
    return sorted(found)


def _staging_set(dest: Path) -> set[str]:
    if not dest.is_dir():
        return set()
    return {path.name for path in dest.iterdir() if path.name.startswith(_WELL_KNOWN)}


def _publish(work, dest):
    return db.publish_provenance_migration(work, dest, **_KW)


def _seam_fail_once():
    real_metrics = db._catalog_snapshot_metrics_con
    mutated = {"yes": False}

    def evil_metrics(con):
        if not mutated["yes"]:
            try:
                db_list = con.execute("PRAGMA database_list").fetchone()
                main_path = Path(db_list[2]) if db_list and db_list[2] else None
            except Exception:
                main_path = None
            if main_path is not None and "publish-staging" in main_path.name:
                mutated["yes"] = True
                con.execute(
                    "INSERT INTO models(repo_id,status,numcopies) "
                    "VALUES('org/inc036-seam','discovered',1)"
                )
        return real_metrics(con)

    return mutated, evil_metrics


def test_c01_failed_publish_leftover_must_not_block_retry(tmp_path):
    """Pre-dest-link failure leftover must remain, and a second publish must succeed."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    mutated, evil = _seam_fail_once()
    first_error = None
    with mock.patch.object(db, "_catalog_snapshot_metrics_con", side_effect=evil):
        try:
            _publish(work, dest)
        except RuntimeError as exc:
            first_error = exc
    assert mutated["yes"] is True
    assert first_error is not None
    assert not dest_catalog.exists()
    leftovers = _staging_mains(dest)
    assert leftovers, "first failure must leave a staging leftover"
    leftover = leftovers[0]
    snap = _identity(leftover)

    retry_error = None
    published = None
    try:
        published = _publish(work, dest)
    except RuntimeError as exc:
        retry_error = exc
    assert published is not None and published.get("status") == "ok", (
        "retry after pre-dest-link failure must succeed; "
        f"got published={published!r} error={retry_error!r}"
    )
    assert dest_catalog.is_file()
    assert leftover.exists(), "first leftover must not be unlinked"
    assert _identity(leftover) == snap


def test_c02_planted_well_known_leftover_must_not_block_or_be_replaced(tmp_path):
    """Planted well-known leftover must keep its inode through a successful publish."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _WELL_KNOWN
    leftover.write_bytes(b"inc036-planted-well-known")
    snap = _identity(leftover)
    published = None
    error = None
    try:
        published = _publish(work, dest)
    except RuntimeError as exc:
        error = exc
    assert published is not None and published.get("status") == "ok", (
        "planted well-known leftover must not block publish; "
        f"got published={published!r} error={error!r}"
    )
    assert leftover.exists(), "planted leftover must not disappear"
    assert _identity(leftover) == snap, (
        "planted leftover must keep the same inode and bytes"
    )


def test_c03_post_link_sentinel_keeps_inode_and_is_not_dest(tmp_path):
    """Swapped staging name after fd-link keeps its inode; dest is not the sentinel."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    injected = {"yes": False, "snap": None}
    real_fd = db._link_fd_no_clobber

    def fd_hook(fd, dest_cat, *args, **kwargs):
        out = real_fd(fd, dest_cat)
        staging = Path(dest_cat).parent / _WELL_KNOWN
        if not staging.exists():
            mains = _staging_mains(Path(dest_cat).parent)
            staging = mains[0] if mains else staging
        rival = tmp_path / "inc036-rival"
        rival.write_bytes(_SENTINEL)
        os.replace(str(rival), str(staging))
        injected["yes"] = True
        injected["snap"] = _identity(staging)
        injected["path"] = staging
        return out

    published = None
    with mock.patch.object(db, "_link_fd_no_clobber", side_effect=fd_hook):
        published = _publish(work, dest)
    assert injected["yes"] is True
    assert published.get("status") == "ok"
    sentinel = injected["path"]
    assert sentinel.exists()
    assert _identity(sentinel) == injected["snap"]
    assert dest_catalog.read_bytes() != _SENTINEL


def test_c04_occupied_unique_main_refuses_once_then_recovers(tmp_path):
    """Factory collision on unique main: one call, refuse, later invocation recovers."""
    factory = getattr(db, "_publish_staging_path", None)
    assert callable(factory), (
        "publish must use an injectable _publish_staging_path(dest_dir) factory"
    )
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    occupied = dest / ".catalog.sqlite.publish-staging.c04occ"
    occupied.write_bytes(b"inc036-c04-occupant")
    free = dest / ".catalog.sqlite.publish-staging.c04free"
    snap = _identity(occupied)
    paths = [occupied, free]
    calls = {"n": 0}

    def stub(dest_dir):
        idx = calls["n"]
        calls["n"] += 1
        return paths[idx]

    dest_catalog = dest / "catalog.sqlite"
    first_error = None
    with mock.patch.object(db, "_publish_staging_path", side_effect=stub):
        try:
            _publish(work, dest)
        except RuntimeError as exc:
            first_error = exc
    assert calls["n"] == 1, "collision invocation must call the factory exactly once"
    assert first_error is not None
    assert not dest_catalog.exists()
    assert not free.exists(), "free path must stay untouched on the refusing call"
    assert _identity(occupied) == snap

    published = None
    with mock.patch.object(db, "_publish_staging_path", side_effect=stub):
        published = _publish(work, dest)
    assert calls["n"] == 2, "recovery invocation must make exactly the next factory call"
    assert published.get("status") == "ok"
    assert dest_catalog.is_file()
    assert _identity(occupied) == snap


@pytest.mark.parametrize("suffix", _SIDECARS)
def test_c05_occupied_unique_sidecar_refuses_once_then_recovers(tmp_path, suffix):
    """Factory collision on unique staging sidecar: one call, refuse, then recover."""
    factory = getattr(db, "_publish_staging_path", None)
    assert callable(factory), (
        "publish must use an injectable _publish_staging_path(dest_dir) factory"
    )
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    occupied_main = dest / f".catalog.sqlite.publish-staging.c05{suffix}"
    sidecar = occupied_main.with_name(occupied_main.name + suffix)
    sidecar.write_bytes(f"inc036-c05-occupant{suffix}".encode())
    free = dest / f".catalog.sqlite.publish-staging.c05free{suffix}"
    snap = _identity(sidecar)
    paths = [occupied_main, free]
    calls = {"n": 0}

    def stub(dest_dir):
        idx = calls["n"]
        calls["n"] += 1
        return paths[idx]

    dest_catalog = dest / "catalog.sqlite"
    first_error = None
    with mock.patch.object(db, "_publish_staging_path", side_effect=stub):
        try:
            _publish(work, dest)
        except RuntimeError as exc:
            first_error = exc
    assert calls["n"] == 1, "collision invocation must call the factory exactly once"
    assert first_error is not None
    assert not dest_catalog.exists()
    assert not occupied_main.exists()
    assert not free.exists()
    assert _identity(sidecar) == snap

    published = None
    with mock.patch.object(db, "_publish_staging_path", side_effect=stub):
        published = _publish(work, dest)
    assert calls["n"] == 2
    assert published.get("status") == "ok"
    assert dest_catalog.is_file()
    assert _identity(sidecar) == snap


def test_c06_same_dest_retry_is_occupancy_and_creates_no_extra_staging(tmp_path):
    """Successful publish then same-dest retry refuses occupancy; staging set frozen."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    published = _publish(work, dest)
    assert published.get("status") == "ok"
    snap = _identity(dest_catalog)
    names_after = _staging_set(dest)
    error = None
    try:
        _publish(work, dest)
    except RuntimeError as exc:
        error = exc
    assert error is not None
    assert "already exists" in str(error).lower() or "overwrite" in str(error).lower()
    assert _identity(dest_catalog) == snap
    assert _staging_set(dest) == names_after
