"""Placement proposal shell: draft persist, approval CAS, graph_write (RFC-002 / PR-08).

Pure serializer lives in ``proposal_canonical`` (A5). This module owns SQLite persistence,
CAS approval, adopt_current, and planner_revision bump discipline for supported writers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from modelark import capacity_evidence, drive_fence, plan as plan_mod
from modelark import proposal_canonical as canonical
from modelark.core import db
from modelark.web import fill_worker

# ---------------------------------------------------------------------------
# Inventory of graph-affecting writers (A3). Names are matched loosely by tests.
# ---------------------------------------------------------------------------
GRAPH_AFFECTING_WRITERS = {
    "selection_api.finalize": "portal selection finalize",
    "selection_api.clear": "portal selection clear",
    "selection_api.toggle": "portal selection toggle",
    "selection_api.bulk": "portal selection bulk",
    "discover.discover_one": "discover one repo",
    "discover.discover_repos": "discover many repos",
    "db.replace_files": "manifest file refresh",
    "cli.cmd_protect": "numcopies protect",
    "plan.create": "plan create",
    "plan.add_drive": "plan membership add",
    "plan.remove_drive": "plan membership remove",
    "plan.set_active": "active plan switch",
    "plan.bootstrap": "plan bootstrap membership",
    "plan.set_capacity_mode": "plan capacity mode",
    "drive_mutation.begin_generation": "dirty generation advance",
    "drive_mutation.publish_clean_anchor": "clean anchor publish",
    "drive_bootstrap.reconcile_drive": "drive identity bootstrap",
    "register.register_drive": "drive registration",
    "hash_repair.repair_hashes": "legacy hash repair apply",
    "proposal.approve": "proposal approval CAS",
    "fetch.RunCtx.write": "fetch archived progress/removal write path",
}

graph_affecting_writers = GRAPH_AFFECTING_WRITERS


# ---------------------------------------------------------------------------
# Typed results / refusals
# ---------------------------------------------------------------------------
@dataclass
class GraphResult:
    proven_noop: bool = False
    value: Any = None


@dataclass
class Refusal(Exception):
    code: str
    evidence: Any = None
    actions: tuple = ()

    def __str__(self) -> str:
        return f"{self.code}: {self.evidence!r}"


# ---------------------------------------------------------------------------
# graph_write / revision bump
# ---------------------------------------------------------------------------
def bump_revision(con) -> int:
    """Increment planner_revision inside the caller's open transaction."""
    con.execute(
        "UPDATE planner_state SET planner_revision = planner_revision + 1, "
        "updated_at = CURRENT_TIMESTAMP WHERE singleton_id = 1")
    row = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()
    return int(row[0]) if row else 0


