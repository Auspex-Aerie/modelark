"""Disposable Linux destination contracts; never use an actual archive or USB drive."""
import importlib
import os
from types import SimpleNamespace

import pytest

from modelark.slice.transaction import DestinationBinding, TransferRefusal


@pytest.fixture
def destination(tmp_path):
    module = importlib.import_module("modelark.slice.destination")
    linux = importlib.import_module("modelark.slice.linux")
    root = tmp_path / "destination"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    binding = DestinationBinding("test-device", "test-filesystem", "test-mount", 10_000_000)
    plan = SimpleNamespace(destination=binding, seal="a" * 64)
    store = SimpleNamespace(root=private, load=lambda tx: plan)
    with linux.BoundTree(root, writable=True) as tree:
        adapter = module.UsbDestination(tree, binding, store, "b" * 32)
        yield adapter, root, store, module


def test_certified_directory_and_file_survive_adapter_reconstruction(destination):
    adapter, root, store, module = destination
    adapter.create_directory("delivery", "1" * 32)
    adapter.create_file("delivery/.slice-" + "2" * 32, "2" * 32)
    adapter.append("delivery/.slice-" + "2" * 32, "2" * 32, b"original bytes")
    adapter.flush("delivery/.slice-" + "2" * 32)
    reopened = module.UsbDestination(adapter.tree, adapter.binding, store, adapter.tx)
    assert reopened.inspect("delivery").token == "1" * 32
    reopened.publish("delivery/.slice-" + "2" * 32, "delivery/model", "2" * 32)
    with reopened.read("delivery/model") as stream:
        assert stream.read() == b"original bytes"
    assert (root / "delivery/model").stat().st_nlink == 2
    reopened.discard_temporary("delivery/.slice-" + "2" * 32, "2" * 32)
    assert (root / "delivery/model").stat().st_nlink == 1


def test_directory_crash_before_certificate_is_never_adopted(destination, monkeypatch):
    adapter, root, _, _ = destination
    def crash(*args):
        raise RuntimeError("power cut before durable certificate")
    monkeypatch.setattr(adapter, "_certify", crash)
    with pytest.raises(RuntimeError):
        adapter.create_directory("uncertain", "1" * 32)
    assert (root / "uncertain").is_dir()
    assert adapter.inspect("uncertain").token is None
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.create_directory("uncertain", "1" * 32)
    assert (root / "uncertain").is_dir()


