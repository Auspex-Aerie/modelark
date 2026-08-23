"""INC-036 Gate-1 contracts — one reserved staging slot, refuse-and-review.

Contracts only. Unique-name retry is withdrawn. Production is unchanged, so
durable slot-state, structured leftover reports, and dest free-space
admission stay red until Gate 2. Occupied-slot refuse without mutation is
already true at the frozen primitive.
"""
from __future__ import annotations

import json
import os
import stat
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


def _member_for(members: list, path: Path) -> dict:
    want = path.resolve()
    for member in members:
        assert isinstance(member, dict)
        if Path(member["path"]).resolve() == want:
            return member
    raise AssertionError(f"staging_report members missing {want}")


def _assert_durable_install(seams: _SlotSeams, state_path: Path, run_root: Path) -> None:
    final = state_path.read_bytes()
    state_ino = int(state_path.stat().st_ino)
    run_ino = int(run_root.stat().st_ino)
    saw_file = False
    for kind, ino, payload in seams.fsync_events:
        if not saw_file and kind == "file" and ino == state_ino and payload == final:
            saw_file = True
            continue
        if saw_file and kind == "dir" and ino == run_ino:
            return
    raise AssertionError(
        "slot-state final bytes must be fsync'd, then the run directory fsync'd"
    )


def _assert_member_identity(member: dict, path: Path) -> None:
    st = path.stat()
    assert int(member["st_dev"]) == int(st.st_dev)
    assert int(member["st_ino"]) == int(st.st_ino)
    assert int(member["st_nlink"]) == int(st.st_nlink)
    assert int(member["allocated_bytes_estimate"]) == int(st.st_blocks) * 512


class _SlotSeams:
    def __init__(self, dest: Path, state_path: Path | None = None):
        self.dest = dest.resolve()
        self.state_path = state_path.resolve() if state_path is not None else None
        self.open_excl = 0
        self.unlink = 0
        self.ftruncate = 0
        self.fsync_events: list[tuple[str, int, bytes]] = []
        self._real_open = os.open
        self._real_unlink = os.unlink
        self._real_ftruncate = os.ftruncate
        self._real_fsync = os.fsync

    def _is_reserved(self, path) -> bool:
        p = Path(path)
        try:
            return p.name == _RESERVED and p.resolve().parent == self.dest
        except OSError:
            return p.name == _RESERVED

    def open_hook(self, path, flags, *args, **kwargs):
        if self._is_reserved(path) and flags & os.O_CREAT and flags & os.O_EXCL:
            self.open_excl += 1
        return self._real_open(path, flags, *args, **kwargs)

    def unlink_hook(self, path, *args, **kwargs):
        if self._is_reserved(path):
            self.unlink += 1
        return self._real_unlink(path, *args, **kwargs)

    def ftruncate_hook(self, fd, length):
        try:
            name = os.readlink(f"/proc/self/fd/{int(fd)}")
        except OSError:
            name = ""
        if Path(name).name == _RESERVED:
            self.ftruncate += 1
        return self._real_ftruncate(fd, length)

    def fsync_hook(self, fd):
        self._real_fsync(fd)
        st = os.fstat(fd)
        kind = "dir" if stat.S_ISDIR(st.st_mode) else "file"
        payload = b""
        if kind == "file" and self.state_path is not None:
            try:
                if int(st.st_ino) == int(self.state_path.stat().st_ino):
                    payload = self.state_path.read_bytes()
            except OSError:
                pass
        self.fsync_events.append((kind, int(st.st_ino), payload))
        return None

    def patch(self):
        return mock.patch.multiple(
            os,
            open=self.open_hook,
            unlink=self.unlink_hook,
            ftruncate=self.ftruncate_hook,
            fsync=self.fsync_hook,
        )


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
    seams = _SlotSeams(dest)
    error = None
    published = None
    with seams.patch():
        try:
            published = _publish(work, dest)
        except RuntimeError as exc:
            error = exc
    assert error is not None, f"occupied slot must refuse, got {published!r}"
    assert seams.open_excl == 0 and seams.unlink == 0 and seams.ftruncate == 0
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
    state_path = _slot_state_path(work)
    seams = _SlotSeams(dest, state_path)
    error = None
    with seams.patch():
        try:
            _publish(work, dest)
        except RuntimeError as exc:
            error = exc
    assert error is not None
    assert seams.open_excl == 0 and seams.unlink == 0 and seams.ftruncate == 0
    report = _staging_report(error)
    assert isinstance(report, dict)
    member = _member_for(report["members"], sidecar)
    _assert_member_identity(member, sidecar)
    assert state_path.is_file()
    payload = json.loads(state_path.read_text())
    state_report = payload.get("staging_report") if isinstance(payload.get("staging_report"), dict) else payload
    state_member = _member_for(state_report["members"], sidecar)
    _assert_member_identity(state_member, sidecar)
    assert (state_member["st_dev"], state_member["st_ino"], state_member["st_nlink"],
            state_member["allocated_bytes_estimate"]) == (
        member["st_dev"], member["st_ino"], member["st_nlink"],
        member["allocated_bytes_estimate"])
    assert report.get("dest_relation") == "absent"
    assert (state_report.get("dest_relation") or payload.get("dest_relation")) == "absent"
    _assert_durable_install(seams, state_path, _run_root(work))
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
        assert staging.exists()
        assert (int(os.fstat(fd).st_dev), int(os.fstat(fd).st_ino)) == (
            int(staging.stat().st_dev),
            int(staging.stat().st_ino),
        )
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
    snap = leftover.stat()
    state_path = _slot_state_path(work)
    seams = _SlotSeams(dest, state_path)
    with seams.patch():
        try:
            _publish(work, dest)
        except RuntimeError:
            pass
    assert state_path.is_file(), (
        f"occupied refuse must write {_SLOT_STATE} under the run dir; "
        f"missing {state_path}"
    )
    _assert_durable_install(seams, state_path, _run_root(work))
    payload = json.loads(state_path.read_text())
    assert payload.get("dest_relation") == "absent"
    report = payload.get("staging_report") if isinstance(payload.get("staging_report"), dict) else payload
    members = report.get("members")
    assert isinstance(members, list)
    member = _member_for(members, leftover)
    _assert_member_identity(member, leftover)
    assert int(member["st_ino"]) == int(snap.st_ino)


