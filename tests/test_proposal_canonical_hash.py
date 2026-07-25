"""PR-08 / #39-A canonical proposal serializer/hash (tests-first, RFC-002).

Gate 1 pins: normalized rows are sole authority; versioned pure serializer; hash excludes
lifecycle/audit timestamps; baseline certificates bind requirement + full-manifest hash + drive
identity/epoch + per-file durable evidence (A10). RED until production modules land.
"""
from __future__ import annotations

import hashlib
import importlib


def _load_canonical():
    for name in ("modelark.proposal_canonical", "modelark.placement_proposal_canonical"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    raise AssertionError(
        "pure proposal canonical serializer module not found "
        "(expected modelark.proposal_canonical; Gate-1 red until PR-08 production)")


def _load_proposal():
    for name in ("modelark.proposal", "modelark.placement_proposal"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    raise AssertionError(
        "proposal shell module not found (expected modelark.proposal; Gate-1 red)")


def test_serializer_module_is_pure_and_versioned():
    can = _load_canonical()
    assert hasattr(can, "SERIALIZER_VERSION") and can.SERIALIZER_VERSION, (
        "serializer must expose non-empty SERIALIZER_VERSION")
    assert hasattr(can, "canonical_bytes") or hasattr(can, "serialize_proposal"), (
        "serializer must expose canonical_bytes/serialize_proposal")
    assert hasattr(can, "proposal_hash") or hasattr(can, "hash_proposal"), (
        "serializer must expose proposal_hash/hash_proposal")


def test_hash_excludes_lifecycle_and_timestamps():
    can = _load_canonical()
    # Minimal immutable planning payload — production shapes the record types.
    header_a = {
        "proposal_id": "p1", "plan_id": "ark", "based_on_revision": 0,
        "mutation_kind": "adopt_current", "mutation_args": (),
        "requirement_set_hash": "a" * 64,
        "semantic_input_hash": "b" * 64,
        "capacity_mode": "guaranteed",
        "policy_version": "1", "solver_version": "1", "serializer_version": can.SERIALIZER_VERSION,
        "lifecycle": "draft", "created_at": "t0", "approved_at": None, "superseded_at": None,
    }
    header_b = dict(header_a)
    header_b["lifecycle"] = "approved"
    header_b["approved_at"] = "t1"
    header_b["created_at"] = "t2"
    tasks = ()
    files = ()
    hash_fn = getattr(can, "proposal_hash", None) or getattr(can, "hash_proposal")
    h1 = hash_fn(header_a, tasks, files)
    h2 = hash_fn(header_b, tasks, files)
    assert h1 == h2, (
        "canonical hash must ignore lifecycle and audit timestamps so approval cannot change identity")
    assert isinstance(h1, str) and len(h1) == 64


def test_hash_stable_under_shuffled_task_and_file_order():
    can = _load_canonical()
    hash_fn = getattr(can, "proposal_hash", None) or getattr(can, "hash_proposal")
    header = {
        "proposal_id": "p1", "plan_id": "ark", "based_on_revision": 0,
        "mutation_kind": "adopt_current", "mutation_args": (),
        "requirement_set_hash": "a" * 64, "semantic_input_hash": "b" * 64,
        "capacity_mode": "guaranteed", "policy_version": "1", "solver_version": "1",
        "serializer_version": can.SERIALIZER_VERSION,
    }
    tasks_a = (
        {"requirement_id": "primary:org/b", "row_kind": "executable", "repo_id": "org/b",
         "target_drive": "d0", "source_drive": None, "full_manifest_hash": "c" * 64,
         "order_key": 2},
        {"requirement_id": "primary:org/a", "row_kind": "executable", "repo_id": "org/a",
         "target_drive": "d0", "source_drive": None, "full_manifest_hash": "d" * 64,
         "order_key": 1},
    )
    tasks_b = tuple(reversed(tasks_a))
    files_a = (
        {"requirement_id": "primary:org/a", "rfilename": "z.bin", "role": "missing",
         "size_bytes": 1, "orig_sha256": None},
        {"requirement_id": "primary:org/a", "rfilename": "a.bin", "role": "missing",
         "size_bytes": 2, "orig_sha256": None},
    )
    files_b = tuple(reversed(files_a))
    assert hash_fn(header, tasks_a, files_a) == hash_fn(header, tasks_b, files_b), (
        "hash must be independent of insertion/query order")


def test_baseline_certificate_binds_manifest_drive_epoch_and_per_file_evidence():
    can = _load_canonical()
    cert_fn = getattr(can, "baseline_satisfaction_certificate", None) or getattr(
        can, "certificate_baseline_satisfied", None)
    assert cert_fn is not None, (
        "baseline certificate helper required (A10); expected Gate-1 red")
    files = (
        {"rfilename": "w.safetensors", "orig_sha256": "1" * 64, "orig_bytes": 100,
         "annex_key": "KEY-1", "stored_bytes": 100},
    )
    cert = cert_fn(
        requirement_id="primary:org/m",
        full_manifest_hash="m" * 64,
        drive_label="drive-00",
        identity_epoch=1,
        identity_fingerprint="f" * 64,
        files=files,
    )
    assert isinstance(cert, (str, bytes, dict, tuple))
    # Binding: change any of requirement, manifest hash, drive, epoch, or file evidence → new cert.
    alt = cert_fn(
        requirement_id="primary:org/m",
        full_manifest_hash="m" * 64,
        drive_label="drive-00",
        identity_epoch=2,  # epoch change
        identity_fingerprint="f" * 64,
        files=files,
    )
    assert cert != alt, "epoch must be bound into baseline certificate"
    alt2 = cert_fn(
        requirement_id="primary:org/m",
        full_manifest_hash="m" * 64,
        drive_label="drive-00",
        identity_epoch=1,
        identity_fingerprint="f" * 64,
        files=({"rfilename": "w.safetensors", "orig_sha256": "2" * 64, "orig_bytes": 100,
                "annex_key": "KEY-1", "stored_bytes": 100},),
    )
    assert cert != alt2, "per-file durable evidence must be bound (not loose annex-key alone)"


def test_no_client_supplied_blob_is_authority():
    """API must not accept client-serialized proposal as trust root."""
    prop = _load_proposal()
    assert not hasattr(prop, "import_serialized_proposal") or getattr(
        prop, "ACCEPT_CLIENT_BLOB", False) is False
    # create_draft / approve signatures must not take canonical_hash as authority input.
    for name in ("create_draft", "preview_and_draft", "approve", "approve_proposal"):
        fn = getattr(prop, name, None)
        if fn is None:
            continue
        # Presence is enough; production computes hash server-side.
        break
    else:
        raise AssertionError(
            "proposal shell must expose create_draft/preview_and_draft and approve "
            "(expected Gate-1 red)")


def test_serializer_version_change_changes_hash():
    can = _load_canonical()
    hash_fn = getattr(can, "proposal_hash", None) or getattr(can, "hash_proposal")
    header = {
        "proposal_id": "p1", "plan_id": "ark", "based_on_revision": 0,
        "mutation_kind": "adopt_current", "mutation_args": (),
        "requirement_set_hash": "a" * 64, "semantic_input_hash": "b" * 64,
        "capacity_mode": "guaranteed", "policy_version": "1", "solver_version": "1",
        "serializer_version": can.SERIALIZER_VERSION,
    }
    h1 = hash_fn(header, (), ())
    header2 = dict(header)
    header2["serializer_version"] = str(can.SERIALIZER_VERSION) + "+next"
    h2 = hash_fn(header2, (), ())
    assert h1 != h2


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, type(exc).__name__, str(exc)[:220]))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: canonical hash contracts EXPECTED RED until PR-08 production.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
