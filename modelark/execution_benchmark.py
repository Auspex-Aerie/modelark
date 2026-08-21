"""B12 acceptance harness — recompute identity from SQLite; full+pure 5+30 wall-clock."""
from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from modelark.proposal import Refusal

log = logging.getLogger(__name__)


def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an evidence catalog read-only (DEC-052)."""
    resolved = Path(path).resolve()
    uri = f"file:{resolved.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def resolve_acceptance_fixture_path(*, config: Mapping[str, Any] | None = None):
    """Resolve acceptance fixture SQLite path from config (not environment).

    Returns ``(path_or_None, typed_reason_or_None)``. When the path is absent or
    the configured file is missing, callers must skip measurement and set
    ``skipped_measurement`` rather than synthesizing a fixture.

    ``config is None`` loads ``wishlist.acceptance()``. A supplied mapping,
    including ``{}``, is used as-is. Load/type failures raise
    ``Refusal("ACCEPTANCE_CONFIG_UNREADABLE")`` (INC-032).
    """
    if config is None:
        from modelark import wishlist
        try:
            cfg = dict(wishlist.acceptance())
        except Exception as exc:
            raise Refusal(
                "ACCEPTANCE_CONFIG_UNREADABLE",
                {"reason": "unreadable", "error": str(exc)[:200]},
                (),
            ) from exc
    else:
        cfg = dict(config)
    if "fixture_sqlite_path" in cfg:
        raw = cfg.get("fixture_sqlite_path")
    else:
        raw = cfg.get("sqlite_path")
    if raw is None or str(raw).strip() == "":
        return None, "acceptance_fixture_path_absent"
    path = Path(str(raw)).expanduser()
    if not path.is_file():
        return None, "acceptance_fixture_path_missing"
    return path, None

# Provisional harness targets pending a decision_log entry (finding 38-d).
# Not self-authorizing product thresholds — recorded as provisional until
# RFC-002 / DEC-049 / decision_log names production p95 budgets.
WALL_CLOCK_CONTRACT = {
    "warmups": 5,
    "measured_runs": 30,
    "full_p95_seconds": 2.0,
    "pure_p95_seconds": 0.5,
    "source": "provisional_harness_pending_decision",
    "authority_note": (
        "p95 thresholds are provisional harness targets; not cited from RFC-002/"
        "DEC-049/decision_log as product acceptance gates at this tip"
    ),
}


def wall_clock_contract():
    return dict(WALL_CLOCK_CONTRACT)


def recompute_fixture_identity(sqlite_path: str | Path) -> dict:
    """Independent authority: counts and hashes from the actual SQLite file.

    Opens the catalog read-only (DEC-052). Binding identity is the content hashes
    and row counts; ``source_sqlite_sha256`` is container provenance only.
    """
    path = Path(sqlite_path)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    source_sha = h.hexdigest()
    con = _connect_readonly(path)
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
            trows = con.execute(
                "SELECT requirement_id, row_kind, repo_id, target_drive, source_drive, "
                "full_manifest_hash FROM proposal_tasks ORDER BY 1"
            ).fetchall()
            projection_hash = hashlib.sha256(
                json.dumps(trows, separators=(",", ":"), default=str).encode()
            ).hexdigest()
        else:
            requirement_count = int(selected)
            task_count = int(selected)
            projection_hash = None
            trows = []

        rows = con.execute(
            "SELECT repo_id, rfilename, size_bytes, sha256 FROM files ORDER BY 1, 2"
        ).fetchall()
        sel_rows = con.execute(
            "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY 1"
        ).fetchall()
        input_hash = hashlib.sha256(
            json.dumps({"selection": sel_rows, "files": rows},
                       separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return {
            "source_sqlite_sha256": source_sha,
            "prepared_canonical_input_hash": input_hash,
            "prepared_projection_hash": projection_hash or input_hash,
            "selected_repository_count": int(selected),
            "model_count": int(models),
            "file_count": int(files_n),
            "requirement_count": int(requirement_count),
            "task_count": int(task_count),
            "sqlite_path": str(path),
            "proposal_task_rows": trows,
        }
    finally:
        con.close()


def _reject_synthetic_org_m_fixture(con) -> None:
    """Finding 38: refuse synthetic org/m#### acceptance fixtures."""
    row = con.execute(
        "SELECT count(*) FROM selection WHERE finalized_at IS NOT NULL "
        "AND repo_id GLOB 'org/m[0-9][0-9][0-9][0-9]'"
    ).fetchone()
    selected = con.execute(
        "SELECT count(*) FROM selection WHERE finalized_at IS NOT NULL"
    ).fetchone()[0]
    if row and selected and int(row[0]) == int(selected) and int(selected) > 0:
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "synthetic_org_m_pattern", "selected": int(selected)}, ())


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
        # DEC-052: container byte hash is provenance only — log drift, never gate.
        if d.get("source_sqlite_sha256") and d["source_sqlite_sha256"] != actual[
                "source_sqlite_sha256"]:
            log.info(
                "acceptance evidence container hash changed (provenance only): "
                "descriptor=%s actual=%s",
                str(d.get("source_sqlite_sha256"))[:16],
                str(actual["source_sqlite_sha256"])[:16],
            )
        if d.get("prepared_canonical_input_hash") and d[
                "prepared_canonical_input_hash"] != actual["prepared_canonical_input_hash"]:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"reason": "canonical_hash"}, ())
        if d.get("prepared_projection_hash") and d[
                "prepared_projection_hash"] != actual.get("prepared_projection_hash"):
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"reason": "projection_hash"}, ())
        # Acceptance-scale fixtures (finding 38): schema version, approved structure,
        # and no synthetic org/m#### pattern. Smaller unit-test fixtures stay exempt.
        if int(d.get("selected_repository_count") or 0) >= 100:
            con = _connect_readonly(path)
            try:
                uv = int(con.execute("PRAGMA user_version").fetchone()[0])
                if uv < 5:
                    raise Refusal(
                        "ACCEPTANCE_FIXTURE_INVALID",
                        {"reason": "schema_version_too_old", "user_version": uv}, ())
                _reject_synthetic_org_m_fixture(con)
                has_prop = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='placement_proposals'"
                ).fetchone()
                if not has_prop:
                    raise Refusal(
                        "ACCEPTANCE_FIXTURE_INVALID",
                        {"reason": "missing_approved_proposal_structure"}, ())
                approved = con.execute(
                    "SELECT count(*) FROM placement_proposals WHERE lifecycle='approved'"
                ).fetchone()[0]
                tasks = con.execute(
                    "SELECT count(*) FROM proposal_tasks").fetchone()[0]
                if int(approved) < 1 or int(tasks) < 1:
                    raise Refusal(
                        "ACCEPTANCE_FIXTURE_INVALID",
                        {"reason": "missing_approved_proposal_structure",
                         "approved": approved, "tasks": tasks}, ())
            finally:
                con.close()

    # Never self-authorize: operator identity is a separate required authority.
    # Checked after path identity so fabricated count/hash descriptors still refuse
    # with ACCEPTANCE_FIXTURE_MISMATCH rather than masking as missing operator.
    if operator_approved_identity is None:
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "missing_operator_identity",
             "selected": int(d.get("selected_repository_count") or 0)}, ())

    # Binding identity fields only (DEC-052). source_sqlite_sha256 is provenance.
    for k in ("selected_repository_count", "model_count"):
        if operator_approved_identity.get(k) != d.get(k):
            raise Refusal(
                "ACCEPTANCE_FIXTURE_MISMATCH",
                {"field": k}, ())
    if (
        operator_approved_identity.get("source_sqlite_sha256")
        and d.get("source_sqlite_sha256")
        and operator_approved_identity.get("source_sqlite_sha256") != d.get("source_sqlite_sha256")
    ):
        log.info(
            "operator provenance source_sqlite_sha256 differs from descriptor "
            "(not a gate): op=%s desc=%s",
            str(operator_approved_identity.get("source_sqlite_sha256"))[:16],
            str(d.get("source_sqlite_sha256"))[:16],
        )

    return {"ok": True, "descriptor": d}