def test_c05_occupied_refuse_carries_structured_staging_report(tmp_path):
    """Refuse must expose a staging_report (neighbors, size estimates), not only a string."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    leftover = dest / _RESERVED
    leftover.write_bytes(b"inc036-report-occupant")
    planted = [leftover]
    for suffix in _SIDECARS:
        side = leftover.with_name(leftover.name + suffix)
        side.write_bytes(f"inc036-neighbor{suffix}".encode())
        planted.append(side)
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
    assert report.get("dest_relation") == "absent"
    members = report.get("members")
    assert isinstance(members, list) and members, "report must list leftover members"
    assert report.get("dest_relation") == "absent"
    reported = {Path(m["path"]).resolve() for m in members}
    assert {p.resolve() for p in planted} <= reported
    state = json.loads(_slot_state_path(work).read_text())
    state_report = state.get("staging_report") if isinstance(state.get("staging_report"), dict) else state
    for path in planted:
        member = _member_for(members, path)
        _assert_member_identity(member, path)
        state_member = _member_for(state_report["members"], path)
        _assert_member_identity(state_member, path)
        assert (state_member["st_dev"], state_member["st_ino"], state_member["st_nlink"],
                state_member["allocated_bytes_estimate"]) == (
            member["st_dev"], member["st_ino"], member["st_nlink"],
            member["allocated_bytes_estimate"])


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
    dest.mkdir()
    dest_res = dest.resolve()
    seen_dest = {"yes": False}

    class _Tiny:
        f_bavail = 1
        f_frsize = 4096
        f_bsize = 4096
        f_blocks = 100
        f_bfree = 1

    class _Ample:
        f_bavail = 10**12
        f_frsize = 4096
        f_bsize = 4096
        f_blocks = 10**9
        f_bfree = 10**12

    def vfs(path):
        p = Path(path).resolve()
        if p == dest_res or dest_res in p.parents:
            seen_dest["yes"] = True
            return _Tiny()
        return _Ample()

    seams = _SlotSeams(dest)
    error = None
    published = None
    with mock.patch.object(os, "statvfs", side_effect=vfs), seams.patch():
        try:
            published = _publish(work, dest)
        except RuntimeError as exc:
            error = exc
    assert error is not None, f"low free space must refuse, got {published!r}"
    assert seen_dest["yes"] is True, "capacity check must statvfs the destination filesystem"
    assert "capacity" in str(error).lower() or "free" in str(error).lower()
    assert seams.open_excl == 0 and seams.unlink == 0
    assert not dest_catalog.exists()
    assert not leftover.exists()


def test_c08_success_includes_staging_report(tmp_path):
    """Success dict must include staging_report (path, dest relation, member sizes)."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    published = _publish(work, dest)
    assert published.get("status") == "ok"
    dest_catalog = dest / "catalog.sqlite"
    leftover = dest / _RESERVED
    report = _staging_report(published)
    assert isinstance(report, dict), "success must include staging_report"
    assert report.get("dest_relation") == "same_attempt_inode"
    members = report.get("members")
    assert isinstance(members, list) and members
    member = _member_for(members, leftover)
    _assert_member_identity(member, leftover)
    assert (int(member["st_dev"]), int(member["st_ino"])) == (
        int(dest_catalog.stat().st_dev),
        int(dest_catalog.stat().st_ino),
    )


def test_c09_dest_renamed_away_does_not_truncate_slot(tmp_path):
    """If dest spelling is gone but the slot still names the catalog inode, do not wipe it."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    published = _publish(work, dest)
    assert published.get("status") == "ok"
    leftover = dest / _RESERVED
    mains = {n for n in _staging_names(dest) if not n.endswith(_SIDECARS)}
    assert mains == {_RESERVED}, f"success must leave exactly the reserved slot, got {mains}"
    assert leftover.exists()
    assert (int(leftover.stat().st_dev), int(leftover.stat().st_ino)) == (
        int(dest_catalog.stat().st_dev),
        int(dest_catalog.stat().st_ino),
    )
    assert leftover.read_bytes() == dest_catalog.read_bytes()
    snap = _identity(leftover)
    backup = dest / "catalog.sqlite.moved"
    dest_catalog.rename(backup)
    seams = _SlotSeams(dest)
    error = None
    with seams.patch():
        try:
            _publish(work, dest)
        except RuntimeError as exc:
            error = exc
    assert error is not None
    assert seams.ftruncate == 0 and seams.unlink == 0
    assert leftover.exists()
    assert _identity(leftover) == snap
    assert backup.exists()
    assert _identity(backup) == snap
