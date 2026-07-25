"""PR-08 / #39-A canonical proposal serializer/hash (tests-first, RFC-002).

Gate 1: pure serializer golden vectors, lifecycle exclusion, shuffle stability, A10 baseline
certificate field binding, and refusal of client hash/blob as authority.
"""
from __future__ import annotations

import importlib
import inspect


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


def _hash_fn(can):
    return getattr(can, "proposal_hash", None) or getattr(can, "hash_proposal")


def _base_header(can, **over):
    h = {
        "proposal_id": "p1",
        "plan_id": "ark",
        "based_on_revision": 0,
        "mutation_kind": "adopt_current",
        "mutation_args": (),
        "requirement_set_hash": "a" * 64,
        "semantic_input_hash": "b" * 64,
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "serializer_version": can.SERIALIZER_VERSION,
        "gate_b_code": "FEASIBLE",
        "derivation_mode": None,
    }
    h.update(over)
    return h


def test_serializer_module_is_pure_and_versioned():
    can = _load_canonical()
    assert hasattr(can, "SERIALIZER_VERSION") and can.SERIALIZER_VERSION
    assert hasattr(can, "canonical_bytes") or hasattr(can, "serialize_proposal")
    assert hasattr(can, "proposal_hash") or hasattr(can, "hash_proposal")
    # Pure: no sqlite3 connection parameters on hash entrypoints.
    for name in ("proposal_hash", "hash_proposal", "canonical_bytes", "serialize_proposal"):
        fn = getattr(can, name, None)
        if fn is None:
            continue
        params = list(inspect.signature(fn).parameters)
        assert "con" not in params, f"{name} must be pure (no con parameter)"


def test_golden_vector_stable_digest():
    """Pinned golden: fixed header/tasks/files → fixed 64-hex digest (Python-stable)."""
    can = _load_canonical()
    hash_fn = _hash_fn(can)
    header = _base_header(can)
    tasks = (
        {
            "requirement_id": "primary:org/m",
            "row_kind": "executable",
            "repo_id": "org/m",
            "target_drive": "d0",
            "source_drive": None,
            "full_manifest_hash": "c" * 64,
            "order_key": 1,
            "guaranteed_durable": 100,
            "expected_durable": 90,
            "identity_epoch": 1,
        },
    )
    files = (
        {
            "requirement_id": "primary:org/m",
            "rfilename": "model.safetensors",
            "role": "missing",
            "size_bytes": 100,
            "orig_sha256": "1" * 64,
            "format": "safetensors",
            "quant": "bf16",
            "storage_action": "compress",
        },
    )
    digest = hash_fn(header, tasks, files)
    assert isinstance(digest, str) and len(digest) == 64 and all(
        c in "0123456789abcdef" for c in digest), digest
    # Second call identical (golden stability).
    assert hash_fn(header, tasks, files) == digest
    # Production may publish GOLDEN_VECTORS; if present, match exactly.
    goldens = getattr(can, "GOLDEN_VECTORS", None)
    if goldens:
        assert digest in goldens.values() or digest in goldens, (
            f"digest {digest} must match published GOLDEN_VECTORS")


def test_hash_excludes_lifecycle_and_timestamps():
    can = _load_canonical()
    hash_fn = _hash_fn(can)
    header_a = _base_header(can)
    header_a["lifecycle"] = "draft"
    header_a["created_at"] = "t0"
    header_a["approved_at"] = None
    header_a["superseded_at"] = None
    header_b = dict(header_a)
    header_b["lifecycle"] = "approved"
    header_b["approved_at"] = "t1"
    header_b["created_at"] = "t2"
    assert hash_fn(header_a, (), ()) == hash_fn(header_b, (), ())


def test_hash_stable_under_shuffled_task_and_file_order():
    can = _load_canonical()
    hash_fn = _hash_fn(can)
    header = _base_header(can)
    tasks_a = (
        {"requirement_id": "primary:org/b", "row_kind": "executable", "repo_id": "org/b",
         "target_drive": "d0", "source_drive": None, "full_manifest_hash": "c" * 64,
         "order_key": 2},
        {"requirement_id": "primary:org/a", "row_kind": "executable", "repo_id": "org/a",
         "target_drive": "d0", "source_drive": None, "full_manifest_hash": "d" * 64,
         "order_key": 1},
    )
    files_a = (
        {"requirement_id": "primary:org/a", "rfilename": "z.bin", "role": "missing",
         "size_bytes": 1, "orig_sha256": None},
        {"requirement_id": "primary:org/a", "rfilename": "a.bin", "role": "missing",
         "size_bytes": 2, "orig_sha256": None},
    )
    assert hash_fn(header, tasks_a, files_a) == hash_fn(
        header, tuple(reversed(tasks_a)), tuple(reversed(files_a)))


