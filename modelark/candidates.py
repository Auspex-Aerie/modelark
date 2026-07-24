"""Pure requirement & candidate construction for placement approval (RFC-002 / DEC-049, issue #36a).

The functional core. From an immutable :class:`PlannerInput` it derives, for every desired copy, either
canonical proof that the copy is already satisfied or the full set of reusable-work alternatives — every
verified finish-in-place partial and every policy-permitted fresh target — with exact reuse/provenance,
exact missing-file identity, deterministic costs, and singular replica sources. It chooses nothing:
feasibility, ranking, and target selection are #38.

Pure: no SQLite, filesystem, configuration, clock, or network access. The impure shell
(:func:`modelark.reconcile.capture_planner_input`) captures catalog facts in one consistent read
transaction and freezes config/ratio into the input as data. Budgets come from the shared
:mod:`modelark.budgets` seam so the legacy capacity path and this core cannot drift.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from modelark import archive_manifest, budgets
from modelark.budgets import CandidateBudget, FileBudget  # noqa: F401 — re-exported: one shared budget truth


class RequirementKind(str, Enum):
    PRIMARY = "primary"
    PROTECTED_HOME = "protected_home"
    PROTECTED_REPLICA = "protected_replica"


class TaskKind(str, Enum):
    FETCH = "fetch"
    REPLICATE = "replicate"


class ProofSource(str, Enum):
    MANIFEST_SHA256 = "manifest_sha256"          # upstream LFS hash present and matched
    ARCHIVED_ORIG_SHA256 = "archived_orig_sha256"  # hashless manifest bound by a non-null archived hash


@dataclass(frozen=True)
class DriveFact:
    drive_label: str
    role: str
    raid_backed: bool
    capacity_bytes: int
    filesystem_capacity_bytes: int | None
    identity_epoch: int
    fs_uuid: str | None = None
    annex_uuid: str | None = None
    serial: str | None = None


@dataclass(frozen=True)
class ArchivedFileFact:
    repo_id: str
    drive_label: str
    rfilename: str
    orig_sha256: str | None
    orig_bytes: int | None
    stored_bytes: int | None
    annex_key: str | None = None


@dataclass(frozen=True)
class CopyRequirement:
    requirement_id: str
    repo_id: str
    kind: RequirementKind
    eligible_drives: tuple[str, ...]
    independent_of: str | None = None


@dataclass(frozen=True)
class RequirementGraph:
    desired: tuple[CopyRequirement, ...]
    requirement_set_hash: str


@dataclass(frozen=True)
class ReusableFile:
    rfilename: str
    size_bytes: int
    bound_hash: str
    proof_source: ProofSource


@dataclass(frozen=True)
class SourceIdentity:
    """The exact drive a replica copies from. Singular per candidate: differing stored sizes/annex keys
    across complete homes cannot share one exact budget, so each proven home yields its own candidate."""
    drive_label: str
    annex_key: str | None
    orig_sha256: str | None


@dataclass(frozen=True)
class PendingHome:
    """A replica source whose home target is not yet resolved; #38 resolves it to an exact drive."""
    requirement_id: str


@dataclass(frozen=True)
class MovementCost:
    transfer_bytes: int          # fetch: raw acquisition bytes; replica: stored transfer from its source


@dataclass(frozen=True)
class Candidate:
    requirement_id: str
    task_kind: TaskKind
    target_drive: str
    source: SourceIdentity | PendingHome | None
    depends_on_requirement: str | None
    reused_files: tuple[ReusableFile, ...]
    missing_files: tuple[archive_manifest.ManifestFile, ...]
    budget: CandidateBudget
    movement_cost: MovementCost


@dataclass(frozen=True)
class SatisfiedCopy:
    drive_label: str
    reused_files: tuple[ReusableFile, ...]
    source_identity: SourceIdentity | None


@dataclass(frozen=True)
class Satisfaction:
    requirement_id: str
    copies: tuple[SatisfiedCopy, ...]


@dataclass(frozen=True)
class DriftRow:
    """A required-file archived row that cannot back canonical work: an unproven/mismatched row on a
    policy-permitted target (its target is omitted, never silently overwritten), or a proven copy on a
    non-eligible drive (no annex-to-annex relocation candidate). Preserved for operator visibility."""
    requirement_id: str
    drive_label: str
    rfilename: str
    reason: str


@dataclass(frozen=True)
class CandidateSet:
    satisfied: tuple[Satisfaction, ...]
    by_requirement: tuple[tuple[str, tuple[Candidate, ...]], ...]
    drift: tuple[DriftRow, ...]


@dataclass(frozen=True)
class PlannerInput:
    plan_id: str
    selection: tuple[str, ...]
    manifests: tuple[tuple[str, tuple[archive_manifest.ManifestFile, ...]], ...]
    numcopies: tuple[tuple[str, int], ...]
    drives: tuple[DriveFact, ...]
    archived: tuple[ArchivedFileFact, ...]
    compression_cfg: tuple[tuple[str, object], ...]
    float_ratio: float


