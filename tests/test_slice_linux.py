"""Kernel confinement tests on disposable directories, not real mount/device probes."""
import ctypes
import os

import pytest

from modelark.slice.linux import BoundTree, _Statx, link_fd, rename_noreplace
from modelark.slice.transaction import TransferRefusal


def test_statx_buffer_matches_kernel_abi():
    assert ctypes.sizeof(_Statx) == 256
    assert _Statx.mount_id.offset == 144


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
