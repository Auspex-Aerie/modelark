"""Mount routing is only a hint; ambiguity never selects a weaker adapter."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from modelark.slice import folder_operator as folder, transaction as t


@pytest.mark.parametrize("parent,expected", [
    ("/media/disk/exports", "vfat"),
    ("/media/disk-other/exports", "ext4"),
    ("/media/disk/nested/exports", "ext4"),
])
def test_routing_uses_deepest_component_ancestor(monkeypatch, parent, expected):
    mounts = [SimpleNamespace(path=path, fs_type=kind) for path, kind in (
        ("/", "ext4"), ("/media/disk", "vfat"), ("/media/disk/nested", "ext4"))]
    monkeypatch.setattr(folder, "read_fs_text", lambda *args: "fixture")
    monkeypatch.setattr(folder, "parse_mounts", lambda text: mounts)
    assert folder._filesystem_type(Path(parent)) == expected


@pytest.mark.parametrize("mounts", [[], [
    SimpleNamespace(path="/", fs_type="ext4"), SimpleNamespace(path="/", fs_type="vfat"),
]])
def test_missing_or_ambiguous_mount_refuses(monkeypatch, mounts):
    monkeypatch.setattr(folder, "read_fs_text", lambda *args: "fixture")
    monkeypatch.setattr(folder, "parse_mounts", lambda text: mounts)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_UNPROVEN"):
        folder._filesystem_type(Path("/exports"))


def test_routing_io_failure_is_typed(monkeypatch):
    def unavailable(*args):
        raise OSError("mount inventory unavailable")
    monkeypatch.setattr(folder, "read_fs_text", unavailable)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_UNPROVEN"):
        folder._filesystem_type(Path("/exports"))