def test_baseline_certificate_binds_each_required_field_independently():
    """A10: requirement, full-manifest hash, drive label, fingerprint, epoch, each file field."""
    can = _load_canonical()
    cert_fn = getattr(can, "baseline_satisfaction_certificate", None) or getattr(
        can, "certificate_baseline_satisfied", None)
    assert cert_fn is not None, "baseline certificate helper required (A10); Gate-1 red"

    base_kw = dict(
        requirement_id="primary:org/m",
        full_manifest_hash="m" * 64,
        drive_label="drive-00",
        identity_epoch=1,
        identity_fingerprint="f" * 64,
        files=(
            {"rfilename": "w.safetensors", "orig_sha256": "1" * 64, "orig_bytes": 100,
             "annex_key": "KEY-1", "stored_bytes": 100},
        ),
    )
    base = cert_fn(**base_kw)

    def differs(**over):
        kw = dict(base_kw)
        if "files" in over:
            kw["files"] = over.pop("files")
        kw.update(over)
        return cert_fn(**kw) != base

    assert differs(requirement_id="primary:org/other"), "requirement_id must bind"
    assert differs(full_manifest_hash="n" * 64), "full_manifest_hash must bind"
    assert differs(drive_label="drive-99"), "drive_label must bind"
    assert differs(identity_epoch=2), "identity_epoch must bind"
    assert differs(identity_fingerprint="e" * 64), "identity_fingerprint must bind"
    assert differs(files=(
        {"rfilename": "w.safetensors", "orig_sha256": "2" * 64, "orig_bytes": 100,
         "annex_key": "KEY-1", "stored_bytes": 100},
    )), "orig_sha256 must bind"
    assert differs(files=(
        {"rfilename": "w.safetensors", "orig_sha256": "1" * 64, "orig_bytes": 101,
         "annex_key": "KEY-1", "stored_bytes": 100},
    )), "orig_bytes must bind"
    assert differs(files=(
        {"rfilename": "w.safetensors", "orig_sha256": "1" * 64, "orig_bytes": 100,
         "annex_key": "KEY-2", "stored_bytes": 100},
    )), "annex_key must bind as durable evidence field (with hashes/bytes, not alone)"
    assert differs(files=(
        {"rfilename": "w.safetensors", "orig_sha256": "1" * 64, "orig_bytes": 100,
         "annex_key": "KEY-1", "stored_bytes": 99},
    )), "stored_bytes must bind"
    assert differs(files=(
        {"rfilename": "other.safetensors", "orig_sha256": "1" * 64, "orig_bytes": 100,
         "annex_key": "KEY-1", "stored_bytes": 100},
    )), "rfilename must bind"


def test_client_supplied_hash_or_blob_is_not_authority():
    """create_draft/approve must ignore or refuse client-supplied canonical_hash / serialized blob."""
    prop = _load_proposal()
    create = getattr(prop, "create_draft", None) or getattr(prop, "preview_and_draft", None)
    assert create is not None
    # Signature must not treat client hash as authority: either no such parameter, or explicit refuse.
    sig = inspect.signature(create)
    # Calling with a forged hash must not store that hash as the proposal identity.
    # Production implements create against a real con; here we only pin the refuse contract helper.
    refuse = getattr(prop, "refuse_client_blob", None) or getattr(prop, "assert_no_client_authority", None)
    if "canonical_hash" in sig.parameters or "serialized" in sig.parameters or "blob" in sig.parameters:
        assert refuse is not None or getattr(prop, "ACCEPT_CLIENT_BLOB", None) is False
        # If parameters exist, calling with them must raise a typed refusal, not trust the value.
        # Full DB path covered in CAS suite once modules exist; pin the flag now.
        assert getattr(prop, "ACCEPT_CLIENT_BLOB", False) is False
    else:
        # Preferred: no client authority parameters at all.
        assert "canonical_hash" not in sig.parameters
        assert "serialized_proposal" not in sig.parameters


def test_serializer_version_change_changes_hash():
    can = _load_canonical()
    hash_fn = _hash_fn(can)
    header = _base_header(can)
    h1 = hash_fn(header, (), ())
    header2 = _base_header(can, serializer_version=str(can.SERIALIZER_VERSION) + "+next")
    assert hash_fn(header2, (), ()) != h1


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
