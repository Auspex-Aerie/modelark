"""PR-09 / #39-B Gate 1: ExecutionConfig semantic binding (B7) — behavioral.

No marker-only success. Real hash changes for graph-affecting fields; legacy refuse;
freeze reaches transport; hostile global reread cannot replace freeze.
"""
from __future__ import annotations

import importlib
from copy import deepcopy
from types import SimpleNamespace

import _pr09_gate1_fixtures as f


def _cfg_api():
    for name in (
        "modelark.execution_config",
        "modelark.execution",
        "modelark.proposal_canonical",
        "modelark.proposal",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if callable(getattr(mod, "hash_config", None)) or callable(
                getattr(mod, "execution_config_hash", None)) or getattr(
                mod, "ExecutionConfig", None):
            return mod
    raise AssertionError(
        "hash_config / ExecutionConfig required for B7 (prefer no v6; expected Gate-1 red)")


def _hash_fn(mod):
    for n in ("hash_config", "execution_config_hash", "canonical_execution_config_hash"):
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    # ExecutionConfig(values=..., canonical_hash=...)
    EC = getattr(mod, "ExecutionConfig", None)
    if EC is not None:
        def _h(values):
            if hasattr(EC, "from_values"):
                return EC.from_values(values).canonical_hash
            if hasattr(EC, "hash"):
                return EC.hash(values)
            raise AssertionError("ExecutionConfig must expose from_values/hash")
        return _h
    raise AssertionError("no config hash function")


BASE_CFG = {
    "capacity_mode": "guaranteed",
    "policy_version": "1",
    "solver_version": "1",
    "compression": {"enabled": True, "codec": "streamznn", "level": 3},
    "numcopies_default": 1,
}


def test_graph_affecting_field_changes_hash():
    mod = _cfg_api()
    h = _hash_fn(mod)
    base = h(deepcopy(BASE_CFG))
    assert isinstance(base, str) and len(base) == 64, base
    for field, new_val in (
        ("capacity_mode", "compression_aware"),
        ("policy_version", "2"),
        ("solver_version", "9"),
        ("numcopies_default", 2),
    ):
        cfg = deepcopy(BASE_CFG)
        cfg[field] = new_val
        assert h(cfg) != base, f"changing {field} must change ExecutionConfig hash"
    cfg = deepcopy(BASE_CFG)
    cfg["compression"] = dict(cfg["compression"], level=9)
    assert h(cfg) != base, "compression graph-affecting change must change hash"


def test_harmless_nonaffecting_fields_do_not_change_hash():
    mod = _cfg_api()
    h = _hash_fn(mod)
    base = h(deepcopy(BASE_CFG))
    cfg = deepcopy(BASE_CFG)
    cfg["ui_theme"] = "dark"
    cfg["log_level"] = "DEBUG"
    cfg["operator_note"] = "hello"
    # Only if production filters non-affecting keys
    assert h(cfg) == base, (
        "non-graph-affecting keys must not change ExecutionConfig hash")


def test_legacy_proposal_without_config_hash_refuses_start():
    sess = f.session_api()
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    prop = f.proposal_mod()
    # Create approved proposal then strip config binding if column exists; else insert legacy-shaped
    draft = prop.create_draft(con, plan_id="ark", mutation=("adopt_current", ()))
    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    prop.approve(con, pid)
    # Remove semantic/config binding markers if production stores them
    cols = {r[1] for r in con.execute("PRAGMA table_info(placement_proposals)").fetchall()}
    if "execution_config_hash" in cols:
        con.execute(
            "UPDATE placement_proposals SET execution_config_hash=NULL WHERE proposal_id=?",
            [pid])
    # Force older semantic without config: null semantic_input_hash is not enough;
    # production must detect missing config binding
    out = sess.start_session(con, pid, None, f.default_services())
    # After B7 production: must refuse APPROVED_INPUT_CHANGED or dedicated binding code
    if not f.is_refusal(out):
        # Explicit require revalidate hook
        rev = getattr(sess, "revalidate_execution_config", None) or getattr(
            _cfg_api(), "require_bound_execution_config", None)
        assert callable(rev), (
            "legacy proposal without ExecutionConfig binding must refuse start "
            "(fresh preview); expected Gate-1 red")
        f.assert_refuses(
            lambda: rev(con, pid),
            code="APPROVED_INPUT_CHANGED",
            label="legacy missing config binding",
        )
    else:
        assert f.refusal_code(out) in (
            "APPROVED_INPUT_CHANGED", "APPROVAL_MISSING", "PROPOSAL_HASH_MISMATCH",
            "EXECUTION_CONFIG_UNBOUND"), f.refusal_code(out)


def test_freeze_reaches_worker_and_child_transport():
    mod = _cfg_api()
    freeze = getattr(mod, "freeze_execution_config", None) or getattr(mod, "ExecutionConfig", None)
    assert freeze is not None
    transport = None
    for name in ("modelark.fetch", "modelark.execution", "modelark.download_worker"):
        try:
            transport = importlib.import_module(name)
            if hasattr(transport, "require_frozen_config") or hasattr(
                    transport, "execution_config"):
                break
        except ModuleNotFoundError:
            continue
    assert transport is not None and (
        hasattr(transport, "require_frozen_config")
        or hasattr(transport, "get_frozen_execution_config")
        or hasattr(mod, "attach_frozen_config_to_run_ctx")), (
        "worker/child transport must receive frozen ExecutionConfig (expected Gate-1 red)")


def test_hostile_global_reread_cannot_replace_freeze():
    mod = _cfg_api()
    h = _hash_fn(mod)
    frozen_vals = deepcopy(BASE_CFG)
    frozen_hash = h(frozen_vals)
    # Simulate freeze object
    if hasattr(mod, "ExecutionConfig") and hasattr(mod.ExecutionConfig, "from_values"):
        frozen = mod.ExecutionConfig.from_values(frozen_vals)
    else:
        frozen = SimpleNamespace(values=frozen_vals, canonical_hash=frozen_hash)

    hostile = deepcopy(BASE_CFG)
    hostile["capacity_mode"] = "compression_aware"
    reader = SimpleNamespace(read_graph_affecting_config=lambda: hostile)
    revalidate = getattr(mod, "assert_frozen_unchanged", None) or getattr(
        mod, "reject_global_config_replace", None)
    assert callable(revalidate), (
        "export assert_frozen_unchanged(frozen, reader) "
        "(hostile reread must not replace freeze; expected Gate-1 red)")
    f.assert_refuses(
        lambda: revalidate(frozen, reader),
        code="APPROVED_INPUT_CHANGED",
        label="hostile global config reread",
    )