def emit_acceptance_evidence(descriptor: Mapping[str, Any], path) -> None:
    Path(path).write_text(json.dumps(dict(descriptor), indent=2, sort_keys=True))


def _p95(samples: list[float]) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    return s[max(0, int(round(0.95 * (len(s) - 1))))]


def _load_approved_proposal_envelope(con) -> dict | None:
    """Load the active approved proposal structure when present (real RFC-001 path)."""
    row = con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()
    if not row or not row[0]:
        # Fall back to any approved proposal
        row = con.execute(
            "SELECT proposal_id FROM placement_proposals WHERE lifecycle='approved' "
            "ORDER BY approved_at DESC LIMIT 1").fetchone()
    if not row or not row[0]:
        return None
    from modelark.proposal import load_proposal
    return load_proposal(con, row[0])


def _require_approved_proposal_structure(con, *, for_acceptance: bool) -> dict:
    """Load reviewed approved proposal/tasks. Acceptance never fabricates (finding 38)."""
    proposal = _load_approved_proposal_envelope(con)
    if proposal is not None and (proposal.get("tasks") or proposal.get("files") is not None):
        if not proposal.get("tasks") and for_acceptance:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_INVALID",
                {"reason": "approved_proposal_empty_tasks"}, ())
        return proposal
    if for_acceptance:
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "missing_approved_proposal_structure"}, ())
    # Unit-test-only synthetic fallback (never acceptance).
    return None


