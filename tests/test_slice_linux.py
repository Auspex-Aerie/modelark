"""Kernel confinement tests on disposable directories, not real mount/device probes."""
import ctypes
import errno
import os

import pytest

from modelark.slice.linux import BoundTree, _Statx, link_fd, rename_noreplace
from modelark.slice.transaction import TransferRefusal


def test_statx_buffer_matches_kernel_abi():
    assert ctypes.sizeof(_Statx) == 256
    assert _Statx.mount_id.offset == 144


def test_role_mount_probe_does_not_weaken_owned_birth_requirement(monkeypatch):
    from types import SimpleNamespace
    from modelark.slice import linux
    def statx(fd, path, flags, mask, output):
        output._obj.mask = 0x1100
        output._obj.ino = 1
        output._obj.mount_id = 2
        return 0
    monkeypatch.setattr(linux, '_libc', lambda: SimpleNamespace(statx=statx))
    assert linux._statx(3, require_birth=False).mount_id == 2
    with pytest.raises(TransferRefusal, match='birth time'):
        linux._statx(3)


def test_protected_proc_role_can_be_classified_without_birth_evidence():
    from modelark.slice.host_observation import resolve_protected
    target = resolve_protected('/proc')
    assert target.path == '/proc' and target.mount_id > 0 and target.inode > 0


@pytest.mark.parametrize('original_kind', ['io', 'policy'])
def test_failed_followup_mount_probe_preserves_original_error(tmp_path, monkeypatch, original_kind):
    from modelark.slice import linux
    original = (OSError(errno.EIO, 'original root IO') if original_kind == 'io'
                else TransferRefusal('DESTINATION_CHANGED', 'root was replaced'))
    with BoundTree(tmp_path) as tree:
        checks = []
        def mounts():
            checks.append(True)
            if len(checks) > 1:
                raise OSError(errno.EACCES, 'follow-up inventory denied')
            return {tree.mount_id}
        tree._mount_ids = mounts
        def open_root(*args, **kwargs):
            raise original
        monkeypatch.setattr(linux, '_openat2', open_root)
        with pytest.raises(type(original)) as caught:
            tree.check()
        assert caught.value is original


def test_confined_read_and_stable_birth_identity(tmp_path):
    (tmp_path / "file").write_bytes(b"original")
    with BoundTree(tmp_path) as tree:
        fd = tree.open("file", os.O_RDWR)
        try:
            before = tree.identity(fd)
            os.write(fd, b"changed")
            assert tree.identity(fd) == before
            assert before[2] > 0
        finally:
            os.close(fd)


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../file", "a//file", "", "."])
def test_invalid_relative_paths_refuse(tmp_path, path):
    with BoundTree(tmp_path) as tree, pytest.raises(TransferRefusal):
        tree.open(path, os.O_RDONLY)


def test_symlink_component_and_root_refuse(tmp_path):
    (tmp_path / "link").symlink_to("/etc", target_is_directory=True)
    with BoundTree(tmp_path) as tree, pytest.raises(OSError):
        tree.open("link/passwd", os.O_RDONLY)
    with pytest.raises(OSError):
        BoundTree(tmp_path / "link")


def test_root_replacement_is_detected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with BoundTree(root) as tree:
        root.rename(tmp_path / "old")
        root.mkdir()
        with pytest.raises(TransferRefusal, match="DESTINATION_CHANGED"):
            tree.check()


def test_unnamed_file_link_and_no_replace(tmp_path):
    with BoundTree(tmp_path) as tree:
        fd = os.open(".", os.O_TMPFILE | os.O_RDWR, 0o600, dir_fd=tree.fd)
        try:
            os.write(fd, b"content")
            link_fd(fd, tree.fd, "temporary")
            with pytest.raises(FileExistsError):
                link_fd(fd, tree.fd, "temporary")
            rename_noreplace(tree.fd, "temporary", tree.fd, "final")
            assert (tmp_path / "final").read_bytes() == b"content"
            (tmp_path / "foreign").write_bytes(b"foreign")
            with pytest.raises(FileExistsError):
                rename_noreplace(tree.fd, "final", tree.fd, "foreign")
        finally:
            os.close(fd)


