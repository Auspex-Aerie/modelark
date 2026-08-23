"""INC-036 Gate-1 contracts — one reserved staging slot, refuse-and-review.

Contracts only. Unique-name retry is withdrawn. Production is unchanged, so
durable slot-state, structured leftover reports, and dest free-space
admission stay red until Gate 2. Occupied-slot refuse without mutation is
already true at the frozen primitive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from test_dec053_054_gate2_remediation import _rehearse_ok


_RESERVED = ".catalog.sqlite.publish-staging"
_SIDECARS = ("-wal", "-shm", "-journal")
_SENTINEL = b"inc036-staging-name-sentinel"
_SLOT_STATE = "publication-slot-state.json"
_KW = {"confirm_stopped": "MODELARK-STOPPED", "writers_stopped": True}


def _identity(path: Path) -> tuple[int, int, bytes]:
    st = path.stat()
    return (int(st.st_dev), int(st.st_ino), path.read_bytes())


def _staging_names(dest: Path) -> set[str]:
    if not dest.is_dir():
        return set()
    return {p.name for p in dest.iterdir() if p.name.startswith(_RESERVED)}


def _run_root(work: Path) -> Path:
    return work / "pub"


def _slot_state_path(work: Path) -> Path:
    return _run_root(work) / _SLOT_STATE


def _publish(work, dest):
    return db.publish_provenance_migration(work, dest, **_KW)


def _staging_report(payload) -> dict | None:
    if isinstance(payload, dict):
        report = payload.get("staging_report")
        return report if isinstance(report, dict) else None
    report = getattr(payload, "staging_report", None)
    return report if isinstance(report, dict) else None


def test_c01_occupied_reserved_slot_refuses_without_mutation(tmp_path):
    """Dest absent + reserved leftover: refuse, leftover inode/bytes exact, no dest."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"inc036-occupied-slot")
    snap = _identity(leftover)
    dest_catalog = dest / "catalog.sqlite"
    names_before = _staging_names(dest)
    error = None
    published = None
    try:
        published = _publish(work, dest)
    except RuntimeError as exc:
        error = exc
    assert error is not None, f"occupied slot must refuse, got {published!r}"
    assert published is None or published.get("status") != "ok"
    assert not dest_catalog.exists()
    assert leftover.exists()
    assert _identity(leftover) == snap
    assert _staging_names(dest) == names_before


@pytest.mark.parametrize("suffix", _SIDECARS)
def test_c02_reserved_sidecar_refuses_before_main_mutation(tmp_path, suffix):
    """Reserved sidecar occupant is refused before any main create/truncate."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    main = dest / _RESERVED
    sidecar = main.with_name(main.name + suffix)
    sidecar.write_bytes(f"inc036-sidecar{suffix}".encode())
    snap = _identity(sidecar)
    dest_catalog = dest / "catalog.sqlite"
    error = None
    try:
        _publish(work, dest)
    except RuntimeError as exc:
        error = exc
    assert error is not None
    assert not dest_catalog.exists()
    assert not main.exists()
    assert sidecar.exists()
    assert _identity(sidecar) == snap


def test_c03_post_link_sentinel_keeps_inode_and_is_not_dest(tmp_path):
    """Swapped staging name after fd-link keeps its inode; dest is not the sentinel."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    injected = {"yes": False, "snap": None, "path": None}
    real_fd = db._link_fd_no_clobber

    def fd_hook(fd, dest_cat, *args, **kwargs):
        out = real_fd(fd, dest_cat)
        staging = Path(dest_cat).parent / _RESERVED
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


def test_c04_occupied_refuse_writes_durable_slot_state(tmp_path):
    """Occupied refuse must persist slot-state in the rehearsal run dir before returning."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"inc036-state-occupant")
    try:
        _publish(work, dest)
    except RuntimeError:
        pass
    state_path = _slot_state_path(work)
    assert state_path.is_file(), (
        f"occupied refuse must write {_SLOT_STATE} under the run dir; "
        f"missing {state_path}"
    )
    payload = json.loads(state_path.read_text())
    assert payload.get("dest_relation") in {"absent", "different_inode", "unknown"}
    assert _RESERVED in str(payload.get("staging_path") or payload.get("reserved_path") or "")


def test_c05_occupied_refuse_carries_structured_staging_report(tmp_path):
    """Refuse must expose a staging_report (neighbors, size estimates), not only a string."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"inc036-report-occupant")
    error = None
    try:
        _publish(work, dest)
    except RuntimeError as exc:
        error = exc
    assert error is not None
    report = _staging_report(error)
    assert isinstance(report, dict), (
        "occupied refuse must attach staging_report with dest relation and members"
    )
    assert report.get("dest_relation") in {"absent", "different_inode", "unknown"}
    members = report.get("members")
    assert isinstance(members, list) and members, "report must list leftover members"
    names = {Path(m["path"]).name if isinstance(m, dict) and "path" in m else "" for m in members}
    assert _RESERVED in names


def test_c06_same_dest_retry_is_occupancy_and_creates_no_extra_staging(tmp_path):
    """Successful publish then same-dest retry refuses occupancy; staging names frozen."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    published = _publish(work, dest)
    assert published.get("status") == "ok"
    snap = _identity(dest_catalog)
    names_after = _staging_names(dest)
    error = None
    try:
        _publish(work, dest)
    except RuntimeError as exc:
        error = exc
    assert error is not None
    assert "already exists" in str(error).lower() or "overwrite" in str(error).lower()
    assert _identity(dest_catalog) == snap
    assert _staging_names(dest) == names_after


def test_c07_low_dest_free_space_refuses_before_creating_staging(tmp_path):
    """Dest filesystem free-space estimate must refuse before the reserved slot is created."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    leftover = dest / _RESERVED

    class _Tiny:
        f_bavail = 1
        f_frsize = 4096
        f_bsize = 4096
        f_blocks = 100
        f_bfree = 1

    error = None
    published = None
    with mock.patch.object(os, "statvfs", return_value=_Tiny()):
        try:
            published = _publish(work, dest)
        except RuntimeError as exc:
            error = exc
    assert error is not None, f"low free space must refuse, got {published!r}"
    assert not dest_catalog.exists()
    assert not leftover.exists()


def test_c08_success_includes_staging_report(tmp_path):
    """Success dict must include staging_report (path, dest relation, member sizes)."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    published = _publish(work, dest)
    assert published.get("status") == "ok"
    report = _staging_report(published)
    assert isinstance(report, dict), "success must include staging_report"
    assert report.get("dest_relation") in {"same_attempt_inode", "absent", "unknown"}
    members = report.get("members")
    assert isinstance(members, list)


def test_c09_dest_renamed_away_does_not_truncate_slot(tmp_path):
    """If dest spelling is gone but the slot still names the catalog inode, do not wipe it."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    published = _publish(work, dest)
    assert published.get("status") == "ok"
    leftover = dest / _RESERVED
    if not leftover.exists():
        pytest.skip("frozen production left no extra staging name after success")
    snap = _identity(leftover)
    backup = dest / "catalog.sqlite.moved"
    dest_catalog.rename(backup)
    error = None
    try:
        _publish(work, dest)
    except RuntimeError as exc:
        error = exc
    assert error is not None
    assert leftover.exists()
    assert _identity(leftover) == snap
    assert backup.exists()
    assert _identity(backup) == snap