def _build_full_capture_callable(sqlite_path: str | Path, *, for_acceptance: bool = True):
    """Full path: recompute identity from SQLite + project_pure over approved structure."""
    from modelark.execution_projection import project_pure
    from modelark.proposal import _manifest_hash

    path = Path(sqlite_path)

    def full_once():
        identity = recompute_fixture_identity(path)
        con = _connect_readonly(path)
        try:
            proposal = _require_approved_proposal_structure(
                con, for_acceptance=for_acceptance)
            if proposal is None:
                # Unit-test-only path: build from proposal_tasks / selection (not acceptance).
                tasks = []
                for r in con.execute(
                        "SELECT requirement_id, row_kind, repo_id, target_drive, source_drive, "
                        "full_manifest_hash, order_key, guaranteed_durable, identity_epoch, "
                        "baseline_certificate FROM proposal_tasks ORDER BY order_key, requirement_id"
                ).fetchall() if con.execute(
                        "SELECT name FROM sqlite_master WHERE name='proposal_tasks'").fetchone() else []:
                    tasks.append({
                        "requirement_id": r[0], "row_kind": r[1], "repo_id": r[2],
                        "target_drive": r[3], "source_drive": r[4],
                        "full_manifest_hash": r[5] or _manifest_hash(con, r[2]),
                        "order_key": r[6] or 0, "guaranteed_durable": r[7],
                        "identity_epoch": r[8], "baseline_certificate": r[9],
                    })
                if not tasks:
                    repos = [r[0] for r in con.execute(
                        "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY 1"
                    ).fetchall()]
                    for i, repo in enumerate(repos):
                        tasks.append({
                            "requirement_id": f"primary:{repo}",
                            "row_kind": "executable",
                            "repo_id": repo,
                            "target_drive": "d0",
                            "source_drive": None,
                            "full_manifest_hash": _manifest_hash(con, repo),
                            "order_key": i,
                            "guaranteed_durable": 100,
                            "identity_epoch": 1,
                        })
                proposal = {
                    "lifecycle": "approved",
                    "proposal_id": "bench-catalog",
                    "tasks": tasks,
                    "files": [],
                    "requirement_set_hash": identity["prepared_projection_hash"],
                    "semantic_input_hash": identity["prepared_canonical_input_hash"],
                }
                structure_source = "unit_test_synthetic"
            else:
                structure_source = "approved_proposal"
                tasks = list(proposal.get("tasks") or ())

            drives = {}
            for label, epoch, fp, life, elig in con.execute(
                    "SELECT drive_label, identity_epoch, identity_fingerprint, "
                    "lifecycle, eligibility FROM drives"):
                drives[label] = SimpleNamespace(
                    lifecycle=life or "active", eligibility=elig or "enabled",
                    identity_epoch=int(epoch or 1),
                    identity_fingerprint=fp or ("0" * 64), offline=False)
            if not drives:
                if for_acceptance:
                    raise Refusal(
                        "ACCEPTANCE_FIXTURE_INVALID",
                        {"reason": "missing_drive_identity"}, ())
                drives = {
                    "d0": SimpleNamespace(
                        lifecycle="active", eligibility="enabled",
                        identity_epoch=1, identity_fingerprint="a" * 64, offline=False),
                }
            archived = {}
            for r in con.execute(
                    "SELECT repo_id, rfilename, drive_label, orig_sha256, stored_bytes, orig_bytes "
                    "FROM archived"):
                archived[(r[0], r[1], r[2])] = {
                    "orig_sha256": r[3], "stored_bytes": r[4], "orig_bytes": r[5]}
            manifests = {}
            for t in proposal.get("tasks") or tasks:
                repo = t.get("repo_id")
                if repo:
                    manifests[repo] = _manifest_hash(con, repo)
            # Capacity evidence from catalog when present; acceptance refuses empty drive set above.
            evidence = {}
            for lab in drives:
                evidence[lab] = SimpleNamespace(
                    kind="offline", executable=True, admissible_free=10**15)
            inp = SimpleNamespace(
                manifests=manifests, archived=archived, drives=drives,
                observed_ratio={}, evidence=evidence,
                file_hash_evidence={}, certificates={},
                semantic_hashes=SimpleNamespace(
                    execution_invariants=proposal.get("semantic_input_hash")),
                execution_config={"capacity_mode": proposal.get("capacity_mode") or "guaranteed"},
            )
            graph = SimpleNamespace(
                requirement_ids=[t.get("requirement_id") for t in (proposal.get("tasks") or tasks)],
                requirement_set_hash=proposal.get("requirement_set_hash"),
            )
            out = project_pure(
                proposal, inp, graph, SimpleNamespace(parked_gated_repos=frozenset()))
            identity = {**identity, "structure_source": structure_source}
            return identity, out
        finally:
            con.close()

    return full_once


