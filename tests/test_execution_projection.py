"""PR-09 / #39-B Gate 1: pure monotonic projection contracts (B1, B13 drift refuse).

Tests-only. Expected red until production projection lands. Projection may remove only
newly satisfied approved work; changed/invalid/lost/expanded/remapped work must typed-refuse
without altering the approved map.
"""
from __future__ import annotations

import importlib
import sqlite3
from copy import deepcopy

from modelark.core import db


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _projection():
    for name in (
        "modelark.execution_projection",
        "modelark.projection",
        "modelark.execution",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if any(hasattr(mod, n) for n in (
                "project_remaining", "project", "pure_project", "project_approved")):
            return mod
    raise AssertionError(
        "pure projection module required "
        "(modelark.execution_projection / projection / execution; expected Gate-1 red)")


def _project_fn(mod):
    for n in ("project_remaining", "project", "pure_project", "project_approved"):
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    raise AssertionError(
        "export project_remaining (or project/pure_project/project_approved); "
        "expected Gate-1 red")


def _sample_approved_map():
    """Minimal immutable approved assignment for pure projection inputs."""
    return {
        "proposal_id": "prop-1",
        "canonical_hash": "a" * 64,
        "tasks": [
            {
                "requirement_id": "primary:org/a",
                "row_kind": "executable",
                "repo_id": "org/a",
                "target_drive": "d0",
                "source_drive": None,
                "full_manifest_hash": "b" * 64,
                "order_key": 1,
                "guaranteed_durable": 100,
            },
            {
                "requirement_id": "primary:org/b",
                "row_kind": "baseline_satisfied",
                "repo_id": "org/b",
                "target_drive": "d0",
                "satisfying_drive": "d0",
                "full_manifest_hash": "c" * 64,
                "baseline_certificate": "d" * 64,
                "order_key": 2,
                "guaranteed_durable": 50,
            },
        ],
    }


def test_projection_api_is_pure_export():
    mod = _projection()
    project = _project_fn(mod)
    # Pure path: no connection required for pure project of frozen facts, or explicit pure flag.
    assert callable(project)


def test_satisfied_only_shrink_removes_completed_executable():
    """B1: newly satisfied approved work may leave the remaining set."""
    mod = _projection()
    project = _project_fn(mod)
    approved = _sample_approved_map()
    facts = {
        "archived_complete": {"org/a": ["d0"]},  # primary:org/a now durable on target
        "drives": {"d0": {"lifecycle": "active", "identity_epoch": 1, "fingerprint": "f" * 64}},
        "manifests": {"org/a": "b" * 64, "org/b": "c" * 64},
    }
    before = deepcopy(approved["tasks"])
    out = project(approved, facts)
    remaining = out["tasks"] if isinstance(out, dict) else list(out)
    ids = {t["requirement_id"] for t in remaining}
    assert "primary:org/a" not in ids, (
        "newly satisfied executable must shrink out of remaining work; got {ids}")
    # Approved map identity must not be rewritten in place.
    assert approved["tasks"] == before, "projection must not mutate approved map in place"


def test_invalid_or_lost_work_refuses_without_map_edit():
    """B1: lost/invalid satisfying evidence → typed refuse; approved tasks unchanged."""
    mod = _projection()
    project = _project_fn(mod)
    approved = _sample_approved_map()
    before = deepcopy(approved)
    facts = {
        "archived_complete": {},
        "drives": {
            "d0": {"lifecycle": "lost", "identity_epoch": 1, "fingerprint": "f" * 64},
        },
        "manifests": {"org/a": "b" * 64, "org/b": "c" * 64},
    }
    try:
        out = project(approved, facts)
    except Exception as exc:
        code = str(getattr(exc, "code", "") or exc).upper()
        assert any(k in code for k in (
            "APPROVED_PLACEMENT", "NO_LONGER", "DRIFT", "LOST", "REFUS", "INVALID",
            "LIFECYCLE", "TERMINAL")), (
            f"lost/invalid must typed-refuse; got {type(exc).__name__}: {exc}")
        assert approved == before, "refusal must not alter approved map"
        return
    # Returned typed refusal object
    code = str(getattr(out, "code", None) or (out.get("code") if isinstance(out, dict) else "")).upper()
    assert code and any(k in code for k in (
        "APPROVED_PLACEMENT", "NO_LONGER", "DRIFT", "LOST", "REFUS", "INVALID", "LIFECYCLE")), (
        f"expected typed refusal, got success {out!r}")
    assert approved == before


def test_content_drift_refuses_without_shrinking_map():
    """B13: full_manifest / baseline drift refuses; does not drop tasks as 'invalid work'."""
    mod = _projection()
    project = _project_fn(mod)
    approved = _sample_approved_map()
    before = deepcopy(approved)
    facts = {
        "archived_complete": {"org/b": ["d0"]},
        "drives": {"d0": {"lifecycle": "active", "identity_epoch": 1, "fingerprint": "f" * 64}},
        "manifests": {"org/a": "b" * 64, "org/b": "9" * 64},  # drifted from stored c*64
    }
    try:
        out = project(approved, facts)
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in (
            "MANIFEST", "DRIFT", "HASH", "BASELINE", "CONTENT", "CHANGED", "REFUS")), exc
        assert approved == before
        return
    code = str(getattr(out, "code", None) or (out.get("code") if isinstance(out, dict) else "")).upper()
    assert code, f"content drift must refuse; got {out!r}"
    assert approved == before


def test_expanded_or_remapped_work_refuses():
    """B1: expansion or target remap is not a shrink — typed failure."""
    mod = _projection()
    project = _project_fn(mod)
    approved = _sample_approved_map()
    facts = {
        "archived_complete": {},
        "drives": {"d0": {"lifecycle": "active", "identity_epoch": 1, "fingerprint": "f" * 64}},
        "manifests": {"org/a": "b" * 64, "org/b": "c" * 64},
        "expanded_requirements": ["primary:org/new"],  # work not in approved map
        "remap": {"primary:org/a": "d1"},
    }
    try:
        out = project(approved, facts)
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in (
            "EXPAND", "REMAP", "FRESH", "PREVIEW", "APPROVED", "REFUS", "DIVERG")), exc
        return
    code = str(getattr(out, "code", None) or (out.get("code") if isinstance(out, dict) else "")).upper()
    assert code, f"expand/remap must refuse; got {out!r}"


def test_projection_deterministic_under_task_shuffle():
    mod = _projection()
    project = _project_fn(mod)
    approved = _sample_approved_map()
    facts = {
        "archived_complete": {},
        "drives": {"d0": {"lifecycle": "active", "identity_epoch": 1, "fingerprint": "f" * 64}},
        "manifests": {"org/a": "b" * 64, "org/b": "c" * 64},
    }
    a = deepcopy(approved)
    b = deepcopy(approved)
    b["tasks"] = list(reversed(b["tasks"]))
    out_a = project(a, facts)
    out_b = project(b, facts)
    # Normalize comparable remaining ids
    def ids(o):
        tasks = o["tasks"] if isinstance(o, dict) else list(o)
        return sorted(t["requirement_id"] for t in tasks)
    assert ids(out_a) == ids(out_b), "projection remaining set must be order-independent"


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
