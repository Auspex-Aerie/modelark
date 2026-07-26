"""Shared Gate-1 fixtures for PR-09 / #39-B (tests-only).

Canonical seams (RFC-002):
  project_pure(proposal, current_input, current_graph, session_overlay)
  start_session(con, proposal_id, predecessor_id, services)

Drive identities are distinct. Services include worker. Refusal helpers never
swallow AssertionError.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock


# Distinct fingerprints / epochs per drive (finding 18).
DRIVE_IDS = {
    "d0": {"fingerprint": "a" * 64, "epoch": 1, "role": "primary"},
    "d1": {"fingerprint": "b" * 64, "epoch": 1, "role": "replica"},
}


def proposal_mod():
    return importlib.import_module("modelark.proposal")


def mem_con():
    from modelark.core import db
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    if con.execute("SELECT count(*) FROM planner_state WHERE singleton_id=1").fetchone()[0] == 0:
        con.execute(
            "INSERT INTO planner_state(singleton_id,planner_revision,"
            "active_approved_proposal_id,next_fencing_token) VALUES(1,0,NULL,0)")
    return con


def seed_plan_selection(con, *, repos=("org/a", "org/b"), with_archive_on=None):
    from modelark import plan
    for repo in repos:
        con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
        con.execute(
            "INSERT OR IGNORE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?, 'model.safetensors', 100, 'safetensors', 'bf16', ?)",
            [repo, "1" * 64])
        con.execute(
            "INSERT OR IGNORE INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')",
            [repo])
    for label, meta in DRIVE_IDS.items():
        free = 10**12
        con.execute(
            "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
            "write_authority,filesystem_capacity_bytes) "
            "VALUES(?,?,?,?,0,'active','enabled',?,1,?,'dedicated_local',?)",
            [label, free, free, meta["role"], meta["epoch"], meta["fingerprint"], free])
        con.execute(
            "INSERT OR IGNORE INTO drive_dirty_generations"
            "(drive_label,identity_epoch,generation,operation_code) VALUES(?,?,1,'seed')",
            [label, meta["epoch"]])
        con.execute(
            "INSERT OR IGNORE INTO drive_clean_anchors"
            "(drive_label,identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
            "identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
            "VALUES(?,?,1,?,?,?,'dedicated_local','seed','seed','2026-01-01T00:00:00Z')",
            [label, meta["epoch"], free, free, meta["fingerprint"]])
    if plan.get(con, "ark") is None:
        plan.create(con, "ark", name="Ark")
    for label in DRIVE_IDS:
        if label not in plan.plan_drive_labels(con, "ark"):
            plan.add_drive(con, "ark", label)
    plan.set_active(con, "ark")
    if with_archive_on:
        for repo, drive in with_archive_on:
            con.execute(
                "INSERT OR IGNORE INTO archived("
                "repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes,orig_sha256) "
                "VALUES(?,?,?,0,100,100,?)",
                [repo, "model.safetensors", drive, "1" * 64])
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")


def default_services(**overrides):
    """RFC start/recovery services: clock, config, controller/drive fences, worker, capacity."""
    clock = SimpleNamespace(now=lambda: "2026-01-01T00:00:00Z")
    def _read_cfg():
        try:
            from modelark import wishlist
            compression = dict(wishlist.compression() or {})
        except Exception:
            compression = {"enabled": True, "codec": "streamznn", "level": 3}
        return {
            "capacity_mode": "guaranteed",
            "policy_version": "1",
            "solver_version": "1",
            "compression": compression,
            "numcopies_default": 1,
        }
    config = SimpleNamespace(read_graph_affecting_config=_read_cfg)
    controller_flock = SimpleNamespace(
        hold=lambda: mock.MagicMock(
            __enter__=lambda s: None, __exit__=lambda *a: False))
    drive_fences = SimpleNamespace(
        hold_all_sorted=lambda ids: mock.MagicMock(
            __enter__=lambda s: tuple(ids), __exit__=lambda *a: False))
    worker = SimpleNamespace(
        identity="worker-test-1",
        claim=lambda **k: None,
        inherit_fence_fds=lambda fds: list(fds),
    )
    ns = SimpleNamespace(
        clock=clock,
        config=config,
        controller_flock=controller_flock,
        drive_fences=drive_fences,
        worker=worker,
        lease_ttl=3600,
        observe_exact_capacity=lambda *a, **k: {
            label: SimpleNamespace(
                kind="offline", executable=True, admissible_free=10**12)
            for label in DRIVE_IDS
        },
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def create_and_approve(con, *, mutation=("adopt_current", ())):
    prop = proposal_mod()
    create = getattr(prop, "create_draft", None) or getattr(prop, "preview_and_draft")
    try:
        draft = create(con, plan_id="ark", mutation=mutation)
    except TypeError:
        draft = create(con, "ark", mutation)
    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    approve = getattr(prop, "approve", None) or getattr(prop, "approve_proposal")
    try:
        approve(con, pid, mutation=mutation)
    except TypeError:
        approve(con, pid)
    loaded = prop.load_proposal(con, pid)
    assert loaded["lifecycle"] == "approved", loaded
    return prop, pid, loaded


def project_pure_fn():
    for name in (
        "modelark.execution_projection",
        "modelark.projection",
        "modelark.execution",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        fn = getattr(mod, "project_pure", None)
        if callable(fn):
            return mod, fn
    raise AssertionError(
        "export project_pure(proposal, current_input, current_graph, session_overlay) "
        "per RFC-002 (expected Gate-1 red)")


def session_api():
    for name in (
        "modelark.execution_session",
        "modelark.execution_sessions",
        "modelark.execution",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if callable(getattr(mod, "start_session", None)):
            return mod
    raise AssertionError(
        "export start_session(con, proposal_id, predecessor_id, services) "
        "per RFC-002 (expected Gate-1 red)")


def refusal_code(result) -> str | None:
    if result is None:
        return None
    if isinstance(result, dict):
        return (result.get("code") or result.get("error") or "").upper() or None
    code = getattr(result, "code", None)
    if code is not None:
        return str(getattr(code, "value", code)).upper()
    return None


def assert_refuses(call: Callable[[], Any], *, code: str, label: str):
    want = code.upper()
    try:
        out = call()
    except AssertionError:
        raise
    except Exception as exc:
        got = refusal_code(exc)
        msg = str(exc).upper()
        if got == want or want in msg:
            return exc
        raise AssertionError(
            f"{label}: expected refusal code {want}, got {type(exc).__name__}: {exc}"
        ) from exc
    else:
        got = refusal_code(out)
        if got == want:
            return out
        raise AssertionError(
            f"{label}: expected refusal code {want}, call returned successfully: {out!r}")


def is_refusal(out) -> bool:
    if out is None:
        return False
    if isinstance(out, dict) and (out.get("code") or out.get("error")):
        return True
    if isinstance(out, BaseException) and getattr(out, "code", None):
        return True
    return bool(getattr(out, "code", None)) and not hasattr(out, "tasks")


def require_success(out, *, label: str):
    """Fail hard if result is a refusal — no soft skip paths."""
    if is_refusal(out):
        raise AssertionError(f"{label}: expected success, got refusal {refusal_code(out)}: {out!r}")
    return out


def session_fields(out):
    """Normalize start_session return to session object/dict."""
    if hasattr(out, "session"):
        return out.session
    if isinstance(out, dict) and "session" in out:
        return out["session"]
    return out


def get_field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


EMPTY_OVERLAY = SimpleNamespace(parked_gated_repos=frozenset())


def complete_projection_inputs(proposal, *, archived=None, drives=None, manifests=None,
                               observed_ratio=None, evidence=None, extra_requirements=None,
                               semantic_hashes=None, execution_config=None):
    """Canonical complete input bundle for project_pure (no binary floats in ratios)."""
    tasks = list(proposal.get("tasks") or ())
    files = list(proposal.get("files") or ())
    file_hash_evidence = {}
    for ff in files:
        key = (ff.get("requirement_id"), ff.get("rfilename"))
        file_hash_evidence[key] = {
            "orig_sha256": ff.get("orig_sha256"),
            "size_bytes": ff.get("size_bytes"),
            "role": ff.get("role"),
        }
    if drives is None:
        drives = {
            label: SimpleNamespace(
                lifecycle="active",
                eligibility="enabled",
                identity_epoch=meta["epoch"],
                identity_fingerprint=meta["fingerprint"],
                offline=False,
            )
            for label, meta in DRIVE_IDS.items()
        }
    if manifests is None:
        manifests = {
            t["repo_id"]: t.get("full_manifest_hash")
            for t in tasks if t.get("repo_id")
        }
    # Fixed-point ratio strings only (no binary float)
    if observed_ratio is None:
        observed_ratio = {}
    for k, v in list(observed_ratio.items()):
        if isinstance(v, float):
            raise AssertionError(
                f"observed_ratio must not use binary float; got {k}={v!r} "
                "(use fixed-point decimal string)")
    cfg = execution_config or {
        "capacity_mode": proposal.get("capacity_mode") or "guaranteed",
        "policy_version": proposal.get("policy_version") or "1",
        "solver_version": proposal.get("solver_version") or "1",
        "compression": {"enabled": True, "codec": "streamznn", "level": 3},
        "numcopies_default": 1,
    }
    current_input = SimpleNamespace(
        manifests=manifests,
        archived=archived if archived is not None else {},
        drives=drives,
        observed_ratio=observed_ratio,
        evidence=evidence or {
            label: SimpleNamespace(kind="offline", executable=True, admissible_free=10**12)
            for label in drives
        },
        file_hash_evidence=file_hash_evidence,
        execution_config=cfg,
        semantic_hashes=semantic_hashes or SimpleNamespace(
            execution_invariants=proposal.get("semantic_input_hash") or proposal.get(
                "semantic_hashes", {}).get("execution_invariants")
            if isinstance(proposal.get("semantic_hashes"), dict) else proposal.get(
                "semantic_input_hash"),
            approval_input=proposal.get("semantic_input_hash"),
        ),
        certificates={
            t["requirement_id"]: t.get("baseline_certificate")
            for t in tasks if t.get("row_kind") == "baseline_satisfied"
        },
    )
    req_ids = [t["requirement_id"] for t in tasks]
    if extra_requirements:
        req_ids = list(req_ids) + list(extra_requirements)
    current_graph = SimpleNamespace(
        requirement_ids=req_ids,
        requirement_set_hash=(
            "e" * 64 if extra_requirements
            else proposal.get("requirement_set_hash")),
    )
    return current_input, current_graph


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def catalog_identity_bundle(con_or_path) -> dict:
    """Recompute counts/hashes from an actual SQLite catalog (B12 independent authority)."""
    if isinstance(con_or_path, (str, bytes)):
        path = str(con_or_path)
        con = sqlite3.connect(path)
        close = True
        source_sha = sha256_file(path)
    else:
        con = con_or_path
        close = False
        # Serialize a snapshot for hashing
        source_sha = hashlib.sha256(
            json.dumps(
                con.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall(),
                default=str,
            ).encode()
        ).hexdigest()
    try:
        selected = con.execute(
            "SELECT count(*) FROM selection WHERE finalized_at IS NOT NULL").fetchone()[0]
        models = con.execute("SELECT count(*) FROM models").fetchone()[0]
        files = con.execute("SELECT count(*) FROM files").fetchone()[0]
        # Placeholder for requirement/task once projection exists — still from DB facts
        req = con.execute(
            "SELECT count(*) FROM proposal_tasks").fetchone()[0] if con.execute(
            "SELECT name FROM sqlite_master WHERE name='proposal_tasks'").fetchone() else 0
        tasks = req
        payload = {
            "selected_repository_count": int(selected),
            "model_count": int(models),
            "file_count": int(files),
            "requirement_count": int(tasks),
            "task_count": int(tasks),
            "source_sqlite_sha256": source_sha,
        }
        # Canonical input hash over selection+files digests
        rows = con.execute(
            "SELECT repo_id, rfilename, size_bytes, sha256 FROM files ORDER BY 1,2"
        ).fetchall()
        payload["prepared_canonical_input_hash"] = hashlib.sha256(
            json.dumps(rows, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return payload
    finally:
        if close:
            con.close()
