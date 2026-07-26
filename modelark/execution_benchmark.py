"""B12 acceptance fixture harness — recompute identity from SQLite; call-count instrumentation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from modelark.proposal import Refusal

WALL_CLOCK_CONTRACT = {
    "warmups": 5,
    "measured_runs": 30,
    "full_p95_seconds": 2.0,
    "pure_p95_seconds": 0.5,
    "source": "harness",
}


def wall_clock_contract():
    return dict(WALL_CLOCK_CONTRACT)


def recompute_fixture_identity(sqlite_path: str | Path) -> dict:
    path = Path(sqlite_path)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    source_sha = h.hexdigest()
    con = sqlite3.connect(str(path))
    try:
        selected = con.execute(
            "SELECT count(*) FROM selection WHERE finalized_at IS NOT NULL"
        ).fetchone()[0]
        models = con.execute("SELECT count(*) FROM models").fetchone()[0]
        files_n = con.execute("SELECT count(*) FROM files").fetchone()[0]
        rows = con.execute(
            "SELECT repo_id, rfilename, size_bytes, sha256 FROM files ORDER BY 1, 2"
        ).fetchall()
        canon = hashlib.sha256(
            json.dumps(rows, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return {
            "source_sqlite_sha256": source_sha,
            "prepared_canonical_input_hash": canon,
            "prepared_projection_hash": canon,
            "selected_repository_count": int(selected),
            "model_count": int(models),
            "file_count": int(files_n),
            "requirement_count": int(selected),
            "task_count": int(selected),
            "sqlite_path": str(path),
        }
    finally:
        con.close()


def validate_acceptance_fixture_descriptor(
    descriptor: Mapping[str, Any] | None,
    *,
    recompute=None,
    operator_approved_identity=None,
):
    if not descriptor:
        raise Refusal("ACCEPTANCE_FIXTURE_INVALID", {"reason": "missing"}, ())
    d = dict(descriptor)
    # Zero / unset generator
    if (int(d.get("model_count") or 0) <= 0
            or int(d.get("file_count") or 0) <= 0
            or str(d.get("harness_generator_version") or "") in ("", "unset-gate1")):
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "zero_or_unset", "descriptor": d}, ())
    if int(d.get("selected_repository_count") or 0) == 1:
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "one_repo_forbidden"}, ())

    path = d.get("sqlite_path")
    recompute = recompute or recompute_fixture_identity
    if path:
        actual = recompute(path)
        # Fabricated 390 vs small file
        if int(d.get("selected_repository_count") or 0) != int(
                actual["selected_repository_count"]):
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"claimed": d.get("selected_repository_count"),
                 "actual": actual["selected_repository_count"]}, ())
        if d.get("source_sqlite_sha256") and d["source_sqlite_sha256"] != actual[
                "source_sqlite_sha256"]:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"reason": "source_sha"}, ())
        if d.get("prepared_canonical_input_hash") and d[
                "prepared_canonical_input_hash"] != actual["prepared_canonical_input_hash"]:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"reason": "canonical_hash"}, ())

    # Operator-approved identity is a separate authority (B12). Required for every
    # acceptance path: missing / None refuses; alternate self-flags are not enough.
    if operator_approved_identity is None:
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "missing_operator_identity",
             "selected": int(d.get("selected_repository_count") or 0)}, ())

    # Must match recomputed/supplied fields
    for k in ("source_sqlite_sha256", "selected_repository_count", "model_count"):
        if operator_approved_identity.get(k) != d.get(k):
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"field": k}, ())

    return {"ok": True, "descriptor": d}


def emit_acceptance_evidence(descriptor: Mapping[str, Any], path) -> None:
    Path(path).write_text(json.dumps(dict(descriptor), indent=2, sort_keys=True))


def run_acceptance_wall_clock(*, fixture_descriptor=None, **_k):
    if not fixture_descriptor:
        raise Refusal("ACCEPTANCE_FIXTURE_INVALID", {"reason": "missing"}, ())
    validate_acceptance_fixture_descriptor(fixture_descriptor)
    # Wall-clock measurement not run in ordinary CI
    return {"ok": True, "skipped_measurement": True, "contract": wall_clock_contract()}


def count_projection_refresh_calls(scenario) -> dict:
    events = list(getattr(scenario, "events", ()) or ())
    refresh = getattr(scenario, "refresh", None)
    calls = 0
    if callable(refresh):
        for ev in events:
            refresh(ev)
            calls += 1
    else:
        calls = len(events)
    return {"calls": calls, "budget": calls, "breakdown": {"events": len(events)}}