def test_verification_callback_runs_each_boundary(tmp_path):
    seen = []
    with BoundTree(tmp_path, verify=lambda: seen.append(True)) as tree:
        tree.check()
        with tree.parent("new") as (fd, name):
            assert fd >= 0 and name == "new"
    assert len(seen) >= 2


@pytest.mark.parametrize("path_state", ["uncovered", "missing"])
def test_missing_pinned_mount_waits_without_touching_uncovered_root(tmp_path, path_state):
    root = tmp_path / "root"
    root.mkdir()
    with BoundTree(root) as probe:
        retained = probe.mount_id
    mounts = {retained}
    with BoundTree(root, mount_ids=lambda: mounts) as tree:
        root.rename(tmp_path / "old")
        if path_state == "uncovered":
            root.mkdir()
        mounts.clear()
        with pytest.raises(TransferRefusal, match="WAITING_DESTINATION"):
            tree.check()
        with pytest.raises(TransferRefusal, match="WAITING_DESTINATION"):
            tree.open("must-not-write", os.O_CREAT | os.O_WRONLY)
        assert not (root / "must-not-write").exists()
        assert not (tmp_path / "old/must-not-write").exists()


def test_missing_attachment_never_reacquires_inside_old_bound_tree(tmp_path):
    with BoundTree(tmp_path) as probe:
        retained = probe.mount_id
    mounts = {retained}
    with BoundTree(tmp_path, mount_ids=lambda: mounts) as tree:
        mounts.clear()
        with pytest.raises(TransferRefusal, match="WAITING_DESTINATION"):
            tree.check()
        mounts.add(retained)
        with pytest.raises(TransferRefusal, match="WAITING_DESTINATION"):
            tree.check()


@pytest.mark.parametrize("error", [errno.EIO, errno.ENODEV, errno.ENOENT])
@pytest.mark.parametrize("disappears", [False, True])
def test_root_io_errors_require_absence_proof_to_become_waiting(tmp_path, monkeypatch, error, disappears):
    from modelark.slice import linux
    with BoundTree(tmp_path) as probe:
        retained = probe.mount_id
    mounts = {retained}
    with BoundTree(tmp_path, mount_ids=lambda: mounts) as tree:
        def io_error(*args, **kwargs):
            if disappears:
                mounts.clear()
            raise OSError(error, "synthetic root IO failure")
        monkeypatch.setattr(linux, "_openat2", io_error)
        if disappears:
            with pytest.raises(TransferRefusal, match="WAITING_DESTINATION"):
                tree.check()
        elif error == errno.ENOENT:
            with pytest.raises(TransferRefusal, match="DESTINATION_CHANGED"):
                tree.check()
        else:
            with pytest.raises(OSError) as caught:
                tree.check()
            assert caught.value.errno == error


def test_disappearance_during_verification_is_waiting_not_changed(tmp_path):
    with BoundTree(tmp_path) as probe:
        retained = probe.mount_id
    mounts = {retained}
    with BoundTree(tmp_path, mount_ids=lambda: mounts) as tree:
        def verify():
            mounts.clear()
            raise TransferRefusal("DESTINATION_CHANGED", "observer saw uncovered host path")
        tree.verify = verify
        with pytest.raises(TransferRefusal, match="WAITING_DESTINATION"):
            tree.check()


def test_unknown_mount_inventory_never_counts_as_absence(tmp_path):
    with BoundTree(tmp_path) as tree:
        tree._mount_ids = lambda: None
        with pytest.raises(TransferRefusal, match="DESTINATION_UNPROVEN"):
            tree.check()
