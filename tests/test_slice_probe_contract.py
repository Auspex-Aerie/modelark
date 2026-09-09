"""Cross-adapter IO evidence contracts; faults are injected below actual probe helpers.

Every path/device is disposable. A volume probe receives a synthetic opened block
descriptor backed by a temporary file; no real block device is opened or read.
"""
import errno
import os
import stat
from types import MethodType, SimpleNamespace

import pytest

from modelark.slice import capacity, hardware, linux, operator, transaction as t
from modelark.slice.destination import OWNER_XATTR, _port_io
from test_slice_transaction import api as api, setup as setup


MEDIA_ERRORS = (errno.EIO, errno.ENODEV, errno.ENXIO)
POLICY_CODES = {
    "metadata": "DESTINATION_CAPACITY_UNPROVEN",
    "marker": "DESTINATION_ALLOCATION_UNPROVEN",
    "volume": "DESTINATION_CAPACITY_UNPROVEN",
    "acl": "DESTINATION_CAPACITY_UNPROVEN",
    "writable": "DESTINATION_NOT_WRITABLE",
}


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "probe-root"
    root.mkdir()
    with linux.BoundTree(root) as bound:
        yield bound


def inject_probe(monkeypatch, tree, name, number):
    """Patch a syscall beneath the helper, not the classification/helper itself."""
    original = OSError(number, "injected " + name + " failure")

    def fail(*args, **kwargs):
        raise original

    if name == "metadata":
        monkeypatch.setattr(capacity.fcntl, "ioctl", fail)
        call = lambda: capacity._root_metadata(tree)
    elif name in {"marker", "acl", "writable"}:
        attribute = {"marker": OWNER_XATTR, "acl": "system.posix_acl_default",
                     "writable": "user.modelark.slice-capability-probe"}[name]
        getxattr = os.getxattr

        def read_attribute(fd, key, *args, **kwargs):
            if fd == tree.fd and key == attribute:
                raise original
            return getxattr(fd, key, *args, **kwargs)

        monkeypatch.setattr(os, "getxattr", read_attribute)
        call = {"marker": lambda: capacity._require_unique_marker(tree.fd, "a" * 32),
                "acl": lambda: linux.require_no_default_acl(tree.fd),
                "writable": lambda: hardware._writable_root(tree, SimpleNamespace(options={"rw"}))}[name]
    else:
        assert name == "volume"
        # Duplicate the retained temporary-directory fd, then present only that
        # duplicate as the expected block-device descriptor. pread fails before
        # any content is consumed; regular-file/device mismatch cannot mask it.
        actual_open, actual_fstat, actual_pread = os.open, os.fstat, os.pread
        opened = set()
        device = actual_fstat(tree.fd).st_dev
        block_path = f"/dev/block/{os.major(device)}:{os.minor(device)}"

        def open_block(path, *args, **kwargs):
            if path == block_path:
                fd = os.dup(tree.fd)
                opened.add(fd)
                return fd
            return actual_open(path, *args, **kwargs)

        def block_info(fd):
            if fd in opened:
                return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=device)
            return actual_fstat(fd)

        def read_block(fd, *args):
            if fd in opened:
                raise original
            return actual_pread(fd, *args)

        monkeypatch.setattr(os, "open", open_block)
        monkeypatch.setattr(os, "fstat", block_info)
        monkeypatch.setattr(os, "pread", read_block)
        call = lambda: capacity._volume(tree, SimpleNamespace(fs_uuid="synthetic-volume"))
    return call, original


@pytest.mark.parametrize("name", POLICY_CODES)
@pytest.mark.parametrize("number", (*MEDIA_ERRORS, errno.EACCES, errno.EOPNOTSUPP))
def test_low_level_probe_retains_original_errno(tree, monkeypatch, name, number):
    call, original = inject_probe(monkeypatch, tree, name, number)
    with pytest.raises(OSError) as caught:
        call()
    assert caught.value.errno == original.errno
    if caught.value is not original:
        assert caught.value.original is original


def test_missing_acl_is_positive_absence_not_probe_failure(tree, monkeypatch):
    call, _ = inject_probe(monkeypatch, tree, "acl", errno.ENODATA)
    assert call() is None


def test_missing_marker_is_policy_refusal_not_unplug(tree, monkeypatch):
    call, _ = inject_probe(monkeypatch, tree, "marker", errno.ENODATA)
    with pytest.raises(t.TransferRefusal) as caught:
        call()
    assert caught.value.code == "DESTINATION_ALLOCATION_UNPROVEN"


@pytest.mark.parametrize("probe_result,expected", [
    (None, "FALLBACK"),
    ("WAITING_DESTINATION", "WAITING_DESTINATION"),
    ("DESTINATION_CHANGED", "DESTINATION_CHANGED"),
    ("DESTINATION_UNPROVEN", "FALLBACK"),
    ("DESTINATION_CAPACITY_UNPROVEN", "FALLBACK"),
    ("raw-error", "FALLBACK"),
])
def test_classifier_only_conclusive_attachment_proof_overrides(probe_result, expected):
    from modelark.slice.io_errors import classify_io
    original = OSError(errno.EIO, "original disk IO")
    fallback = t.TransferRefusal("DESTINATION_CAPACITY_UNPROVEN", "original capacity probe")
    response = (OSError(errno.ENODEV, "reprobe failed") if probe_result == "raw-error"
                else t.TransferRefusal(probe_result, "reprobe result") if probe_result else None)

    def check():
        if response:
            raise response

    result = classify_io(original, check, fallback)
    assert result is (fallback if expected == "FALLBACK" else response)


