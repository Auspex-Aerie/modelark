"""Shadow-only desired-state reconciliation for archive work (DEC-045 Phase 1).

The durable catalog remains authoritative.  This module derives exact copy facts,
requirements, and unassigned work intents without mutating the database or changing the
legacy fill executor.  Capacity placement materializes concrete tasks in a later phase.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from modelark import archive_manifest


class RequirementKind(str, Enum):
    PRIMARY = "primary"
    PROTECTED_HOME = "protected_home"
    PROTECTED_REPLICA = "protected_replica"


class TaskKind(str, Enum):
    FETCH = "fetch"
    REPLICATE = "replicate"


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
class DriveFact:
    drive_label: str
    role: str
    raid_backed: bool
    capacity_bytes: int
    fs_uuid: str | None = None
    annex_uuid: str | None = None
    serial: str | None = None


@dataclass(frozen=True)
class CopyRequirement:
    requirement_id: str
    repo_id: str
    kind: RequirementKind
    eligible_drives: tuple[str, ...]
    independent_of: str | None = None


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
    task_id: str
    kind: TaskKind
    requirement_id: str
    repo_id: str
    eligible_drives: tuple[str, ...]
    pinned_target: str | None
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
                    "pinned_target": item.pinned_target,
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


def _drives(con, plan_id: str) -> tuple[DriveFact, ...]:
    rows = con.execute(
        "SELECT d.drive_label,coalesce(d.role,'primary'),coalesce(d.raid_backed,0),"
        "coalesce(d.capacity_bytes,d.free_bytes,0),d.fs_uuid,d.annex_uuid,d.serial "
        "FROM plan_drives pd JOIN drives d USING(drive_label) WHERE pd.plan_id=? "
        "ORDER BY d.drive_label",
        [plan_id],
    ).fetchall()
    return tuple(
        DriveFact(
            drive_label=row[0],
            role=row[1],
            raid_backed=bool(row[2]),
            capacity_bytes=int(row[3] or 0),
            fs_uuid=row[4],
            annex_uuid=row[5],
            serial=row[6],
        )
        for row in rows
    )


def _requirements(con, repo_ids: tuple[str, ...], drives: tuple[DriveFact, ...]) -> tuple[CopyRequirement, ...]:
    if not repo_ids:
        return ()
    placeholders = ",".join("?" for _ in repo_ids)
    copies = dict(
        con.execute(
            f"SELECT repo_id,coalesce(numcopies,1) FROM models WHERE repo_id IN ({placeholders})",
            list(repo_ids),
        ).fetchall()
    )
    primary = tuple(d.drive_label for d in drives if d.role == "primary")
    raids = tuple(d.drive_label for d in drives if d.role == "primary" and d.raid_backed)
    replicas = tuple(d.drive_label for d in drives if d.role == "replica")
    fallback = ()
    if not raids and primary:
        by_label = {d.drive_label: d for d in drives}
        fallback = (
            sorted(primary, key=lambda label: (-by_label[label].capacity_bytes, label))[0],
        )

    out = []
    for repo_id in repo_ids:
        if int(copies.get(repo_id, 1) or 1) < 2:
            out.append(
                CopyRequirement(
                    requirement_id=f"primary:{repo_id}",
                    repo_id=repo_id,
                    kind=RequirementKind.PRIMARY,
                    eligible_drives=primary,
                )
            )
            continue
        home_id = f"protected_home:{repo_id}"
        out.append(
            CopyRequirement(
                requirement_id=home_id,
                repo_id=repo_id,
                kind=RequirementKind.PROTECTED_HOME,
                eligible_drives=raids or fallback,
            )
        )
        out.append(
            CopyRequirement(
                requirement_id=f"protected_replica:{repo_id}",
                repo_id=repo_id,
                kind=RequirementKind.PROTECTED_REPLICA,
                eligible_drives=replicas,
                independent_of=home_id,
            )
        )
    return tuple(out)


def _copy_facts(
    con,
    repo_ids: tuple[str, ...],
    plan_drives: tuple[DriveFact, ...],
    manifests: Mapping[str, tuple[archive_manifest.ManifestFile, ...]],
) -> tuple[CopyFact, ...]:
    if not repo_ids or not plan_drives:
        return ()
    repo_ph = ",".join("?" for _ in repo_ids)
    drive_labels = tuple(d.drive_label for d in plan_drives)
    drive_ph = ",".join("?" for _ in drive_labels)
    rows = con.execute(
        "SELECT repo_id,drive_label,rfilename,coalesce(stored_bytes,0) FROM archived "
        f"WHERE repo_id IN ({repo_ph}) AND drive_label IN ({drive_ph}) "
        "ORDER BY repo_id,drive_label,rfilename",
        [*repo_ids, *drive_labels],
    ).fetchall()
    grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for repo_id, drive_label, rfilename, stored_bytes in rows:
        grouped.setdefault((repo_id, drive_label), []).append((rfilename, int(stored_bytes or 0)))

    facts = []
    for (repo_id, drive_label), present in sorted(grouped.items()):
        manifest = manifests.get(repo_id)
        if manifest is None:
            continue
        facts.append(
            CopyFact(
                repo_id=repo_id,
                drive_label=drive_label,
                required_files=frozenset(item.rfilename for item in manifest),
                present_files=frozenset(name for name, _ in present),
                stored_bytes_by_file=tuple(present),
            )
        )
    return tuple(facts)


def _drive_rank(kind: RequirementKind, drive: DriveFact) -> tuple:
    if kind == RequirementKind.PROTECTED_REPLICA:
        return (drive.capacity_bytes, drive.drive_label)
    # PRIMARY and PROTECTED_HOME both prefer RAID-backed drives, then largest capacity.
    return (0 if drive.raid_backed else 1, -drive.capacity_bytes, drive.drive_label)


def _choose_complete(
    requirement: CopyRequirement,
    facts: Sequence[CopyFact],
    drive_by_label: Mapping[str, DriveFact],
) -> CopyFact | None:
    candidates = [
        fact for fact in facts
        if fact.complete and fact.drive_label in requirement.eligible_drives
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda fact: _drive_rank(requirement.kind, drive_by_label[fact.drive_label]))[0]


def _choose_partial(
    requirement: CopyRequirement,
    facts: Sequence[CopyFact],
    manifest: Sequence[archive_manifest.ManifestFile],
    drive_by_label: Mapping[str, DriveFact],
) -> CopyFact | None:
    sizes = {item.rfilename: item.size_bytes for item in manifest}
    candidates = [
        fact for fact in facts
        if not fact.complete and fact.required_present and fact.drive_label in requirement.eligible_drives
    ]
    if not candidates:
        return None

    def rank(fact: CopyFact) -> tuple:
        missing = sum(sizes[name] for name in fact.required_files - fact.present_files)
        return (
            missing,
            -len(fact.required_present),
            _drive_rank(requirement.kind, drive_by_label[fact.drive_label]),
            fact.drive_label,
        )

    return sorted(candidates, key=rank)[0]


def _same_failure_domain(left: DriveFact, right: DriveFact) -> bool:
    return any(
        a and b and a == b
        for a, b in (
            (left.fs_uuid, right.fs_uuid),
            (left.annex_uuid, right.annex_uuid),
            (left.serial, right.serial),
        )
    )


def reconcile_plan(
    con,
    plan_id: str,
    repo_ids: Sequence[str] | None = None,
    policy: archive_manifest.ArchivePolicy | None = None,
) -> ReconcileResult:
    """Derive exact requirements and work intents without writes or physical I/O."""
    repos = _selected_repos(con, repo_ids)
    manifest_batch = archive_manifest.inspect_manifests_for_repos(con, repos, policy)
    usable_repos = tuple(repo for repo in repos if repo in manifest_batch.manifests)
    drives = _drives(con, plan_id)
    drive_by_label = {drive.drive_label: drive for drive in drives}
    requirements = _requirements(con, usable_repos, drives)
    facts = _copy_facts(con, usable_repos, drives, manifest_batch.manifests)
    facts_by_repo: dict[str, list[CopyFact]] = {}
    for fact in facts:
        facts_by_repo.setdefault(fact.repo_id, []).append(fact)

    diagnostics = [
        _diag(
            "MANIFEST_POLICY",
            DiagnosticSeverity.BLOCKING,
            RecoveryClass.OPERATOR_ACTION,
            None,
            repo_id=repo_id,
            message=str(error),
        )
        for repo_id, error in sorted(manifest_batch.errors.items())
    ]
    matches: dict[str, str] = {}
    requirement_by_id = {item.requirement_id: item for item in requirements}

    for requirement in requirements:
        if not requirement.eligible_drives:
            diagnostics.append(
                _diag(
                    "TARGET_TIER_MISSING",
                    DiagnosticSeverity.BLOCKING,
                    RecoveryClass.OPERATOR_ACTION,
                    requirement.requirement_id,
                    repo_id=requirement.repo_id,
                    kind=requirement.kind.value,
                )
            )
            continue
        match = _choose_complete(
            requirement, facts_by_repo.get(requirement.repo_id, ()), drive_by_label
        )
        if match is not None:
            matches[requirement.requirement_id] = match.drive_label
            continue
        wrong = sorted(
            fact.drive_label
            for fact in facts_by_repo.get(requirement.repo_id, ())
            if fact.complete and fact.drive_label not in requirement.eligible_drives
        )
        if wrong:
            diagnostics.append(
                _diag(
                    "COPY_POLICY_DRIFT",
                    DiagnosticSeverity.WARNING,
                    RecoveryClass.AUTOMATIC,
                    requirement.requirement_id,
                    repo_id=requirement.repo_id,
                    drives=tuple(wrong),
                )
            )

    intents = []
    for requirement in requirements:
        if requirement.requirement_id in matches:
            continue
        manifest = manifest_batch.manifests[requirement.repo_id]
        partial = _choose_partial(
            requirement, facts_by_repo.get(requirement.repo_id, ()), manifest, drive_by_label
        )
        source_drive = None
        depends_on = None
        if requirement.kind == RequirementKind.PROTECTED_REPLICA:
            home_id = requirement.independent_of
            source_drive = matches.get(home_id or "")
            if source_drive is None and home_id in requirement_by_id:
                depends_on = home_id
        kind = (TaskKind.REPLICATE if requirement.kind == RequirementKind.PROTECTED_REPLICA
                else TaskKind.FETCH)
        intents.append(
            WorkIntent(
                task_id=f"{kind.value}:{requirement.requirement_id}",
                kind=kind,
                requirement_id=requirement.requirement_id,
                repo_id=requirement.repo_id,
                eligible_drives=requirement.eligible_drives,
                pinned_target=partial.drive_label if partial else None,
                source_drive=source_drive,
                depends_on_requirement=depends_on,
            )
        )
        if kind == TaskKind.REPLICATE and source_drive is None and depends_on is None:
            diagnostics.append(
                _diag(
                    "SOURCE_INCOMPLETE",
                    DiagnosticSeverity.ERROR,
                    RecoveryClass.CODE_DEFECT,
                    requirement.requirement_id,
                    repo_id=requirement.repo_id,
                )
            )

    # Warn only from matched copies: unmet dependencies already have a more useful root diagnostic.
    for repo_id in usable_repos:
        home = matches.get(f"protected_home:{repo_id}")
        replica = matches.get(f"protected_replica:{repo_id}")
        if home and replica and _same_failure_domain(drive_by_label[home], drive_by_label[replica]):
            diagnostics.append(
                _diag(
                    "FAILURE_DOMAIN_SUSPECT",
                    DiagnosticSeverity.WARNING,
                    RecoveryClass.OPERATOR_ACTION,
                    f"protected_replica:{repo_id}",
                    repo_id=repo_id,
                    drives=(home, replica),
                )
            )

    diagnostics.sort(key=lambda item: (item.code, item.requirement_id or "", item.detail))
    intents.sort(key=lambda item: (item.repo_id, item.requirement_id))
    return ReconcileResult(
        plan_id=plan_id,
        repo_ids=repos,
        manifests=dict(manifest_batch.manifests),
        requirements=requirements,
        facts=facts,
        intents=tuple(intents),
        satisfied=frozenset(matches),
        matches=tuple(sorted(matches.items())),
        diagnostics=tuple(diagnostics),
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
    provisioning: str = "uncompressed",
) -> dict:
    """Read-only new/legacy diagnostic. The legacy executor does not consume this result."""
    result = reconcile_plan(con, plan_id, repo_ids)
    legacy_error = None
    reservations: tuple[LegacyReservation, ...] = ()
    try:
        from modelark import librarian  # local import keeps the reconciler dependency-neutral

        placement = librarian.plan_placements(
            con, repos=list(repo_ids) if repo_ids is not None else None,
            plan_id=plan_id, provisioning=provisioning,
        )
        reservations = _legacy_reservations(con, placement)
    except Exception as exc:
        # This is a comparison seam: legacy planner/adapter drift must not discard the
        # independently derived graph that the operator invoked --explain to inspect.
        legacy_error = f"{type(exc).__name__}: {exc}"
    normalized = normalize_legacy_reservations(reservations, result)
    payload = result.to_dict()
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
        "executor": "legacy (shadow result is not consumed)",
    }
    return payload
