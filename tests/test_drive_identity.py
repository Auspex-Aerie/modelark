"""Compatible exclusion is a pure AND-set policy, never identity admission."""
import ast
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from modelark import drive_fence
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.drive_identity import FenceIdentity, UnprovenFenceIdentity, compatible_keys


def identity(*, serial="SERIAL", legacy=False, capacity=1000, epoch=1):
    fp = identity_fingerprint_v1(
        fs_uuid="fs", annex_uuid="annex", serial=None if legacy else serial or None,
        filesystem_capacity_bytes=capacity)
    return FenceIdentity("fs", "annex", serial, capacity, epoch, fp)


def test_old_and_new_fingerprints_expand_to_same_sorted_and_set():
    old, new = identity(legacy=True), identity()
    assert old.identity_fingerprint != new.identity_fingerprint
    assert old.lock_keys() == new.lock_keys() == tuple(sorted([
        (old.identity_fingerprint, 1), (new.identity_fingerprint, 1)]))


@pytest.mark.parametrize("serial", [None, ""])
def test_genuine_absence_has_one_key(serial):
    facts = identity(serial=serial)
    assert facts.lock_keys() == ((facts.identity_fingerprint, 1),)


@pytest.mark.parametrize("field,value", [
    ("serial", " SERIAL"), ("serial", 12), ("serial", False),
    ("fs_uuid", "fs "), ("fs_uuid", []), ("annex_uuid", {}),
    ("filesystem_capacity_bytes", None), ("filesystem_capacity_bytes", True),
    ("filesystem_capacity_bytes", 0), ("filesystem_capacity_bytes", -1),
    ("filesystem_capacity_bytes", "1000"), ("filesystem_capacity_bytes", 1000.0),
    ("identity_epoch", None), ("identity_epoch", True), ("identity_epoch", 0),
    ("identity_epoch", -1), ("identity_epoch", "1"), ("identity_epoch", 1.0),
    ("identity_fingerprint", None), ("identity_fingerprint", []),
    ("identity_fingerprint", "invented"), ("serial", "different"),
    ("fs_uuid", "different"), ("annex_uuid", "different"),
])
def test_malformed_or_unbound_facts_refuse_without_raw_key_fallback(field, value):
    with pytest.raises(UnprovenFenceIdentity):
        replace(identity(), **{field: value}).lock_keys()


def test_no_stable_uuid_refuses_before_hash_derivation():
    with pytest.raises(UnprovenFenceIdentity):
        replace(identity(), fs_uuid=None, annex_uuid=None).lock_keys()


def test_global_deduplication_capacity_and_epoch_boundaries():
    old, new = identity(), identity(capacity=2000, epoch=2)
    keys = compatible_keys([new, old, identity(legacy=True), new])
    assert keys == tuple(sorted(set(old.lock_keys() + new.lock_keys())))
    assert len(keys) == 4
    assert set(old.lock_keys()).isdisjoint(new.lock_keys())
    assert set(old.lock_keys()).isdisjoint(replace(old, identity_epoch=2).lock_keys())


@pytest.fixture
def locks(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    return tmp_path / "locks"


def child_probe(locks, keys, *, read=False):
    """Independent open-file descriptions, equivalent to legacy raw-key callers."""
    script = """
import ast
from pathlib import Path
import sys
from modelark import drive_fence
drive_fence._LOCK_DIR = Path(sys.argv[1])
hold = drive_fence.hold_drive_reads_sorted if sys.argv[3] == "read" else drive_fence.hold_drives_sorted
try:
    with hold(ast.literal_eval(sys.argv[2]), blocking=False):
        pass
except drive_fence.FenceUnavailable:
    sys.exit(7)
"""
    return subprocess.run([sys.executable, "-c", script, str(locks), repr(keys), "read" if read else "write"],
                          capture_output=True, text=True)


@pytest.mark.parametrize("legacy", [True, False])
def test_updated_holder_excludes_each_legacy_raw_key_across_processes(locks, legacy):
    with drive_fence.hold_drives_sorted(identity().lock_keys(), blocking=False):
        result = child_probe(locks, [(identity(legacy=legacy).identity_fingerprint, 1)])
        assert result.returncode == 7, result.stderr
    assert child_probe(locks, identity().lock_keys()).returncode == 0


@pytest.mark.parametrize("legacy", [True, False])
def test_each_legacy_raw_holder_excludes_updated_process(locks, legacy):
    with drive_fence.hold_drives_sorted(
            [(identity(legacy=legacy).identity_fingerprint, 1)], blocking=False):
        result = child_probe(locks, identity().lock_keys())
        assert result.returncode == 7, result.stderr


def test_context_close_retains_all_inherited_child_aliases(locks):
    child = None
    try:
        with drive_fence.hold_drives_sorted(identity().lock_keys(), blocking=False) as handles:
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                pass_fds=tuple(handle.fileno() for handle in handles),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == "ready"
        for key in identity().lock_keys():
            assert child_probe(locks, [key]).returncode == 7
        child.communicate("")
        assert child.returncode == 0
        assert child_probe(locks, identity().lock_keys()).returncode == 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.communicate()


def test_duplicate_labels_do_not_reacquire_same_open_file_description(locks):
    keys = identity().lock_keys()
    with drive_fence.hold_drives_sorted([*reversed(keys), *keys], blocking=False) as handles:
        assert len(handles) == 2


def test_shared_snapshots_coexist_across_processes_but_exclude_each_raw_writer(locks):
    with drive_fence.hold_drive_reads_sorted(identity().lock_keys()) as handles:
        assert len(handles) == 2
        assert child_probe(locks, identity().lock_keys(), read=True).returncode == 0
        for key in identity().lock_keys():
            assert child_probe(locks, [key]).returncode == 7


@pytest.mark.parametrize("legacy", [True, False])
def test_each_raw_writer_excludes_shared_snapshots(locks, legacy):
    with drive_fence.hold_drives_sorted([(identity(legacy=legacy).identity_fingerprint, 1)]):
        assert child_probe(locks, identity().lock_keys(), read=True).returncode == 7
    assert child_probe(locks, identity().lock_keys(), read=True).returncode == 0


def test_policy_has_no_io_or_workflow_dependency():
    root = Path(__file__).resolve().parents[1] / "modelark"
    tree = ast.parse((root / "drive_identity.py").read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports == {"dataclasses", "modelark.capacity_evidence"}
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))


def test_every_physical_lock_adapter_uses_shared_expansion():
    root = Path(__file__).resolve().parents[1] / "modelark"
    expected = {
        "drive_mutation.py", "drive_bootstrap.py", "admission.py", "proposal.py",
        "execution_service.py", "execution_recovery.py", "slice/sources.py",
        "drive_lifecycle.py",
    }
    found = set()
    for path in root.rglob("*.py"):
        if path.name == "drive_fence.py":
            continue
        tree = ast.parse(path.read_text())
        if not any(isinstance(node, ast.Attribute)
                   and node.attr in {"hold_drives_sorted", "hold_drive_reads_sorted", "drive_lock_path"}
                   for node in ast.walk(tree)):
            continue
        relative = path.relative_to(root).as_posix()
        found.add(relative)
        text = path.read_text()
        assert ("modelark.drive_identity" in text or "proposal._fence_keys" in text), relative
    assert found == expected