def test_probe_failure_decorator_retains_original_and_policy():
    from modelark.slice.io_errors import ProbeFailure, probe_io
    original = OSError(errno.ENXIO, "original IO")

    @probe_io("DESTINATION_CAPACITY_UNPROVEN", "root metadata probe")
    def failed():
        raise original

    with pytest.raises(ProbeFailure) as caught:
        failed()
    assert caught.value.original is original
    assert caught.value.errno == original.errno
    assert caught.value.code == "DESTINATION_CAPACITY_UNPROVEN"
    assert "root metadata probe" in caught.value.detail


def test_outer_probe_does_not_erase_narrower_policy_or_original_exception():
    from modelark.slice.io_errors import ProbeFailure, probe_io
    original = OSError(errno.EIO, "marker read")
    narrow = ProbeFailure(original, "DESTINATION_ALLOCATION_UNPROVEN", "marker")

    @probe_io("DESTINATION_CAPACITY_UNPROVEN", "outer capacity")
    def outer():
        raise narrow

    with pytest.raises(ProbeFailure) as caught:
        outer()
    assert caught.value is narrow
    assert caught.value.original is original


def test_policy_refusal_is_not_reinterpreted_as_an_io_probe_failure():
    from modelark.slice.io_errors import probe_io
    original = t.TransferRefusal("DESTINATION_CAPACITY_UNPROVEN", "unsupported present ACL")

    @probe_io("DESTINATION_NOT_WRITABLE", "outer writable proof")
    def unsupported():
        raise original

    with pytest.raises(t.TransferRefusal) as caught:
        unsupported()
    assert caught.value is original


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("attachment", ("attached", "absent", "replaced", "unknown", "policy"))
def test_source_read_classification_preserves_source_side_and_original_failure(wrapped, attachment):
    from modelark.slice.io_errors import ProbeFailure
    from modelark.slice.local_source import _SourceErrors
    original = OSError(errno.EIO, "original source failure")
    error = (ProbeFailure(original, "DESTINATION_CAPACITY_UNPROVEN", "source metadata probe")
             if wrapped else original)

    def read(size):
        raise error

    def check():
        if attachment == "absent":
            raise t.TransferRefusal("WAITING_DESTINATION", "source backing absent")
        if attachment == "replaced":
            raise t.TransferRefusal("DESTINATION_CHANGED", "source backing replaced")
        if attachment == "unknown":
            raise OSError(errno.EACCES, "reprobe denied")
        if attachment == "policy":
            raise t.TransferRefusal("DESTINATION_UNPROVEN", "reprobe unknown")

    source = _SourceErrors(SimpleNamespace(read=read), "source-label", check)
    with pytest.raises(t.TransferRefusal) as caught:
        source.read(1024)
    expected = {"absent": "SOURCE_MISSING", "replaced": "SOURCE_CHANGED"}.get(
        attachment, "SOURCE_IDENTITY_UNPROVEN" if wrapped else "SOURCE_READ_FAILED")
    assert caught.value.code == expected
    assert caught.value.__cause__ is error
    if attachment not in {"absent", "replaced"}:
        assert "original source failure" in caught.value.detail
        assert "reprobe" not in caught.value.detail


def checked_adapter(tree, attachment):
    """Actual _CheckedDestination IO classifier with synthetic backing evidence only."""
    adapter = object.__new__(operator._CheckedDestination)
    adapter.tree = tree
    adapter._evidence = SimpleNamespace()

    def check_attachment(*args, **kwargs):
        if attachment == "absent":
            raise t.TransferRefusal("WAITING_DESTINATION", "bound backing absent")
        if attachment == "replaced":
            raise t.TransferRefusal("DESTINATION_CHANGED", "bound backing replaced")
        if attachment == "unknown":
            raise OSError(errno.EACCES, "backing reprobe unavailable")
        if attachment == "policy":
            raise t.TransferRefusal("DESTINATION_UNPROVEN", "backing evidence unavailable")

    adapter._observer = SimpleNamespace(recheck_attachment=check_attachment)
    return adapter


@pytest.mark.parametrize("name", ("metadata", "marker", "volume", "acl"))
@pytest.mark.parametrize("attachment", ("attached", "absent", "replaced", "unknown", "policy"))
def test_real_helper_through_port_io_uses_proof_based_classification(tree, monkeypatch, name, attachment):
    probe, original = inject_probe(monkeypatch, tree, name, errno.EIO)
    adapter = checked_adapter(tree, attachment)

    @_port_io
    def operation(self):
        probe()

    with pytest.raises(t.TransferRefusal) as caught:
        operation(adapter)
    expected = {"absent": "WAITING_DESTINATION", "replaced": "DESTINATION_CHANGED"}.get(
        attachment, POLICY_CODES[name])
    assert caught.value.code == expected
    if attachment in {"attached", "unknown", "policy"}:
        assert "injected " + name in caught.value.detail
        assert isinstance(caught.value.__cause__, OSError)
        assert caught.value.__cause__.errno == original.errno


@pytest.mark.parametrize("name", ("metadata", "marker", "volume", "acl"))
@pytest.mark.parametrize("attachment,state", [("absent", "waiting_destination"),
                                              ("attached", "invalidated"), ("replaced", "invalidated"),
                                              ("unknown", "invalidated")])
def test_probe_classification_reaches_durable_transaction_state(setup, tree, monkeypatch, name, attachment, state):
    transaction, store, plan, tx, _, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    probe, _ = inject_probe(monkeypatch, tree, name, errno.ENODEV)
    adapter = checked_adapter(tree, attachment)

    @_port_io
    def check(self, *args):
        probe()

    adapter.check = MethodType(check, adapter)
    with pytest.raises(t.TransferRefusal):
        transaction.start(store, tx, adapter, sources)
    assert store.status(tx).state == state