def test_file_crash_before_certificate_never_leaves_a_named_object(destination, monkeypatch):
    adapter, root, _, _ = destination
    monkeypatch.setattr(adapter, "_certify", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        adapter.create_file(".slice-" + "1" * 32, "1" * 32)
    assert tuple(root.iterdir()) == ()


def test_copied_xattr_and_name_cannot_authenticate_replacement_inode(destination):
    adapter, root, _, module = destination
    path, token = ".slice-" + "2" * 32, "2" * 32
    adapter.create_file(path, token)
    (root / path).rename(root / "stolen")
    (root / path).write_bytes(b"foreign")
    os.setxattr(root / path, module.OWNER_XATTR, token.encode())
    assert adapter.inspect(path).token is None
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.append(path, token, b"must not append")
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.discard_temporary(path, token)
    assert (root / path).read_bytes() == b"foreign"


def test_publish_never_replaces_foreign_destination(destination):
    adapter, root, _, _ = destination
    path, token = ".slice-" + "2" * 32, "2" * 32
    adapter.create_file(path, token)
    (root / "final").write_bytes(b"foreign")
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.publish(path, "final", token)
    assert (root / "final").read_bytes() == b"foreign"
    assert adapter.inspect(path).token == token


def test_symlink_escape_is_not_followed(destination, tmp_path):
    adapter, root, _, _ = destination
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises((TransferRefusal, OSError)):
        adapter.create_file("escape/.slice-" + "2" * 32, "2" * 32)
    assert not tuple(outside.iterdir())


def test_allocation_counts_hardlinked_inode_once_and_rejects_drift(destination, monkeypatch):
    adapter, root, _, _ = destination
    path, token = ".slice-" + "2" * 32, "2" * 32
    adapter.create_file(path, token)
    adapter.append(path, token, b"a" * 8192)
    adapter.publish(path, "model", token)
    own = adapter.inspect(path).allocated_bytes
    root_delta = os.fstat(adapter.tree.fd).st_blocks * 512 - adapter._root_blocks
    monkeypatch.setattr(adapter, "_available", lambda: adapter.binding.available_bytes - own - root_delta)
    adapter.check(adapter.binding, own, 1_000_000)
    monkeypatch.setattr(adapter, "_available", lambda: adapter.binding.available_bytes - own - root_delta - 4096)
    with pytest.raises(TransferRefusal, match="DESTINATION_CAPACITY_CHANGED"):
        adapter.check(adapter.binding, own, 1_000_000)


def test_unknown_objects_are_reported_without_traversing_symlinks(destination):
    adapter, root, _, _ = destination
    adapter.create_directory("delivery", "1" * 32)
    (root / "delivery/foreign").write_bytes(b"unknown")
    (root / "delivery/link").symlink_to("/etc")
    assert adapter.list_paths("delivery") == ("delivery/foreign", "delivery/link")


def test_mutating_final_path_is_forbidden(destination):
    adapter, root, _, _ = destination
    token = "2" * 32
    path = ".slice-" + token
    adapter.create_file(path, token)
    adapter.publish(path, "model", token)
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.discard_temporary("model", token)
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.append("model", token, b"not allowed")
    assert (root / "model").exists()


def test_certified_but_unlinked_file_crash_can_retry_without_adoption(destination, monkeypatch):
    adapter, root, _, module = destination
    token = "2" * 32
    path = ".slice-" + token
    link = module.linux.link_fd
    monkeypatch.setattr(module.linux, "link_fd", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        adapter.create_file(path, token)
    assert tuple(root.iterdir()) == ()
    monkeypatch.setattr(module.linux, "link_fd", link)
    adapter.create_file(path, token)
    assert adapter.inspect(path).token == token


def test_external_hardlinks_never_receive_allocation_credit(destination, tmp_path, monkeypatch):
    adapter, root, _, _ = destination
    token = "2" * 32
    path = ".slice-" + token
    adapter.create_file(path, token)
    os.link(root / path, tmp_path / "foreign-link")
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.check(adapter.binding, adapter.inspect(path).allocated_bytes, 1_000_000)


def test_other_transaction_cannot_use_an_existing_certificate(destination):
    adapter, _, store, module = destination
    token = "2" * 32
    path = ".slice-" + token
    adapter.create_file(path, token)
    other = module.UsbDestination(adapter.tree, adapter.binding, store, "c" * 32)
    assert other.inspect(path).token is None
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        other.discard_temporary(path, token)


@pytest.mark.parametrize("path", [".", "..", "../escape", "/absolute", "a//b", "a/../b", ""])
def test_noncanonical_paths_refuse_before_creation(destination, path):
    adapter, root, _, _ = destination
    with pytest.raises(TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.create_directory(path, "1" * 32)
    assert tuple(root.iterdir()) == ()


def test_publication_links_retained_inode_not_replaced_source_name(destination, monkeypatch):
    adapter, root, _, module = destination
    token = "2" * 32
    path = ".slice-" + token
    adapter.create_file(path, token)
    adapter.append(path, token, b"authentic")
    link = module.linux.link_fd
    def replace_source_then_link(fd, parent, name):
        (root / path).rename(root / "displaced-original")
        (root / path).write_bytes(b"foreign replacement")
        return link(fd, parent, name)
    monkeypatch.setattr(module.linux, "link_fd", replace_source_then_link)
    adapter.publish(path, "model", token)
    assert (root / "model").read_bytes() == b"authentic"
    assert (root / path).read_bytes() == b"foreign replacement"
    assert adapter.inspect("model").token == token
    assert adapter.inspect(path).token is None