# ------------------------------------------------------------------------------------------------
# Requirements
# ------------------------------------------------------------------------------------------------
def requirements(inp: PlannerInput) -> RequirementGraph:
    """Desired copy set: numcopies<2 → one primary copy; numcopies≥2 → a protected home plus an
    independent replica. Home eligibility is RAID-backed primaries, or (no-RAID fallback for this slice)
    the single largest primary. Only repositories with a supported manifest yield requirements."""
    by_label = {d.drive_label: d for d in inp.drives}
    order = sorted(by_label)
    primary = tuple(label for label in order if by_label[label].role == "primary")
    raids = tuple(label for label in primary if by_label[label].raid_backed)
    replicas = tuple(label for label in order if by_label[label].role == "replica")
    fallback: tuple[str, ...] = ()
    if not raids and primary:
        fallback = (sorted(primary, key=lambda label: (-by_label[label].capacity_bytes, label))[0],)

    numcopies = dict(inp.numcopies)
    desired: list[CopyRequirement] = []
    for repo, _manifest in inp.manifests:
        if int(numcopies.get(repo, 1) or 1) < 2:
            desired.append(CopyRequirement(f"primary:{repo}", repo, RequirementKind.PRIMARY, primary))
            continue
        home_id = f"protected_home:{repo}"
        desired.append(CopyRequirement(home_id, repo, RequirementKind.PROTECTED_HOME, raids or fallback))
        desired.append(CopyRequirement(
            f"protected_replica:{repo}", repo, RequirementKind.PROTECTED_REPLICA, replicas,
            independent_of=home_id))

    desired.sort(key=lambda item: item.requirement_id)
    return RequirementGraph(tuple(desired), _hash_requirements(desired))


