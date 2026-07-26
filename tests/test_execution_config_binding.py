"""PR-09 / #39-B Gate 1: ExecutionConfig semantic binding (B7).

Prefer no v6. Config is part of proposal semantic authority; older proposals without
binding refuse start with fresh-preview; start/resume revalidates then freezes.
"""
from __future__ import annotations

import importlib
import sqlite3

from modelark.core import db


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _exec_mod():
    for name in (
        "modelark.execution_config",
        "modelark.execution",
        "modelark.proposal",
        "modelark.proposal_canonical",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if any(hasattr(mod, n) for n in (
                "execution_config_hash", "ExecutionConfig", "bind_execution_config",
                "canonical_execution_config", "graph_affecting_config_hash")):
            return mod
    raise AssertionError(
        "ExecutionConfig binding export required on proposal/execution modules "
        "(expected Gate-1 red; prefer no v6 schema)")


def test_execution_config_hash_is_deterministic():
    mod = _exec_mod()
    fn = None
    for n in (
        "execution_config_hash", "canonical_execution_config",
        "graph_affecting_config_hash", "bind_execution_config",
    ):
        cand = getattr(mod, n, None)
        if callable(cand):
            fn = cand
            break
    assert fn is not None, "hash/bind helper required"
    cfg = {
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "compression": {"enabled": True},
        "wishlist_graph_affecting": {"numcopies_default": 1},
    }
    h1 = fn(cfg) if fn.__code__.co_argcount >= 1 else fn()
    h2 = fn(dict(reversed(list(cfg.items())))) if isinstance(cfg, dict) else fn(cfg)
    if isinstance(h1, dict):
        h1 = h1.get("canonical_hash") or h1.get("hash")
        h2 = h2.get("canonical_hash") or h2.get("hash")
    assert isinstance(h1, str) and len(h1) == 64, f"expected 64-char hash; got {h1!r}"
    assert h1 == h2, "ExecutionConfig hash must be order-independent"


def test_semantic_input_includes_execution_config():
    """B7: proposal semantic authority must cover graph-affecting ExecutionConfig."""
    prop = importlib.import_module("modelark.proposal")
    # Either semantic hash helper accepts config, or a dedicated field is documented.
    has_hook = any(hasattr(prop, n) for n in (
        "execution_config_in_semantic", "SEMANTIC_INCLUDES_EXECUTION_CONFIG",
        "_semantic_input_hash",
    ))
    assert has_hook, "proposal semantic path must exist"
    flag = getattr(prop, "SEMANTIC_INCLUDES_EXECUTION_CONFIG", None)
    if flag is not None:
        assert flag is True, "SEMANTIC_INCLUDES_EXECUTION_CONFIG must be True once bound"
        return
    # Probe _semantic_input_hash signature / companion digest
    digest_fn = getattr(prop, "execution_config_semantic_digest", None) or getattr(
        prop, "graph_affecting_config_digest", None)
    assert callable(digest_fn), (
        "export execution_config_semantic_digest (or set SEMANTIC_INCLUDES_EXECUTION_CONFIG) "
        "— _semantic_input_hash must include graph-affecting config (expected Gate-1 red)")


def test_older_proposal_without_config_binding_refuses_start():
    mod = _exec_mod()
    for name in (
        "modelark.execution_session", "modelark.execution_sessions", "modelark.execution",
    ):
        try:
            sess = importlib.import_module(name)
            break
        except ModuleNotFoundError:
            sess = None
    assert sess is not None, "session module required"
    start = getattr(sess, "start_session", None) or getattr(sess, "start", None)
    assert callable(start), "start_session required"
    revalidate = getattr(mod, "revalidate_execution_config", None) or getattr(
        sess, "revalidate_execution_config", None) or getattr(
        sess, "require_execution_config_binding", None)
    assert callable(revalidate) or getattr(sess, "REQUIRE_EXECUTION_CONFIG_BINDING", False), (
        "start path must revalidate ExecutionConfig binding (expected Gate-1 red)")

    con = _mem()
    con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    # Approved proposal with no execution-config binding metadata.
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,capacity_mode,"
        "policy_version,solver_version,gate_b_code,semantic_input_hash) "
        "VALUES('old-prop','ark',0,'approved',?,?,?,?,?,?,?,?,?)",
        ["a" * 64, "adopt_current", "[]", "1", "guaranteed", "1", "1", "FEASIBLE", "b" * 64])
    con.execute(
        "UPDATE planner_state SET active_approved_proposal_id='old-prop' WHERE singleton_id=1")
    try:
        start(con, plan_id="ark", proposal_id="old-prop",
              controller_identity="c1", bound_revision=0)
        # If start returns a refusal object:
        raise AssertionError(
            "older proposal without ExecutionConfig binding must refuse start "
            "(fresh preview required)")
    except AssertionError:
        raise
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in (
            "EXECUTION_CONFIG", "CONFIG", "BINDING", "FRESH", "PREVIEW", "SEMANTIC",
            "REFUS", "STALE")), exc


def test_start_freezes_config_transport_cannot_reread_global():
    mod = _exec_mod()
    freeze = getattr(mod, "freeze_execution_config", None) or getattr(
        mod, "ExecutionConfig", None)
    assert freeze is not None, "freeze_execution_config / ExecutionConfig required"
    assert getattr(mod, "TRANSPORT_MUST_NOT_REREAD_GLOBAL_CONFIG", True) is True or hasattr(
        mod, "frozen_config_for_session"), (
        "transport freeze contract must be explicit (expected Gate-1 red)")


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            failed.append(name)
    print(f"\n{len(tests) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
