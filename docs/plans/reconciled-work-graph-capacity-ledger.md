# Reconciled work graph and capacity ledger

| Field | Value |
|---|---|
| Status | **Phases 1–2 merged; Phase 3 empirical gates passed; selected-artifact policy decision pending** |
| Scope | Fill planning, copy reconciliation, capacity accounting, execution, terminal failures, and operator surfaces |
| Impact | Large blast radius across every fill safety path; phased shadow rollout is mandatory |
| Trigger | A live legacy fill falsely reported a capacity stop after double-counting protected first copies already stored on the RAID home |
| Related decisions | DEC-014, DEC-017, DEC-019, DEC-020, DEC-025, DEC-029, DEC-030, DEC-031, DEC-032, DEC-034, DEC-042, DEC-045 |
| Review gates | Architecture review, external review, implementation reviews per phase, read-only legacy-catalog replay, operator-attended cutover |

This document is an implementation proposal. It does not supersede an accepted decision, authorize a
live deployment, or authorize writes to the legacy ModelDump catalog. Line references describe the
code at the time this plan was written and will drift after implementation begins.

## 1. Executive decision

Replace the current count-based placement calculation with a desired-state reconciler that produces a
small dependency graph of missing copy work. Feed that graph into a single capacity ledger and execute
only its ready tasks.

The architectural rule is:

```text
desired copy requirements
          +
observed complete/partial copies by drive
          |
          v
     reconciler
          |
          v
 missing work graph -----> capacity ledger -----> typed feasibility result
          |                         |
          +-------------------------+
                    |
                    v
           idempotent executor
                    |
                    v
        reconcile again from facts
```

The database's `files` and `archived` rows remain the durable facts. The work graph is derived, not a
second source of truth. A completed file changes the facts; the next reconciliation naturally removes
that file from outstanding work.

## 2. Why this change is necessary

### 2.1 Incident evidence, sanitized

A read-only inspection of the private legacy catalog produced this aggregate state:

| Fact | Observed value |
|---|---:|
| Finalized models | 444 |
| Protected models requiring two copies | 125 |
| Protected models with a complete copy on the RAID home | 116 |
| Protected models still lacking a complete home copy | 9 |
| Of those nine, models with partial home files | 8 |
| Raw bytes actually missing from those home copies | about 0.139 TB |
| Compression-aware estimate for those missing files | about 0.100 TB |
| RAID physical free after the unified safety floor, before new-work reservation | about 0.687 TB |
| Estimated eventual replica payload | about 0.663 TB |
| Replica usable free after reserve | about 0.933 TB |

The remaining required work fits in both capacity modes. The legacy planner nevertheless reserved a
new first copy for all 125 protected models because none had two complete copies yet. Under its
guaranteed/raw calculation it reserved about 0.978 TB and reported a 0.292 TB shortfall. About 0.825 TB
of that reservation belonged to models whose first copies were already complete.

### 2.2 Current root cause

`placed_copies()` collapses observed state to one integer per repository at
`modelark/librarian.py:197`. `plan_placements()` keeps a repository in `pool` until that integer reaches
`numcopies` at `modelark/librarian.py:237-244`. It then creates both `must_items` and `must_repl` for
every protected repository in the pool at `modelark/librarian.py:258-266`.

```text
protected repository has one complete RAID copy
                    |
                    v
placed_copies(repo) == 1, numcopies == 2
                    |
                    v
repo remains in the incomplete pool
                    |
          +---------+----------+
          |                    |
          v                    v
reserve copy #1 again    reserve copy #2
          |
          v
live free already includes the physical cost of copy #1
          +
raw/estimated copy #1 is reserved a second time
          |
          v
false capacity failure
```

Execution partially compensates: `_primary_order()` excludes repositories with any complete copy at
`modelark/fill.py:121-148`. The global capacity gate runs first at `modelark/fill.py:190-207`, so the
executor never gets the opportunity to skip the duplicate work.

### 2.3 Additional inconsistencies found during investigation

These are part of the architecture change, not unrelated cleanup:

| Inconsistency | Existing location | Consequence |
|---|---|---|
| Completeness is count-based rather than an exact manifest set comparison | `modelark/librarian.py:197-211` | Extra archived files can make a drive appear complete even when a required filename is missing |
| Fetch file-selection policy and completeness SQL can drift | `modelark/fetch.py:116`; `modelark/librarian.py:202-210`; `modelark/plan.py:154-193` | Planner, executor, totals, and verifier can disagree about what constitutes a copy |
| Plan totals are copy-aware, but per-drive placement is not stage-aware | `modelark/plan.py:195-232`; `modelark/librarian.py:230-304` | Aggregate bars can say a plan fits while placement falsely blocks it |
| Copy #2 is estimated even when exact source `stored_bytes` exist | `modelark/librarian.py:260-266` | Replica capacity is less accurate than the available evidence |
| Per-drive planning and plan totals use different RAID headroom rules | `modelark/librarian.py:84-118`; `modelark/plan.py:130-151` | UI capacity and execution feasibility can disagree |
| A model-level fit callback ignores missing-file state | `modelark/fill.py:108-118`; `modelark/fetch.py:405-418` | Partial models reserve a full model even though fetch resumes per file |
| Replica execution assumes one source for every repository | `modelark/fetch.py:690-773` | The executor cannot safely represent per-repository sources or policy drift |
| Replica DB recording is batch-success based | `modelark/fetch.py:729-771` | A partly successful annex operation can leave physical and catalog state out of sync |
| Terminal persistence stores only a message and a few fields | `modelark/web/fill_api.py:28-46` | The UI cannot render precise capacity evidence or actions |
| The persistent modal is checked only on application load | `modelark/web/static/app.js:60-85` | A live terminal transition does not open the durable modal |
| `plan-capacity-stop` is absent from the Fill page's terminal map | `modelark/web/static/fill.js:217-231`, `modelark/web/static/fill.js:421-427` | The live page does not treat this state as terminal and can fall through to “Not running. Press Start” until refresh |
| Capacity mode names sound like storage codecs | `modelark/core/schema.sql:158-164`; `modelark/plan.py:43-96` | Operators can reasonably infer that `uncompressed` changes how bytes are stored |

## 3. Goals, non-goals, and invariants

### 3.1 Goals

1. Represent desired copies separately from observed copies.
2. Compare exact required filenames with exact archived filenames per drive.
3. Generate only genuinely missing fetch or replication work.
4. Express dependencies explicitly, especially replica work that needs a complete source.
5. Account for actual stored bytes exactly once.
6. Distinguish durable capacity from transient compression workspace.
7. Use the same ledger for plan display, admission, execution preflight, and failures.
8. Preserve crash-resume by deriving work from durable facts after every restart.
9. Return typed failures containing evidence and valid operator actions.
10. Surface non-success terminal states immediately and persistently.
11. Rename capacity policy without implying a storage-format change.
12. Validate the new engine read-only against a copied legacy catalog before cutover.

### 3.2 Non-goals

1. Do not change ZipNN, StreamZNN, canary, hash, or restore formats.
2. Do not automatically move already archived bulk data between drives.
3. Do not delete extra or policy-drifted copies.
4. Do not add non-loopback portal access.
5. Do not introduce concurrent fill workers.
6. Do not generalize the schema beyond two requested copies in this change.
7. Do not silently relax the single RAID-home policy.
8. Do not perform the legacy migration or service cutover without the operator present.

### 3.3 Safety invariants

| ID | Invariant |
|---|---|
| INV-1 | `files` plus the archive policy define the canonical required manifest for a repository |
| INV-2 | A copy is complete only when every required manifest filename is recorded on the same drive |
| INV-3 | Physical/live free space and archived bytes are never both subtracted for the same mounted-drive fact |
| INV-4 | A satisfied copy requirement produces no work task and reserves no bytes |
| INV-5 | Replica work has a complete, eligible source or a dependency that will create one |
| INV-6 | Exact source `stored_bytes` size replica work whenever those bytes exist |
| INV-7 | Estimated future replica bytes are replaced by exact values after the source copy completes |
| INV-8 | Every write begins only after a fresh target and workspace preflight |
| INV-9 | Filesystem safety floor is never presented as usable durable capacity |
| INV-10 | A crash cannot make derived work disappear; reconciliation recreates every unmet task |
| INV-11 | Terminal failure payloads are structured data; human messages are renderings, not control flow |
| INV-12 | A mode migration preserves the operator's previous risk policy exactly |
| INV-13 | If every protected copy #1 is complete and copy #2 is deferred only because an otherwise valid source or target is offline, the run pauses resumably; a genuinely incomplete protected copy #1 is an error |
| INV-14 | A scheduler drains the current drive's ready batch before selecting work on another drive; global priority chooses the next drive batch, not the next cross-drive task |
| INV-15 | Replica presence is recorded only after every required annex key is proven present at the target annex UUID |
| INV-16 | The work graph is always derived from durable catalog facts and policy; it is never persisted as completion truth |

## 4. Terminology and capacity-mode rename

