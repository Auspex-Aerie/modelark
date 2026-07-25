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


def _reference_canonical_digest(header, tasks, files) -> str:
    """Independent reference form for the Gate-1 golden (sort_keys JSON SHA-256).

    Production ``proposal_hash`` must match this literal for the fixed payload below.
    Serializer version field is part of the payload; lifecycle/audit fields excluded.
    """
    import hashlib
    import json
    skip = {"lifecycle", "created_at", "approved_at", "superseded_at"}
    body = {
        "header": {k: header[k] for k in sorted(header) if k not in skip},
        "tasks": sorted(tasks, key=lambda t: (t["requirement_id"], t.get("order_key", 0))),
        "files": sorted(files, key=lambda f: (f["requirement_id"], f["rfilename"])),
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


# Literal golden for the fixed payload in test_golden_vector_stable_digest (serializer_version="1").
_GOLDEN_DIGEST = "2567d7b8ab7c7c4cfe1f7377e86ceb4a4054506058a649f195955b7b2770c457"


def test_golden_vector_stable_digest():
    """Pinned golden: fixed payload → exact precomputed digest (not self-comparison)."""
    can = _load_canonical()
    hash_fn = _hash_fn(can)
    header = _base_header(can, serializer_version="1")
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
    # Self-check reference form still equals the published literal.
    assert _reference_canonical_digest(header, list(tasks), list(files)) == _GOLDEN_DIGEST
    digest = hash_fn(header, tasks, files)
    assert digest == _GOLDEN_DIGEST, (
        f"production hash must match golden {_GOLDEN_DIGEST}; got {digest}")


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
    """Exercise draft creation with forged hash/blob — must refuse or independently recompute (finding 24)."""
    import sqlite3
    from modelark.core import db

    prop = _load_proposal()
    create = getattr(prop, "create_draft", None) or getattr(prop, "preview_and_draft", None)
    assert create is not None

    # Build a minimal in-memory v5 catalog for draft persistence.
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "planner_state" not in tables:
        raise AssertionError("v5 schema required (expected Gate-1 red)")
    if con.execute("SELECT count(*) FROM planner_state").fetchone()[0] == 0:
        con.execute(
            "INSERT INTO planner_state(singleton_id,planner_revision,"
            "active_approved_proposal_id,next_fencing_token) VALUES(1,0,NULL,0)")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16',?)", ["1" * 64])
    con.execute(
        "INSERT INTO selection(repo_id,finalized_at) VALUES('org/m','2026-01-01')")
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch) VALUES('d0',10**12,10**12,'primary',0,"
        "'active','enabled',1)")
    from modelark import plan
    plan.create(con, "ark", name="Ark")
    plan.add_drive(con, "ark", "d0")
    plan.set_active(con, "ark")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")

    forged = "f" * 64
    # Attempt to pass forged hash/blob via kwargs (even if not in signature — **kwargs traps).
    try:
        draft = create(
            con, plan_id="ark", mutation=("adopt_current", ()),
            canonical_hash=forged, serialized_proposal=b"FORGED", blob={"canonical_hash": forged})
    except TypeError:
        try:
            draft = create(con, "ark", ("adopt_current", ()),
                           canonical_hash=forged, serialized=b"FORGED")
        except TypeError:
            # No kwargs accepted — still create normally and prove stored hash is recomputed.
            draft = create(con, plan_id="ark", mutation=("adopt_current", ()))
        except Exception as exc:
            # Refusal of forged input is acceptable.
            assert "HASH" in str(exc).upper() or "BLOB" in str(exc).upper() or \
                "AUTHORITY" in str(exc).upper() or "CLIENT" in str(exc).upper(), exc
            return
    except Exception as exc:
        assert "HASH" in str(exc).upper() or "BLOB" in str(exc).upper() or \
            "AUTHORITY" in str(exc).upper() or "CLIENT" in str(exc).upper(), exc
        return

    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    stored = con.execute(
        "SELECT canonical_hash FROM placement_proposals WHERE proposal_id=?",
        [pid]).fetchone()[0]
    assert stored != forged, (
        "stored canonical_hash must not equal client-forged value — recompute or refuse")
    recompute = getattr(prop, "recompute_hash", None) or getattr(prop, "hash_stored_proposal")
    assert recompute(con, pid) == stored, "stored hash must equal independent recompute"


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