def _hash_requirements(desired) -> str:
    payload = [
        [item.requirement_id, item.repo_id, item.kind.value,
         list(item.eligible_drives), item.independent_of]
        for item in sorted(desired, key=lambda item: item.requirement_id)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------
# Candidates
# ------------------------------------------------------------------------------------------------
def _proof(mf: archive_manifest.ManifestFile, row: ArchivedFileFact | None) -> str:
    """Reuse-proof verdict for one required file on one drive: ``missing`` (no row → safe to fetch),
    ``proven`` (provenance binds), or ``unproven`` (a row exists but its provenance does not bind)."""
    if row is None:
        return "missing"
    if row.orig_bytes != mf.size_bytes:                       # size gate — both branches
        return "unproven"
    if mf.sha256 is not None:
        return "proven" if row.orig_sha256 == mf.sha256 else "unproven"
    return "proven" if row.orig_sha256 is not None else "unproven"  # hashless residual: any non-null hash


def _reusable(mf: archive_manifest.ManifestFile, row: ArchivedFileFact) -> ReusableFile:
    if mf.sha256 is not None:
        return ReusableFile(mf.rfilename, mf.size_bytes, mf.sha256, ProofSource.MANIFEST_SHA256)
    return ReusableFile(mf.rfilename, mf.size_bytes, row.orig_sha256, ProofSource.ARCHIVED_ORIG_SHA256)


def _source_key(source) -> tuple[str, str]:
    if isinstance(source, SourceIdentity):
        return ("source", source.drive_label)
    if isinstance(source, PendingHome):
        return ("pending", source.requirement_id)
    return ("", "")


def candidates(inp: PlannerInput, graph: RequirementGraph) -> CandidateSet:
    manifests = dict(inp.manifests)
    cfg = dict(inp.compression_cfg)
    ratio = inp.float_ratio
    eligible_all = {d.drive_label for d in inp.drives}
    archived: dict[tuple[str, str], dict[str, ArchivedFileFact]] = {}
    for row in inp.archived:
        archived.setdefault((row.repo_id, row.drive_label), {})[row.rfilename] = row
    reqs_by_id = {item.requirement_id: item for item in graph.desired}

    satisfied: list[Satisfaction] = []
    by_requirement: list[tuple[str, tuple[Candidate, ...]]] = []
    drift: list[DriftRow] = []

    for req in graph.desired:
        manifest = manifests[req.repo_id]
        eligible = req.eligible_drives

        complete: list[tuple[str, tuple[ReusableFile, ...]]] = []
        valid: list[tuple[str, tuple[ReusableFile, ...], tuple[archive_manifest.ManifestFile, ...]]] = []
        for label in eligible:
            rows = archived.get((req.repo_id, label), {})
            reused: list[ReusableFile] = []
            missing: list[archive_manifest.ManifestFile] = []
            blocked = False
            for mf in manifest:
                verdict = _proof(mf, rows.get(mf.rfilename))
                if verdict == "missing":
                    missing.append(mf)
                elif verdict == "proven":
                    reused.append(_reusable(mf, rows[mf.rfilename]))
                else:
                    drift.append(DriftRow(req.requirement_id, label, mf.rfilename, "unproven_provenance"))
                    blocked = True
            if blocked:
                continue                                   # target omitted — never overwrite an unproven row
            reused_t = tuple(sorted(reused, key=lambda item: item.rfilename))
            if missing:
                valid.append((label, reused_t, tuple(sorted(missing, key=lambda item: item.rfilename))))
            else:
                complete.append((label, reused_t))

        # Wrong-tier: a proven-complete copy on a non-eligible drive is drift, not a relocation candidate.
        for label in sorted(eligible_all - set(eligible)):
            rows = archived.get((req.repo_id, label), {})
            if rows and all(_proof(mf, rows.get(mf.rfilename)) == "proven" for mf in manifest):
                for mf in manifest:
                    drift.append(DriftRow(req.requirement_id, label, mf.rfilename, "wrong_tier"))

        if complete:
            copies = tuple(
                SatisfiedCopy(label, reused, None)
                for label, reused in sorted(complete, key=lambda item: item[0])
            )
            satisfied.append(Satisfaction(req.requirement_id, copies))
            continue

        cands: list[Candidate] = []
        sources = _home_sources(req, reqs_by_id, manifest, archived) \
            if req.kind == RequirementKind.PROTECTED_REPLICA else ()
        for label, reused, missing in valid:
            if req.kind == RequirementKind.PROTECTED_REPLICA:
                if sources:
                    for source in sources:
                        cands.append(_replica_candidate(req, label, reused, missing, source, cfg, ratio, archived))
                else:
                    pending = PendingHome(req.independent_of)
                    cands.append(_replica_candidate(req, label, reused, missing, pending, cfg, ratio, archived))
            else:
                cands.append(_fetch_candidate(req, label, reused, missing, cfg, ratio))
        if cands:
            cands.sort(key=lambda c: (c.target_drive, _source_key(c.source), c.task_kind.value))
            by_requirement.append((req.requirement_id, tuple(cands)))

    satisfied.sort(key=lambda item: item.requirement_id)
    by_requirement.sort(key=lambda item: item[0])
    drift = sorted(set(drift), key=lambda item: (item.requirement_id, item.drive_label, item.rfilename, item.reason))
    return CandidateSet(tuple(satisfied), tuple(by_requirement), tuple(drift))


def _home_sources(req, reqs_by_id, manifest, archived) -> tuple[SourceIdentity, ...]:
    home = reqs_by_id.get(req.independent_of)
    if home is None:
        return ()
    sources: list[SourceIdentity] = []
    for label in home.eligible_drives:
        rows = archived.get((req.repo_id, label), {})
        if rows and all(_proof(mf, rows.get(mf.rfilename)) == "proven" for mf in manifest):
            key = orig = None
            if len(manifest) == 1:                         # single-file convenience evidence
                row = rows[manifest[0].rfilename]
                key, orig = row.annex_key, row.orig_sha256
            sources.append(SourceIdentity(label, key, orig))
    return tuple(sorted(sources, key=lambda item: item.drive_label))


def _fetch_candidate(req, target, reused, missing, cfg, ratio) -> Candidate:
    file_budgets = tuple(budgets.file_budget(mf, ratio, cfg) for mf in missing)
    budget = budgets.aggregate(file_budgets)
    return Candidate(
        requirement_id=req.requirement_id, task_kind=TaskKind.FETCH, target_drive=target,
        source=None, depends_on_requirement=None, reused_files=reused, missing_files=missing,
        budget=budget, movement_cost=MovementCost(budget.guaranteed_durable))


def _replica_candidate(req, target, reused, missing, source, cfg, ratio, archived) -> Candidate:
    if isinstance(source, SourceIdentity):
        stored = archived.get((req.repo_id, source.drive_label), {})

        def source_bytes(mf):
            row = stored.get(mf.rfilename)
            return int(row.stored_bytes) if row is not None and row.stored_bytes is not None else None

        # Exact only when every missing file has a known positive stored size on the source; an unknown
        # or zero stored size (unmeasured) falls back to the ratio/margin estimate, as the legacy path did.
        exact = all((source_bytes(mf) or 0) > 0 or mf.size_bytes == 0 for mf in missing)
        if exact:
            file_budgets = tuple(
                budgets.file_budget(mf, ratio, cfg, exact_source_bytes=source_bytes(mf) or 0)
                for mf in missing
            )
        else:
            file_budgets = tuple(budgets.file_budget(mf, ratio, cfg, replica=True) for mf in missing)
        depends_on = None
    else:                                                  # PendingHome
        file_budgets = tuple(budgets.file_budget(mf, ratio, cfg, replica=True) for mf in missing)
        depends_on = source.requirement_id
    budget = budgets.aggregate(file_budgets)
    return Candidate(
        requirement_id=req.requirement_id, task_kind=TaskKind.REPLICATE, target_drive=target,
        source=source, depends_on_requirement=depends_on, reused_files=reused, missing_files=missing,
        budget=budget, movement_cost=MovementCost(budget.guaranteed_durable))