Storage behavior does not change with the capacity mode: eligible files are always offered to the
configured compressor, and safe raw fallback remains possible.

| Old persisted value | New persisted value | Operator label | Meaning |
|---|---|---|---|
| `uncompressed` | `guaranteed` | Guaranteed capacity | Reserve future fetch data without depending on compression savings |
| `compressed` | `compression_aware` | Compression-aware capacity | Reserve future fetch data using observed compression plus margin; runtime guards carry estimate risk |

Use underscore form in Python, JSON, CLI, and SQLite; render “Compression-aware” in prose.

Compatibility requirements:

- Existing `uncompressed` rows migrate transactionally to `guaranteed`.
- Existing `compressed` rows migrate transactionally to `compression_aware`.
- CLI old values remain temporary input aliases for one release and print a deprecation warning.
- API responses emit canonical `capacity_mode` and retain `provisioning` as a deprecated input/output
  alias for exactly one compatibility release.
- No migration may silently switch an existing plan from guaranteed to compression-aware.

The correctness engine may use `CapacityMode` internally before the rename release by translating the
old persisted values at its boundary. The schema/CLI/API rename is a separately shippable release after
the reconciler-driven executor is proven; it must not block the incident fix.

## 5. Proposed module boundaries

```text
modelark/archive_manifest.py
    canonical file-selection policy and manifest facts
                 |
                 v
modelark/reconcile.py <------ catalog rows / plan policy / drive roles
    requirements, observed copies, partials, dependency graph
                 |
                 v
modelark/capacity.py <------- live/snapshot free, ratios, headroom policy
    sizing, placement, ledger, typed feasibility failures
                 |
          +------+------+
          |             |
          v             v
modelark/librarian.py   modelark/fill.py
presentation facade     task scheduler/executor
                              |
                              v
                       modelark/fetch.py
                       low-level fetch/replicate actions
```

### 5.1 `archive_manifest.py` — one definition of a copy

Move the selection logic currently in `fetch.plan()` at `modelark/fetch.py:116-155` into a dependency-
neutral module. Fetch, reconcile, plan totals, verifier, restore, and completeness checks must call the
same function rather than reproduce SQL filters.

This includes the archive verifier in `modelark/verifier.py` and the restore workflow in
`modelark/restore.py`; it does not mean the remote-header Tier A parser in `modelark/verify.py`.
Both safety-critical consumers are in implementation scope. Their behavior must remain unchanged
except that required-file selection comes from this module, and both receive dedicated regressions in
Sections 12–14. The migration is not complete while either reconstructs its own required manifest.

Proposed interface:

```python
@dataclass(frozen=True)
class ManifestFile:
    rfilename: str
    size_bytes: int
    sha256: str | None
    format: str
    quant: str | None
    storage_action: Literal["compress", "raw"]


def manifest_for_repo(con, repo_id: str, policy: ArchivePolicy) -> tuple[ManifestFile, ...]: ...
def manifests_for_repos(con, repo_ids: Sequence[str], policy: ArchivePolicy) -> dict[str, tuple[ManifestFile, ...]]: ...
```

The bulk form prevents per-repository query amplification in plan-wide reconciliation.

### 5.2 `reconcile.py` — desired state, observed state, and work graph

Proposed core types:

```python
class RequirementKind(StrEnum):
    PRIMARY = "primary"
    PROTECTED_HOME = "protected_home"
    PROTECTED_REPLICA = "protected_replica"


class TaskKind(StrEnum):
    FETCH = "fetch"
    REPLICATE = "replicate"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"
    ERROR = "error"


class RecoveryClass(StrEnum):
    AUTOMATIC = "automatic"
    OPERATOR_ACTION = "operator_action"
    CODE_DEFECT = "code_defect"


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
    stored_bytes_by_file: Mapping[str, int]

    @property
    def complete(self) -> bool:
        return self.required_files <= self.present_files


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
class WorkTask:
    task_id: str
    kind: TaskKind
    requirement_id: str
    repo_id: str
    target_drive: str
    source_drive: str | None
    files: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class PlanDiagnostic:
    code: str
    severity: DiagnosticSeverity
    recovery: RecoveryClass
    requirement_id: str | None
    detail: Mapping[str, object]


@dataclass(frozen=True)
class WorkGraph:
    requirements: tuple[CopyRequirement, ...]
    facts: tuple[CopyFact, ...]
    tasks: tuple[WorkTask, ...]
    satisfied: frozenset[str]
    diagnostics: tuple[PlanDiagnostic, ...]
```

Reconciliation emits `WorkIntent` values: missing work and constraints, but no invented target unless
durable partial files pin it. The capacity placement policy evaluates candidate-specific missing files
and budgets, then materializes assigned `WorkTask` values. `task_id` and `requirement_id` must remain
deterministic across that boundary. They are identifiers for logs, UI, and tests; they are not initially
persisted as authoritative state.

### 5.3 `capacity.py` — one ledger and one headroom policy

Proposed types:

```python
class CapacityMode(StrEnum):
    GUARANTEED = "guaranteed"
    COMPRESSION_AWARE = "compression_aware"


class FreeEvidence(StrEnum):
    LIVE = "live"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class TaskBudget:
    task_id: str
    target_drive: str
    guaranteed_durable: int
    expected_durable: int
    workspace_peak_guaranteed: int
    workspace_peak_expected: int
    evidence: Literal["exact", "estimate"]


@dataclass(frozen=True)
class DriveLedger:
    drive_label: str
    physical_free: int
    free_evidence: FreeEvidence
    safety_floor: int
    usable_now: int
    guaranteed_durable: int
    expected_durable: int
    workspace_peak_guaranteed: int
    workspace_peak_expected: int


@dataclass(frozen=True)
class CapacityFailure:
    code: FailureCode
    severity: DiagnosticSeverity
    recovery: RecoveryClass
    capacity_mode: CapacityMode
    requirement_id: str | None
    task_ids: tuple[str, ...]
    target_tier: str | None
    eligible_drives: tuple[str, ...]
    required_bytes: int
    available_bytes: int
    safety_floor_bytes: int
    workspace_bytes: int
    shortfall_bytes: int
    evidence: FreeEvidence | None
    actions: tuple[RecoveryAction, ...]
```

`plan._headroom()` at `modelark/plan.py:130-136` becomes the single headroom function used by both
nominal totals and live drive ledgers. `librarian.headroom_bytes()` remains its tranche helper or moves
under `capacity.py`; `librarian.drives()` must not apply a different RAID rule.

`capacity.py` does not choose targets through an unspecified generic bin packer. Its placement entry
point implements the versioned `tiered_v1` objective in Section 6.4; feasibility then budgets that
fixed deterministic assignment. Any future placement objective requires its own decision, policy
version, shadow diff, and migration story.

### 5.4 `librarian.py` — facade, not state inference

Keep public CLI/UI-facing functions temporarily, but make them adapters over `WorkGraph` and
`CapacityPlan`. Remove independent copy-state and sizing logic from `plan_placements()`, `plan_view()`,
and `queue_view()` as consumers migrate.

`placed_copies()` can remain as a reporting helper, but it must be derived from exact `CopyFact.complete`
values and must not drive placement or Gate C.

## 6. Reconciliation algorithm

### 6.1 Step-by-step

1. Resolve the active plan, selected repositories, drive set, roles, and canonical capacity mode in one
   read snapshot.
2. Build canonical manifests for the selected repositories.
3. Load archived rows for those repositories and plan drives in one query.
4. Group archived rows by `(repo_id, drive_label)` and compare filename sets exactly.
5. Generate desired requirements:
   - one primary requirement for a bulk repository;
   - one protected-home requirement plus one protected-replica requirement for a protected repository.
6. Match complete facts to requirements by eligibility and independence policy.
7. Retain unmatched complete copies as extras; emit advisory facts but never delete them.
8. Select and pin partial work when matching files exist on one or more eligible drives, unless policy
   forbids it. Never combine filenames from different partial drives into a fictional complete copy.
9. Generate unassigned fetch intents for unmet home/primary requirements and replica intents for unmet
   replica requirements. A replica intent names a complete source or depends on the protected-home
   requirement that will create one.
10. Build candidate-specific missing-file sets and budgets for each eligible target; a partial pin has
    exactly one candidate.
11. Assign intents through the deterministic `tiered_v1` placement policy in Section 6.4, materializing
    concrete tasks and dependency task IDs.
12. Return either a feasible graph plus ledgers or typed failures plus the still-inspectable intents and
    candidate evidence.

### 6.2 Pseudocode

