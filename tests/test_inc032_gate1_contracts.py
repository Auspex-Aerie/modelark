"""INC-032 Gate-1 contracts — acceptance skip vs fail.

Contracts only. Production is unchanged, so c01/c01b/c02/c03b/c04 stay red until
Gate 2 routes omitted config through ``wishlist.acceptance()``, treats ``{}`` as
authoritative empty, and drops generic exceptions from the DEC-052 immutability
tuple.
"""
from __future__ import annotations

import ast
import inspect
from unittest import mock

from modelark import execution_benchmark as bench
from modelark import wishlist
from modelark.proposal import Refusal
from test_pr10_gate1_contracts import (
    test_dec052_measure_refresh_leaves_evidence_bytes_unchanged as _dec052_immutability,
)


_ABSENT_REASONS = frozenset({
    "acceptance_fixture_path_absent",
    "acceptance_fixture_path_missing",
})
_FORBIDDEN_MEASURE_TYPES = ("ValueError", "RuntimeError", "OSError", "sqlite3.Error")


class _SentinelConfigError(Exception):
    """Distinct from production exceptions so c01/c02 cannot pass by accident."""


def _acceptance_boom(_hits):
    def boom(*_a, **_k):
        _hits["n"] += 1
        raise _SentinelConfigError("inc032-config-unreadable")
    return boom


def _descriptor():
    return {
        "harness_generator_version": "inc032-gate1",
        "selected_repository_count": 2,
        "model_count": 2,
        "file_count": 2,
        "operator_approved_identity": {
            "selected_repository_count": 2,
            "model_count": 2,
        },
    }


def test_c01_resolver_does_not_map_load_failure_to_absent_skip():
    """Omitted config must consult wishlist.acceptance and must not skip-as-absent."""
    hits = {"n": 0}
    error = None
    path = None
    reason = None
    with mock.patch.object(wishlist, "acceptance", side_effect=_acceptance_boom(hits)):
        try:
            path, reason = bench.resolve_acceptance_fixture_path()
        except Refusal as exc:
            error = exc

    assert hits["n"] >= 1, (
        "omitted config must call wishlist.acceptance(); "
        f"calls={hits['n']}, path={path!r}, reason={reason!r}, error={error!r}"
    )
    assert error is not None, (
        "load/acceptance failure must surface, not return a skip tuple; "
        f"got path={path!r} reason={reason!r}"
    )
    assert error.code == "ACCEPTANCE_CONFIG_UNREADABLE", error
    assert isinstance(error.__cause__, _SentinelConfigError), error.__cause__
    assert not (path is None and reason in _ABSENT_REASONS), (
        f"must not map acceptance failure to skip {reason!r}"
    )


def test_c01b_omitted_config_uses_acceptance_success_not_load():
    """Omitted config must use a successful acceptance() mapping, never wishlist.load()."""
    acc = {"n": 0}

    def fake_acceptance():
        acc["n"] += 1
        return {}

    def poisoned_load(*_a, **_k):
        raise AssertionError("wishlist.load must not run when acceptance() succeeds")

    with mock.patch.object(wishlist, "acceptance", side_effect=fake_acceptance), \
            mock.patch.object(wishlist, "load", side_effect=poisoned_load):
        path, reason = bench.resolve_acceptance_fixture_path()
    assert acc["n"] >= 1, "omitted config must call wishlist.acceptance()"
    assert path is None
    assert reason == "acceptance_fixture_path_absent"


def test_c02_wall_clock_does_not_skip_ok_on_load_failure():
    """Omitted acceptance_config must not return ok skipped_measurement on load failure."""
    hits = {"n": 0}
    error = None
    result = None
    with mock.patch.object(wishlist, "acceptance", side_effect=_acceptance_boom(hits)):
        try:
            result = bench.run_acceptance_wall_clock(fixture_descriptor=_descriptor())
        except Refusal as exc:
            error = exc

    assert hits["n"] >= 1, (
        "omitted acceptance_config must call wishlist.acceptance(); "
        f"calls={hits['n']}, result={result!r}, error={error!r}"
    )
    assert error is not None, (
        "wall_clock must not skip-ok on acceptance failure; "
        f"got {result!r}"
    )
    assert error.code == "ACCEPTANCE_CONFIG_UNREADABLE", error
    assert isinstance(error.__cause__, _SentinelConfigError), error.__cause__
    if result is not None:
        assert result.get("skipped_measurement") is not True, result
        assert result.get("ok") is not True, result


def test_c03_null_key_skips_without_wishlist():
    """Explicit null fixture key is genuine absence and must not consult wishlist."""
    cfg = {"fixture_sqlite_path": None}
    with mock.patch.object(wishlist, "acceptance") as acc_spy, mock.patch.object(
        wishlist, "load"
    ) as load_spy:
        path, reason = bench.resolve_acceptance_fixture_path(config=cfg)
        result = bench.run_acceptance_wall_clock(
            fixture_descriptor=_descriptor(),
            acceptance_config=cfg,
        )
    assert acc_spy.call_count == 0
    assert load_spy.call_count == 0
    assert path is None
    assert reason == "acceptance_fixture_path_absent"
    assert result.get("skipped_measurement") is True, result
    assert result.get("skip_reason") == "acceptance_fixture_path_absent", result


def test_c03b_empty_mapping_is_authoritative_and_does_not_load_wishlist():
    """Explicit {} is genuine absence: skip without wishlist.acceptance or load."""
    with mock.patch.object(wishlist, "acceptance") as acc_spy, mock.patch.object(
        wishlist, "load", return_value={"acceptance": {}}
    ) as load_spy:
        path, reason = bench.resolve_acceptance_fixture_path(config={})
    assert acc_spy.call_count == 0, (
        "explicit empty config must not consult wishlist.acceptance; "
        f"calls={acc_spy.call_count}"
    )
    assert load_spy.call_count == 0, (
        "explicit empty config must not consult wishlist.load; "
        f"calls={load_spy.call_count}"
    )
    assert path is None
    assert reason == "acceptance_fixture_path_absent"


def _isinstance_tuple_names(fn) -> list[str]:
    """Names in the first ``isinstance(err, (...))`` tuple in ``fn``."""
    tree = ast.parse(inspect.getsource(fn))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "isinstance":
            continue
        if len(node.args) != 2 or not isinstance(node.args[1], ast.Tuple):
            continue
        names: list[str] = []
        for elt in node.args[1].elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
            elif isinstance(elt, ast.Attribute) and isinstance(elt.value, ast.Name):
                names.append(f"{elt.value.id}.{elt.attr}")
        if names:
            return names
    return []


def test_c04_immutability_pin_rejects_generic_exceptions():
    """DEC-052 immutability pin must not treat generic exceptions as valid outcomes."""
    src = inspect.getsource(_dec052_immutability)
    assert "drain_hits" in src, "must retain the drain-entry pin"
    names = _isinstance_tuple_names(_dec052_immutability)
    assert names, "immutability pin must keep an isinstance accepted-outcome check"
    assert "Refusal" in names, f"measurement Refusal must remain admissible; got {names}"
    for name in _FORBIDDEN_MEASURE_TYPES:
        assert name not in names, (
            "immutability pin must not accept generic "
            f"{name} as a valid measurement outcome; tuple={names}"
        )

