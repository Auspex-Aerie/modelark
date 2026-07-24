"""Desired-state reconciliation for archive work (DEC-045, RFC-002 / DEC-049, issue #36a).

The durable catalog remains authoritative. ``reconcile_plan`` is now a compatibility façade over the
pure functional core in :mod:`modelark.candidates`: one consistent read transaction captures catalog
facts (and freezes config/ratio) into an immutable ``PlannerInput``; the pure ``requirements`` and
``candidates`` functions are the sole semantic authority for satisfaction, reuse, sources, and candidate
work. The canonical, no-pin :class:`~modelark.candidates.CandidateSet` is exposed on the result; the
legacy ``ReconcileResult`` fields are projections of it for compatibility. Placement selection is the
legacy ``tiered_v1`` adapter in :mod:`modelark.capacity` (removed at #38) — reconcile chooses nothing.
"""
from __future__ import annotations

import hashlib
import json
import warnings
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from modelark import archive_manifest, candidates
from modelark.candidates import (  # re-exported: candidates.py is the owner of these shared types
    CopyRequirement,
    DriveFact,
    RequirementKind,
    TaskKind,
)

__all__ = [  # names other modules import from reconcile
    "CopyFact", "CopyRequirement", "DriveFact", "DiagnosticSeverity", "PlanDiagnostic",
    "RecoveryClass", "ReconcileResult", "RequirementKind", "TaskKind", "WorkIntent",
    "capture_planner_input", "reconcile_plan", "shadow_report",
]


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"
    ERROR = "error"


class RecoveryClass(str, Enum):
    AUTOMATIC = "automatic"
    OPERATOR_ACTION = "operator_action"
    CODE_DEFECT = "code_defect"


@dataclass(frozen=True)
class CopyFact:
    repo_id: str
    drive_label: str
    required_files: frozenset[str]
    present_files: frozenset[str]
    stored_bytes_by_file: tuple[tuple[str, int], ...]

    @property
    def complete(self) -> bool:
        return self.required_files <= self.present_files

    @property
    def required_present(self) -> frozenset[str]:
        return self.required_files & self.present_files


@dataclass(frozen=True)
class WorkIntent:
    # Compatibility projection only: an unsatisfied requirement's work list for legacy consumers.
    # It carries NO pinned_target — placement selection is the capacity tiered_v1 adapter over the
    # canonical CandidateSet, never a reconcile-side pin (#36a removes _choose_partial authority).
    task_id: str
    kind: TaskKind
    requirement_id: str
    repo_id: str
    eligible_drives: tuple[str, ...]
    source_drive: str | None
    depends_on_requirement: str | None


@dataclass(frozen=True)
class PlanDiagnostic:
    code: str
    severity: DiagnosticSeverity
    recovery: RecoveryClass
    requirement_id: str | None
    detail: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class LegacyReservation:
    requirement_id: str
    repo_id: str
    target_drive: str
    order: int