```python
def reconcile_plan(con, plan_id: str, mode: CapacityMode) -> ReconciledPlan:
    snapshot = read_plan_snapshot(con, plan_id)
    manifests = manifests_for_repos(con, snapshot.selected_repos, snapshot.policy)
    facts = observe_copies(con, manifests, snapshot.drives)
    requirements = desired_requirements(snapshot.selected_repos, snapshot.drives)

    match = match_complete_facts(requirements, facts)
    intents = []

    for requirement in requirements:
        if requirement.id in match.satisfied:
            continue

        partial = choose_eligible_partial(requirement, facts)

        if requirement.kind in {PRIMARY, PROTECTED_HOME}:
            intents.append(fetch_intent(requirement, pinned_target=drive_of(partial)))
            continue

        source = match.complete_home_source(requirement.repo_id)
        if source:
            intents.append(replica_intent(requirement, source_drive=source.drive))
        else:
            intents.append(replica_intent(
                requirement,
                depends_on_requirement=protected_home_id(requirement.repo_id),
            ))

    candidates = size_candidates(
        intents, manifests, facts, snapshot.drives, snapshot.compression_evidence
    )
    tasks, ledgers, failures = place_and_budget(
        snapshot.drives, intents, candidates, mode, placement_policy="tiered_v1"
    )
    return ReconciledPlan(snapshot, requirements, facts, WorkGraph(tasks), ledgers, failures)
```

### 6.3 Requirement matching policy

Initial behavior preserves existing placement policy:

| Requirement | Eligible fact/target |
|---|---|
| Bulk primary | Any `role=primary` drive, including RAID where DEC-020 permits bulk |
| Protected home | A RAID-backed primary; if no RAID exists, the selected fallback primary |
| Protected replica | A `role=replica` drive distinct from the matched home drive |

A complete copy on an ineligible drive remains valuable data but does not silently satisfy the wrong
requirement. The plan reports `COPY_POLICY_DRIFT` and creates the missing policy-compliant task.

When a RAID-backed home exists, a complete copy on a non-RAID primary is always policy drift, not a
satisfied protected-home requirement. Preserve it as an extra and generate the RAID-home task. For the
current fleet, different drive labels satisfy replica independence. Add a best-effort
`FAILURE_DOMAIN_SUSPECT` warning when registered/mount provenance shows that nominally independent
source and target resolve to the same physical block device or network storage host; failure-domain
modeling beyond that warning is deferred.

If more than one eligible RAID home exists, target selection must be deterministic. The first release
does not silently span one protected-home set across multiple homes; changing that policy requires a
separate reviewed decision.

### 6.4 Placement objective and deterministic packing

Placement is behavior-preserving with DEC-014, DEC-017, DEC-020, and DEC-034 except for the deliberate
correction that satisfied requirements and already-present files create no new reservation. The new
ledger does not grant permission to invent a different bin-packing policy when retiring
`librarian._consolidate()` and `_group()`.

Apply these rules in order:

1. Match satisfied requirements first. For an unmet requirement with partials on multiple eligible
   drives, choose one pin deterministically: least guaranteed missing bytes, then most required
   filenames already present, then the applicable drive policy rank, then `drive_label`. Other partials
   remain valuable extra facts but do not contribute files to the chosen copy. Satisfied requirements
   and the selected partial are not repacked.
2. Reserve every missing protected-home task before bulk. Use the distinguished RAID-backed primary;
   with no RAID, use the largest eligible fallback primary. Keep all files of one requirement on one
   target.
3. Pack bulk with deterministic first-fit decreasing in the selected capacity currency. Try the RAID's
   remaining usable space first, then non-RAID primaries by descending capacity. This preserves
   DEC-020's RAID-first use and DEC-014's practical objective of opening the fewest primary drives.
4. Pack protected replicas independently of their home. If the entire replica set fits on one target,
   choose the smallest sufficient replica. Otherwise first-fit-decreasing spans replica drives from
   smallest upward, while keeping each repository requirement whole on one replica target.
5. Use stable tie-breakers everywhere, including the partial-pin rule above: task budget descending
   then `requirement_id`; drive policy rank, usable/capacity in the direction above, then `drive_label`.
   Identical facts and policy must produce byte-for-byte identical assignment output.
6. After assignment, DEC-034 chooses the next drive batch from the globally highest-ranked pending
   task (giant, protected home, then bulk; largest first), then drains that drive as specified in
   Section 8.1. Scheduling never changes the assigned target.

This is a deterministic preservation of the existing heuristics, not a claim of mathematically optimal
bin packing. Shadow comparison normalizes away only intentional differences: satisfied copy #1 work is
absent, partial work contains only missing files, and exact replica bytes replace estimates. For all
remaining tasks, target labels and batch ordering must agree with the legacy planner on synthetic
fixtures; on the incident-shaped fixture, the normalized remaining assignments must agree while the
116 duplicate home reservations disappear.

The comparison normalizer is safety-critical test code and receives direct review/tests. It may only:

- remove legacy work whose exact requirement is independently proven satisfied by fixture facts;
- replace a legacy full-repository file set with the independently computed canonical missing set; and
- compare size evidence in a common declared currency.

It may not relabel targets, reorder tasks/batches, suppress unmatched work, or rewrite a divergence into
equivalence. Unit tests feed it deliberately changed target labels, ordering, eligibility, and extra
tasks and require those mutations to survive normalization as failures.

## 7. Capacity ledger

### 7.1 Separate byte categories

| Quantity | Source | Use |
|---|---|---|
| Physical free | `shutil.disk_usage()` for a mounted drive | Current fact, already includes archived data, partials, cruft, and compression outcomes |
| Snapshot free | Registered `drives.free_bytes` adjusted by catalog evidence | Offline preview only; explicitly lower-confidence |
| Safety floor | Unified tranche rule plus RAID minimum | Space never promised as durable plan capacity |
| Guaranteed durable | Raw bytes of genuinely missing fetch files; exact bytes for replica tasks | Completion without depending on future compression savings |
| Expected durable | Compression estimate for missing fetch files; exact bytes for replica tasks | Compression-aware completion forecast |
| Workspace peak | Maximum additional temporary bytes for one active task/file operation | Prevent compression or replica-staging transient ENOSPC |

For a mounted drive:

```text
usable_now = max(0, physical_free - safety_floor)
```

Do not subtract catalog `archived.stored_bytes` again: the filesystem already did.

For an unmounted drive, derive an explicitly approximate value from the registration snapshot and
catalog records. Execution still requires a fresh live mount and write probe before writing.

### 7.2 Fetch task sizing

For each missing file:

```text
raw_size       = canonical manifest size
expected_store = (compressible ? observed_ratio * raw_size : raw_size) * margin
```

For a task:

```text
guaranteed_durable = sum(raw_size of missing files)
expected_durable   = sum(expected_store of missing files)
```

Existing `.incomplete` download artifacts already reduce physical free. If their attributable byte
count is trustworthy, subtract it from remaining transfer bytes; otherwise reserve the full missing
file and mark the estimate conservative. The first implementation must never infer completion from an
`.incomplete` artifact.

### 7.3 Compression workspace

Current fetch behavior downloads a raw file, writes a compressed output beside it, verifies the
compressed output by streaming decompression into a hash, and only then removes the raw source at
`modelark/fetch.py:405-505`. The canary does not materialize another restored file on disk.

Files whose archive policy selects raw storage create no compressed temporary. The existing policy
routes non-float/incompressible formats such as FP8, GPTQ, and AWQ to that path before codec selection.

For a float file that does enter compression, define and enforce an on-disk output cap. For
StreamZNN:

```text
n_chunks             = ceil(raw_size / stream_chunk_bytes)
stream_framing        = len(SZNN_MAGIC) + 4 * n_chunks
compressed_temp_cap  = raw_size + stream_framing

guaranteed workspace extra       = compressed_temp_cap
compression-aware workspace extra
    = raw_size + compressed_temp_cap - expected_durable_already_reserved
```

This is a guarantee only if the codec adapter enforces it before bytes can cross the cap. StreamZNN
checks `len(compressed_chunk) <= len(raw_chunk)` while the blob is still in memory and before writing
that chunk's length frame or blob; on failure it abandons/unlinks the temporary and raw-fallbacks.
Whole-file ZipNN checks the in-memory result before its first output write. A streaming zstd adapter
checks `bytes_written + len(next_output_chunk)` before each write and aborts before crossing its
declared raw-plus-framing cap. The raw source remains intact in every case. A post-write size check is
not sufficient because one expanded chunk could transiently violate the promised workspace ceiling.
Thus a poorly compressing float shard cannot grow without bound merely because historical bf16 ratios
were favorable. Each enabled codec needs a closed-form cap and a pre-write over-cap raw-fallback test;
an adapter without one is not eligible for the guaranteed ledger.

Before Phase 3, validate the StreamZNN formula on a real representative bf16 shard and retain the
measured output/temp high-water evidence in the review artifacts. The empirical run validates the
implementation and normal margin; the enforced cap, not the observed ratio, is the safety proof.
Because the fill worker is single-threaded, workspace is a maximum over scheduled files on a drive,
not a sum over every file.

Before each file starts, repeat the live check with that file's exact operation budget. The global
ledger proves plan feasibility; the file guard prevents an estimate or external disk change from
becoming ENOSPC.

### 7.4 Replica task sizing

When a complete source exists:

```text
replica_durable = sum(source archived.stored_bytes for files absent on target)
evidence        = exact
```

When the source is a future dependency:

```text
replica_durable = expected stored size of the source task
evidence        = estimate
```