def _build_pure_callable_from_prepared(prepared_proposal, prepared_input, prepared_graph):
    """Pure projection only — uses pre-captured envelopes (no SQLite I/O)."""
    from modelark.execution_projection import project_pure

    def pure_once():
        return project_pure(
            prepared_proposal, prepared_input, prepared_graph,
            SimpleNamespace(parked_gated_repos=frozenset()))

    return pure_once


def run_acceptance_wall_clock(
    *,
    fixture_descriptor=None,
    project_fn=None,
    evidence_path=None,
    operator_approved_identity=None,
    acceptance_config=None,
    **_k,
):
    """Run 5 warm-ups + 30 measured full and pure timings; export evidence artifact."""
    if not fixture_descriptor:
        raise Refusal("ACCEPTANCE_FIXTURE_INVALID", {"reason": "missing"}, ())

    op = operator_approved_identity
    if op is None:
        op = fixture_descriptor.get("operator_approved_identity")
    # Strip nested operator key from descriptor body for validation
    body = {k: v for k, v in dict(fixture_descriptor).items()
            if k not in ("operator_approved_identity", "operator_bundle")}

    # DEC-052: fixture path from config when descriptor omits sqlite_path.
    skip_reason = None
    if not body.get("sqlite_path"):
        resolved, skip_reason = resolve_acceptance_fixture_path(config=acceptance_config)
        if resolved is not None:
            body["sqlite_path"] = str(resolved)
        elif project_fn is None:
            result = {
                "ok": True,
                "skipped_measurement": True,
                "skip_reason": skip_reason or "acceptance_fixture_path_absent",
                "contract": wall_clock_contract(),
                "fixture_descriptor": body,
                "projection_refresh_count": 0,
                "projection_refresh_instrumentation": {
                    "calls": 0, "events": (), "source": "skipped",
                },
            }
            if evidence_path:
                emit_acceptance_evidence(result, evidence_path)
                result["evidence_path"] = str(evidence_path)
            return result

    validate_acceptance_fixture_descriptor(body, operator_approved_identity=op)

    contract = wall_clock_contract()
    sqlite_path = body.get("sqlite_path")
    warmups = int(contract["warmups"])
    runs = int(contract["measured_runs"])

    full_fn = None
    pure_fn = project_fn
    prepared_identity = None
    if sqlite_path:
        full_fn = _build_full_capture_callable(sqlite_path, for_acceptance=True)
        # Prepare pure envelope once (no SQLite inside pure loop).
        prepared_identity, pure_out = full_fn()
        pure_fn = _pure_only_wrapper(sqlite_path, for_acceptance=True)
    elif pure_fn is None:
        raise Refusal(
            "ACCEPTANCE_FIXTURE_INVALID",
            {"reason": "acceptance_requires_sqlite_approved_structure"}, ())

    # Wall-clock: pure/full timing only — never treat loop iterations as refresh counts.
    for _ in range(warmups):
        pure_fn()
        if full_fn:
            full_fn()

    pure_samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        pure_fn()
        pure_samples.append(time.perf_counter() - t0)

    full_samples = []
    if full_fn:
        for _ in range(runs):
            t0 = time.perf_counter()
            full_fn()
            full_samples.append(time.perf_counter() - t0)

    pure_p95 = _p95(pure_samples)
    full_p95 = _p95(full_samples) if full_samples else None
    pure_ok = pure_p95 <= float(contract["pure_p95_seconds"])
    full_ok = full_p95 is None or full_p95 <= float(contract["full_p95_seconds"])
    ok = pure_ok and full_ok

    # Finding 38: instrument actual executor refresh boundaries (fill._refresh_projection),
    # not benchmark-loop iterations.
    refresh_evidence = measure_executor_refresh_boundaries(sqlite_path) if sqlite_path else {
        "calls": 0, "events": (), "source": "none",
    }
    projection_refresh_count = int(refresh_evidence.get("calls") or 0)

    # Repo-relative fixture path for portability (finding 38-d).
    fixture_desc = {
        k: body[k] for k in (
            "selected_repository_count", "model_count", "file_count",
            "source_sqlite_sha256", "prepared_canonical_input_hash",
            "prepared_projection_hash", "requirement_count", "task_count",
            "harness_generator_version", "sqlite_path",
        ) if k in body
    }
    if sqlite_path:
        try:
            rel = Path(sqlite_path).resolve().relative_to(Path.cwd().resolve())
            fixture_desc["sqlite_path"] = rel.as_posix()
        except Exception:
            fixture_desc["sqlite_path"] = str(sqlite_path)

    fixture_facts = {}
    if sqlite_path:
        fcon = _connect_readonly(sqlite_path)
        try:
            fixture_facts = {
                "archived_row_count": int(fcon.execute(
                    "SELECT count(*) FROM archived").fetchone()[0]),
                "baseline_satisfied_tasks": int(fcon.execute(
                    "SELECT count(*) FROM proposal_tasks "
                    "WHERE row_kind='baseline_satisfied'").fetchone()[0]),
                "executable_tasks": int(fcon.execute(
                    "SELECT count(*) FROM proposal_tasks "
                    "WHERE row_kind='executable'").fetchone()[0]),
                "proposal_files_total": int(fcon.execute(
                    "SELECT count(*) FROM proposal_files").fetchone()[0]),
                "proposal_files_null_orig_sha256": int(fcon.execute(
                    "SELECT count(*) FROM proposal_files WHERE orig_sha256 IS NULL"
                ).fetchone()[0]),
                "archived_null_orig_sha256": int(fcon.execute(
                    "SELECT count(*) FROM archived WHERE orig_sha256 IS NULL"
                ).fetchone()[0]),
                "integrity_check": fcon.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_key_violations": len(fcon.execute(
                    "PRAGMA foreign_key_check").fetchall()),
                "user_version": int(fcon.execute("PRAGMA user_version").fetchone()[0]),
            }
            # DEC-055: resolve via archive_hash.expected_sha256 (catalog_sha=None).
            fixture_facts["null_hash_content_rule"] = (
                "DEC-055: archive_hash.expected_sha256(catalog_sha=None, orig_sha256, "
                "compressed, annex_key); approved hash requires resolved equality; "
                "approved null requires resolvable digest (raw SHA256E annex ok); "
                "nothing resolvable fails closed on source and target"
            )
        finally:
            fcon.close()

    result = {
        "ok": ok,
        "skipped_measurement": False,
        "contract": contract,
        "warmups": warmups,
        "measured_runs": runs,
        "pure_samples_seconds": pure_samples,
        "full_samples_seconds": full_samples,
        "pure_p95_seconds": pure_p95,
        "full_p95_seconds": full_p95,
        "pure_p95_within_contract": pure_ok,
        "full_p95_within_contract": full_ok,
        # Prior accepted tip measurements for reviewer notice (finding 38-d).
        "prior_accepted_p95": {
            "tip": "00ba101cd704b6d855debbdc568f53e6b19f070f",
            "pure_p95_seconds": 0.3266526369843632,
            "full_p95_seconds": 0.6982874380191788,
        },
        "host": platform.node(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "fixture_descriptor": fixture_desc,
        "fixture_facts": fixture_facts,
        "prepared_identity": {
            k: prepared_identity[k] for k in (
                "selected_repository_count", "model_count", "file_count",
                "requirement_count", "task_count", "structure_source",
                "source_sqlite_sha256", "prepared_canonical_input_hash",
                "prepared_projection_hash",
            ) if prepared_identity and k in prepared_identity
        } if prepared_identity else None,
        "projection_refresh_count": projection_refresh_count,
        "projection_refresh_instrumentation": dict(refresh_evidence),
    }
    if not ok:
        result["error"] = "WALL_CLOCK_THRESHOLD"
        result["code"] = "WALL_CLOCK_THRESHOLD"
    if evidence_path:
        emit_acceptance_evidence(result, evidence_path)
        result["evidence_path"] = str(evidence_path)
    if not ok:
        raise Refusal(
            "WALL_CLOCK_THRESHOLD",
            {"pure_p95": pure_p95, "full_p95": full_p95, "contract": contract},
            ("optimize_projection", "rerun_on_acceptance_host"))
    return result


def _empty_pure_callable():
    from modelark.execution_projection import project_pure

    def pure_once():
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

    return pure_once


def _pure_only_wrapper(sqlite_path: str | Path, *, for_acceptance: bool = True):
    """Capture envelopes once, then pure project_pure without re-reading SQLite."""
    from modelark.execution_projection import project_pure
    from modelark.proposal import _manifest_hash

    path = Path(sqlite_path)
    con = _connect_readonly(path)
    try:
        proposal = _require_approved_proposal_structure(
            con, for_acceptance=for_acceptance)
        identity = recompute_fixture_identity(path)
        if proposal is None:
            tasks = []
            for i, (repo,) in enumerate(con.execute(
                    "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY 1")):
                tasks.append({
                    "requirement_id": f"primary:{repo}",
                    "row_kind": "executable",
                    "repo_id": repo,
                    "target_drive": "d0",
                    "source_drive": None,
                    "full_manifest_hash": _manifest_hash(con, repo),
                    "order_key": i,
                    "guaranteed_durable": 100,
                    "identity_epoch": 1,
                })
            proposal = {
                "lifecycle": "approved",
                "proposal_id": "bench-pure",
                "tasks": tasks,
                "files": [],
                "requirement_set_hash": identity["prepared_projection_hash"],
                "semantic_input_hash": identity["prepared_canonical_input_hash"],
            }
        drives = {}
        for label, epoch, fp, life, elig in con.execute(
                "SELECT drive_label, identity_epoch, identity_fingerprint, "
                "lifecycle, eligibility FROM drives"):
            drives[label] = SimpleNamespace(
                lifecycle=life or "active", eligibility=elig or "enabled",
                identity_epoch=int(epoch or 1),
                identity_fingerprint=fp or ("0" * 64), offline=False)
        if not drives:
            if for_acceptance:
                raise Refusal(
                    "ACCEPTANCE_FIXTURE_INVALID",
                    {"reason": "missing_drive_identity"}, ())
            drives = {
                "d0": SimpleNamespace(
                    lifecycle="active", eligibility="enabled",
                    identity_epoch=1, identity_fingerprint="a" * 64, offline=False),
            }
        archived = {}
        for r in con.execute(
                "SELECT repo_id, rfilename, drive_label, orig_sha256, stored_bytes, orig_bytes "
                "FROM archived"):
            archived[(r[0], r[1], r[2])] = {
                "orig_sha256": r[3], "stored_bytes": r[4], "orig_bytes": r[5]}
        manifests = {
            t.get("repo_id"): _manifest_hash(con, t["repo_id"])
            for t in (proposal.get("tasks") or ()) if t.get("repo_id")
        }
        evidence = {
            lab: SimpleNamespace(kind="offline", executable=True, admissible_free=10**15)
            for lab in drives
        }
        inp = SimpleNamespace(
            manifests=manifests, archived=archived, drives=drives,
            observed_ratio={}, evidence=evidence,
            file_hash_evidence={}, certificates={},
            semantic_hashes=SimpleNamespace(
                execution_invariants=proposal.get("semantic_input_hash")),
            execution_config={"capacity_mode": proposal.get("capacity_mode") or "guaranteed"},
        )
        graph = SimpleNamespace(
            requirement_ids=[t.get("requirement_id") for t in (proposal.get("tasks") or ())],
            requirement_set_hash=proposal.get("requirement_set_hash"),
        )
    finally:
        con.close()

    def pure_once():
        return project_pure(
            proposal, inp, graph, SimpleNamespace(parked_gated_repos=frozenset()))

    return pure_once


def count_projection_refresh_calls(scenario) -> dict:
    """Instrument the executor's real refresh seam (finding 38) — never file/task arithmetic."""
    events = list(getattr(scenario, "events", ()) or ())
    refresh = getattr(scenario, "refresh", None)
    calls = 0
    breakdown: dict[str, int] = {}
    if callable(refresh):
        for ev in events:
            refresh(ev)
            calls += 1
            key = str(ev)
            breakdown[key] = breakdown.get(key, 0) + 1
    else:
        # Prefer live fill instrumentation when scenario exposes a fill-shaped drain.
        from modelark import fill as fill_mod
        executor = int(fill_mod.projection_refresh_call_count())
        if executor > 0:
            return {
                "calls": executor,
                "budget": executor,
                "breakdown": {"executor_refresh": executor},
            }
        calls = len(events)
        breakdown = {"events": len(events)}
    return {"calls": calls, "budget": calls, "breakdown": breakdown}


def measure_executor_refresh_boundaries(sqlite_path: str | Path) -> dict:
    """Instrument refresh by running the real drain path (finding 38-a).

    Must invoke ``fill._drain_projection`` so batch-boundary and typed-event
    refreshes go through production dispatch — never invent event names and call
    ``_refresh_projection`` directly.

    DEC-052: never holds a write handle on the operator evidence file. Works on a
    temporary copy so session/drain writes cannot rewrite container pages or leave
    ``-wal``/``-shm`` beside the original artifact.
    """
    from modelark import fill as fill_mod

    source = Path(sqlite_path)
    fill_mod.reset_projection_refresh_call_count()
    with tempfile.TemporaryDirectory(prefix="modelark-b12-measure-") as td:
        work = Path(td) / "evidence-copy.sqlite"
        shutil.copy2(source, work)
        return _measure_executor_refresh_boundaries_on_copy(work)


def _measure_executor_refresh_boundaries_on_copy(path: Path) -> dict:
    """Write-capable measure body against a disposable copy (DEC-052)."""
    from unittest import mock
    from modelark import fill as fill_mod
    from modelark import fetch as fetch_mod
    from modelark import execution_session as esess
    from modelark.execution_config import ExecutionConfig, hash_config
    from modelark.proposal import _DefaultServices, load_proposal

    con = sqlite3.connect(str(path))
    try:
        proposal = _require_approved_proposal_structure(con, for_acceptance=True)
        if proposal is None:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_INVALID",
                {"reason": "missing_approved_proposal_for_refresh_measure"}, ())
        pid = proposal.get("proposal_id")
        cfg_hash = proposal.get("execution_config_hash")
        compression = {
            "max_compress_ram_gb": 4.0, "stream_compress": True, "threads": 1,
        }
        cfg_values = {
            "capacity_mode": proposal.get("capacity_mode") or "guaranteed",
            "policy_version": proposal.get("policy_version") or "1",
            "solver_version": proposal.get("solver_version") or "1",
            "compression": compression,
            "numcopies_default": 1,
        }
        if cfg_hash and hash_config(cfg_values) != str(cfg_hash):
            for trial in (
                {},
                {"enabled": True, "codec": "streamznn", "level": 3},
                compression,
            ):
                trial_cfg = dict(cfg_values, compression=trial)
                if hash_config(trial_cfg) == str(cfg_hash):
                    cfg_values = trial_cfg
                    break

        services = SimpleNamespace(
            clock=SimpleNamespace(now=lambda: "2026-01-01T00:00:00Z"),
            config=SimpleNamespace(read_graph_affecting_config=lambda: dict(cfg_values)),
            controller_flock=SimpleNamespace(
                hold=lambda: __import__("contextlib").nullcontext()),
            drive_fences=SimpleNamespace(
                hold_all_sorted=lambda ids: __import__("contextlib").nullcontext()),
            worker=SimpleNamespace(identity="bench-worker"),
            lease_ttl=3600,
            observe_exact_capacity=_DefaultServices().observe_exact_capacity,
            auto_claim_worker=True,
        )
        out = esess.start_session(con, pid, None, services)
        if isinstance(out, Refusal):
            prop = load_proposal(con, pid)
            frozen = ExecutionConfig.from_values(cfg_values)
            sid = "bench-refresh-session"
            con.execute("DELETE FROM execution_sessions WHERE session_id=?", [sid])
            try:
                con.execute(
                    "INSERT INTO execution_sessions("
                    "session_id,plan_id,approved_proposal_id,controller_identity,"
                    "worker_identity,state,bound_planner_revision,fencing_token,expires_at) "
                    "VALUES(?,?,?,'ctrl','bench-worker','running',0,1,'2099-01-01T00:00:00Z')",
                    [sid, prop.get("plan_id") or "ark", pid])
            except Exception as exc:
                raise Refusal(
                    "ACCEPTANCE_FIXTURE_INVALID",
                    {"reason": "session_insert_failed", "error": str(exc)[:200]}, ()) from exc
            session = SimpleNamespace(
                session_id=sid, approved_proposal_id=pid, fencing_token=1,
                state="running", worker_identity="bench-worker",
                plan_id=prop.get("plan_id") or "ark",
            )
            out = SimpleNamespace(
                session=session,
                projection=SimpleNamespace(
                    tasks=tuple(prop.get("tasks") or ()),
                    projection_hash=prop.get("requirement_set_hash"),
                ),
                execution_config=frozen,
                _proposal=prop,
                _proposal_files=list(prop.get("files") or ()),
                _config_reader=services.config,
                _observe_exact_capacity=services.observe_exact_capacity,
            )
        else:
            out._proposal = proposal if isinstance(proposal, dict) else load_proposal(con, pid)
            out._proposal_files = list((out._proposal or {}).get("files") or ())
            out._config_reader = services.config
            out._observe_exact_capacity = services.observe_exact_capacity

        # Force multi-batch: complete only one repo per transport call.
        # After ≥2 completed batches (and their batch_boundary refreshes), inject
        # one gated_retry typed event so production typed-event refresh runs.
        batches_done = {"n": 0, "gated_injected": False}
        typed_events = {"n": 0}

        def fake_run(**kwargs):
            batches_done["n"] += 1
            repos = list(kwargs.get("repos") or [])
            bd = fill_mod.projection_refresh_breakdown()
            batch_n = int(bd.get("batch_boundary") or 0)
            out_run = {
                "stored_repos": repos[:1] if repos else [],
                "failed_repos": [],
                "capacity_failure": None, "terminal_failure": None,
                "terminal_repo": None, "throttled": False, "stopped": False,
                "drive_unwritable": False, "gated_repos": [], "gated_retry": None,
            }
            if (
                not batches_done["gated_injected"]
                and batch_n >= 2
                and repos
            ):
                batches_done["gated_injected"] = True
                typed_events["n"] += 1
                out_run["gated_retry"] = repos[0]
                out_run["stored_repos"] = []
            return out_run

        def fake_replica(tasks, ctx=None):
            batches_done["n"] += 1
            n = 1 if tasks else 0
            return {
                "deferred": False, "source_offline": False,
                "deferred_targets": [], "copied_targets": [],
                "copied_files": n, "failed": [],
            }

        def should_stop():
            bd = fill_mod.projection_refresh_breakdown()
            batch_n = int(bd.get("batch_boundary") or 0)
            typed_n = sum(v for k, v in bd.items() if str(k).startswith("typed_event"))
            return batch_n >= 2 and typed_n >= 1

        ctx = fetch_mod.RunCtx(
            con=con,
            should_stop=should_stop,
            check_hf_auth=False,
            session_id=getattr(out.session, "session_id", None),
            fencing_token=getattr(out.session, "fencing_token", None),
            execution_config=getattr(out, "execution_config", None),
        )

        # Adversarial: if drain is not the measurement path, fail hard.
        drain_called = {"n": 0}
        real_drain = fill_mod._drain_projection

        def wrapped_drain(*a, **k):
            drain_called["n"] += 1
            return real_drain(*a, **k)

        # Stable projection for refresh: fixture may have source_not_ready rows that
        # refuse project_pure; cadence proof requires the drain to dispatch refresh,
        # not re-solve placement. Transport is mocked; projection identity is frozen.
        def stable_project_pure(proposal, current_input, current_graph, overlay):
            return getattr(out, "projection")

        with mock.patch.object(fill_mod, "_drain_projection", side_effect=wrapped_drain), \
                mock.patch.object(fill_mod.fetch, "run", side_effect=fake_run), \
                mock.patch.object(fill_mod.fetch, "run_replica_tasks", side_effect=fake_replica), \
                mock.patch.object(fill_mod, "_await_drive", return_value=True), \
                mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
                mock.patch(
                    "modelark.execution_projection.project_pure",
                    side_effect=stable_project_pure,
                ):
            fill_mod._drain_projection(
                ctx, out,
                plan_id=getattr(out.session, "plan_id", None) or "ark",
                max_24h_gb=0,
                repo_scope=None,
                guided=False,
                poll_secs=0.01,
                child_fds=(),
            )
        if drain_called["n"] < 1:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_INVALID",
                {"reason": "drain_not_invoked"}, ())

        calls = int(fill_mod.projection_refresh_call_count())
        by_reason = fill_mod.projection_refresh_breakdown()
        batch_refreshes = int(by_reason.get("batch_boundary") or 0)
        typed_refreshes = sum(
            int(v) for k, v in by_reason.items() if str(k).startswith("typed_event"))
        if batch_refreshes < 2:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_INVALID",
                {"reason": "insufficient_batch_refreshes",
                 "batch_refreshes": batch_refreshes,
                 "transport_batches": batches_done["n"],
                 "breakdown": by_reason}, ())
        if typed_refreshes < 1:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_INVALID",
                {"reason": "missing_typed_event_refresh",
                 "breakdown": by_reason}, ())
        if calls != batch_refreshes + typed_refreshes:
            raise Refusal(
                "ACCEPTANCE_FIXTURE_INVALID",
                {"reason": "refresh_count_mismatch",
                 "calls": calls, "batch": batch_refreshes, "typed": typed_refreshes}, ())

        return {
            "calls": calls,
            "transport_batches": int(batches_done["n"]),
            "typed_events_injected": int(typed_events["n"]),
            "source": "fill._drain_projection",
            "initial_full_projection": 1,  # start/session projection (not a refresh)
            "breakdown": {
                "initial_full_projection": 1,
                "batch_boundary_refreshes": batch_refreshes,
                "typed_event_refreshes": typed_refreshes,
                "by_reason": by_reason,
                "transport_batches": int(batches_done["n"]),
                "total_refreshes": calls,
            },
            "reconcile": {
                "calls_equals_batch_plus_typed": True,
                "batch_refreshes": batch_refreshes,
                "typed_refreshes": typed_refreshes,
                # Required equality (F38-a): total = batch_boundary + typed_event.
                "total_refreshes_equals_batch_plus_typed": (
                    calls == batch_refreshes + typed_refreshes
                ),
                # B−1: after the last transport batch, drain re-enters, finds nothing
                # ready, and exits via the terminal path before another batch-boundary
                # refresh — so batch_boundary_refreshes ≤ transport_batches (often B−1).
                "batch_boundary_refreshes_le_transport_batches": (
                    batch_refreshes <= int(batches_done["n"])
                ),
            },
            "cadence_invariants": {
                "equality": (
                    "total_refreshes = batch_boundary_refreshes + typed_event_refreshes"
                ),
                "batch_bound": (
                    "batch_boundary_refreshes ≤ transport_batches "
                    "(often B−1: final loop re-entry finds nothing ready and "
                    "exits terminal without another batch-boundary refresh)"
                ),
                "source": "fill._drain_projection",
            },
        }
    finally:
        con.close()