@dataclass(frozen=True)
class ReconcileResult:
    plan_id: str
    repo_ids: tuple[str, ...]
    manifests: Mapping[str, tuple[archive_manifest.ManifestFile, ...]]
    requirements: tuple[CopyRequirement, ...]
    facts: tuple[CopyFact, ...]
    intents: tuple[WorkIntent, ...]
    satisfied: frozenset[str]
    matches: tuple[tuple[str, str], ...]
    diagnostics: tuple[PlanDiagnostic, ...]
    candidates: candidates.CandidateSet  # canonical no-pin authority; legacy fields above project from it

    def to_dict(self) -> dict:
        payload = {
            "plan_id": self.plan_id,
            "repos": list(self.repo_ids),
            "manifests": {
                repo_id: [
                    {
                        "rfilename": item.rfilename,
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                        "format": item.format,
                        "quant": item.quant,
                        "storage_action": item.storage_action,
                    }
                    for item in manifest
                ]
                for repo_id, manifest in sorted(self.manifests.items())
            },
            "requirements": [
                {
                    "id": item.requirement_id,
                    "repo": item.repo_id,
                    "kind": item.kind.value,
                    "eligible_drives": list(item.eligible_drives),
                    "independent_of": item.independent_of,
                }
                for item in self.requirements
            ],
            "facts": [
                {
                    "repo": item.repo_id,
                    "drive": item.drive_label,
                    "required": sorted(item.required_files),
                    "present": sorted(item.present_files),
                    "stored_bytes_by_file": dict(item.stored_bytes_by_file),
                    "complete": item.complete,
                }
                for item in self.facts
            ],
            "intents": [
                {
                    "id": item.task_id,
                    "kind": item.kind.value,
                    "requirement_id": item.requirement_id,
                    "repo": item.repo_id,
                    "eligible_drives": list(item.eligible_drives),
                    "source_drive": item.source_drive,
                    "depends_on_requirement": item.depends_on_requirement,
                }
                for item in self.intents
            ],
            "satisfied": sorted(self.satisfied),
            "matches": dict(self.matches),
            "diagnostics": [
                {
                    "code": item.code,
                    "severity": item.severity.value,
                    "recovery": item.recovery.value,
                    "requirement_id": item.requirement_id,
                    "detail": dict(item.detail),
                }
                for item in self.diagnostics
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload["graph_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        return payload


def _diag(
    code: str,
    severity: DiagnosticSeverity,
    recovery: RecoveryClass,
    requirement_id: str | None,
    **detail,
) -> PlanDiagnostic:
    return PlanDiagnostic(
        code=code,
        severity=severity,
        recovery=recovery,
        requirement_id=requirement_id,
        detail=tuple(sorted(detail.items())),
    )


def _selected_repos(con, repo_ids: Sequence[str] | None) -> tuple[str, ...]:
    if repo_ids is not None:
        return tuple(sorted(set(repo_ids)))
    return tuple(
        row[0]
        for row in con.execute(
            "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY repo_id"
        ).fetchall()
    )


@contextmanager
def _consistent_read(con):
    """One consistent catalog read snapshot. Reuse the caller's transaction if one is already open."""
    if con.in_transaction:
        yield
        return
    con.execute("BEGIN")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _drive_facts(con, plan_id: str) -> tuple[DriveFact, ...]:
    rows = con.execute(
        "SELECT d.drive_label,coalesce(d.role,'primary'),coalesce(d.raid_backed,0),"
        "coalesce(d.capacity_bytes,d.free_bytes,0),"
        "coalesce(d.filesystem_capacity_bytes,d.capacity_bytes,0),coalesce(d.identity_epoch,1),"
        "d.fs_uuid,d.annex_uuid,d.serial "
        "FROM plan_drives pd JOIN drives d USING(drive_label) WHERE pd.plan_id=? "
        "ORDER BY d.drive_label",
        [plan_id],
    ).fetchall()
    return tuple(
        DriveFact(
            drive_label=row[0], role=row[1], raid_backed=bool(row[2]),
            capacity_bytes=int(row[3] or 0), filesystem_capacity_bytes=int(row[4] or 0),
            identity_epoch=int(row[5] or 1), fs_uuid=row[6], annex_uuid=row[7], serial=row[8],
        )
        for row in rows
    )


def _numcopies(con, repo_ids: tuple[str, ...]) -> dict[str, int]:
    if not repo_ids:
        return {}
    placeholders = ",".join("?" for _ in repo_ids)
    return dict(
        con.execute(
            f"SELECT repo_id,coalesce(numcopies,1) FROM models WHERE repo_id IN ({placeholders})",
            list(repo_ids),
        ).fetchall()
    )


def _archived_facts(
    con, repo_ids: tuple[str, ...], drive_labels: tuple[str, ...], rfilenames: tuple[str, ...]
) -> tuple[candidates.ArchivedFileFact, ...]:
    # Only archived rows for files some manifest actually references are relevant to satisfaction/reuse;
    # skipping the rest keeps candidate construction bounded by required files, not total archived rows.
    if not repo_ids or not drive_labels or not rfilenames:
        return ()
    repo_ph = ",".join("?" for _ in repo_ids)
    drive_ph = ",".join("?" for _ in drive_labels)
    file_ph = ",".join("?" for _ in rfilenames)
    rows = con.execute(
        "SELECT repo_id,drive_label,rfilename,orig_sha256,orig_bytes,stored_bytes,annex_key FROM archived "
        f"WHERE repo_id IN ({repo_ph}) AND drive_label IN ({drive_ph}) AND rfilename IN ({file_ph}) "
        "ORDER BY repo_id,drive_label,rfilename",
        [*repo_ids, *drive_labels, *rfilenames],
    ).fetchall()
    return tuple(
        candidates.ArchivedFileFact(
            repo_id=row[0], drive_label=row[1], rfilename=row[2], orig_sha256=row[3],
            orig_bytes=(int(row[4]) if row[4] is not None else None),
            stored_bytes=(int(row[5]) if row[5] is not None else None), annex_key=row[6],
        )
        for row in rows
    )


@dataclass(frozen=True)
class _Facts:
    planner_input: candidates.PlannerInput
    manifest_batch: archive_manifest.ManifestBatch
    selected_repos: tuple[str, ...]


def _read_facts(con, plan_id, repo_ids=None, policy=None, compression_cfg=None) -> _Facts:
    from modelark import capacity, wishlist  # local: capacity imports reconcile at module scope
    with _consistent_read(con):
        repos = _selected_repos(con, repo_ids)
        batch = archive_manifest.inspect_manifests_for_repos(con, repos, policy)
        usable = tuple(repo for repo in repos if repo in batch.manifests)
        drives = _drive_facts(con, plan_id)
        numcopies = _numcopies(con, usable)
        manifest_files = tuple(sorted({
            item.rfilename for manifest in batch.manifests.values() for item in manifest
        }))
        archived = _archived_facts(con, usable, tuple(d.drive_label for d in drives), manifest_files)
        # Graph-affecting compression config is captured ONCE here (the impure shell); an explicit
        # override is the supported injection point (e.g. tests), never a later plan_capacity argument.
        config = wishlist.compression() if compression_cfg is None else dict(compression_cfg)
        ratio = capacity.plan_float_ratio(con)
    planner_input = candidates.PlannerInput(
        plan_id=plan_id,
        selection=usable,
        manifests=tuple((repo, batch.manifests[repo]) for repo in usable),
        numcopies=tuple(sorted(numcopies.items())),
        drives=drives,
        archived=archived,
        compression_cfg=tuple(sorted((str(key), value) for key, value in config.items())),
        float_ratio=ratio,
    )
    return _Facts(planner_input, batch, repos)


def capture_planner_input(con, plan_id, repo_ids=None, policy=None,
                          compression_cfg=None) -> candidates.PlannerInput:
    """Fact reader (impure shell): capture catalog facts in one consistent read transaction and freeze
    graph-affecting config (``compression_cfg`` override or ``wishlist.compression()``) plus the observed
    float ratio into an immutable, pure ``PlannerInput``."""
    return _read_facts(con, plan_id, repo_ids, policy, compression_cfg).planner_input


def _same_failure_domain(left: DriveFact, right: DriveFact) -> bool:
    return any(
        a and b and a == b
        for a, b in (
            (left.fs_uuid, right.fs_uuid),
            (left.annex_uuid, right.annex_uuid),
            (left.serial, right.serial),
        )
    )


def _rank_satisfying_drive(kind: RequirementKind, drive: DriveFact) -> tuple:
    """Canonical pick among proven complete copies for the legacy ``matches`` projection (mirrors the
    pre-#36a ``_choose_complete`` order: RAID-first/largest for homes, smallest for replicas)."""
    if kind == RequirementKind.PROTECTED_REPLICA:
        return (drive.capacity_bytes, drive.drive_label)
    return (0 if drive.raid_backed else 1, -drive.capacity_bytes, drive.drive_label)


def _facts_projection(inp: candidates.PlannerInput) -> tuple[CopyFact, ...]:
    """Raw archived facts for display (ruling #4: raw facts may remain visible). Not authority."""
    manifests = dict(inp.manifests)
    grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for row in inp.archived:
        grouped.setdefault((row.repo_id, row.drive_label), []).append(
            (row.rfilename, int(row.stored_bytes or 0))
        )
    facts = []
    for (repo_id, drive_label), present in sorted(grouped.items()):
        manifest = manifests.get(repo_id)
        if manifest is None:
            continue
        facts.append(
            CopyFact(
                repo_id=repo_id, drive_label=drive_label,
                required_files=frozenset(item.rfilename for item in manifest),
                present_files=frozenset(name for name, _ in present),
                stored_bytes_by_file=tuple(present),
            )
        )
    return tuple(facts)


def _intents_projection(cset: candidates.CandidateSet) -> tuple[WorkIntent, ...]:
    """Compatibility work list projected from the canonical candidates — no pin, one row per unsatisfied
    requirement, with the canonical source/dependency for legacy display only."""
    intents = []
    for requirement_id, cands in cset.by_requirement:
        if not cands:
            continue
        kind = cands[0].task_kind
        repo_id = requirement_id.split(":", 1)[1]
        eligible = tuple(sorted({item.target_drive for item in cands}))
        source_drive = None
        depends_on = None
        sources = sorted({
            item.source.drive_label for item in cands
            if isinstance(item.source, candidates.SourceIdentity)
        })
        if sources:
            source_drive = sources[0]
        else:
            pending = [
                item.source.requirement_id for item in cands
                if isinstance(item.source, candidates.PendingHome)
            ]
            depends_on = pending[0] if pending else None
        intents.append(
            WorkIntent(
                task_id=f"{kind.value}:{requirement_id}", kind=kind, requirement_id=requirement_id,
                repo_id=repo_id, eligible_drives=eligible, source_drive=source_drive,
                depends_on_requirement=depends_on,
            )
        )
    return tuple(sorted(intents, key=lambda item: (item.repo_id, item.requirement_id)))


def reconcile_plan(
    con,
    plan_id: str,
    repo_ids: Sequence[str] | None = None,
    policy: archive_manifest.ArchivePolicy | None = None,
    *,
    compression_cfg: Mapping[str, object] | None = None,
) -> ReconcileResult:
    """Compatibility façade over the pure core: capture one consistent snapshot, run pure
    requirements/candidates (the sole authority for satisfaction, reuse, sources, and work), expose the
    canonical no-pin ``CandidateSet``, and project the legacy fields from it. Chooses no placement."""
    facts = _read_facts(con, plan_id, repo_ids, policy, compression_cfg)
    inp = facts.planner_input
    graph = candidates.requirements(inp)
    cset = candidates.candidates(inp, graph)

    drive_by_label = {drive.drive_label: drive for drive in inp.drives}
    req_by_id = {item.requirement_id: item for item in graph.desired}

    satisfied = frozenset(item.requirement_id for item in cset.satisfied)
    matches: dict[str, str] = {}
    for sat in cset.satisfied:
        kind = req_by_id[sat.requirement_id].kind
        matches[sat.requirement_id] = sorted(
            (copy.drive_label for copy in sat.copies),
            key=lambda label: _rank_satisfying_drive(kind, drive_by_label[label]),
        )[0]

    diagnostics = [
        _diag("MANIFEST_POLICY", DiagnosticSeverity.BLOCKING, RecoveryClass.OPERATOR_ACTION, None,
              repo_id=repo_id, message=str(error))
        for repo_id, error in sorted(facts.manifest_batch.errors.items())
    ]
    unproven_drives: dict[str, set[str]] = {}
    for row in cset.drift:
        if row.reason == "unproven_provenance":
            unproven_drives.setdefault(row.requirement_id, set()).add(row.drive_label)

    # Every blocked requirement (no eligible tier, or all eligible targets unproven) is a BLOCKING
    # diagnostic — an unfinished copy can never be reported feasible (gap #1).
    for item in cset.blocked:
        req = req_by_id[item.requirement_id]
        if item.reason == "no_eligible_tier":
            diagnostics.append(_diag(
                "TARGET_TIER_MISSING", DiagnosticSeverity.BLOCKING, RecoveryClass.OPERATOR_ACTION,
                item.requirement_id, repo_id=req.repo_id, kind=req.kind.value))
        else:
            diagnostics.append(_diag(
                "UNPROVEN_PROVENANCE", DiagnosticSeverity.BLOCKING, RecoveryClass.OPERATOR_ACTION,
                item.requirement_id, repo_id=req.repo_id,
                drives=tuple(sorted(unproven_drives.get(item.requirement_id, ())))))

    # Drift on requirements that still have a valid candidate is advisory only (ruling #4): wrong-tier
    # proven copies are COPY_POLICY_DRIFT; unproven rows on some targets while others remain valid warn.
    blocked_ids = {item.requirement_id for item in cset.blocked}
    with_candidates = {requirement_id for requirement_id, _ in cset.by_requirement}
    wrong_tier: dict[str, set[str]] = {}
    for row in cset.drift:
        if (row.reason == "wrong_tier" and row.requirement_id not in satisfied
                and row.requirement_id not in blocked_ids):
            wrong_tier.setdefault(row.requirement_id, set()).add(row.drive_label)
    for requirement_id, drives in sorted(wrong_tier.items()):
        diagnostics.append(_diag(
            "COPY_POLICY_DRIFT", DiagnosticSeverity.WARNING, RecoveryClass.AUTOMATIC, requirement_id,
            repo_id=requirement_id.split(":", 1)[1], drives=tuple(sorted(drives))))
    for requirement_id, drives in sorted(unproven_drives.items()):
        if requirement_id in with_candidates:
            diagnostics.append(_diag(
                "UNPROVEN_PROVENANCE", DiagnosticSeverity.WARNING, RecoveryClass.OPERATOR_ACTION,
                requirement_id, repo_id=requirement_id.split(":", 1)[1], drives=tuple(sorted(drives))))

    for repo_id in inp.selection:
        home = matches.get(f"protected_home:{repo_id}")
        replica = matches.get(f"protected_replica:{repo_id}")
        if home and replica and _same_failure_domain(drive_by_label[home], drive_by_label[replica]):
            diagnostics.append(_diag(
                "FAILURE_DOMAIN_SUSPECT", DiagnosticSeverity.WARNING, RecoveryClass.OPERATOR_ACTION,
                f"protected_replica:{repo_id}", repo_id=repo_id, drives=(home, replica)))

    diagnostics.sort(key=lambda item: (item.code, item.requirement_id or "", item.detail))
    return ReconcileResult(
        plan_id=plan_id,
        repo_ids=facts.selected_repos,
        manifests=dict(inp.manifests),
        requirements=graph.desired,
        facts=_facts_projection(inp),
        intents=_intents_projection(cset),
        satisfied=satisfied,
        matches=tuple(sorted(matches.items())),
        diagnostics=tuple(diagnostics),
        candidates=cset,
    )


def normalize_legacy_reservations(
    reservations: Sequence[LegacyReservation],
    result: ReconcileResult,
) -> tuple[LegacyReservation, ...]:
    """Remove only reservations independently proven satisfied; never rewrite targets/order."""
    return tuple(item for item in reservations if item.requirement_id not in result.satisfied)


def _legacy_reservations(con, placement: dict) -> tuple[LegacyReservation, ...]:
    repos = {
        item["repo"]
        for tier in (placement["primary"]["assign"], placement["replica"]["assign"])
        for items in tier.values()
        for item in items
    }
    copies = {}
    if repos:
        ordered = sorted(repos)
        placeholders = ",".join("?" for _ in ordered)
        copies = dict(
            con.execute(
                f"SELECT repo_id,coalesce(numcopies,1) FROM models WHERE repo_id IN ({placeholders})",
                ordered,
            ).fetchall()
        )
    out = []
    order = 0
    for label, items in placement["primary"]["assign"].items():
        for item in items:
            repo_id = item["repo"]
            kind = "protected_home" if int(copies.get(repo_id, 1)) >= 2 else "primary"
            out.append(LegacyReservation(f"{kind}:{repo_id}", repo_id, label, order))
            order += 1
    for label, items in placement["replica"]["assign"].items():
        for item in items:
            repo_id = item["repo"]
            out.append(LegacyReservation(f"protected_replica:{repo_id}", repo_id, label, order))
            order += 1
    return tuple(out)


def shadow_report(
    con,
    plan_id: str,
    repo_ids: Sequence[str] | None = None,
    capacity_mode: str | None = None,
    *,
    provisioning: str | None = None,
) -> dict:
    """Read-only reconciled graph/ledger diagnostic with a legacy-placement comparison."""
    canonical_alias = {
        "uncompressed": "guaranteed", "compressed": "compression_aware",
    }
    if provisioning is not None:
        warnings.warn(
            "shadow_report(provisioning=...) is deprecated; use capacity_mode=...",
            DeprecationWarning,
            stacklevel=2,
        )
        if capacity_mode is not None:
            canonical = canonical_alias.get(capacity_mode, capacity_mode)
            legacy = canonical_alias.get(provisioning, provisioning)
            if canonical != legacy:
                raise ValueError("capacity_mode and deprecated provisioning disagree")
        capacity_mode = provisioning
    capacity_mode = capacity_mode or "guaranteed"
    aliases = {
        "guaranteed": ("guaranteed", "uncompressed"),
        "compression_aware": ("compression_aware", "compressed"),
        "uncompressed": ("guaranteed", "uncompressed"),
        "compressed": ("compression_aware", "compressed"),
    }
    try:
        canonical_mode, legacy_mode = aliases[capacity_mode]
    except KeyError as exc:
        raise ValueError(f"unsupported capacity mode {capacity_mode!r}") from exc
    result = reconcile_plan(con, plan_id, repo_ids)
    legacy_error = None
    reservations: tuple[LegacyReservation, ...] = ()
    try:
        from modelark import librarian  # local import keeps the reconciler dependency-neutral

        placement = librarian.plan_placements(
            con, repos=list(repo_ids) if repo_ids is not None else None,
            plan_id=plan_id, capacity_mode=legacy_mode,
        )
        reservations = _legacy_reservations(con, placement)
    except Exception as exc:
        # This is a comparison seam: legacy planner/adapter drift must not discard the
        # independently derived graph that the operator invoked --explain to inspect.
        legacy_error = f"{type(exc).__name__}: {exc}"
    normalized = normalize_legacy_reservations(reservations, result)
    payload = result.to_dict()
    from datetime import datetime, timezone

    from modelark import admission, capacity, fetch  # local imports: types/transport depend on graph types
    labels = [row[0] for row in con.execute(
        "SELECT drive_label FROM plan_drives WHERE plan_id=? ORDER BY drive_label", [plan_id]).fetchall()]
    now = datetime.now(timezone.utc).isoformat(sep=" ")
    evidence = admission.preview_by_drive(
        con, labels, observe=lambda label: fetch.observe_for_admission(con, label), now=now)
    capacity_result = capacity.plan_capacity(
        con, result, capacity_mode=canonical_mode, evidence_by_drive=evidence)
    payload["capacity"] = capacity_result.to_dict()
    legacy_counts = Counter(item.requirement_id for item in normalized)
    legacy_duplicates = sorted(key for key, count in legacy_counts.items() if count > 1)
    legacy_targets = {item.requirement_id: item.target_drive for item in normalized}
    new_targets = {item.requirement_id: item.target_drive for item in capacity_result.tasks}
    shared = sorted(legacy_targets.keys() & new_targets.keys())
    target_mismatches = [
        {
            "requirement_id": requirement_id,
            "legacy": legacy_targets[requirement_id],
            "tiered_v1": new_targets[requirement_id],
        }
        for requirement_id in shared
        if legacy_targets[requirement_id] != new_targets[requirement_id]
    ]
    payload["placement_comparison"] = {
        "target_equivalent": (
            not legacy_duplicates
            and not target_mismatches
            and legacy_targets.keys() == new_targets.keys()
        ),
        "legacy_duplicate_requirements": legacy_duplicates,
        "legacy_only": sorted(legacy_targets.keys() - new_targets.keys()),
        "tiered_v1_only": sorted(new_targets.keys() - legacy_targets.keys()),
        "target_mismatches": target_mismatches,
        "normalization": "satisfied requirements removed only; targets are never rewritten",
    }
    payload["shadow"] = {
        "legacy_error": legacy_error,
        "legacy_reservations": len(reservations),
        "normalized_legacy_reservations": len(normalized),
        "satisfied_legacy_reservations_removed": len(reservations) - len(normalized),
        "legacy_remaining": [
            {
                "requirement_id": item.requirement_id,
                "repo": item.repo_id,
                "target": item.target_drive,
                "order": item.order,
            }
            for item in normalized
        ],
        "new_intents": len(result.intents),
        "executor": "reconciled (legacy data is comparison-only)",
    }
    return payload