After the source task completes, reconciliation replaces the estimate with exact stored bytes before
replica execution.

Replica work also receives a transient target-workspace term. Until a filesystem trace proves that
the supported `git annex copy --to` path publishes directly without coexisting target-side staging,
reserve conservatively:

```text
per-task replica_durable   = exact bytes absent on target for that repository
per-task replica_workspace = per-task replica_durable

drive durable total        = sum(per-task replica_durable)
drive workspace peak       = max(per-task replica_workspace)
drive required peak        = drive durable total + drive workspace peak
```

The workspace term is per task and aggregates by maximum, never by sum: replica execution is
single-threaded and only one task can hold target-side staging at a time. For the incident fixture this
means roughly the 0.663 TB total replica payload plus the largest single repository's conservative
workspace—not 1.326 TB. The same max-not-sum rule used for fetch workspace therefore prevents the
conservative staging assumption from becoming another false whole-tier capacity stop.

Phase 2 must trace a representative per-key copy on the supported git-annex/filesystem combination,
including `.git/annex/tmp` and the final object directory. A later reviewed reduction may use the
measured upper bound; executor conversion does not proceed with an unmodeled zero workspace term.

### 7.5 Feasibility semantics

**Accepted for this release (DEC-045): Gate B admission is all-or-nothing.** Before a fill starts, a
structurally infeasible committed set blocks the entire run. Concretely, a replica tier that is too
small for protected copy #2 blocks otherwise-feasible bulk copy #1 even when the primary tier has ample
space. The operator must add eligible replica capacity, reduce protection requirements and re-plan, or
change the committed set before starting.

This is an admission rule only, not an all-or-nothing runtime model. Once an admitted fill is running:

- an otherwise valid source or target going offline follows DEF-022 and pauses/defer replicas
  resumably; completed/safe copy #1 work is not reclassified as failure; and
- a drive filling unexpectedly mid-run produces the resumable typed successor to
  `plan-capacity-stop`, preserving completed bytes and permitting re-plan/resume.

The choice is conservative and scope-limiting for this incident fix, not a claim that blocking all
feasible progress is the uniquely safest product design. A future explicit partial-continuation mode
could make feasible progress while presenting typed unmet requirements, but it needs separate choices
about subset policy, resume semantics, operator consent, and UI.

The result must distinguish:

- the complete graph is feasible;
- the graph is feasible only under compression-aware capacity;
- durable capacity is short;
- transient workspace is short;
- an eligible target tier is missing;
- a replica has no possible source;
- only offline/snapshot evidence is available;
- work is feasible but an operational drive is currently absent.

A later, separately reviewed feature may allow an operator to continue a prioritized subset. It is not
part of this change.

## 8. Work scheduling and execution

### 8.1 Scheduler separation

The graph expresses prerequisites; a scheduler chooses among ready tasks. Priority is not encoded by
pretending that unready or completed work is absent.

Initial scheduler policy preserves the established operator order:

1. Resume a task with durable partial files on its assigned target.
2. Fetch giant first copies over 250 GB raw.
3. Fetch protected home copies.
4. Fetch remaining bulk first copies.
5. Replicate protected copies.

Bulk-before-replica remains the default: protected copy #1 is already on RAID, and an early replica
pass would add hot swaps for little risk reduction. Expose a scheduler setting with
`replica_order = after_bulk | before_bulk`, defaulting to `after_bulk`.

Drive affinity is mandatory, not a tie-breaker. Global priority selects the next drive batch by the
highest-priority ready task it contains. Once selected, the scheduler pins the current target drive and
drains all of that drive's ready work in priority order before considering an unmounted drive. A task
on another drive cannot preempt the batch merely because its model is globally larger. Replica batches
pin the target and require their selected source; guided await/probe behavior controls any necessary
swap. This preserves DEC-034's one-drive-at-a-time hot-swap contract.

### 8.2 Proposed control flow

```text
START
  |
  v
reconcile facts -> graph -> ledgers
  |
  +-- structural/capacity failure --> persist typed terminal --> STOP
  |
  v
choose next drive batch from highest-priority READY task
  |
  +-- none and all requirements satisfied --> Gate C --> DONE
  |
  +-- none but unmet requirements ----------> graph invariant ERROR
  |
  v
await/probe target (and source for replica)
  |
  +-- absent/unwritable --> operational PAUSE/AWAIT, graph remains valid
  |
  v
pin drive; drain its idempotent ready tasks
  |
  +-- fetch: preflight each missing file, then download/verify/compress/canary/record
  |
  +-- replica: copy missing annex keys, verify result per repo/file, then record
  |
  v
reconcile from durable facts at batch boundary
  |
  +-------------------------------> loop
```

#### Reconciliation cadence and locking

A full graph reconciliation occurs:

1. on fill start or process restart;
2. before selecting each new drive batch;
3. after the pinned batch completes, is deferred, or changes topology/evidence materially;
4. before changing from fetch work to replica work; and
5. before any terminal Gate B/Gate C decision.

It does not rescan the full `archived` table after every file or repository. Within a pinned batch, the
executor rechecks the concrete file row and live capacity before each operation, and tracks completed
tasks only as ephemeral loop state; a crash discards that state and the next full reconciliation
rebuilds it from durable facts.

The reconciler uses a dedicated read connection and one short WAL read transaction to bulk-load the
selected plan, canonical manifests, and relevant archived rows. Graph construction and capacity math
run after the snapshot rows are materialized and do not hold the portal's shared `data._lock`; only
normal brief catalog writes/status publication use that lock. The bulk manifest API is the only
per-repository amplification path permitted.

Phase 1 adds an instrumented 100,000-row fixture with multiple eligible drives per intent: full
reconciliation must complete within 2 seconds in CI, within 500 ms p95 on the release host/copy of the
legacy catalog, and must not hold `data._lock` while constructing the graph. Phase 2 repeats the same
benchmark through candidate-specific sizing and placement so the full `intents × eligible drives`
cross-product is exercised rather than benchmarked away. These are review gates; a measured regression
changes the cadence, query shape, or bounded candidate representation before executor adoption.

### 8.3 Fetch changes

Replace the repository-level `fits(repo)` callback at `modelark/fetch.py:539-640` with an operation
guard that receives the target, manifest file, current partial state, and codec plan.

```python
guard.before_file(
    task_id=task.task_id,
    target_drive=task.target_drive,
    file=manifest_file,
    current_artifacts=inspect_artifacts(...),
)
```

`fetch_model()` remains file-idempotent but accepts an explicit allowed/missing manifest from the task.
It rechecks the database before each operation so a stale graph cannot duplicate a completed file.

### 8.4 Replica changes

Replace `run_replica(replica_assign, source)` with task-oriented execution:

```python
def run_replica_tasks(tasks: Sequence[WorkTask], ctx: RunCtx) -> ReplicaResult:
    for source, target, grouped_tasks in group_ready_tasks(tasks):
        probe(source)
        probe(target)
        annex_copy(grouped_tasks)
        verify_each_required_key_on_target_uuid(grouped_tasks)
        record_only_verified_files(grouped_tasks)
```

Batching is an optimization, not the success unit. A nonzero batch exit must not prevent recording
individual repositories proven present, and a zero exit must not substitute for per-repository target
evidence. Proof is per annex key via `git annex whereis` (or equivalent plumbing) and must match the
target annex UUID before that file's target `archived` row is inserted.

### 8.5 Gate reinterpretation

| Current gate | Proposed meaning |
|---|---|
| Gate A | Every immediately executable task has resolvable required drives for CLI mode; guided mode may await |
| Gate B | Reconciled graph is structurally valid and feasible under the selected capacity mode |
| File guard | The next concrete write fits live durable plus workspace constraints |
| Gate C | Every desired requirement is matched by an exact complete fact on an eligible drive |

Gate C must no longer use only `placed_copies() >= numcopies` at `modelark/fill.py:278-307`.
It must preserve DEF-022 explicitly: when every protected-home requirement is satisfied and the only
unmet work is replica work deferred by an offline/unwritable source or target, return resumable
`paused`. If any protected-home requirement is genuinely incomplete and no valid ready/dependent task
can complete it, return `error`; never downgrade missing irreplaceable copy #1 data to an offline-copy
pause.

## 9. Typed failures and terminal state

### 9.1 Diagnostic and failure codes

Every diagnostic has a severity (`info`, `warning`, `blocking`, or `error`) and a recovery class
(`automatic`, `operator_action`, or `code_defect`). Not every diagnostic stops the fill; for example,
an existing wrong-tier copy is an advisory when the graph can create the required eligible copy.

Initial codes:

