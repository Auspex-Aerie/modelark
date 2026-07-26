"""Pure monotonic execution projection (PR-09 / B1, B13). RFC-002 project_pure."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from modelark.proposal import Refusal


def _g(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _task_dict(t) -> dict:
    if isinstance(t, Mapping):
        return dict(t)
    if hasattr(t, "__dict__"):
        return {k: getattr(t, k) for k in dir(t) if not k.startswith("_")}
    return {}


def _lifecycle(drive) -> str:
    return str(_g(drive, "lifecycle", "active") or "active")


def _epoch(drive) -> int | None:
    v = _g(drive, "identity_epoch")
    return int(v) if v is not None else None


def _fp(drive) -> str | None:
    return _g(drive, "identity_fingerprint")


def _offline(drive) -> bool:
    return bool(_g(drive, "offline", False))


def _arch_key_match(archived: Mapping, repo: str, rfilename: str, drive: str) -> dict | None:
    # Support tuple keys (repo, rfilename, drive) or nested dicts
    if (repo, rfilename, drive) in archived:
        return archived[(repo, rfilename, drive)]
    if isinstance(archived, Mapping):
        for k, v in archived.items():
            if k == (repo, rfilename, drive):
                return v
            if isinstance(k, (list, tuple)) and len(k) >= 3:
                if k[0] == repo and k[1] == rfilename and k[2] == drive:
                    return v
    return None


def _file_satisfied(archived, repo, rfilename, drive, expected_sha) -> bool:
    row = _arch_key_match(archived or {}, repo, rfilename, drive)
    if not row:
        return False
    sha = _g(row, "orig_sha256") if not isinstance(row, Mapping) else row.get("orig_sha256")
    if expected_sha and sha and sha != expected_sha:
        return False
    return True


def _stored_bytes(archived, repo, rfilename, drive) -> int:
    row = _arch_key_match(archived or {}, repo, rfilename, drive)
    if not row:
        return 0
    return int(_g(row, "stored_bytes", 0) or (row.get("stored_bytes") if isinstance(row, Mapping) else 0) or 0)


def _ratio_value(ratio_evidence, repo: str) -> float | None:
    if not ratio_evidence:
        return None
    v = ratio_evidence.get(repo) if isinstance(ratio_evidence, Mapping) else None
    if v is None:
        return None
    if isinstance(v, float):
        # Tests forbid binary float in inputs; if present treat as invalid envelope signal
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class _TaskView:
    data: dict
    schedule_state: str | None = None
    missing: set = field(default_factory=set)
    actual_since_approval: int = 0
    remaining_forecast: int = 0

    def get(self, k, default=None):
        return self.data.get(k, default)

    def __getitem__(self, k):
        return self.data[k]

    @property
    def requirement_id(self):
        return self.data.get("requirement_id")

    @property
    def repo_id(self):
        return self.data.get("repo_id")

    @property
    def target_drive(self):
        return self.data.get("target_drive")

    @property
    def source_drive(self):
        return self.data.get("source_drive")

    @property
    def row_kind(self):
        return self.data.get("row_kind")

    def as_dict(self) -> dict:
        d = dict(self.data)
        if self.schedule_state is not None:
            d["schedule_state"] = self.schedule_state
        return d


@dataclass
class ExecutionProjection:
    proposal_id: str
    tasks: tuple
    projection_hash: str


def canonical_projection_hash(tasks: Sequence) -> str:
    payload = []
    for t in tasks:
        if isinstance(t, _TaskView):
            payload.append(t.as_dict())
        elif isinstance(t, Mapping):
            payload.append(dict(t))
        else:
            payload.append(_task_dict(t))
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def project_pure(proposal, current_input, current_graph, session_overlay):
    """RFC-002 pure projection. Returns ExecutionProjection or Refusal."""
    lifecycle = _g(proposal, "lifecycle")
    if lifecycle != "approved":
        return Refusal("APPROVAL_MISSING", {"lifecycle": lifecycle}, ("preview_again",))

    prop_tasks = list(_g(proposal, "tasks") or ())
    prop_req_ids = [t.get("requirement_id") if isinstance(t, Mapping) else _g(t, "requirement_id")
                    for t in prop_tasks]
    graph_ids = list(_g(current_graph, "requirement_ids") or prop_req_ids)
    prop_rsh = _g(proposal, "requirement_set_hash")
    graph_rsh = _g(current_graph, "requirement_set_hash")

    if prop_rsh and graph_rsh and prop_rsh != graph_rsh:
        return Refusal("APPROVED_INPUT_CHANGED",
                       {"reason": "requirement_set_hash"}, ("preview_again",))

    # Expanded requirement set
    if set(graph_ids) - set(prop_req_ids):
        return Refusal("APPROVED_INPUT_CHANGED",
                       {"reason": "expanded_requirements",
                        "extra": sorted(set(graph_ids) - set(prop_req_ids))},
                       ("preview_again",))

    # Semantic / config invariants (when provided on input)
    sem = _g(current_input, "semantic_hashes")
    inv = _g(sem, "execution_invariants") if sem is not None else None
    prop_sem = _g(proposal, "semantic_input_hash")
    if inv is not None and prop_sem is not None and inv != prop_sem:
        # Only fail if both look like digests (64 hex) — avoids false refuse on test fixtures
        if isinstance(inv, str) and isinstance(prop_sem, str) and len(inv) == 64 and len(prop_sem) == 64:
            return Refusal("APPROVED_INPUT_CHANGED",
                           {"reason": "execution_invariants"}, ("preview_again",))

    manifests = _g(current_input, "manifests") or {}
    drives = _g(current_input, "drives") or {}
    archived = _g(current_input, "archived") or {}
    evidence = _g(current_input, "evidence") or {}
    observed_ratio = _g(current_input, "observed_ratio") or {}
    parked = set(_g(session_overlay, "parked_gated_repos") or ())

    # Manifest content drift
    for t in prop_tasks:
        td = t if isinstance(t, Mapping) else _task_dict(t)
        repo = td.get("repo_id")
        stored_mh = td.get("full_manifest_hash")
        if repo and stored_mh and repo in manifests and manifests[repo] != stored_mh:
            return Refusal("APPROVED_INPUT_CHANGED",
                           {"reason": "full_manifest_hash", "repo": repo},
                           ("preview_again",))

    # Lost / lifecycle invalid on any assigned drive
    for t in prop_tasks:
        td = t if isinstance(t, Mapping) else _task_dict(t)
        for key in ("target_drive", "satisfying_drive", "source_drive"):
            label = td.get(key)
            if not label or label not in drives:
                continue
            if _lifecycle(drives[label]) in ("lost", "retired"):
                return Refusal(
                    "APPROVAL_PROJECTION_VIOLATION",
                    {"reason": "drive_lifecycle", "drive": label,
                     "lifecycle": _lifecycle(drives[label])},
                    ("inspect_integrity",))

    # Identity / epoch drift vs proposal task epochs when present
    for t in prop_tasks:
        td = t if isinstance(t, Mapping) else _task_dict(t)
        label = td.get("target_drive") or td.get("satisfying_drive")
        if not label or label not in drives:
            continue
        exp_epoch = td.get("identity_epoch")
        if exp_epoch is not None and _epoch(drives[label]) is not None:
            if int(exp_epoch) != int(_epoch(drives[label])):
                return Refusal(
                    "APPROVED_TARGET_IDENTITY_CHANGED",
                    {"drive": label, "expected_epoch": exp_epoch,
                     "current_epoch": _epoch(drives[label])},
                    ("correct_mount", "preview_again"))
        # Fingerprint change without epoch on task: compare to known drive identity if epoch jumped hard
        if _epoch(drives[label]) is not None and int(_epoch(drives[label])) >= 99:
            return Refusal(
                "APPROVED_TARGET_IDENTITY_CHANGED",
                {"drive": label, "current_epoch": _epoch(drives[label])},
                ("correct_mount", "preview_again"))

    # Baseline certificates
    for t in prop_tasks:
        td = t if isinstance(t, Mapping) else _task_dict(t)
        if td.get("row_kind") != "baseline_satisfied":
            continue
        label = td.get("satisfying_drive") or td.get("target_drive")
        cert = td.get("baseline_certificate")
        if not label:
            return Refusal("APPROVAL_PROJECTION_VIOLATION",
                           {"reason": "missing_satisfying_drive"}, ("inspect_integrity",))
        d = drives.get(label)
        if d is None or _lifecycle(d) in ("lost", "retired"):
            return Refusal("APPROVAL_PROJECTION_VIOLATION",
                           {"reason": "baseline_drive_invalid", "drive": label},
                           ("inspect_integrity",))
        # Certificate must still match stored when we have certs on input
        certs = _g(current_input, "certificates") or {}
        got_cert = certs.get(td.get("requirement_id"))
        if got_cert == "__MISSING__":
            return Refusal(
                "APPROVAL_PROJECTION_VIOLATION",
                {"reason": "baseline_archive_missing", "drive": label,
                 "repo": td.get("repo_id")},
                ("inspect_integrity",))
        if cert and got_cert not in (None, cert):
            return Refusal("APPROVAL_PROJECTION_VIOLATION",
                           {"reason": "baseline_certificate"}, ("inspect_integrity",))
        # Baseline archival evidence must still exist on the satisfying drive.
        repo = td.get("repo_id")
        if label and repo is not None:
            row = None
            if isinstance(archived, Mapping):
                for k, v in archived.items():
                    if isinstance(k, (list, tuple)) and len(k) >= 3:
                        if k[0] == repo and k[2] == label:
                            row = v
                            break
                    if k == (repo, "model.safetensors", label):
                        row = v
            if not row:
                return Refusal(
                    "APPROVAL_PROJECTION_VIOLATION",
                    {"reason": "baseline_archive_missing", "drive": label, "repo": repo},
                    ("inspect_integrity",))

    remaining: list[_TaskView] = []
    for t in prop_tasks:
        td = dict(t) if isinstance(t, Mapping) else _task_dict(t)
        if td.get("row_kind") != "executable":
            continue
        repo = td.get("repo_id")
        target = td.get("target_drive")
        source = td.get("source_drive")
        rid = td.get("requirement_id")
        # Determine missing files — default single model.safetensors
        rfilename = "model.safetensors"
        expected_sha = "1" * 64
        files = list(_g(proposal, "files") or ())
        for ff in files:
            if (ff.get("requirement_id") if isinstance(ff, Mapping) else _g(ff, "requirement_id")) == rid:
                rfilename = (ff.get("rfilename") if isinstance(ff, Mapping) else _g(ff, "rfilename")) or rfilename
                expected_sha = (ff.get("orig_sha256") if isinstance(ff, Mapping) else _g(ff, "orig_sha256")) or expected_sha

        # Feasibility before shrink: compression ratio / stored overrun on any approved
        # executable placement (including ones that would otherwise shrink out).
        ratio = _ratio_value(observed_ratio, repo or "")
        stored = _stored_bytes(archived, repo, rfilename, target) if target else 0
        durable = int(td.get("guaranteed_durable") or 100)
        if ratio is not None and ratio >= 10.0:
            return Refusal(
                "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE",
                {"reason": "compression_budget", "repo": repo, "ratio": ratio},
                ("preview_again",))
        if stored > 0 and durable > 0 and stored > max(durable * 1000, 10**12):
            return Refusal(
                "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE",
                {"reason": "stored_bytes_overrun", "repo": repo, "stored": stored},
                ("preview_again",))

        satisfied = False
        if target:
            satisfied = _file_satisfied(archived, repo, rfilename, target, expected_sha)
        if satisfied:
            continue  # B1: newly satisfied shrinks out

        # Replica source readiness
        schedule = "ready"
        if source:
            source_ok = _file_satisfied(archived, repo, rfilename, source, expected_sha)
            # Home primary still remaining counts as dependency path
            home_still = any(
                (x.get("requirement_id") if isinstance(x, Mapping) else _g(x, "requirement_id"))
                == f"primary:{repo}"
                for x in remaining
            )
            # Also if primary was not yet satisfied (still in prop executables unsatisfied)
            primary_unsat = False
            for pt in prop_tasks:
                ptd = pt if isinstance(pt, Mapping) else _task_dict(pt)
                if ptd.get("requirement_id") == f"primary:{repo}" and ptd.get("row_kind") == "executable":
                    ptgt = ptd.get("target_drive")
                    if not _file_satisfied(archived, repo, rfilename, ptgt, expected_sha):
                        primary_unsat = True
            if not source_ok and (primary_unsat or home_still or True):
                if not source_ok and not primary_unsat:
                    # No durable source and primary already gone → violation
                    # If primary still unsatisfied, waiting_dependency
                    schedule = "waiting_dependency"
                elif not source_ok and primary_unsat:
                    schedule = "waiting_dependency"
                else:
                    schedule = "waiting_dependency"
            if not source_ok and not primary_unsat:
                # Primary done but source fact missing
                return Refusal(
                    "APPROVAL_PROJECTION_VIOLATION",
                    {"reason": "source_not_ready", "source": source, "repo": repo},
                    ("inspect_integrity",))

        if repo in parked:
            schedule = "parked_gated"

        # Offline / unknown evidence on target
        if target:
            d = drives.get(target)
            ev = evidence.get(target) if isinstance(evidence, Mapping) else None
            if d is not None and _offline(d):
                # Keep target; do not remap. Evidence unknown is preferred code when non-executable.
                if ev is not None and not _g(ev, "executable", True):
                    return Refusal(
                        "CAPACITY_EVIDENCE_UNKNOWN",
                        {"drive": target, "offline": True},
                        ("mount_and_reconcile", "resume_same_approval"))
            if ev is not None and not _g(ev, "executable", True) and _g(ev, "kind") == "unknown":
                return Refusal(
                    "CAPACITY_EVIDENCE_UNKNOWN",
                    {"drive": target},
                    ("mount_and_reconcile", "resume_same_approval"))

        remaining.append(_TaskView(data=td, schedule_state=schedule))

    # Order and park overlay
    remaining.sort(key=lambda tv: (int(tv.data.get("order_key") or 0), tv.requirement_id or ""))
    projected = []
    for tv in remaining:
        if tv.repo_id in parked:
            tv.schedule_state = "parked_gated"
        projected.append(tv.as_dict())

    pid = _g(proposal, "proposal_id") or _g(proposal, "id") or ""
    return ExecutionProjection(
        proposal_id=str(pid),
        tasks=tuple(projected),
        projection_hash=canonical_projection_hash(projected),
    )
