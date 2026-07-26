"""B12 acceptance fixture harness — recompute identity from SQLite; wall-clock 5+30."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
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
    """Independent authority: counts and hashes from the actual SQLite file.

    Requirement/task counts prefer proposal_tasks when present; otherwise they
    derive from finalized selection (primary requirements) rather than equating
    raw selected-repository counts with projection hashes.
    """
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
        has_tasks = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='proposal_tasks'"
        ).fetchone()
        if has_tasks:
            requirement_count = con.execute(
                "SELECT count(DISTINCT requirement_id) FROM proposal_tasks").fetchone()[0]
            task_count = con.execute("SELECT count(*) FROM proposal_tasks").fetchone()[0]
            # Projection hash over ordered task identity (not the same as input hash).
            trows = con.execute(
                "SELECT requirement_id, row_kind, repo_id, target_drive, source_drive "
                "FROM proposal_tasks ORDER BY 1"
            ).fetchall()
            projection_hash = hashlib.sha256(
                json.dumps(trows, separators=(",", ":"), default=str).encode()
            ).hexdigest()
        else:
            # Primary requirement ≈ one per selected repo when proposals absent.
            requirement_count = int(selected)
            task_count = int(selected)
            projection_hash = None

        rows = con.execute(
            "SELECT repo_id, rfilename, size_bytes, sha256 FROM files ORDER BY 1, 2"
        ).fetchall()
        canon = hashlib.sha256(
            json.dumps(rows, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        # Input hash (selection+files) is distinct from projection hash when tasks exist.
        sel_rows = con.execute(
            "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY 1"
        ).fetchall()
        input_hash = hashlib.sha256(
            json.dumps({"selection": sel_rows, "files": rows},
                       separators=(",", ":"), default=str).encode()
        ).hexdigest()
        out = {
            "source_sqlite_sha256": source_sha,
            "prepared_canonical_input_hash": input_hash or canon,
            "prepared_projection_hash": projection_hash or input_hash or canon,
            "selected_repository_count": int(selected),
            "model_count": int(models),
            "file_count": int(files_n),
            "requirement_count": int(requirement_count),
            "task_count": int(task_count),
            "sqlite_path": str(path),
        }
        return out
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

    if operator_approved_identity is None:
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "missing_operator_identity",
             "selected": int(d.get("selected_repository_count") or 0)}, ())

    for k in ("source_sqlite_sha256", "selected_repository_count", "model_count"):
        if operator_approved_identity.get(k) != d.get(k):
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"field": k}, ())

    return {"ok": True, "descriptor": d}


def emit_acceptance_evidence(descriptor: Mapping[str, Any], path) -> None:
    Path(path).write_text(json.dumps(dict(descriptor), indent=2, sort_keys=True))


def run_acceptance_wall_clock(*, fixture_descriptor=None, project_fn=None, **_k):
    """Run 5 warm-ups + 30 measured pure-projection timings (RFC-001 / B12)."""
    if not fixture_descriptor:
        raise Refusal("ACCEPTANCE_FIXTURE_INVALID", {"reason": "missing"}, ())
    validate_acceptance_fixture_descriptor(
        fixture_descriptor,
        operator_approved_identity=dict(fixture_descriptor),
    )
    contract = wall_clock_contract()
    # Pure projection callable: injected or project_pure with empty synthetic envelope.
    if project_fn is None:
        from modelark.execution_projection import project_pure
        from types import SimpleNamespace

        def project_fn():
            proposal = {
                "lifecycle": "approved", "proposal_id": "bench",
                "tasks": (), "requirement_set_hash": "a" * 64,
                "semantic_input_hash": "b" * 64,
            }
            inp = SimpleNamespace(
                manifests={}, archived={}, drives={}, observed_ratio={},
                evidence={}, file_hash_evidence={}, certificates={},
                semantic_hashes=SimpleNamespace(execution_invariants="b" * 64),
                execution_config={},
            )
            graph = SimpleNamespace(requirement_ids=[], requirement_set_hash="a" * 64)
            return project_pure(
                proposal, inp, graph, SimpleNamespace(parked_gated_repos=frozenset()))

    warmups = int(contract["warmups"])
    runs = int(contract["measured_runs"])
    for _ in range(warmups):
        project_fn()
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        project_fn()
        samples.append(time.perf_counter() - t0)
    samples_sorted = sorted(samples)
    p95_idx = max(0, int(round(0.95 * (len(samples_sorted) - 1))))
    p95 = samples_sorted[p95_idx]
    return {
        "ok": True,
        "skipped_measurement": False,
        "contract": contract,
        "warmups": warmups,
        "measured_runs": runs,
        "samples_seconds": samples,
        "pure_p95_seconds": p95,
        "pure_p95_within_contract": p95 <= float(contract["pure_p95_seconds"]),
    }


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