| Code | Default severity | Meaning |
|---|---|---|
| `CAPACITY_DURABLE_SHORT` | Blocking | Required durable bytes exceed eligible usable capacity |
| `CAPACITY_WORKSPACE_SHORT` | Blocking | Durable result fits, but the next codec operation cannot fit transiently |
| `TARGET_TIER_MISSING` | Blocking | A requirement has no eligible drive class |
| `TARGET_UNAVAILABLE` | Warning/pause | Planned target exists but is currently absent or unwritable |
| `SOURCE_INCOMPLETE` | Error | Replica task has no complete source and no valid dependency |
| `SOURCE_UNAVAILABLE` | Warning/pause | Complete source exists but is offline or unreadable |
| `COPY_POLICY_DRIFT` | Warning when correctable | Existing complete copy is on a drive that does not satisfy its requirement |
| `FAILURE_DOMAIN_SUSPECT` | Warning | Nominally independent drives appear to share a physical device or storage host |
| `GRAPH_INVARIANT` | Error | Unmet requirements exist but the graph contains no task capable of satisfying them |
| `TASK_FAILED` | Error | A concrete fetch/replica task failed for a non-capacity reason |
| `THROTTLED` | Warning/pause | Download budget paused otherwise feasible work |

Diagnostics follow the dependency graph and deduplicate to root causes. If a protected-home intent is
infeasible, its replica intent is reported as `blocked_by_requirement=<home requirement>` evidence on
the home capacity/tier failure; it does not emit a second `SOURCE_INCOMPLETE`. Emit
`SOURCE_INCOMPLETE` only when no complete source and no valid home dependency can exist despite the
absence of an upstream blocking diagnostic. Terminal actions therefore address the real first failure
instead of spamming derived consequences.

The new engine should not classify the same capacity condition as `blocked` before progress and
`plan-capacity-stop` after progress. That distinction currently depends on the process-local
`fetched_any` flag at `modelark/fill.py:188-207` and can change merely by restarting. Emit a stable
terminal status such as `blocked` plus the typed capacity code; retain `occurred_after_progress` and
the last completed task as evidence. Continue reading/rendering legacy `plan-capacity-stop` payloads.

### 9.2 Terminal payload

Persist a versioned document rather than a lossy message:

```json
{
  "version": 2,
  "status": "blocked",
  "when": "ISO-8601 timestamp",
  "plan_id": "ark",
  "capacity_mode": "guaranteed",
  "failure": {
    "code": "CAPACITY_DURABLE_SHORT",
    "requirement_id": "protected_home:org/model",
    "target_tier": "raid",
    "eligible_drives": ["drive-00"],
    "required_bytes": 1000,
    "available_bytes": 750,
    "safety_floor_bytes": 100,
    "workspace_bytes": 25,
    "shortfall_bytes": 275,
    "actions": ["expand_eligible_tier", "trim_selection", "change_capacity_mode"]
  },
  "message": "Human-readable fallback"
}
```

The actual payload may include multiple failures, but must bound persisted/UI detail. The API must not
expose local paths, hardware identifiers, or unrelated catalog data.

`last_terminal()` reads legacy unversioned files and normalizes them to version 2 in memory. It does not
rewrite merely because the portal read it.

### 9.3 Immediate modal and durable page state

- Move modal rendering behind a shared `MA.terminal.show(payload)` or a `modelark:terminal` event.
- `app.js` still loads the persisted terminal on application start.
- `fill.js:421-427` invokes the same renderer immediately when polling observes a terminal transition.
- Replace separate hard-coded terminal maps with one shared terminal classifier that includes every
  backend terminal state, including `plan-capacity-stop` and future typed capacity failures.
- The Fill page retains a terminal card with evidence and actions after the modal is dismissed.
- Rendering continues to use `textContent` or centralized escaping; no typed field becomes raw HTML.
- Acknowledgement hides the modal but does not falsify the worker's current terminal status.

## 10. Plan totals and operator surfaces

The existing bars compare a static full raw footprint and a mixed actual/estimated compressed
footprint with aggregate fleet capacity at `modelark/plan.py:195-260`. Retain high-level forecasting,
but separate it from constrained remaining-work feasibility.

Proposed display:

```text
Plan policy footprint
  Guaranteed scenario:          [====================      ]
  Compression-aware scenario:   [==============            ]

Remaining reconciled work
  Home/primary durable:          139 GB guaranteed / 100 GB expected
  Replica durable:               663 GB exact/estimated
  Maximum workspace:               5 GB raw + codec overhead

Constraint status
  RAID home:       FITS, 548 GB guaranteed margin
  Replica tier:    FITS, 270 GB margin
  Bulk primaries:  FITS
```

Aggregate fleet capacity must not display green while a constrained tier is infeasible without an
adjacent constraint warning. Conversely, a tier-specific shortfall must not say “Library is full.”

## 11. Persistence and migrations

### 11.1 Capacity-mode schema migration

`plans.provisioning` has a SQLite `CHECK` constraint limited to old values at
`modelark/core/schema.sql:158-164`. Updating rows alone is insufficient. Add a versioned, transactional
table rebuild through `modelark/core/db.py:126-255`:

1. Replace the current unconditional `_SCHEMA_VERSION = 1` stamping with a monotonic dispatcher that
   reads `PRAGMA user_version` once, rejects versions newer than the program, and runs only migrations
   whose target is greater than the current version.
2. Treat the existing integrity rebuild as the v0→v1 migration. Its foreign-key presence check may be
   an idempotence/repair assertion, but it is not the version dispatcher and must never stamp a v2
   database back down to v1.
3. For `user_version < 2`, create the existing non-overwriting database backup.
4. Build canonical v2 tables with a renamed `capacity_mode` column accepting only `guaranteed` and
   `compression_aware`. The column rename is decided; retaining `provisioning` avoids no rebuild
   because changing its SQLite `CHECK` requires the same table swap.
5. Copy rows with explicit value mapping, then rebuild indexes and views.
6. Run `foreign_key_check`, value assertions, and schema/version assertions.
7. Commit and set `PRAGMA user_version=2` only after validation; roll back completely on any invalid
   row.

The API/CLI `provisioning` alias does not require a database alias column. The compatibility adapter
maps the legacy field at the boundary for one release.

### 11.2 Work-graph persistence decision

Do not persist the derived graph initially. Persisted graphs become stale and compete with `archived`
as a source of truth.

Target stickiness after a crash must still be addressed:

- completed archived files pin a partial copy to their drive;
- a worker does not reassign during an active file;
- a crash before the first file record may leave only an `.incomplete` artifact, which is not durable
  task or completion evidence.

Ship without `task_claims`. Deterministic placement recreates the target after a pre-first-record
crash; a safely attributable transport partial may be reused by the downloader, but feasibility still
reserves the full missing file and reconciliation never infers completion from `.incomplete`. Add an
assignment-claim table only through a later reviewed schema decision if Phase 3 crash tests prove that
deterministic target stickiness is insufficient.

## 12. File-by-file implementation map

| File | Existing responsibility/reference | Planned change |
|---|---|---|
| `modelark/archive_manifest.py` | New; logic originates in `fetch.py:116-155` | Canonical single/bulk manifest APIs and archive policy types |
| `modelark/reconcile.py` | New | Desired requirements, exact copy facts, matching, task graph, deterministic IDs |
| `modelark/capacity.py` | New | Capacity modes, ratio sizing, unified headroom, live/snapshot evidence, ledgers, typed failures |
| `modelark/librarian.py` | Copy counts and placement at lines 84-304 | Become facade over reconciliation/capacity; retire independent inference and sizing |
| `modelark/plan.py` | Plan modes and totals at lines 43-96 and 128-260 | Canonical mode names; forecast plus reconciled remaining/constraint summaries |
| `modelark/fill.py` | Phase loops and gates at lines 100-311 | Task scheduler loop, graph-based Gate B/C, typed results, file-operation guard |
| `modelark/fetch.py` | Manifest, fetch, and replica actions at lines 116-155, 405-516, 539-640, 690-773 | Consume explicit tasks/manifests; per-file guard; per-source replica groups; verified result recording |
| `modelark/verifier.py` | Archive completeness and physical-copy verification | Consume the canonical manifest; preserve tri-state physical verification and exact required-file checks |
| `modelark/restore.py` | Verified archive restore and required-file selection | Consume the canonical manifest; preserve atomic publish, replica fallback, and final hash checks |
| `modelark/streamznn.py` | Stream framing and temporary output | Enforce the raw-plus-framing output cap and abort safely to caller-controlled raw fallback |
| `modelark/compress.py` | Codec selection/adapters | Declare a closed-form filesystem output cap for every enabled codec and reject over-cap output |
| `modelark/core/schema.sql` | `plans.provisioning` at lines 158-164 | Canonical capacity-mode constraint/column |
| `modelark/core/db.py` | Integrity migration and unconditional v1 version writes at lines 126-255 | Monotonic version dispatcher plus idempotent schema-v2 mode migration with backup and assertions |
| `modelark/web/plan_api.py` | Mode fields/mutation at lines 14-74 | Canonical `capacity_mode`, richer ledger/constraint summaries, legacy input alias window |
| `modelark/web/fill_api.py` | Terminal persistence/status at lines 28-168 | Versioned typed terminal document and lossless propagation |
| `modelark/web/static/app.js` | Load-only modal at lines 60-85 | Shared terminal renderer callable on load and live transition |
| `modelark/web/static/fill.js` | Count-based queue and bars at lines 245-270 and 421-475 | Work-graph queue states, immediate terminal, renamed modes, constrained ledgers |
| `modelark/cli.py` | Planner and mode commands around lines 200-301 and 414-427 | Canonical mode CLI, aliases, typed feasibility output, optional JSON graph diagnostics |
| `docs/fill_pipeline.md` | Current phase architecture | Replace with reconciler/task execution diagram after implementation |
| `wishlist.yaml`, `modelark/default_wishlist.yaml` | Compression/operator defaults | Add validated `scheduler.replica_order`, default `after_bulk` |
| `docs/decision_log.md` | Accepted historical decisions through DEC-045 plus INC-014 | Keep DEC-045 implementation status/evidence current as phases land |
| `docs/legacy-cutover.md` | Operator-attended migration | Add schema-v2 preflight, read-only plan diff, and explicit capacity-mode mapping |

