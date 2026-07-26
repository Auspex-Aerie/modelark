"""PR-09 Gate 1: ExecutionConfig — behavioral hash/freeze/start path, no markers."""
from __future__ import annotations

import importlib
from copy import deepcopy
from types import SimpleNamespace

import _pr09_gate1_fixtures as f

BASE_CFG = {
    "capacity_mode": "guaranteed",
    "policy_version": "1",
    "solver_version": "1",
    "compression": {"enabled": True, "codec": "streamznn", "level": 3},
    "numcopies_default": 1,
}


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
        "hash_config / ExecutionConfig required (prefer no v6; expected Gate-1 red)")


def _hash_fn(mod):
    for n in ("hash_config", "execution_config_hash", "canonical_execution_config_hash"):
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    EC = getattr(mod, "ExecutionConfig", None)
    if EC is not None and hasattr(EC, "from_values"):
        return lambda values: EC.from_values(values).canonical_hash
    if EC is not None and hasattr(EC, "hash"):
        return EC.hash
    raise AssertionError("no config hash function")


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
        assert h(cfg) != base, f"{field} must change hash"
    cfg = deepcopy(BASE_CFG)
    cfg["compression"] = dict(cfg["compression"], level=9)
    assert h(cfg) != base


def test_harmless_nonaffecting_fields_stable_hash():
    mod = _cfg_api()
    h = _hash_fn(mod)
    base = h(deepcopy(BASE_CFG))
    cfg = deepcopy(BASE_CFG)
    cfg["ui_theme"] = "dark"
    cfg["log_level"] = "DEBUG"
    assert h(cfg) == base


def test_legacy_unbound_semantic_refuses_start():
    """Convert approved proposal to pre-PR09 unbound semantic and refuse start."""
    sess = f.session_api()
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    prop = f.proposal_mod()
    draft = prop.create_draft(con, plan_id="ark", mutation=("adopt_current", ()))
    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    prop.approve(con, pid)
    # Explicitly strip config binding from stored proposal
    convert = getattr(sess, "strip_execution_config_binding_for_test", None) or getattr(
        _cfg_api(), "mark_proposal_pre_pr09_unbound", None)
    assert callable(convert), (
        "test/production helper to mark pre-PR09 unbound proposal required "
        "for legacy refuse contract (expected Gate-1 red)")
    convert(con, pid)
    f.assert_refuses(
        lambda: sess.start_session(con, pid, None, f.default_services()),
        code="APPROVED_INPUT_CHANGED",
        label="legacy unbound config start",
    )


def test_freeze_reaches_worker_child_via_start_path():
    """Frozen config must be attached on successful start and readable by transport."""
    sess = f.session_api()
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    _p, pid, _ = f.create_and_approve(con)
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start freeze")
    # Start result must carry frozen ExecutionConfig
    frozen = f.get_field(out, "execution_config") or f.get_field(out, "frozen_config")
    assert frozen is not None, "SessionStart must include frozen ExecutionConfig"
    ch = f.get_field(frozen, "canonical_hash")
    assert ch and len(str(ch)) == 64
    # Transport seam must consume freeze from session start, not re-read global
    for name in ("modelark.fetch", "modelark.execution", "modelark.download_worker"):
        try:
            transport = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        getter = getattr(transport, "get_frozen_execution_config", None) or getattr(
            transport, "require_frozen_config", None)
        if callable(getter):
            got = getter(out)
            assert f.get_field(got, "canonical_hash") == ch
            return
    raise AssertionError(
        "fetch/execution/download_worker must expose get_frozen_execution_config "
        "from start result (expected Gate-1 red)")


def test_hostile_global_reread_on_refresh_refuses():
    """Start/refresh/transport path must refuse replacing freeze via global reread."""
    sess = f.session_api()
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    _p, pid, _ = f.create_and_approve(con)
    services = f.default_services()
    out = f.require_success(
        sess.start_session(con, pid, None, services), label="start")
    # Mutate global config reader after freeze
    services.config = SimpleNamespace(
        read_graph_affecting_config=lambda: {
            **BASE_CFG, "capacity_mode": "compression_aware"})
    refresh = getattr(sess, "refresh_session_config", None) or getattr(
        sess, "revalidate_frozen_config", None) or getattr(
        _cfg_api(), "refresh_against_global", None)
    assert callable(refresh), (
        "refresh_session_config / revalidate_frozen_config on start path required")
    f.assert_refuses(
        lambda: refresh(out, services),
        code="APPROVED_INPUT_CHANGED",
        label="hostile global reread after freeze",
    )
