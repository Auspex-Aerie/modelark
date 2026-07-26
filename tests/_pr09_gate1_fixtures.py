"""Shared Gate-1 fixtures for PR-09 / #39-B (tests-only).

Authoritative seams (RFC-002):
  project_pure(proposal, current_input, current_graph, session_overlay)
  start_session(con, proposal_id, predecessor_id, services)

Revision, config, and capacity evidence are derived by production/services — tests inject
services and build real PR-08 drafts/approvals rather than client-authored incomplete rows.
"""
from __future__ import annotations

import importlib
import sqlite3
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock


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
    """Seed ark plan, drives, selection, files — same authority model as PR-08 CAS tests."""
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
    for label, free in (("d0", 10**12), ("d1", 10**12)):
        con.execute(
            "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
            "write_authority,filesystem_capacity_bytes) "
            "VALUES(?,?,?,'primary',0,'active','enabled',1,1,?,'dedicated_local',?)",
            [label, free, free, "f" * 64, free])
        con.execute(
            "INSERT OR IGNORE INTO drive_dirty_generations"
            "(drive_label,identity_epoch,generation,operation_code) VALUES(?,1,1,'seed')",
            [label])
        con.execute(
            "INSERT OR IGNORE INTO drive_clean_anchors"
            "(drive_label,identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
            "identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
            "VALUES(?,1,1,?,?,?,'dedicated_local','seed','seed','2026-01-01T00:00:00Z')",
            [label, free, free, "f" * 64])
    if plan.get(con, "ark") is None:
        plan.create(con, "ark", name="Ark")
    for label in ("d0", "d1"):
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
    """Injected services for start_session — clock, config, fences, capacity observe."""
    clock = SimpleNamespace(now=lambda: "2026-01-01T00:00:00Z")
    config = SimpleNamespace(
        read_graph_affecting_config=lambda: {
            "capacity_mode": "guaranteed",
            "policy_version": "1",
            "solver_version": "1",
            "compression": {"enabled": True, "codec": "streamznn"},
            "numcopies_default": 1,
        })
    controller_flock = SimpleNamespace(
        hold=lambda: mock.MagicMock(
            __enter__=lambda s: None, __exit__=lambda *a: False))
    drive_fences = SimpleNamespace(
        hold_all_sorted=lambda _ids: mock.MagicMock(
            __enter__=lambda s: (), __exit__=lambda *a: False))
    ns = SimpleNamespace(
        clock=clock,
        config=config,
        controller_flock=controller_flock,
        drive_fences=drive_fences,
        lease_ttl=3600,
        observe_exact_capacity=lambda *a, **k: {},
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def create_and_approve(con, *, mutation=("adopt_current", ()), services=None):
    """Real PR-08 draft → approve path; returns (prop, proposal_id, loaded_proposal_dict)."""
    prop = proposal_mod()
    create = getattr(prop, "create_draft", None) or getattr(prop, "preview_and_draft")
    try:
        draft = create(con, plan_id="ark", mutation=mutation)
    except TypeError:
        draft = create(con, "ark", mutation)
    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    approve = getattr(prop, "approve", None) or getattr(prop, "approve_proposal")
    svc = services
    if svc is None:
        # Prefer proposal default services with offline/anchor path
        try:
            approve(con, pid, mutation=mutation)
        except TypeError:
            approve(con, pid)
    else:
        try:
            approve(con, pid, mutation=mutation, services=svc)
        except TypeError:
            approve(con, pid, services=svc)
    loaded = prop.load_proposal(con, pid)
    assert loaded["lifecycle"] == "approved", loaded
    return prop, pid, loaded


def project_pure_fn():
    """Canonical RFC-002 seam only."""
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
        start = getattr(mod, "start_session", None)
        if callable(start):
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
    """Returned Refusal/dict or raised exception with exact code — never swallow AssertionError."""
    want = code.upper()
    try:
        out = call()
    except AssertionError:
        raise
    except Exception as exc:
        got = refusal_code(exc)
        msg = str(exc).upper()
        if got == want or want in msg or f"CODE={want}" in msg:
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
    name = type(out).__name__
    if name in ("Refusal", "TypedRefusal") and getattr(out, "code", None):
        return True
    # ExecutionProjection has tasks; refusals do not
    return bool(getattr(out, "code", None)) and not hasattr(out, "tasks")


EMPTY_OVERLAY = SimpleNamespace(parked_gated_repos=frozenset())