## 13. Implementation phases and review boundaries

### Phase 0 — approve this architecture (complete)

- Internal review complete.
- Three external architecture passes complete; pass 3 approved phased implementation.
- Every blocking finding is resolved and every empirical gate is assigned in Section 18.
- Accepted amendments are recorded in this document.
- DEC-045 records the architecture and Gate B admission decision.
- No product code changed during Phase 0.

### Phase 1 — canonical manifest and reconciler, shadow-only

Implementation status (2026-07-14): merged as PR #10. The legacy fill executor still owns every
placement and write decision. The new
graph is reachable only through the read-only `modelark library plan --explain` diagnostic and tests;
`--explain --apply` is rejected before a catalog connection is opened.

Coordination prerequisite: begin from canonical history containing the merged restore/security PR #3
(`55f1515`) and the current verifier implementation. That prerequisite is present in this working
history, but the implementation branch must still be based on the corresponding current `main`; do
not develop competing manifest edits against older restore/verifier branches.

1. Extract canonical manifest logic without changing archive policy.
2. Convert fetch, plan totals, archive verifier, and restore to that manifest API with behavior-preserving
   regression tests.
3. Add exact copy inventory and requirements.
4. Add deterministic work-graph construction and drive-policy diagnostics.
5. Add sanitized live-shaped and 100,000-row performance fixtures.
6. Add a read-only diagnostic path that computes the new graph beside the legacy planner.
7. Do not make the fill executor consume the graph yet.

Review evidence:

- unit tests;
- graph JSON snapshots;
- verifier tri-state and restore atomicity/hash regressions against the canonical manifest;
- old/new comparison against synthetic cases;
- read-only comparison against a copied legacy catalog;
- reconciliation latency/lock evidence meeting Section 8.2;
- proof that 116 satisfied home requirements create zero fetch tasks in the incident-shaped fixture.

Local evidence recorded before implementation review:

- 160 tests pass, including existing verifier tri-state, restore atomicity/hash, fetch-resume, fill,
  portal-security, and migration regressions;
- the sanitized incident fixture derives nine protected-home fetch intents, 125 replica intents, and
  removes exactly 116 independently proven phantom legacy home reservations;
- graph serialization and hashes are deterministic and include exact stored-byte facts;
- the 100,000-row / 10,000-copy-fact / 10,000-candidate-cross-product fixture passes the two-second CI
  bound (0.96 seconds for the complete pytest call on this workstation); and
- Ruff, Python compilation, and CLI help checks pass.

Still required before Phase 1 review closes: run `--explain` against an operator-approved copied or
sanitized legacy catalog, record the graph/legacy diff, and measure the release-host p95 and lock
behavior. None of those gates authorize opening the live legacy catalog for write or changing the
executor.

### Phase 2 — capacity ledger and typed feasibility

Implementation status (2026-07-14): merged as PR #11 after CI and Greptile implementation review.
`tiered_v1`, candidate-specific budgets, typed failures, file preflight, and codec caps are visible
through read-only CLI/API shadow diagnostics only. No fill gate or executor consumes them.

1. Implement the deterministic behavior-preserving placement policy from Section 6.4.
2. Implement internal canonical capacity modes behind an old-value adapter; do not migrate schema yet.
3. Unify headroom.
4. Size fetch, partial, dependent replica, and exact replica tasks.
5. Enforce pre-write codec output caps, add transient workspace calculation, and add the per-file preflight API.
6. Produce typed failures and constraint summaries, including DEC-045 Gate B admission semantics.
7. Run the new ledger in shadow mode through CLI/API diagnostics.
8. Trace target-side git-annex copy behavior and keep the conservative replica workspace budget unless
   a smaller bound is proven and reviewed.

Review evidence:

- arithmetic/property tests;
- normalized legacy/new target and batch-order equivalence on synthetic and incident-shaped fixtures;
- mounted versus snapshot evidence tests;
- codec workspace boundary/pre-write over-cap raw-fallback tests plus real-bf16 StreamZNN high-water evidence;
- annex target workspace trace and conservative-bound test;
- replica drive total equals durable sum plus maximum task workspace, never the workspace sum;
- incident-shaped graph feasible in guaranteed mode;
- deliberate shortfall fixtures produce exact, typed numbers.

Local evidence recorded before implementation review:

- the incident fixture creates nine missing home tasks, removes 116 phantom reservations, places all
  125 replica requirements, and is feasible in guaranteed mode;
- deterministic synthetic placement agrees with normalized legacy targets while deliberate target
  mutation remains a visible comparison failure;
- the 100,000 archived-row fixture exercises 10,000 actual intent/drive candidates and completes the
  full pytest call in 1.31 seconds on this workstation, with an internal two-second placement bound;
- replica durable bytes aggregate by sum while conservative target workspace aggregates by maximum;
- guaranteed/compression-aware boundaries, snapshot/live evidence, root-cause diagnostic deduplication,
  and exact file-preflight boundaries have direct tests;
- StreamZNN, whole ZipNN, and zstd reject output expansion before the write that would cross their
  declared caps, and incompressible output returns to the existing raw-fallback path; and
- `docs/capacity-evidence.md` records a disposable git-annex directory-remote trace showing one
  target temporary object atomically renamed into place. The conservative workspace term remains
  active pending review.

Both empirical gates now pass. The operator-approved real-bf16 StreamZNN run used a SHA-verified
29.36 GB restored shard and measured 19.47 GB filesystem high-water against a 29.36 GB enforced
ceiling. The copied-catalog release-host replay measured the production graph-plus-ledger path at
271.724 ms p95 and 329.878 ms maximum, below the 500 ms budget, while a concurrent writer held the
disposable clone. Sanitized evidence is in `docs/capacity-evidence.md`.

Executor adoption remains paused on a policy finding from that replay: the committed selection has
50 pickle-only repositories refused by the safe default and four repositories whose intended
artifacts are outside the current safetensors/GGUF/opted-in-pickle manifest. Under DEC-045 Gate B,
those root diagnostics block the whole fill. The operator must explicitly choose acquisition policy
or artifact support scope; implementation must not silently drop, weaken, or auto-deselect them.

### Phase 3 — executor conversion

1. Replace primary/replica inference with the ready-task scheduler.
2. Change fetch to explicit missing manifests and per-file preflight.
3. Change replica execution to per-source tasks and per-key target-UUID verification before recording.
4. Reconcile at the Section 8.2 drive-batch/restart boundaries; retain per-file stale-work guards.
5. Replace Gate B and Gate C with graph/requirement checks.
6. Preserve DEF-022 pause-versus-error and DEC-034 drive affinity explicitly.
7. Ship typed terminal persistence, the immediate live modal/card, and the shared terminal classifier.
8. Retain the old planner behind a test-only comparison seam until equivalence cases pass, then remove
   it only after the phase review approves the new engine.

Review evidence:

- crash/stop/resume tests at every file/task boundary;
- no duplicate fetch for a satisfied first copy;
- partial fetch resumes only missing files;
- replica source/target failure tests;
- raw-fallback and compression-estimate miss tests;
- drive-affinity/no-swap-thrash and DEF-022 pause/error tests;
- Playwright live-terminal transition and CSP/XSS regression tests;
- end-to-end restore after graph-driven fill.

Phase 3 is the correctness release that resolves INC-014. It must be independently releasable and must
not depend on either the naming migration or the legacy working-copy cutover.

### Phase 4 — separately shippable capacity-mode rename

1. Add the monotonic version dispatcher and schema-v2 `capacity_mode` migration.
2. Update CLI and API compatibility aliases.
3. Rename operator labels and update plan forecast/constraint surfaces without changing the Phase 3
   reconciliation or admission semantics.
4. Update public documentation and DEC-045 implementation status.

Review evidence:

- migration upgrade/rollback/idempotence tests;
- packaged wheel migration test;
- HTTP schema tests;
- one-release alias/deprecation tests;
- Playwright label/forecast/constraint tests.

Phase 4 is its own release. A migration defect can delay the terminology change without withholding
the reconciler correctness fix.

### Phase 5 — canonical release acceptance per release (no legacy cutover)