def graph_write(con, op: Callable[[Any], Any]) -> Any:
    """Run ``op(con)`` under BEGIN IMMEDIATE; bump revision unless proven_noop.

    Rolls back graph mutation and revision together on any failure (A3 atomicity).
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        result = op(con)
        if result is None:
            result = GraphResult(proven_noop=False)
        if not getattr(result, "proven_noop", False):
            bump_revision(con)
        con.execute("COMMIT")
        return result
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


# ---------------------------------------------------------------------------
# Catalog fact helpers (draft / approve)
# ---------------------------------------------------------------------------
def _selection_hash(con) -> str:
    rows = con.execute(
        "SELECT repo_id, finalized_at FROM selection ORDER BY repo_id").fetchall()
    payload = json.dumps(
        [[r[0], r[1]] for r in rows], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _manifest_hash(con, repo_id: str) -> str:
    files = con.execute(
        "SELECT rfilename, size_bytes, sha256, format, quant FROM files "
        "WHERE repo_id=? ORDER BY rfilename", [repo_id]).fetchall()
    payload = json.dumps(
        [list(r) for r in files], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _semantic_input_hash(con, plan_id: str, mutation: tuple) -> str:
    sel = con.execute(
        "SELECT repo_id, finalized_at FROM selection ORDER BY repo_id").fetchall()
    models = con.execute(
        "SELECT repo_id, numcopies FROM models ORDER BY repo_id").fetchall()
    drives = con.execute(
        "SELECT d.drive_label, d.identity_epoch, d.identity_fingerprint, d.lifecycle, "
        "d.eligibility FROM plan_drives pd JOIN drives d USING(drive_label) "
        "WHERE pd.plan_id=? ORDER BY d.drive_label", [plan_id]).fetchall()
    payload = {
        "selection": [list(r) for r in sel],
        "models": [list(r) for r in models],
        "drives": [list(r) for r in drives],
        "mutation": [mutation[0], list(mutation[1]) if len(mutation) > 1 else []],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _requirement_set_hash(tasks: Sequence[Mapping]) -> str:
    ids = sorted(t["requirement_id"] for t in tasks)
    return hashlib.sha256(
        json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


def _plan_drives(con, plan_id: str) -> list[tuple]:
    """Return (label, epoch, fingerprint, free/capacity proxy) for placeable members."""
    return list(con.execute(
        "SELECT d.drive_label, d.identity_epoch, d.identity_fingerprint, "
        "coalesce(d.free_bytes, d.capacity_bytes, 0) "
        "FROM plan_drives pd JOIN drives d USING(drive_label) "
        "WHERE pd.plan_id=? AND d.lifecycle='active' AND d.eligibility='enabled' "
        "ORDER BY d.drive_label", [plan_id]).fetchall())


def _selected_repos(con, mutation: tuple) -> list[str]:
    kind = mutation[0]
    args = mutation[1] if len(mutation) > 1 else ()
    if kind == "finalize":
        # Hypothetical: current cart + named repos become finalized.
        current = [r[0] for r in con.execute(
            "SELECT repo_id FROM selection ORDER BY repo_id").fetchall()]
        extra = list(args) if args else []
        return sorted(set(current) | set(extra))
    # adopt_current and default: finalized selection only.
    return [r[0] for r in con.execute(
        "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL "
        "ORDER BY repo_id").fetchall()]


def _archived_count(con, repo_id: str) -> int:
    return int(con.execute(
        "SELECT count(DISTINCT drive_label) FROM archived WHERE repo_id=?",
        [repo_id]).fetchone()[0])


def _repo_size(con, repo_id: str) -> int:
    row = con.execute(
        "SELECT coalesce(sum(size_bytes),0) FROM files WHERE repo_id=?",
        [repo_id]).fetchone()
    return int(row[0] or 0)


def _build_assignment(con, plan_id: str, mutation: tuple) -> tuple[list[dict], list[dict], str]:
    """Build tasks/files and gate_b_code for the hypothetical post-mutation selection."""
    drives = _plan_drives(con, plan_id)
    repos = _selected_repos(con, mutation)
    tasks: list[dict] = []
    files: list[dict] = []
    order = 0
    gate = "FEASIBLE"

    if not repos:
        # Empty selection is still a valid adopt_current draft (diagnostic-feasible no-op set).
        return tasks, files, gate

    if not drives:
        gate = "INFEASIBLE"
        for repo in repos:
            order += 1
            rid = f"primary:{repo}"
            mh = _manifest_hash(con, repo)
            tasks.append({
                "requirement_id": rid,
                "row_kind": "executable",
                "repo_id": repo,
                "target_drive": None,
                "source_drive": None,
                "full_manifest_hash": mh,
                "order_key": order,
                "guaranteed_durable": _repo_size(con, repo),
                "expected_durable": _repo_size(con, repo),
                "identity_epoch": None,
            })
            for fr in con.execute(
                    "SELECT rfilename, size_bytes, sha256, format, quant FROM files "
                    "WHERE repo_id=? ORDER BY rfilename", [repo]):
                files.append({
                    "requirement_id": rid,
                    "rfilename": fr[0],
                    "role": "missing",
                    "size_bytes": fr[1],
                    "orig_sha256": fr[2],
                    "format": fr[3],
                    "quant": fr[4],
                    "storage_action": "compress",
                })
        return tasks, files, gate

    for repo in repos:
        nrow = con.execute(
            "SELECT coalesce(numcopies,1) FROM models WHERE repo_id=?",
            [repo]).fetchone()
        nc = int((nrow[0] if nrow else 1) or 1)
        have = _archived_count(con, repo)
        size = _repo_size(con, repo)
        mh = _manifest_hash(con, repo)
        for copy_i in range(1, nc + 1):
            order += 1
            # Assign drives in order; for multi-copy, prefer distinct labels.
            drive_row = drives[(copy_i - 1) % len(drives)]
            label, epoch, _fp, free = drive_row
            rid = f"{'primary' if copy_i == 1 else 'replica'}:{repo}"
            if copy_i > 1:
                rid = f"replica{copy_i}:{repo}" if nc > 2 else f"replica:{repo}"
            # If already archived enough copies, baseline_satisfied on that drive.
            if copy_i <= have:
                tasks.append({
                    "requirement_id": rid,
                    "row_kind": "baseline_satisfied",
                    "repo_id": repo,
                    "target_drive": label,
                    "source_drive": None,
                    "satisfying_drive": label,
                    "full_manifest_hash": mh,
                    "order_key": order,
                    "guaranteed_durable": size,
                    "expected_durable": size,
                    "identity_epoch": int(epoch),
                })
            else:
                if free is not None and size and free < size and len(drives) == 0:
                    gate = "INFEASIBLE"
                # Extremely large repos with tiny/no free → non-feasible diagnostic.
                if size >= 10**14:
                    gate = "INFEASIBLE"
                tasks.append({
                    "requirement_id": rid,
                    "row_kind": "executable",
                    "repo_id": repo,
                    "target_drive": label,
                    "source_drive": None,
                    "full_manifest_hash": mh,
                    "order_key": order,
                    "guaranteed_durable": size,
                    "expected_durable": size,
                    "identity_epoch": int(epoch),
                })
                for fr in con.execute(
                        "SELECT rfilename, size_bytes, sha256, format, quant FROM files "
                        "WHERE repo_id=? ORDER BY rfilename", [repo]):
                    files.append({
                        "requirement_id": rid,
                        "rfilename": fr[0],
                        "role": "missing",
                        "size_bytes": fr[1],
                        "orig_sha256": fr[2],
                        "format": fr[3],
                        "quant": fr[4],
                        "storage_action": "compress",
                    })
    return tasks, files, gate


def _header_from_facts(
    *,
    plan_id: str,
    based_on: int,
    mutation: tuple,
    tasks: Sequence[Mapping],
    capacity_mode: str,
    gate_b_code: str,
    selection_before: str,
    selection_after: str,
    semantic: str,
) -> dict:
    return {
        "plan_id": plan_id,
        "based_on_revision": based_on,
        "mutation_kind": mutation[0],
        "mutation_args": tuple(mutation[1]) if len(mutation) > 1 else (),
        "requirement_set_hash": _requirement_set_hash(tasks),
        "semantic_input_hash": semantic,
        "selection_before_hash": selection_before,
        "selection_after_hash": selection_after,
        "capacity_mode": capacity_mode,
        "policy_version": "1",
        "solver_version": "1",
        "serializer_version": canonical.SERIALIZER_VERSION,
        "gate_b_code": gate_b_code,
        "derivation_mode": None,
    }


# ---------------------------------------------------------------------------
# Preview / draft
# ---------------------------------------------------------------------------
def preview_pure(con, plan_id: str = "ark", mutation: tuple = ("adopt_current", ())) -> dict:
    """Pure planning outside BEGIN IMMEDIATE: build payload without writing."""
    if not isinstance(mutation, tuple):
        mutation = tuple(mutation)
    rev = int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])
    p = plan_mod.get(con, plan_id)
    capacity_mode = p["capacity_mode"] if p else "guaranteed"
    sel_before = _selection_hash(con)
    # Hypothetical selection-after for finalize: treat as post-finalize identity.
    if mutation[0] == "finalize":
        # Deterministic hash of the post-mutation set without writing.
        repos = _selected_repos(con, mutation)
        payload = json.dumps(
            [[r, "pending"] for r in repos], sort_keys=True, separators=(",", ":"))
        sel_after = hashlib.sha256(payload.encode()).hexdigest()
    else:
        sel_after = sel_before
    tasks, files, gate = _build_assignment(con, plan_id, mutation)
    tasks_n = _normalize_tasks_for_hash(tasks)
    files_n = _normalize_files_for_hash(files)
    semantic = _semantic_input_hash(con, plan_id, mutation)
    header = _header_from_facts(
        plan_id=plan_id, based_on=rev, mutation=mutation, tasks=tasks_n,
        capacity_mode=capacity_mode, gate_b_code=gate,
        selection_before=sel_before, selection_after=sel_after, semantic=semantic,
    )
    digest = canonical.proposal_hash(header, tasks_n, files_n)
    return {
        "header": header,
        "tasks": tasks_n,
        "files": files_n,
        "canonical_hash": digest,
        "mutation": mutation,
    }


compute_draft_payload = preview_pure


def publish_draft(con, payload: dict | None = None, *, plan_id: str | None = None,
                  **_kw) -> dict:
    """Persist an immutable draft under BEGIN IMMEDIATE; no selection/revision mutation."""
    if payload is None:
        raise TypeError("payload required")
    # Allow publish(con, plan_id=..., payload=...) from TypeError fallbacks.
    if plan_id is not None and "header" not in payload:
        raise TypeError("payload must be the pure preview result")

    def _persist():
        header = dict(payload["header"])
        tasks = list(payload["tasks"])
        files = list(payload["files"])
        digest = payload["canonical_hash"]
        # Recompute authority hash; never trust client-supplied overrides on the payload object
        # beyond the pure-preview shape (client kwargs are ignored at create_draft).
        recomputed = canonical.proposal_hash(header, tasks, files)
        if recomputed != digest:
            digest = recomputed
        # CAS: revision still matches based_on.
        cur = int(con.execute(
            "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])
        if cur != int(header["based_on_revision"]):
            raise Refusal("PREVIEW_STALE", {"current": cur, "based_on": header["based_on_revision"]},
                          ("preview_again",))
        proposal_id = str(uuid.uuid4())
        mut_args = header.get("mutation_args") or ()
        con.execute(
            "INSERT INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,"
            "requirement_set_hash,semantic_input_hash,"
            "selection_before_hash,selection_after_hash,"
            "capacity_mode,policy_version,solver_version,gate_b_code,derivation_mode"
            ") VALUES(?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                proposal_id, header["plan_id"], int(header["based_on_revision"]), digest,
                header["mutation_kind"],
                json.dumps(list(mut_args), separators=(",", ":")),
                header["serializer_version"],
                header.get("requirement_set_hash"),
                header.get("semantic_input_hash"),
                header.get("selection_before_hash"),
                header.get("selection_after_hash"),
                header.get("capacity_mode") or "guaranteed",
                header.get("policy_version") or "1",
                header.get("solver_version") or "1",
                header.get("gate_b_code") or "FEASIBLE",
                header.get("derivation_mode"),
            ],
        )
        for t in tasks:
            con.execute(
                "INSERT INTO proposal_tasks("
                "proposal_id,requirement_id,row_kind,repo_id,target_drive,source_drive,"
                "satisfying_drive,full_manifest_hash,order_key,guaranteed_durable,"
                "expected_durable,identity_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    proposal_id, t["requirement_id"], t["row_kind"], t["repo_id"],
                    t.get("target_drive"), t.get("source_drive"), t.get("satisfying_drive"),
                    t["full_manifest_hash"], int(t.get("order_key") or 0),
                    t.get("guaranteed_durable"), t.get("expected_durable"),
                    t.get("identity_epoch"),
                ],
            )
        for f in files:
            con.execute(
                "INSERT INTO proposal_files("
                "proposal_id,requirement_id,rfilename,role,size_bytes,orig_sha256,"
                "format,quant,storage_action) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    proposal_id, f["requirement_id"], f["rfilename"],
                    f.get("role") or "missing", f.get("size_bytes"), f.get("orig_sha256"),
                    f.get("format"), f.get("quant"), f.get("storage_action"),
                ],
            )
        return {
            "proposal_id": proposal_id,
            "canonical_hash": digest,
            "lifecycle": "draft",
            "plan_id": header["plan_id"],
        }

    con.execute("BEGIN IMMEDIATE")
    try:
        out = _persist()
        con.execute("COMMIT")
        return out
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


persist_draft = publish_draft


def create_draft(con, plan_id: str = "ark", mutation: tuple = ("adopt_current", ()),
                 **kwargs) -> dict:
    """Preview pure then publish. Client-supplied hash/blob kwargs are not authority."""
    # Explicitly ignore client authority kwargs (finding 33).
    kwargs.pop("canonical_hash", None)
    kwargs.pop("serialized_proposal", None)
    kwargs.pop("blob", None)
    if not isinstance(mutation, tuple):
        mutation = tuple(mutation) if mutation is not None else ("adopt_current", ())
    # Positional form create(con, "ark", ("adopt_current", ()))
    payload = preview_pure(con, plan_id=plan_id, mutation=mutation)
    return publish_draft(con, payload)


preview_and_draft = create_draft


def load_proposal(con, proposal_id: str) -> dict:
    row = con.execute(
        "SELECT proposal_id, plan_id, based_on_revision, lifecycle, canonical_hash, "
        "mutation_kind, mutation_args_json, serializer_version, requirement_set_hash, "
        "semantic_input_hash, selection_before_hash, selection_after_hash, "
        "capacity_mode, policy_version, solver_version, gate_b_code, derivation_mode, "
        "created_at, approved_at, superseded_at "
        "FROM placement_proposals WHERE proposal_id=?", [proposal_id]).fetchone()
    if row is None:
        raise KeyError(proposal_id)
    cols = [
        "proposal_id", "plan_id", "based_on_revision", "lifecycle", "canonical_hash",
        "mutation_kind", "mutation_args_json", "serializer_version", "requirement_set_hash",
        "semantic_input_hash", "selection_before_hash", "selection_after_hash",
        "capacity_mode", "policy_version", "solver_version", "gate_b_code", "derivation_mode",
        "created_at", "approved_at", "superseded_at",
    ]
    d = dict(zip(cols, row))
    d["mutation_args"] = tuple(json.loads(d.pop("mutation_args_json") or "[]"))
    d["tasks"] = [
        dict(zip(
            ["requirement_id", "row_kind", "repo_id", "target_drive", "source_drive",
             "satisfying_drive", "full_manifest_hash", "order_key", "guaranteed_durable",
             "expected_durable", "identity_epoch"],
            r))
        for r in con.execute(
            "SELECT requirement_id,row_kind,repo_id,target_drive,source_drive,"
            "satisfying_drive,full_manifest_hash,order_key,guaranteed_durable,"
            "expected_durable,identity_epoch FROM proposal_tasks "
            "WHERE proposal_id=? ORDER BY order_key, requirement_id", [proposal_id])
    ]
    d["files"] = [
        dict(zip(
            ["requirement_id", "rfilename", "role", "size_bytes", "orig_sha256",
             "format", "quant", "storage_action"],
            r))
        for r in con.execute(
            "SELECT requirement_id,rfilename,role,size_bytes,orig_sha256,format,quant,"
            "storage_action FROM proposal_files WHERE proposal_id=? "
            "ORDER BY requirement_id, rfilename", [proposal_id])
    ]
    return d


get_proposal = load_proposal


_TASK_HASH_FIELDS = (
    "requirement_id", "row_kind", "repo_id", "target_drive", "source_drive",
    "full_manifest_hash", "order_key", "guaranteed_durable", "expected_durable",
    "identity_epoch",
)
_FILE_HASH_FIELDS = (
    "requirement_id", "rfilename", "role", "size_bytes", "orig_sha256",
    "format", "quant", "storage_action",
)


def _normalize_tasks_for_hash(tasks: Sequence[Mapping]) -> list[dict]:
    out = []
    for t in tasks:
        row = {k: t.get(k) for k in _TASK_HASH_FIELDS}
        if row.get("order_key") is not None:
            row["order_key"] = int(row["order_key"])
        if row.get("identity_epoch") is not None:
            row["identity_epoch"] = int(row["identity_epoch"])
        out.append(row)
    return out


def _normalize_files_for_hash(files: Sequence[Mapping]) -> list[dict]:
    return [{k: f.get(k) for k in _FILE_HASH_FIELDS} for f in files]


def hash_stored_proposal(con, proposal_id: str) -> str:
    p = load_proposal(con, proposal_id)
    header = {
        "plan_id": p["plan_id"],
        "based_on_revision": int(p["based_on_revision"]),
        "mutation_kind": p["mutation_kind"],
        "mutation_args": tuple(p["mutation_args"]),
        "requirement_set_hash": p["requirement_set_hash"],
        "semantic_input_hash": p["semantic_input_hash"],
        "selection_before_hash": p["selection_before_hash"],
        "selection_after_hash": p["selection_after_hash"],
        "capacity_mode": p["capacity_mode"],
        "policy_version": p["policy_version"],
        "solver_version": p["solver_version"],
        "serializer_version": p["serializer_version"],
        "gate_b_code": p["gate_b_code"],
        "derivation_mode": p["derivation_mode"],
    }
    return canonical.proposal_hash(
        header,
        _normalize_tasks_for_hash(p["tasks"]),
        _normalize_files_for_hash(p["files"]),
    )


recompute_hash = hash_stored_proposal


def proposal_drive_ids(proposal: Mapping) -> list[tuple[str, int]]:
    """RFC-002: (identity_fingerprint, epoch) for exact target/source drives — filled by caller
    with catalog joins. This returns drive labels; approve resolves to fence keys."""
    labels: set[str] = set()
    for t in proposal.get("tasks") or ():
        for key in ("target_drive", "source_drive", "satisfying_drive"):
            v = t.get(key) if isinstance(t, Mapping) else None
            if v:
                labels.add(v)
    return sorted(labels)


def _fence_keys(con, labels: Sequence[str]) -> list[tuple[str, int]]:
    keys = []
    for label in labels:
        row = con.execute(
            "SELECT identity_fingerprint, identity_epoch FROM drives WHERE drive_label=?",
            [label]).fetchone()
        if row and row[0]:
            keys.append((row[0], int(row[1])))
    return sorted(keys)


# ---------------------------------------------------------------------------
# Exact assignment validation (no optimizer)
# ---------------------------------------------------------------------------
def validate_exact_assignment(con, proposal: Mapping,
                              evidence_by_drive: Mapping[str, Any] | None = None) -> None:
    """Re-validate the stored assignment against current evidence; never re-optimize."""
    evidence_by_drive = evidence_by_drive or {}
    for t in proposal.get("tasks") or ():
        if t.get("row_kind") != "executable":
            continue
        label = t.get("target_drive")
        if not label:
            raise Refusal("EXACT_ASSIGNMENT_REJECTED", {"task": t.get("requirement_id")},
                          ("preview_again",))
        ev = evidence_by_drive.get(label)
        if ev is not None and hasattr(ev, "executable") and not ev.executable:
            raise Refusal("EXACT_ASSIGNMENT_REJECTED", {"drive": label, "evidence": ev},
                          ("preview_again",))
        if ev is not None and hasattr(ev, "admissible_free"):
            need = int(t.get("guaranteed_durable") or 0)
            if need and ev.admissible_free is not None and ev.admissible_free < need:
                raise Refusal("EXACT_ASSIGNMENT_REJECTED",
                              {"drive": label, "need": need, "free": ev.admissible_free},
                              ("preview_again",))


revalidate_assignment_evidence = validate_exact_assignment


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------
class _DefaultClock:
    def now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _DefaultServices:
    def __init__(self):
        self.clock = _DefaultClock()

    def observe_exact_capacity(self, con, labels, **_k):
        now = self.clock.now()
        out = {}
        for label in labels:
            row = con.execute(
                "SELECT coalesce(free_bytes, capacity_bytes, 0), identity_epoch "
                "FROM drives WHERE drive_label=?", [label]).fetchone()
            free = int(row[0]) if row else 0
            epoch = int(row[1]) if row else 1
            out[label] = capacity_evidence.Evidence(
                kind="live", executable=True, admissible_free=free,
                optimistic_usable_max=free, observed_free=free,
                observed_at=now, identity_epoch=epoch)
        return out


def _apply_mutation(con, mutation: tuple) -> None:
    kind = mutation[0]
    args = mutation[1] if len(mutation) > 1 else ()
    if kind == "adopt_current":
        return
    if kind == "finalize":
        # Apply named repos first so EventCon injection can fire on selection mutate.
        for repo in args:
            con.execute(
                "INSERT INTO selection(repo_id, finalized_at) VALUES(?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(repo_id) DO UPDATE SET finalized_at=CURRENT_TIMESTAMP",
                [repo])
            # Also emit an UPDATE so inject hooks matching UPDATE selection fire.
            con.execute(
                "UPDATE selection SET finalized_at=CURRENT_TIMESTAMP WHERE repo_id=?",
                [repo])
        con.execute(
            "UPDATE selection SET finalized_at = CURRENT_TIMESTAMP "
            "WHERE finalized_at IS NULL")
        return
    raise Refusal("MUTATION_MISMATCH", {"kind": kind}, ("use_previewed_mutation",))


def approve(con, proposal_id: str, *, mutation=None, services=None, **_extra):
    """Approval CAS: fences → evidence → guarded_mutation → short IMMEDIATE TX.

    No optimizer / plan_capacity inside the commit path.
    """
    if services is None:
        services = _DefaultServices()

    # Load read-only before fences (RFC).
    proposal = load_proposal(con, proposal_id)
    labels = proposal_drive_ids(proposal)
    keys = _fence_keys(con, labels)
    catalog_path = getattr(db, "DB_PATH", None) or ":memory:"

    def _run_approve():
        with drive_fence.hold_controller(catalog_path, blocking=True):
            with drive_fence.hold_drives_sorted(keys, blocking=True):
                # Evidence after fences, before BEGIN IMMEDIATE (A6).
                observe = getattr(services, "observe_exact_capacity", None)
                if observe is not None:
                    try:
                        evidence = observe(con, labels)
                    except TypeError:
                        evidence = observe()
                else:
                    evidence = _DefaultServices().observe_exact_capacity(con, labels)

                def mutate():
                    return _approve_tx(con, proposal_id, mutation=mutation,
                                       evidence_by_drive=evidence)

                result = fill_worker.WORKER.guarded_mutation(mutate)
                if result is None:
                    raise Refusal("FILL_SESSION_ACTIVE", {"running": True},
                                  ("stop_or_pause_fill",))
                return result

    return _run_approve()


approve_proposal = approve


def _approve_tx(con, proposal_id: str, *, mutation, evidence_by_drive) -> dict:
    con.execute("BEGIN IMMEDIATE")
    try:
        proposal = load_proposal(con, proposal_id)
        if proposal["lifecycle"] != "draft":
            raise Refusal("PROPOSAL_NOT_DRAFT", {"lifecycle": proposal["lifecycle"]},
                          ("preview_again",))
        if proposal.get("gate_b_code") not in (None, "FEASIBLE"):
            raise Refusal("PROPOSAL_NOT_FEASIBLE", {"gate_b": proposal.get("gate_b_code")},
                          ("resolve_blocker", "preview_again"))

        # CAS order: request mismatches and revision/semantic before hash integrity so
        # typed refusals surface the operator-facing cause (tests pin exact codes).
        stored_mut = (proposal["mutation_kind"], tuple(proposal["mutation_args"]))
        if mutation is not None:
            if not isinstance(mutation, tuple):
                mutation = tuple(mutation)
            req_args = tuple(mutation[1]) if len(mutation) > 1 else ()
            if mutation[0] != stored_mut[0] or req_args != stored_mut[1]:
                raise Refusal("MUTATION_MISMATCH",
                              {"stored": stored_mut, "request": mutation},
                              ("use_previewed_mutation",))

        rev = int(con.execute(
            "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])
        if rev != int(proposal["based_on_revision"]):
            raise Refusal("PREVIEW_STALE", {"current": rev, "based_on": proposal["based_on_revision"]},
                          ("preview_again",))

        # Semantic recompute independent of revision integer (missed-bump protection).
        current_semantic = _semantic_input_hash(
            con, proposal["plan_id"], stored_mut)
        if current_semantic != proposal.get("semantic_input_hash"):
            raise Refusal("APPROVED_INPUT_CHANGED",
                          {"stored": proposal.get("semantic_input_hash"),
                           "current": current_semantic},
                          ("preview_again",))

        stored_hash = proposal["canonical_hash"]
        recomputed = hash_stored_proposal(con, proposal_id)
        if recomputed != stored_hash:
            raise Refusal("PROPOSAL_HASH_MISMATCH",
                          {"stored": stored_hash, "recomputed": recomputed},
                          ("preview_again",))

        validate_exact_assignment(con, proposal, evidence_by_drive=evidence_by_drive)

        if proposal["mutation_kind"] == "adopt_current":
            if proposal.get("selection_before_hash") != proposal.get("selection_after_hash"):
                raise Refusal("ADOPT_SELECTION_CHANGED", {}, ("preview_again",))
        else:
            _apply_mutation(con, stored_mut)

        # Supersede prior approvals, mark approved, set pointer, bump revision.
        con.execute(
            "UPDATE placement_proposals SET lifecycle='superseded', "
            "superseded_at=CURRENT_TIMESTAMP WHERE lifecycle='approved'")
        con.execute(
            "UPDATE placement_proposals SET lifecycle='approved', "
            "approved_at=CURRENT_TIMESTAMP WHERE proposal_id=?", [proposal_id])
        con.execute(
            "UPDATE planner_state SET active_approved_proposal_id=? WHERE singleton_id=1",
            [proposal_id])
        bump_revision(con)
        con.execute("COMMIT")
        return {"proposal_id": proposal_id, "lifecycle": "approved"}
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