1. Build and install the canonical wheel in a clean environment.
2. Run the full unit, HTTP security, packaging, migration, and Playwright suites.
3. Replay a copied/sanitized legacy-shaped catalog read-only and review the graph/ledger diff.
4. Run a bounded synthetic fill, verification, and restore on disposable storage.
5. Tag the Phase 3 correctness release after its own acceptance. If Phase 4 follows, repeat this gate
   and publish the terminology/migration release independently.

This phase does not stop, mutate, re-origin, migrate, or deploy the legacy ModelDump working copy.

### Phase 6 — later operator-attended ModelDump cutover

This is the independent DEC-042 operation. It begins only after the fixed canonical engine has landed
and been released, and requires the operator present:

1. Stop all legacy ModelDump processes.
2. Disable the legacy service and prove no old process or open file descriptor can access the catalog.
3. Back up the database, state files, annex map, and service configuration.
4. Run migration against a copy and compare manifests, requirements, tasks, and ledgers.
5. Run verification and a bounded restore test.
6. Only after approval, migrate the production catalog and deploy the new service.
7. Never allow the legacy binary to open the v2 catalog: its pre-dispatch code can stamp the version
   back to v1. Rollback restores the v1 backup and legacy binary/service together; it never points the
   old binary at the migrated database.
8. Observe a no-write plan calculation.
9. Resume with an explicitly bounded task set first.
10. Confirm immediate terminal behavior and replica completion.
11. Preserve rollback artifacts until the operator accepts the run.

RC-0 history sanitation and repository visibility remain separately gated.

## 14. Test plan

### 14.1 Manifest and inventory

- Safetensors plus aux exact manifest.
- GGUF fallback when no safetensors exists.
- Policy-allowed and policy-rejected pickle-only repositories.
- Nested filenames.
- Exact set completeness; extra archived rows do not mask a missing required file.
- Complete, partial, empty, and extra copies on each drive role.
- Archived rows outside the plan and selection do not influence the graph.
- Archive verifier consumes the same manifest and preserves `verified`/`failed`/`unknown` semantics.
- Restore consumes the same manifest and preserves nested paths, replica fallback, hash verification,
  failure atomicity, and final atomic publish.
- A pickle-only repository cannot be treated as aux-only complete by any consumer.

### 14.2 Requirement reconciliation

- Bulk: zero copies -> one fetch task.
- Bulk: partial primary -> fetch only missing files on the pinned target.
- Bulk: complete primary -> no task.
- Protected: zero copies -> home fetch plus dependent replica task.
- Protected: complete home only -> replica task only.
- Protected: complete home and replica -> no tasks.
- Protected: complete copy only on replica -> home task plus policy-drift advisory.
- Protected: complete copy only on non-RAID primary while RAID exists -> policy-drift warning plus RAID-home task.
- No RAID -> deterministic fallback primary.
- Multiple eligible replicas -> deterministic placement.
- Multiple eligible partials -> least missing bytes wins, stable tie-breaks apply, and files are never
  unioned across drives.
- Apparent same-device/storage-host home and replica -> `FAILURE_DOMAIN_SUSPECT` warning.
- Task IDs and graph ordering stable across identical snapshots.
- Protected homes reserve before bulk; bulk uses RAID-first deterministic first-fit-decreasing.
- Bulk consolidation uses the fewest drives achieved by the legacy heuristic and frees the same labels.
- Replica set chooses the smallest sufficient independent target, else the same whole-model span.
- Normalized old/new assignments and DEC-034 batch ordering match on behavior-preserving fixtures.
- Comparison-normalizer unit tests prove target/order/extra-task mutations cannot be hidden.

### 14.3 Capacity ledger

- Mounted free is not reduced by archived bytes again.
- Unmounted snapshot is marked approximate.
- RAID minimum headroom is identical in totals and execution.
- Guaranteed fetch uses missing raw files only.
- Compression-aware fetch uses ratio, raw components, and margin only for missing files.
- Exact replica bytes replace estimates when a source completes.
- Partial target replica reserves only target-missing source files.
- Workspace can block when durable bytes fit.
- Non-compressible policy paths create no compression temporary.
- StreamZNN workspace equals raw plus exact magic/frame overhead; an over-raw chunk aborts to raw.
- Whole ZipNN and zstd over-cap output abort safely without exceeding their declared filesystem caps.
- Real bf16 StreamZNN high-water measurement remains within the enforced bound.
- Replica durable plus conservative staging workspace is enforced until the target trace proves a lower bound.
- Replica drive requirement is durable sum plus maximum per-task workspace, never plus summed workspace.
- The 100,000-row multi-drive fixture exercises candidate sizing for the full bounded
  `intents × eligible drives` set within the reconciliation performance budget.
- One huge file and many small files use maximum workspace, not summed workspace.
- Single-file fragmentation and single-drive eligibility failures report the right target tier.

### 14.4 Executor and recovery

- Crash before first file record.
- Crash after one archived file.
- Stop during download child.
- Stop during compression child.
- Compression crash/raw fallback.
- Actual compression worse than estimate.
- Target disappears before a file.
- Target becomes unwritable mid-task.
- Replica source absent.
- Replica target absent.
- Annex batch partly succeeds; only keys proven at the target annex UUID are recorded.
- Reconcile after restart reconstructs every unmet task and no satisfied task.
- Reconciliation occurs at drive-batch boundaries, not per file, and meets the 100,000-row latency/lock budget.
- A pinned drive batch drains before global priority may select another drive.
- `replica_order=after_bulk` is default; both supported orders preserve drive affinity.
- Safe protected homes plus offline-only replica deferral -> resumable pause; incomplete protected home -> error.
- Gate C validates eligible location, not only copy count.

### 14.5 Failure/API/UI

- Every failure code serializes and round-trips.
- An infeasible protected home emits one root capacity/tier failure; its dependent replica is linked as
  blocked evidence and does not emit derived `SOURCE_INCOMPLETE` noise.
- Gate B structural replica shortfall blocks admission, while runtime offline replica state pauses and
  runtime capacity exhaustion remains resumable.
- Legacy terminal JSON normalizes safely.
- Bounded terminal payload omits paths and hardware identifiers.
- Modal appears on a live transition without refresh.
- `plan-capacity-stop` is classified as terminal by the live Fill page and never renders the generic
  “Not running. Press Start” fallback.
- Modal reappears after reload until acknowledgement.
- Fill page retains a terminal evidence card after modal dismissal.
- Start control does not replace a blocked explanation with generic idle text.
- Every remote/operator string remains escaped.

### 14.6 Migration

- Fresh database starts with canonical values.
- v0 runs v0→v1 then v1→v2 exactly once; v1 runs only v1→v2; v2 runs neither.
- Connecting to v2 never stamps the database back to v1.
- A database newer than the program is refused without writes.
- `uncompressed` maps to `guaranteed`.
- `compressed` maps to `compression_aware`.
- Invalid legacy value fails before swap.
- Backup is non-overwriting.
- Migration is idempotent.
- Foreign keys and one-active-plan invariant survive rebuild.
- Old CLI aliases map without changing risk policy.
- Deprecated API/CLI `provisioning` aliases work for one compatibility release and emit a warning.
- Cutover test/runbook refuses migration while a legacy process or open catalog descriptor exists and
  never invokes the legacy connector against a v2 fixture.

### 14.7 Sanitized incident regression

Construct a generated fixture with the incident shape—125 protected repositories, 116 complete home
copies, eight partial home copies, one empty home copy, and no replicas. Assertions:

- exactly nine home-fetch tasks;
- exactly 125 replica tasks, 116 ready and nine dependent;
- no fetch task for a completed home copy;
- missing-file guaranteed bytes, not full-model bytes, drive the home ledger;
- eventual replica bytes fit their tier;
- conservative replica workspace adds only the largest per-repository task peak, so the replica tier
  remains feasible rather than doubling the entire 0.663 TB payload;
- the graph is feasible in guaranteed mode;
- the old false-capacity condition cannot recur.

## 15. Observability and diagnostics

Every reconciliation should emit bounded structured telemetry:

```text
plan_id, graph_revision/hash, capacity_mode
requirements_total/satisfied
tasks_total/ready/blocked_by_dependency
fetch_tasks, replica_tasks
guaranteed_bytes, expected_bytes, workspace_peak
ledger margins by drive
failure codes, not full sensitive objects
```

Provide a read-only JSON diagnostic through the CLI, for example:

```text
modelark library plan --json --explain
```

The diagnostic must not scan physical file contents, write the catalog, wake unneeded offline media, or
include hardware serials, filesystem UUIDs, annex UUIDs, private paths, or network identifiers.

## 16. Failure and recovery behavior

| Failure | Durable state | Next reconciliation |
|---|---|---|
| Capacity graph infeasible | No new bytes | Same failure until selection, mode, eligible capacity, or policy changes |
| File preflight fails | Prior files remain archived | Task remains with the unstarted file; typed capacity failure |
| Download interrupted | `.incomplete` may remain; no archived row for that file | Resume artifact if safely attributable; requirement remains unmet |
| Compression/canary fails or exceeds output cap | Raw original retained and temp removed; safe raw fallback recorded | Actual stored result updates subsequent ledgers |
| Portal crash after archived row commit | File fact durable | Task shrinks/disappears on restart |
| Replica copy partial | Physical annex evidence may exceed DB rows | Verify each key at target UUID and record only proven presence; remaining task persists |
| Replica source/target offline with all homes safe | No requirement falsified | Run pauses resumably under INV-13; replica task remains available |
| Protected home genuinely incomplete | Irreplaceable requirement remains unmet | Error unless a valid ready/dependent home task can complete it; never report offline-only pause |
| Wrong-tier complete copy | Data retained as extra | Advisory plus task for the unmet eligible requirement |

## 17. Approaches explicitly rejected

1. **Subtract one estimated model size when `placed_copies == 1`.** This still fails for partial files,
   wrong-tier copies, extra files, source selection, and exact replica sizes.
2. **Change only the terminal message.** The live stop was incorrect, not merely poorly worded.
3. **Automatically switch to compression-aware mode.** That silently changes the operator's risk policy.
4. **Use aggregate fleet capacity as Gate B.** Tier constraints and single-file fit cannot be inferred
   from one total.
5. **Persist every generated task as truth.** Derived tasks become stale and can disagree with archived
   facts after crashes or manual recovery.
6. **Remove the global feasibility gate and keep filling anything that fits.** That changes the finalized
   plan from a completion contract into unreviewed best effort.
7. **Automatically spill protected home copies onto ordinary primary drives.** Replica sources and
   failure-domain guarantees would change; that requires its own policy decision.
8. **Treat headroom as an implicit workspace budget.** Filesystem safety and operation workspace are
   distinct constraints and must be reported separately.
9. **Persist assignment/task truth to solve pre-first-file crashes.** Deterministic placement plus full
   missing-file reservation is sufficient initially; `.incomplete` and task rows must not compete with
   archived facts.

## 18. Review resolutions and empirical gates

External review pass 1 resolved the ten design questions as follows:

| # | Resolution |
|---:|---|
| 1 | Rename the database column to `capacity_mode`; the changed `CHECK` already requires a rebuild, so retaining `provisioning` saves no migration work |
| 2 | Keep bulk-before-replica by default; support configurable `replica_order` without weakening drive batching |
| 3 | Treat a non-RAID-primary copy as policy drift whenever a RAID home exists; retain it and create the RAID-home task |
| 4 | Drive-label independence is sufficient for the current fleet; defer a full failure-domain model but warn on detectable same-device/storage-host placement |
| 5 | Ship without `task_claims`; reserve the full missing file and never infer completion from `.incomplete` |
| 6 | Cap StreamZNN filesystem output at `raw_size + magic + 4 bytes per chunk`, enforce raw fallback on over-cap output, and require real-bf16 validation before Phase 3 |
| 7 | Use snapshot feasibility for planning and guided per-drive live validation immediately before execution |
| 8 | Retain `provisioning` as a deprecated API/CLI alias for one compatibility release, then remove it |
| 9 | Keep raw/expected policy bars as clearly labeled forecasts; use reconciled ledgers, not those bars, for admission |
| 10 | Verify every annex key against the target UUID before recording; repository/batch success is insufficient |

External review pass 2 added the following binding amendments:

- placement preserves DEC-014/017/020/034 objectives and deterministic target/batch output as specified
  in Section 6.4;
- replica workspace is per task and aggregates by maximum, while durable replica bytes aggregate by sum;
- every codec cap is checked before the next output write;
- Phase 1 starts from the merged PR #3/current-verifier history; and
- Phase 6 prevents any legacy binary from opening a migrated v2 catalog.

The following are measurements, not unresolved design choices. Each has a conservative behavior that
is safe before optimization:

| Gate | Must be proven by | Conservative behavior until proven |
|---|---|---|
| StreamZNN/codec output high-water | End of Phase 2 | Enforced pre-write raw-plus-framing cap; over-cap compression abandons temp and stores raw |
| git-annex target staging high-water | End of Phase 2 | Per drive, reserve summed durable bytes plus the maximum equal-sized per-task workspace term |
| 100,000-row reconciliation latency and lock behavior | End of Phase 1 | Do not adopt executor; revise query/cadence if the Section 8.2 budget fails |

DEC-045 resolves Section 7.5's Gate B admission decision for this release. No Phase 3 executor work
begins until all three evidence artifacts have passed review.

## 19. Definition of done

This architecture change is complete only when:

- one canonical manifest definition is used across fetch, reconciliation, plan totals, archive
  verification, restore, and completeness checks;
- exact observed copy locations and partial filenames drive reconciliation;
- a satisfied protected home copy reserves no new home bytes;
- work is represented as deterministic fetch/replica tasks with dependencies;
- normalized placement preserves DEC-014/017/020/034 target and batch behavior;
- one capacity ledger drives CLI, API, UI, Gate B, and file preflight;
- guaranteed and compression-aware modes are named and migrated without policy drift;
- exact source bytes size ready replica work;
- codec and replica transient workspace have enforced, reviewed upper bounds;
- replica workspace aggregates by maximum per drive and cannot double the whole replica payload;
- every recorded replica file is proven at the target annex UUID;
- drive batches preserve DEC-034 affinity and the default bulk-before-replica policy;
- DEF-022 safe-home/offline-replica conditions pause while genuinely missing homes error;
- Gate C validates desired requirements on eligible drives;
- typed failures survive persistence and render immediate, prominent operator guidance;
- the sanitized incident regression passes;
- full unit, HTTP security, packaging, and Playwright suites pass;
- a wheel installation passes the same migration and planner tests;
- a copied legacy catalog produces an externally reviewed plan diff;
- Phase 3 correctness and Phase 4 terminology/migration releases can each ship and roll back
  independently.
- DEC-045 records the operator's explicit Gate B admission decision and distinguishes it from runtime
  pause/resume behavior.

The architecture can be complete before the legacy working-copy cutover. Final live migration,
bounded resume, verification, and restore remain a separate DEC-042 acceptance operation performed
only with the operator present; they are not interleaved with engine implementation.

## 20. External reviewer checklist

Ask reviewers to focus on:

1. Whether desired requirements and observed facts are separated cleanly.
2. Whether any byte category can be double-counted or omitted.
3. Whether workspace math is truly safe for every codec path.
4. Whether graph dependencies cover replica-source readiness and partial copies.
5. Whether crash points can lose, duplicate, or misplace work.
6. Whether exact manifest-set completeness matches fetch policy.
7. Whether capacity-mode migration preserves semantics.
8. Whether failure types are sufficient without coupling UI strings to control flow.
9. Whether the phased rollout permits meaningful shadow comparison before execution changes.
10. Whether any proposal accidentally broadens destructive or unattended operational authority.
11. Whether verifier and restore truly consume the canonical manifest without weakening their safety semantics.
12. Whether reconciliation cadence and drive affinity preserve portal responsiveness and hot-swap behavior.
13. Whether INV-13 exactly preserves DEF-022 pause-versus-error behavior.
14. Whether deterministic placement preserves DEC-014/017/020/034 rather than merely tier eligibility.
15. Whether replica workspace uses durable-sum plus workspace-max and whether codec caps are pre-write.
16. Whether the all-or-nothing Gate B completion contract is explicitly accepted by the operator.

## 21. Review procedure and record

Each review pass should identify the exact document revision or commit. Ask the reviewer to return:

1. blocking architectural findings;
2. correctness or safety gaps;
3. unclear or underspecified behavior;
4. unnecessary scope or simpler coherent alternatives;
5. missing failure, migration, or test cases;
6. explicit answers or objections to Section 18;
7. a final disposition: reject, revise, or approve for phased implementation.

Findings should cite a plan section and, where applicable, the current file/line reference that creates
the risk. Do not resolve a blocking finding only in discussion: amend this document, record the
disposition below, and send the amended revision through another pass.

| Pass | Reviewer | Revision/commit | Disposition | Blocking findings resolved in |
|---|---|---|---|---|
| 1 | External reviewer (operator-supplied) | Initial working-tree draft, 2026-07-14 | Revise, then approve for phased implementation | B1: §5.1/§12–14; B2: INV-13/§8.5; B3: §7.3/§18; remaining safety findings: §6.3/§7.4/§8/§11/§13–14 |
| 2 | External reviewer (operator-supplied) | Revised working-tree draft after pass 1, 2026-07-14 | Revise; Phase 1 may proceed after approval, resolve F1–F4 before Phase 2 | F1: §6.4/§13–14; F2: §7.4/§14/§18; F3: §7.3/§13–14; F4: §7.5 pending operator acknowledgment; F5: §13 Phase 1/6 gates |
| 3 | External reviewer (operator-supplied) | Revised working-tree draft after pass 2, 2026-07-14 | **Approve for phased implementation** | N1: §7.5/DEC-045; N2: §6.1/§6.4/§14; N3: §6.4/§8.2/§14; N4: §9.1/§14 |

Phase 1 may begin. Later phases remain subject to their evidence/review gates. Approval of this plan
does not approve the later live migration or cutover.
