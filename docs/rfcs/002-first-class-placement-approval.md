# RFC-002: First-class placement approval and execution control

- **Status:** accepted — implementation not started; binding DEC and issue rewrites next
- **Date:** 2026-07-20
- **Owners:** Auspex-Aerie + operator
- **Related:** DEC-019, DEC-022, DEC-023, DEC-026, DEC-030, DEC-031, DEC-034, DEC-036, DEC-037,
  DEC-040, DEC-042, DEC-045, DEC-046, DEC-047, DEF-022, DEF-028, DEF-029, INC-014,
  INC-018, INC-019, INC-020, INC-021, RFC-001, issues #35–#39
- **Working plan:** `docs/plans/placement-capacity-hardening.md`
- **Review boundary:** this RFC fixes architecture, behavior, migration, and acceptance scope. Detailed
  pseudocode is now included as an implementation contract; production names may change, but weakening
  its transaction, authority, ordering, or failure semantics requires RFC/DEC review.

## Summary

A mid-fill addition exposed that ModelArk can derive safe work but cannot durably express what placement
an operator reviewed and approved. Reconciliation currently reads mutable catalog state and chooses a
partial target; capacity planning reads catalog/config/evidence again and assigns drives; Fill rebuilds
that graph after each drive batch. The repeated derivation is crash-safe, but it can silently produce a
different target map after state or evidence changes.

This RFC makes placement planning, operator approval, and execution control first-class while retaining
DEC-045's central rule: durable archive facts, not a persisted completion queue, remain the only truth
about completed work.

The implementation uses a functional core and imperative shell:

- one fact reader constructs an immutable `PlannerInput`;
- pure requirement, candidate, feasibility, and optimization functions consume that input;
- an immutable, normalized `PlacementProposal` records the exact assignment shown to the operator;
- strict revision CAS atomically approves the proposed selection mutation and placement;
- a pure `ExecutionProjection` derives only the remaining monotonic subset of approved work;
- a minimal durable session plus same-host locks fences execution;
- the existing incident-hardened fetch/replica transport remains protected code.

This is not a generalized artifact-versioning, event-sourcing, distributed-locking, or workflow system.

## Operator-visible consequences

### Evidence divergence stops instead of silently re-placing

Today `fill.execute()` reconciles and calls placement again after every drive batch. That can absorb an
unexpectedly full target or other drift by assigning later work elsewhere. Under this RFC the executor
cannot invoke placement. Once approved, target/source assignments remain fixed.

If current authoritative evidence proves that an approved assignment no longer fits, execution stops at
a safe boundary with `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE`. The terminal explains that completed bytes
remain durable and that another placement may exist, but requires a fresh preview and approval. It never
silently substitutes that alternative.

The following dynamic events are not placement drift:

- completed files and requirements shrink the remaining projection;
- an approved, identity-matching shelved drive follows the existing guided hot-swap await path;
- dirty or unknown capacity evidence requests mount/reconciliation and retries the same approved map if
  the refreshed evidence is sufficient;
- DEC-047 gated retry/skip/timeout parks work only for the current session and does not satisfy, remove,
  or remap its approved requirement;
- stop, throttle, transport retry, and typed acquisition failures preserve the same approval.

An identity/epoch/policy change, expanded work, new task, changed source/target, or known capacity
shortfall is approval drift and requires a fresh preview.

### A live execution session makes planner mutations exclusive

This is an intentional new operator restriction. While a session is `starting`, `running`, or
`stopping`, read-only catalog/plan inspection remains available, but operator graph mutations are
refused: selection finalization/removal/clear, discovery or manifest recompute, protect/`numcopies`, plan
selection/membership/capacity mode, drive identity/role/lifecycle/eligibility, and anchor/dirty repair
outside the owning worker. An external CLI writer is subject to the same rule.

The recovery path is explicit: allow the worker to reach a safe boundary, then Stop or let it enter a
typed `paused`/`blocked` terminal; the live lease and all process/per-drive locks are released; perform
the mutation; Preview Again and approve if the authoritative projection changed. The operator cannot
continue catalog mutation concurrently merely because SQLite WAL would permit the SQL writes.

Recovery reconciliation is not deadlocked by this rule. A dirty/unknown target that needs operator
reconciliation first transitions to non-live `paused`/`blocked`, releases its lease/locks, and then allows
identity-proven reconciliation. If that operation only refreshes evidence and the same approved map still
fits, projection permits resume without new placement approval. Arbitrary graph edits are also possible
once non-live, but authoritative recomputation makes them require a fresh proposal when relevant.

### Cutover requires one fresh approval

Migration never fabricates approval for a legacy/finalized selection. Existing `archived` rows remain
durable completion truth, so the fresh projection excludes work already completed, but no Fill may start
after cutover until the operator previews and approves the current selection.

The current GLM-5.2/BF16 Fill and the established 390-model selection therefore do not auto-resume across
this migration. Schedule production cutover at a Fill-idle boundary, preferably after the current work
drains under the pre-RFC executor. Stop the service and every CLI writer, migrate backup-first, start the
portal without auto-resume, review the derived remaining work, and explicitly approve before Fill.

The #35 clean-anchor migration is also fail-closed. A drive without a trustworthy clean anchor is
`unknown` while offline. Establish identity-proven anchors during the earlier #35 rollout where possible;
the attended cutover must reconcile any remaining required drive before the first approved run.

## Goals

1. Make every planning read explicit in one immutable `PlannerInput`.
2. Make requirements, alternatives, budgets, feasibility, and `tiered_v2` placement pure and testable
   without SQLite, mounts, or global configuration.
3. Make preview→approval a strict revision-bound CAS over an exact target/source assignment.
4. Make start/resume deterministically derive a monotonic subset of approved work without optimization.
5. Give portal and CLI mutations one revision, approval, and execution-session boundary.
6. Centralize free-space authority, drive identity/epoch, dirty generations, and write exclusion.
7. Preserve every DEC-046/047 and INC-018–021 transport/crash behavior.
8. Deliver #35–#39 in reviewable, operator-visible increments with backup-first migration and rollback.

## Non-goals

- Pinning Hugging Face provider commits or introducing provider-wide artifact snapshot tables.
- Proving ordinary Git-tracked, hashless files immutable relative to a moved upstream HEAD.
- Rewriting the download, compression, publication, annex, replica, gated-access, or watchdog pipeline.
- Persisting a mutable work queue or treating proposal/session rows as completion truth.
- Event sourcing, a distributed lease framework, or support for cross-host concurrent NAS writers.
- Multi-plan concurrency, fine-grained per-plan revisions, or a general workflow engine.
- Proving global placement optimality; bounded deterministic improvement remains best effort.
- Partial continuation past a structurally blocked whole-plan Gate B (DEF-028).
- Cross-drive shard spanning, multi-RAID copy-#1 homes, or unsupported artifact-family acquisition.

## Existing invariants retained

- `files` + `archived` + verified physical facts determine completion; proposals never do.
- Gate B remains whole-plan and mode-labelled per DEC-045.
- Gate B distinguishes `FEASIBLE`, `PACKING_INCONCLUSIVE`, `CAPACITY_EVIDENCE_UNKNOWN`,
  `INFEASIBLE_UNDER_ADMISSION_BUDGET`, and
  `INFEASIBLE_EVEN_AT_OPTIMISTIC_USABLE_CAPACITY`, with structural/policy failures taking precedence;
  only `FEASIBLE` is approvable.
- `guaranteed` is raw-bounded; `compression_aware` is estimate-backed and an actual-ratio overrun stops
  as `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE` without remapping.
- A file operation performs a fresh target preflight and never crosses the safety floor.
- Staging and retry state are ephemeral and never imply completion.
- Guided hot-swap awaits the approved drive; offline does not mean lost.
- Gated access is a session-local operator decision and typed follow-up, not a generic retry.
- No verified archived fact is auto-deleted to make policy or placement appear satisfied.

## Current architecture and failure seam

The current authority path is:

```text
selection/files/archived/drives
          |
          v
reconcile_plan(con)          reads facts and chooses one partial target
          |
          v
plan_capacity(con, graph)    reads drives, config, evidence and assigns targets
          |
          v
fill.execute()               executes one drive batch, then repeats both calls
```

This shape has useful typed graph and capacity records, but the boundaries permit hidden policy and
I/O:

- `_choose_partial` selects before global feasibility;
- `plan_capacity` reads SQLite, compression config, observed ratios, and free-space evidence;
- portal/CLI projections can reconstruct inputs differently;
- Fill can obtain a new assignment at a batch boundary without operator approval;
- process-local `FillWorker` exclusion does not cover an external CLI writer.

## Proposed architecture

The target authority path is:

```text
fact reader + current admission evidence
                 |
                 v
           immutable PlannerInput
                 |
       +---------+----------+
       | pure requirements  |
       | pure candidates    |
       | pure feasibility   |
       | pure optimization  |
       +---------+----------+
                 |
                 v
       normalized PlacementProposal
                 |
       strict planner_revision CAS
                 |
                 v
          approved proposal
                 |
      pure monotonic projection + current evidence
                 |
                 v
       ExecutionProjection + durable session/flock
                 |
                 v
       existing exact-task transport pipeline
                 |
                 v
       archived rows + later projection shrink
```

### Functional core

The pure core owns four transformations:

1. `PlannerInput → RequirementGraph`
2. `PlannerInput + RequirementGraph → CandidateSet`
3. `PlannerInput + CandidateSet → PlacementProposal`
4. `Approved PlacementProposal + current PlannerInput/evidence + session overlay → ExecutionProjection`

The fact-reading boundary remains a distinct function/object even if initially located in
`reconcile.py`. Tests construct synthetic `PlannerInput` values directly; pure functions accept no DB
connection, filesystem path, global config loader, clock, or network client.

### Imperative shell

The shell owns:

- consistent catalog reads and `planner_revision` snapshots;
- live/anchor evidence collection and mount-identity proof;
- proposal persistence and approval CAS;
- selection mutation and active-proposal transition;
- execution-session acquisition, heartbeat, fencing token, and release;
- per-drive same-host `flock`, dirty generation, filesystem operation, and clean-anchor recovery;
- invoking the unchanged fetch/replica transport with exact projected tasks.

### Ownership rules

- Reconciliation decides **what copy is required**, never its target.
- Candidate construction exposes every supported finish-in-place/fresh alternative and deterministic
  cost, never chooses among them.
- The solver decides target/source assignments and performs no I/O.
- Proposal persistence cannot reinterpret solver output.
- Approval commit cannot rerun optimization.
- Projection may shrink approved work or refresh evidence, never expand or remap it.
- The executor cannot call reconciliation candidate selection or placement optimization.
- The executor may re-project only at reviewed safe boundaries against the same approved proposal.
- Portal, CLI, and systemd resume enter through the same proposal/session services.

## First-class records

### `PlannerInput` (in memory)

One fact reader constructs a versioned immutable value containing:

- plan id, fixed plan membership, capacity mode, policy and solver versions;
- finalized selection plus the hypothetical add/remove/clear/finalize mutation being previewed;
- canonical manifests with filename, size, existing LFS SHA-256, format, quant, safety, and storage action;
- `numcopies`, protected/bulk classification, failure-domain constraints;
- exact archived facts, original/stored hashes and byte sizes, replica/source facts;
- drive identity, capacity epoch, role, RAID, lifecycle, eligibility, and plan membership;
- derived admission evidence with provenance, dirty generation, exclusivity policy, and safety margins;
- graph-affecting compression configuration and observed-ratio evidence;
- the catalog `planner_revision` from which these facts were read.

The input is assembled from one consistent read transaction plus separately collected volatile evidence
whose observation identity/time is explicit. Volatile `df` values do not increment the catalog revision;
the proposal records the evidence used and approval/start revalidates current authority.

A full fact read is accepted deliberately. At the current single-plan/catalog size SQLite reads
megabytes, not an unbounded remote graph. Copied-catalog performance acceptance retains an explicit p95
budget; incremental invalidation is deferred until measurement shows the full snapshot is material.

### `PlacementProposal` (normalized, durable)

A proposal is the immutable reviewed result of one planning input and hypothetical selection mutation.
It contains:

- based-on `planner_revision` and before/after selection hashes;
- a compact canonical mutation descriptor (operation plus ordered repo ids), stored once;
- capacity mode, policy/config/solver versions and hashes;
- exact requirement/task ids, kinds, target drives, source drives, dependencies, and budgets;
- per-requirement full-manifest hash;
- task-relevant missing files and reused-content evidence;
- Gate-B outcome, diagnostics, derivation mode, and deterministic proposal hash.

The compact mutation descriptor is the only representation of the requested selection delta. Planning
outputs live in normalized child rows. ModelArk never stores a second writable full-proposal JSON blob.
API JSON and canonical hash input are computed deterministically from authoritative rows.

`adopt_current` is a first-class mutation kind for approving the already-finalized selection. Its
before/after selection hashes are equal and approval performs no selection-row write, but it still uses
the same preview, semantic recomputation, exact-assignment validation, CAS, proposal lifecycle, active
pointer, and revision transaction. It is the cutover and deliberate "bless what is selected now" path,
not a bypass for approving arbitrary current state.

### `ExecutionProjection` (in memory)

Projection is a specified pure function. Its inputs are the approved proposal, current durable facts,
current admission-authoritative evidence, and a bounded session overlay such as DEC-047 parked gated
requirements. Its output is the exact remaining task set or one typed refusal.

The projection rules are:

1. Every current desired requirement must exist in the approval.
2. Every remaining task retains its approved target/source and task kind.
3. An approved missing file may disappear only when a matching durable archived fact now satisfies it on
   that approved target.
4. Present/reused facts may grow and missing sets may shrink; neither may reverse without a typed integrity
   or approval-drift failure.
5. No new filename, requirement, dependency, target, source, or larger task budget may appear.
6. The current full-manifest hash, plan/policy/config versions, drive identity+epoch, and selection hash
   must equal the approved values.
7. Current authoritative evidence must admit the exact remaining assignment under the approved capacity
   mode.
8. Session-local gated parking suppresses scheduling only; it does not mutate approval or mark completion.
9. Output ordering is canonical and independent of DB query/insertion order.

Repeated projection over unchanged inputs is byte-equivalent. Adding matching approved progress can only
remove work. Property tests enforce both determinism and monotonicity.

### `ExecutionSession` (minimal durable control)

One durable row records session id, approved proposal, owner, status, acquired/heartbeat/expiry times,
bound `planner_revision`, and a monotonically increasing fencing token. Exactly one session may be live
for the active plan.

The row coordinates controllers; same-host process/per-drive `flock` is the physical mutual-exclusion
primitive. Every catalog write from the worker validates the current token, and every filesystem mutation
holds the appropriate `flock`, marks the drive dirty before allocation, and revalidates at safe
boundaries. Lease expiry alone does not override a still-held physical lock. Forced recovery leaves the
affected drive dirty until reconciliation.

Cross-host writers and a distributed NAS fence remain unsupported. A shared remote that another host or
unfenced workflow may write is not admission-authoritative.

Session state defines mutation authority:

| Session state | Lease/lock status | Allowed graph mutation |
|---|---|---|
| `starting` | Live session lease; no task bytes until projection/token checks pass | Worker-owned setup only; operator mutations refused |
| `running` | Live session lease; per-drive `flock` held around each mutation boundary | Worker-owned archived progress and dirty/anchor transitions only |
| `stopping` | Still live until the worker reaches a safe boundary and releases every lock | No operator mutation; UI says to wait for stopped terminal |
| `paused` / `blocked` | Non-live historical row; heartbeat/lease cleared; every `flock` released | Reconciliation and arbitrary operator mutation allowed; resume must pass authoritative projection |
| `stopped` / `done` / `failed` | Non-live terminal row; no lock retained | Mutations allowed; any later start revalidates approval |

An expired `starting`/`running`/`stopping` lease is not immediately equivalent to non-live: recovery first
proves no physical lock/process remains or performs the forced-dirty recovery, then records a non-live
terminal. This prevents both stale-writer takeover and pause/reconciliation deadlock.

Terminal session rows are never reactivated. "Resume" means atomically creating a new `starting` row
with a new session id and fencing token, referencing the same still-valid approved proposal and, for
audit, the prior row through `resumed_from_session_id`. The transaction requires that no live session
exists. Multiple historical sessions may therefore reference one proposal sequentially, but at most one
may be live; the prior `paused`/`blocked` row remains immutable evidence of why execution stopped.

## Schema

This RFC adds five planning/control tables. #35 clean anchors/dirty generations and #37 drive lifecycle
state are separate issue-owned schema changes because they represent durable storage facts.

### `planner_state`

Singleton control row for the current single-active-plan architecture:

- `singleton_id = 1`
- monotonic `planner_revision`
- `active_approved_proposal_id` nullable FK
- monotonic `next_fencing_token`
- update timestamp/schema-policy version as needed

The global revision is deliberately coarse: any graph-affecting catalog mutation invalidates every
uncommitted preview. This is acceptable for one operator and one active plan. Per-plan revisions are
deferred until real concurrency makes false invalidation material.

Every supported writer increments the revision in the same transaction as a graph-affecting change:
selection/finalization, `numcopies`, canonical `files`/manifest refresh, archived progress/removal, plan
membership/capacity mode, drive identity/epoch/role/lifecycle/eligibility, clean-anchor/dirty state, and
persisted policy versions. Repair/migration tools use the same primitive. External config remains a file,
so its canonical hash is rechecked at preview, approval, and execution boundaries rather than pretending
that a catalog counter observed an out-of-band edit. Direct SQL mutation outside these supported paths is
unsupported and must be caught by canonical-hash/replay integrity checks.

An actual active-plan switch is allowed only with no live execution session. In one transaction it
supersedes the current active approved proposal, clears `active_approved_proposal_id`, changes the active
plan, and increments `planner_revision`. The newly active plan always requires a fresh preview/approval;
switching back never silently reactivates its prior proposal. A no-op selection of the already-active plan
does not manufacture a revision or approval change.

### `placement_proposals`

One immutable proposal header:

- id, plan id, based-on revision;
- lifecycle `draft | approved | superseded`;
- mutation kind/arguments, before/after selection hashes;
- desired `requirement_set_hash` and the versioned semantic input-hash bundle;
- capacity mode and policy/config/solver versions;
- Gate-B outcome, derivation mode, and bounded typed diagnostics stored once as authoritative proposal
  metadata;
- stored canonical hash;
- created, approved, superseded timestamps.

Creation inserts the header and all child rows transactionally. Task/file/header planning fields are never
updated afterward. Approval changes only lifecycle/approval metadata while atomically applying the
selection mutation and active pointer. A new approval supersedes the previous active proposal.

At most one approved-active proposal exists per plan. Approved and superseded proposals are retained as
audit/provenance. Unapproved drafts may be garbage-collected after a documented retention interval only
when no session/reference exists; GC is not required for the first release and may initially be manual.

### `proposal_tasks`

Normalized execution-authority assignments:

- proposal id + stable requirement/task id;
- repository, requirement/task kind and dependency;
- exact target and source where applicable;
- full-manifest hash;
- guaranteed/expected durable and workspace budgets;
- approved drive identity/capacity epoch and admission-evidence reference/class;
- reuse/movement cost and evidence class;
- deterministic execution ordering fields.

Foreign keys bind referenced plan/drive identities where schema evolution permits. Application validation
also binds the recorded drive capacity epoch so label reuse cannot transfer approval.

### `proposal_files`

Only task-relevant file evidence is copied:

- missing files the executor may fetch/copy;
- reused files whose content/byte evidence affected candidate cost or task construction;
- filename, size, existing upstream LFS SHA-256 when supplied;
- archived original hash for reused content when present;
- format/quant/storage action and per-file budget evidence;
- approved target/source association.

The entire repository manifest is not duplicated. `proposal_tasks.full_manifest_hash` commits to the
canonical complete manifest in `files`; projection recomputes it to detect added, removed, renamed, or
resized files. This bounds proposal growth while retaining exact executable work.

### `execution_sessions`

- session id, plan id, approved proposal id;
- nullable `resumed_from_session_id` self-reference for audit lineage;
- owner/controller identity;
- state `starting | running | stopping | paused | blocked | stopped | done | failed`;
- bound planner revision and fencing token;
- acquired, heartbeat, expiry, terminal timestamps;
- typed terminal code and bounded evidence reference/summary.

A partial unique constraint prevents multiple live sessions for the active plan. Existing versioned
`last_fill.json` remains the operator-facing terminal artifact during compatibility; its payload derives
from the same typed session result.

## Canonical hash contract

Normalized rows are the sole source of truth. A versioned canonical serializer reads one proposal header
plus its tasks/files in explicit sorted order, normalizes enums/integers/nulls, and emits the bytes used for
SHA-256. Store only that hash. The hash covers immutable planning fields only; mutable lifecycle/audit
fields (`draft|approved|superseded` and their timestamps) are deliberately excluded so approval cannot
change the reviewed proposal identity.

The serializer is shared by preview response, commit validation, audit display, tests, and copied-catalog
replay. A stored hash mismatch is an integrity failure. There is no code path that updates both a blob and
rows, and no API accepts a client-supplied serialized proposal as authority.

Golden vectors cover Python versions and shuffled insertion/query order. Changing canonical form requires
a new serializer version; existing approved proposals retain their recorded version.

`planner_revision` is a fast invalidation/CAS pre-check, not semantic authority. Approval and every start/
resume reconstruct current `PlannerInput`, recompute the desired requirement set, selection and full-
manifest hashes, drive identity/epoch, and policy/config hashes, then compare them with the normalized
proposal before capacity validation. These recomputed facts are authoritative even when the integer
revision happens to match. A missed revision increment remains a defect and loses the cheap early reject,
but cannot authorize stale or expanded execution.

## Proposal lifecycle and CAS

### Draft creation

1. Read `planner_revision` and all catalog facts in one consistent snapshot; build `PlannerInput`,
   including the hypothetical mutation.
2. Run pure reconciliation/candidate/feasibility/optimization outside a write transaction.
3. In a short transaction, persist one immutable draft proposal and child rows only if the revision is
   still unchanged; otherwise return a fresh-preview retry.
4. No selection or archive bytes change.

Multiple concurrent drafts may exist. They do not reserve capacity and do not authorize Fill.

### Approval

Approval uses a short `BEGIN IMMEDIATE` transaction and requires:

- proposal remains `draft` and hash-valid;
- current global revision equals `based_on_revision`;
- a freshly reconstructed requirement set and all semantic hashes equal the proposal, independent of the
  revision pre-check;
- the requested mutation matches the stored mutation descriptor;
- current selection-before hash matches;
- current authoritative evidence still admits the exact proposal assignment;
- no live execution session conflicts.

The transaction applies the selection mutation, marks this proposal approved, supersedes the prior active
proposal, updates `planner_state.active_approved_proposal_id`, and increments `planner_revision`. All or
nothing: no finalized selection may be published without its approved proposal.

A CAS failure leaves the draft as non-authoritative diagnostic evidence and returns a fresh-preview action.
It never reruns optimization inside commit.

### Start and resume

Start/resume loads the active approved proposal, collects current facts/evidence, and computes the pure
projection. If valid, it atomically creates a new `starting` session row, allocates a new fencing token,
and binds the current revision; resume links that row to its terminal predecessor rather than reactivating
the predecessor. It then invokes existing exact-task scheduling. It does not call the solver.

Full projection runs at start/resume, completed drive-batch boundaries, and typed state-changing events.
Per-file checks remain fenced and authoritative but do not reread/re-hash the whole plan. Projection can
shrink tasks only. Every non-live terminal releases the live-session constraint; `paused`/`blocked`
approval remains available for a valid later resume.

## Runtime drift and typed terminals

| Condition | Result | Operator action |
|---|---|---|
| Approved target merely offline, identity unchanged, clean anchor sufficient | Existing GATE-A await path | Insert/mount approved drive |
| Target dirty/unknown but identity unchanged | `CAPACITY_EVIDENCE_UNKNOWN` pause | Mount and reconcile; retry same approval |
| Exact approved assignment no longer fits known evidence | `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE` | Fresh preview/approval; completed bytes remain |
| Different drive identity/capacity epoch at approved label | `APPROVED_TARGET_IDENTITY_CHANGED` | Correct mount/identity or fresh preview after explicit lifecycle action |
| Manifest/selection/policy/config changed | `APPROVED_INPUT_CHANGED` | Fresh preview/approval |
| Projection attempts new/expanded/remapped work | `APPROVAL_PROJECTION_VIOLATION` | Fail closed; inspect/report defect |
| Per-file preflight discovers known target shortfall | `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE` at safe boundary | Fresh preview; never re-place automatically |
| `compression_aware` actual durable ratio overruns the approved remaining budget | `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE` after preserving completed bytes | Fresh preview under current facts; never silently remap |
| Gated repo skipped/timed out | Session-local park + typed follow-up | Continue other approved work; later Start retries |
| Operator stop/throttle/transient typed pause | Preserve approval | Resume same proposal if projection remains valid |

The portal must distinguish “mount/reconcile this approved target” from “placement requires new
approval.” The latter displays the approved target, current shortfall/evidence, completed progress, and a
Preview Again action. It must not offer a one-click silent re-plan-and-start operation.

## Approval-integrity boundary

The proposal commits to the catalog's full manifest structure through `full_manifest_hash`, and copies
all task-relevant missing/reused evidence. A catalog manifest refresh that adds, removes, renames, or
resizes a file changes that hash and requires a fresh preview. Upstream additions that were not refreshed
cannot broaden the explicit approved task; an approved file removed/renamed upstream fails lookup; every
downloaded original must match the approved byte length before publish; and LFS weights carry upstream
SHA-256 and are verified at ingest. All ingested originals receive `archived.orig_sha256` for restore
integrity.

The accepted residual is narrower: a same-path, same-size, content-changed file with no upstream SHA-256
(typically a small Git-tracked config/metadata file) can change at provider HEAD between approval and
fetch. ModelArk hashes the bytes it actually ingests, but without provider-revision pinning cannot prove
they equal approval-time content. This does not permit task broadening or weight-hash substitution.

Provider commit pinning, its `hf_xet`/gated-path behavior, and provider-wide snapshot/provenance schema are
deferred to a separate RFC/issue if reproducible whole-repository acquisition becomes a priority.

## Capacity evidence and filesystem mutation

#35 introduces the shared evidence authority used by planner, API/UI, approval, projection, and per-file
preflight:

- identity-proven mounted free space is live authority only under supported write exclusion;
- a clean offline anchor is authority only for the same identity/capacity epoch under exclusive control;
- dirty offline evidence is unknown;
- unknown drives contribute zero executable capacity;
- optimistic unknown-drive capacity is diagnostic only;
- every result names its evidence and capacity mode.

`clean_anchor.anchor_free_bytes` is the raw identity-proven filesystem `available` observation, not an
already-net admission budget. Evidence derivation subtracts the current versioned safety floor exactly
once. Anchor validation uses `[0, filesystem_capacity_for_epoch]`; Gate B uses the resulting post-floor
admissible free and post-floor optimistic maximum.

Every ModelArk filesystem mutation—including mutating probes, directory/temp/staging creation, cleanup,
annex bookkeeping, download, replica, restore, and registration—enters one write-mutation boundary:

1. acquire same-host drive `flock` and validate session token where applicable;
2. durably advance/mark the dirty generation before allocation;
3. perform existing operation and catalog writes with token checks;
4. reconcile filesystem/catalog/annex state;
5. publish a clean anchor and clear only the same generation while the lock remains held.

#35's dirty-generation record reserves nullable `owner_session_id` and `owner_fencing_token` fields,
with a constraint that both are null or both are present. They are null for operator and pre-#39 worker
mutations; #39 starts populating them for session-owned Fill mutations and validates the pair against the
live session in application code. #35 does not create an early FK to the later `execution_sessions`
table. This forward-compatible ownership evidence lets crash recovery identify exactly which drives an
expired token touched without requiring a second dirty-generation migration.

Await polling should use non-mutating presence checks. A required mutating writability probe occurs only
inside this boundary. Cross-host concurrent writers remain unsupported rather than motivating a
distributed-lock subsystem.

## Implementation pseudocode

This section is normative about authority, ordering, transaction scope, deterministic traversal, and
typed outcomes. Names and file boundaries are illustrative. Implementation names should be concise
domain verbs (normally one verb plus one domain noun); module context, parameter/result types, and
docstrings carry the rest of the contract. Avoid sentence-length names. The pseudocode retains `_pure`
only where it helps mark the I/O boundary; such functions accept immutable values and perform no SQLite,
filesystem, configuration, clock, environment, or network access.

### Shared conventions and boundaries

```python
LIVE_STATES = {"starting", "running", "stopping"}
RESUMABLE_STATES = {"paused", "blocked", "stopped", "failed"}

@frozen
class Refusal:
    code: str
    evidence: CanonicalMap
    actions: tuple[str, ...]

@frozen
class CapturedInput:
    planner_input: PlannerInput
    revision: int
    semantic_hashes: SemanticHashes
    evidence_by_drive: CanonicalMap[DriveId, CapacityEvidence]

def immediate_transaction(con, body):
    con.execute("BEGIN IMMEDIATE")
    try:
        result = body(con)
        con.execute("COMMIT")
        return result
    except BaseException:
        con.execute("ROLLBACK")
        raise
```

All IDs, enums, filenames, requirement collections, candidates, and diagnostics cross a boundary in
canonical order. Byte values are non-negative integers; ratios use versioned fixed-point integers or
canonical decimal strings, never binary floats. A function returns a typed `Refusal` for an expected
safety outcome and raises only for an integrity defect or unclassified implementation failure.

`SemanticHashes` is a bundle with two scopes. `approval_input` includes the desired selection,
requirements, manifests, baseline archived present/missing identity, plan/drive identity+epoch,
lifecycle/eligibility, exclusivity policy, capacity mode, solver/policy versions, and graph-affecting
config. `execution_invariants` excludes only the allowlisted archived progress that projection may shrink.
Neither scope includes live/anchor free-byte values, observation timestamps, dirty/clean evidence state,
`planner_revision`, proposal lifecycle, or active-pointer/session metadata: those are validated by their
own contracts. Capacity evidence is recorded with the proposal and revalidated against the exact
assignment, never compared for byte-for-byte equality. Thus ordinary `df` jitter cannot invalidate
approval by hash, and approval's own revision increment cannot invalidate execution.

There are two catalog write primitives:

```python
def graph_write(con, operation):
    # Used by selection, discover/manifest replacement, protect/numcopies,
    # plan membership/mode/select, and drive/lifecycle/anchor repair. The
    # controller flock is re-entrant for an enclosing operator drive mutation.
    with same_host_controller_flock.hold_reentrant():
        def tx(con):
            if live_session_exists(con):
                return Refusal("FILL_SESSION_ACTIVE", evidence=live_owner(con),
                               actions=("stop_or_pause_fill",))
            result = operation(con)
            # Writers bump unless they prove canonical before/after equality.
            # A false positive only invalidates a preview; a false negative is a defect.
            if not result.proven_noop:
                bump_planner_revision(con)
            return result.value
        return immediate_transaction(con, tx)

def session_write(con, session_id, token, operation):
    # The only graph writer admitted while a session is live.
    def tx(con):
        session = require_live_session(con, session_id, token)
        require(planner_revision(con) == session.bound_planner_revision,
                "SESSION_REVISION_DIVERGED")
        result = operation(con)
        new_revision = bump_planner_revision(con)
        update_session_bound_revision(con, session_id, token, new_revision)
        return result
    return immediate_transaction(con, tx)
```

Heartbeat and session-state-only writes validate the fencing token but do not bump `planner_revision`.
They cannot change requirements, placement, evidence, or durable completion facts. Direct callers do not
issue graph-changing SQL; every supported writer routes through one of these primitives.
`proven_noop` requires canonical before/after equality for every graph fact the operation can touch.
Unknown or partial change detection must bump. A false-positive bump is safe over-invalidation; a
false-negative is an implementation defect, even though authoritative semantic recomputation still
prevents stale execution.

The universal same-host acquisition order is **controller `flock` → sorted drive `flock`s → short
SQLite transaction**. No path acquires a physical lock while holding a SQLite write transaction. The
controller lock serializes session creation with operator mutations; the durable live-session row keeps
operator writes excluded after the start path releases that lock.
The controller-lock key derives from the canonical catalog identity/path, never the caller's
`--state-dir`; every process opening the same catalog therefore contends on the same lock. Drive-lock
keys derive from immutable drive identity+epoch, never a mutable label or mount path. Cross-host lock
behavior remains unsupported.

### Capture one planner input

The fact reader has catalog-only and evidence-collection halves. This keeps pure tests independent of
SQLite while making the volatile observation and its identity explicit.

```python
def read_catalog_facts(con, plan_id, mutation) -> CatalogFacts:
    # Caller owns a consistent read or write transaction.
    state = read_planner_state(con)
    plan = read_active_plan_exact(con, plan_id)
    selection_before = read_finalized_selection(con)
    selection_after = apply_mutation_pure(selection_before, mutation)
    return freeze(CatalogFacts(
        planner_revision=state.planner_revision,
        plan=plan,
        mutation=normalize_mutation(mutation),
        selection_before=selection_before,
        selection_after=selection_after,
        manifests=read_canonical_manifests(con, selection_after),
        model_copy_policy=read_copy_policy(con, selection_after),
        archived=read_archived_facts(con, selection_after),
        drives=read_plan_drive_facts(con, plan_id),
        policies=read_policy_versions(con),
    ))

def observe_capacity(facts, drive_fences, now) -> map[DriveId, CapacityEvidence]:
    observations = {}
    for drive in canonical_drive_order(facts.drives):
        # Preview may fail to acquire a fence and return unknown. Approval/start
        # acquire all relevant fences in sorted order before calling this function.
        with drive_fences.try_hold(drive.identity) as fence:
            mount = prove_mount_identity(drive)
            if mount.matches_epoch and fence.held and drive.write_exclusion_supported:
                free = read_live_free_bytes(mount.path)
                observations[drive.id] = live_evidence(
                    observed_free=free,
                    admissible_free=max(0, free - drive.safety_floor),
                    observed_at=now,
                    identity_proof=mount.proof,
                    fence_proof=fence.proof,
                )
            elif mount.present:
                observations[drive.id] = unknown_evidence(
                    "MOUNT_IDENTITY_OR_WRITE_FENCE_UNPROVEN", mount.diagnostics
                )
            elif drive.dirty_generation != 0:
                observations[drive.id] = unknown_evidence("OFFLINE_DIRTY")
            elif valid_exclusive_clean_anchor(drive):
                anchor = latest_clean_anchor(drive)
                if not 0 <= anchor.free_bytes <= drive.filesystem_capacity_for_epoch:
                    observations[drive.id] = unknown_evidence("ANCHOR_OUT_OF_RANGE")
                else:
                    observations[drive.id] = anchor_evidence(
                        observed_free=anchor.free_bytes,
                        admissible_free=max(0, anchor.free_bytes - drive.safety_floor),
                        anchor_id=anchor.id,
                        identity_epoch=anchor.identity_epoch,
                    )
            else:
                observations[drive.id] = unknown_evidence("NO_ADMISSION_AUTHORITY")
    return freeze(observations)

def capture_input(con, plan_id, mutation, config_reader, drive_fences, now):
    with consistent_read_transaction(con):
        facts = read_catalog_facts(con, plan_id, mutation)
    config = freeze(config_reader.read_graph_affecting_config())
    evidence = observe_capacity(facts, drive_fences, now)
    planner_input = assemble_input_pure(facts, config, evidence)
    return CapturedInput(
        planner_input=planner_input,
        revision=facts.planner_revision,
        semantic_hashes=semantic_hashes_pure(planner_input),
        evidence_by_drive=evidence,
    )
```

A preview observation does not reserve bytes. Approval and start collect new evidence and reconstruct
semantic facts again. A mounted drive without the required writer fence is not promoted to live evidence;
an unknown drive contributes zero executable free.

### Pure requirements and candidate construction

Reconciliation emits requirements and alternatives, never a placement pin:

```python
def requirements_pure(inp: PlannerInput) -> RequirementGraph:
    requirements = []
    for repo in sorted(inp.selection_after):
        manifest = require_supported_manifest(inp.manifests[repo])
        requirements += required_copy_set(
            repo=repo,
            numcopies=inp.numcopies[repo],
            protected=is_protected(repo, inp),
            failure_domains=inp.failure_domain_policy,
        )
    return freeze(RequirementGraph(
        desired=canonical_requirements(requirements),
        requirement_set_hash=hash_requirements(requirements),
    ))

def candidates_pure(inp, graph) -> CandidateSet:
    candidates = []
    satisfied = []
    for req in graph.desired:
        complete = matching_complete_facts(req, inp.archived, inp.drives)
        if complete:
            satisfied.append(canonical_satisfaction(req, complete))
            continue

        for drive in policy_permitted_drives(req, inp.drives):
            reusable = verified_reusable_files(
                requirement=req,
                target=drive,
                archived=inp.archived,
                manifest=inp.manifests[req.repo_id],
            )
            missing = manifest_files(req) - reusable
            candidates.append(Candidate(
                requirement_id=req.id,
                task_kind=task_kind_for(req),
                target_drive=drive.id,
                source=replica_source_reference(
                    req,
                    # A complete approved-home fact or the protected-home
                    # requirement whose chosen target the solver must resolve.
                    graph=graph,
                    archived=inp.archived,
                ),
                depends_on_requirement=req.independent_of,
                reused_files=canonical_files(reusable),
                missing_files=canonical_files(missing),
                budget=budget_candidate_pure(req, drive, missing, inp),
                movement_cost=movement_cost_pure(req, drive, reusable, inp),
                supported=finish_in_place_or_fresh_only(req, drive, inp),
            ))

    return freeze(CandidateSet(
        satisfied=canonical_satisfactions(satisfied),
        by_requirement=group_and_sort_candidates(candidates),
    ))
```

Unknown legacy partial provenance produces no reusable-file credit. A fresh target remains a candidate
when a partial exists elsewhere. Unsupported annex relocation is omitted, not silently converted into a
download or an infinite-cost executable task. Existing partial bytes are already reflected in free-space
evidence and are not charged again; only missing durable bytes and peak workspace are admitted.
For a replica whose home is also pending, the candidate carries a requirement reference rather than a
guessed source drive. Feasibility resolves that reference to the same assignment's home target; proposal
normalization then stores the resulting exact source drive and dependency.

### Gate B and deterministic `tiered_v2`

Feasibility and improvement are separate calls:

```python
def gate_b_pure(inp, graph, candidates, feasibility_state_limit) -> GateBResult:
    structural = first_structural_failure(
        graph,
        candidates,
        # Candidate-specific peak on an otherwise empty drive, bounded by the
        # policy-permitted drive's post-safety-floor usable maximum.
        max_usable={d.id: d.max_usable_for_epoch for d in inp.drives},
    )
    if structural:
        return structural

    known_budget = {
        d.id: (inp.evidence[d.id].admissible_free
               if inp.evidence[d.id].is_executable else 0)
        for d in inp.drives
    }
    known = search_feasible(
        graph, candidates, known_budget, state_limit=feasibility_state_limit
    )
    if known.kind == "found":
        return GateBResult("FEASIBLE", assignment=known.first_assignment,
                           capacity_mode=inp.capacity_mode)
    if known.kind == "bound_exhausted":
        return GateBResult("PACKING_INCONCLUSIVE", diagnostics=known.diagnostics,
                           capacity_mode=inp.capacity_mode)

    relevant_unknown = relevant_unknown_drives(inp, candidates)
    if not relevant_unknown:
        return GateBResult("INFEASIBLE_UNDER_ADMISSION_BUDGET",
                           diagnostics=known.proof, capacity_mode=inp.capacity_mode)

    optimistic_budget = dict(known_budget)
    for drive_id in relevant_unknown:
        optimistic_budget[drive_id] = inp.drives[drive_id].max_usable_for_epoch
    optimistic = search_feasible(
        graph, candidates, optimistic_budget, state_limit=feasibility_state_limit
    )
    if optimistic.kind == "found":
        return GateBResult("CAPACITY_EVIDENCE_UNKNOWN",
                           drives=optimistic_drives(optimistic),
                           capacity_mode=inp.capacity_mode)
    if optimistic.kind == "bound_exhausted":
        return GateBResult("PACKING_INCONCLUSIVE",
                           drives=relevant_unknown,
                           diagnostics=optimistic.diagnostics,
                           capacity_mode=inp.capacity_mode)
    return GateBResult("INFEASIBLE_EVEN_AT_OPTIMISTIC_USABLE_CAPACITY",
                       diagnostics=optimistic.proof, capacity_mode=inp.capacity_mode)
```

Preview presents Gate B as an operator decision, not an undifferentiated failure:

| Outcome | Meaning | Operator action |
|---|---|---|
| `FEASIBLE` | One executable assignment fits current admission-authoritative evidence | Review and approve |
| `PACKING_INCONCLUSIVE` | The deterministic feasibility state bound ended before proof | Retry with the reviewed higher bound or reduce the plan; do not infer capacity failure |
| `CAPACITY_EVIDENCE_UNKNOWN` | Known evidence cannot fit, but named unknown drives could change the answer | Mount/fence/reconcile the named drives, then preview again |
| `INFEASIBLE_UNDER_ADMISSION_BUDGET` | Exhaustive known-budget search fails and no relevant unknown drive can help | Add admissible capacity, trim selection, or change an applicable admission policy |
| `INFEASIBLE_EVEN_AT_OPTIMISTIC_USABLE_CAPACITY` | The plan fails even when relevant unknown drives receive their post-safety-floor usable maximum | Add suitable capacity, trim selection, or change hard placement constraints; evidence refresh alone cannot help |
| Structural/policy code | A requirement or hard constraint is impossible independently of current free space | Add a policy-permitted drive or explicitly change the hard requirement/constraint |

The two infeasibility codes are intentionally distinct: `INFEASIBLE_UNDER_ADMISSION_BUDGET` is about
currently admissible budgets; `INFEASIBLE_EVEN_AT_OPTIMISTIC_USABLE_CAPACITY` proves that resolving the
named evidence cannot make the current fleet/policy fit. Neither is reported as raw physical
impossibility beyond the versioned admission and placement constraints.

`search_feasible` orders requirements by constrainedness, then candidate-specific peak,
then stable requirement id; it orders candidates by canonical drive/source/task keys. Its semantic limit
is exactly the number of visited search states. `infeasible` means exhaustive within the finite search
space, not "the heuristic did not find one."

```python
def improve_pure(inp, candidates, first_feasible, state_limit, emergency_caps):
    canonical = first_feasible
    best = canonical
    try:
        for assignment in feasible_assignments(
            inp, candidates, limit=state_limit, emergency_caps=emergency_caps
        ):
            # Earlier tuple elements dominate later ones. Hard objectives 1-3
            # were already enforced by candidate construction + feasibility.
            score = (
                movement_cost(assignment),
                free_space_score(assignment, inp),
                idle_drive_count(assignment),
                canonical_assignment_key(assignment),
            )
            if score < score_of(best):
                best = assignment
    except DeterministicStateLimit:
        return Placement(best, derivation_mode="state_truncated",
                         diagnostic="optimization_truncated")
    except EmergencyResourceLimit:
        # Never publish load-dependent best-so-far state.
        return Placement(canonical, derivation_mode="canonical_fallback",
                         diagnostic="optimization_resource_exhausted")
    return Placement(best, derivation_mode="optimized")
```

Only `FEASIBLE` enters improvement. Commit, start, resume, and the executor never call either search.

### Preview and immutable draft publication

```python
def preview_change(con, request, services) -> PreviewResponse:
    mutation = parse_mutation(request)
    captured = capture_input(
        con, request.plan_id, mutation,
        services.config, services.drive_fences, services.clock.now(),
    )
    graph = requirements_pure(captured.planner_input)
    candidates = candidates_pure(captured.planner_input, graph)
    verdict = gate_b_pure(captured.planner_input, graph, candidates,
                          services.bounds.feasibility_states)
    placement = (
        improve_pure(captured.planner_input, candidates, verdict.assignment,
                               services.bounds.optimization_states,
                               services.bounds.emergency_caps)
        if verdict.code == "FEASIBLE" else None
    )
    normalized = normalize_proposal(
        captured, graph, candidates, verdict, placement, mutation
    )
    expected_hash = hash_proposal(normalized)

    def publish(con):
        if planner_revision(con) != captured.revision:
            return Refusal("PREVIEW_STALE", evidence=current_revision(con),
                           actions=("preview_again",))
        proposal_id = insert_draft(con, normalized, expected_hash)
        # Prove persistence did not reinterpret or reorder solver output.
        require(proposal_hash(con, proposal_id) == expected_hash,
                "PROPOSAL_PERSISTENCE_MISMATCH")
        return proposal_id

    published = immediate_transaction(con, publish)
    if isinstance(published, Refusal):
        return published
    return render_preview(con, published)
```

Draft insertion does not bump `planner_revision`, mutate selection, reserve capacity, or authorize bytes.
Non-feasible outcomes may be retained as diagnostic drafts but are never approvable. The response is
rendered from the persisted authoritative rows, not the pre-insert Python object.

### Approval CAS

Approval takes fresh evidence under sorted drive fences, but does not retain a long-lived execution
lease. It validates the exact stored assignment; it does not ask whether some other assignment fits.

```python
def approve_proposal(con, proposal_id, request, services):
    proposal = load_proposal_rows_read_only(con, proposal_id)
    relevant = proposal_drive_ids(proposal)

    with services.controller_flock.hold(), services.drive_fences.hold_all_sorted(relevant) as fences:
        evidence = observe_exact_capacity(
            proposal, fences, services.clock.now()
        )
        # SQLite cannot freeze an external config file. Read it immediately before
        # the short transaction; start/resume repeats the check before any bytes.
        current_config = services.config.read_graph_affecting_config()

        def approve_tx(con):
            proposal = load_proposal_rows(con, proposal_id)
            if proposal.lifecycle != "draft":
                return Refusal("PROPOSAL_NOT_DRAFT", lifecycle_evidence(proposal),
                               ("preview_again",))
            if proposal.gate_b_outcome != "FEASIBLE":
                return Refusal("PROPOSAL_NOT_FEASIBLE", gate_b_evidence(proposal),
                               ("resolve_blocker", "preview_again"))
            require(proposal_hash(con, proposal_id)
                    == proposal.stored_hash, "PROPOSAL_HASH_MISMATCH")
            if live_session_exists(con):
                return Refusal("FILL_SESSION_ACTIVE", live_owner(con),
                               ("stop_or_pause_fill",))
            if planner_revision(con) != proposal.based_on_revision:
                return Refusal("PREVIEW_STALE", current_revision(con),
                               ("preview_again",))
            if request.mutation != proposal.mutation:
                return Refusal("MUTATION_MISMATCH", mutation_diff(...),
                               ("use_previewed_mutation",))

            # Authoritative even when revision equality was caused by a missed bump.
            current_facts = read_catalog_facts(con, proposal.plan_id, proposal.mutation)
            current_input = assemble_input_pure(current_facts, current_config, evidence)
            current_graph = requirements_pure(current_input)
            current_hashes = semantic_hashes_pure(current_input)
            if current_hashes.approval_input != proposal.semantic_hashes.approval_input:
                return Refusal("APPROVED_INPUT_CHANGED", semantic_diff(...),
                               ("preview_again",))
            if current_graph.requirement_set_hash != proposal.requirement_set_hash:
                return Refusal("APPROVED_INPUT_CHANGED", requirement_diff(...),
                               ("preview_again",))

            exact = validate_assignment_pure(proposal, current_input, current_graph)
            if exact.refusal:
                return exact.refusal

            if proposal.mutation.kind == "adopt_current":
                require(proposal.selection_before_hash == proposal.selection_after_hash,
                        "ADOPT_SELECTION_CHANGED")
            else:
                apply_mutation(con, proposal.mutation,
                               expected_before=proposal.selection_before_hash)
            supersede_approval(con, proposal.plan_id)
            mark_proposal_approved(con, proposal.id)
            set_active_approved_proposal(con, proposal.id)
            bump_planner_revision(con)
            return Approved(proposal.id)

        return immediate_transaction(con, approve_tx)
```

The canonical proposal hash excludes lifecycle fields, so `draft→approved→superseded` never changes the
approved planning identity. Any refusal rolls back selection, proposal lifecycle, active pointer, and
revision together.

### Pure execution projection

```python
def project_pure(proposal, current_input, current_graph, session_overlay):
    if proposal.lifecycle != "approved":
        return Refusal("APPROVAL_MISSING", {}, ("preview_again",))
    if current_graph.requirement_set_hash != proposal.requirement_set_hash:
        return Refusal("APPROVED_INPUT_CHANGED", requirement_diff(...),
                       ("preview_again",))
    if (semantic_hashes_pure(current_input).execution_invariants
            != proposal.semantic_hashes.execution_invariants):
        return Refusal("APPROVED_INPUT_CHANGED", semantic_diff(...),
                       ("preview_again",))

    remaining = []
    for task in proposal.tasks_in_canonical_execution_order:
        baseline_missing = set(proposal.files_missing_for(task))
        baseline_reused = set(proposal.files_reused_for(task))
        now_satisfied = satisfied_files(
            task, current_input.archived, proposal.file_hash_evidence
        )

        if not baseline_reused <= now_satisfied:
            return Refusal("APPROVAL_PROJECTION_VIOLATION",
                           lost_reuse_evidence(task), ("inspect_integrity",))
        missing_now = required_files(task) - now_satisfied
        if not missing_now <= baseline_missing:
            return Refusal("APPROVAL_PROJECTION_VIOLATION",
                           expanded_missing_set(task), ("inspect_integrity",))
        if not missing_now:
            continue

        if task.source_drive is not None and not source_ready_pure(
                task, proposal, current_input.archived, proposal.file_hash_evidence):
            return Refusal("APPROVAL_PROJECTION_VIOLATION",
                           lost_approved_source(task), ("inspect_integrity",))

        budget = remaining_budget_pure(task, missing_now, current_input)
        if budget.exceeds(task.approved_budget):
            return Refusal("APPROVED_PLACEMENT_NO_LONGER_FEASIBLE",
                           budget_overrun(task, budget), ("preview_again",))
        remaining.append(task.with_missing_and_budget(missing_now, budget))

    exact = validate_remaining_pure(
        remaining,
        current_input.evidence,
        expected_drive_identity_epochs=proposal.drive_identity_epochs,
        capacity_mode=proposal.capacity_mode,
    )
    if exact.identity_changed:
        return Refusal("APPROVED_TARGET_IDENTITY_CHANGED", exact.evidence,
                       ("correct_mount", "drive_lifecycle_action", "preview_again"))
    if exact.evidence_unknown:
        return Refusal("CAPACITY_EVIDENCE_UNKNOWN", exact.evidence,
                       ("mount_and_reconcile", "resume_same_approval"))
    if exact.shortfall:
        return Refusal("APPROVED_PLACEMENT_NO_LONGER_FEASIBLE", exact.evidence,
                       ("preview_again",))

    projected = []
    for task in canonical_task_order(remaining):
        if task.repo_id in session_overlay.parked_gated_repos:
            schedule_state = "parked_gated"
        elif dependency_ready(task, proposal, current_input.archived):
            schedule_state = "ready"
        else:
            schedule_state = "waiting_dependency"
        projected.append(task.with_schedule_state(schedule_state))
    return ExecutionProjection(
        proposal_id=proposal.id,
        tasks=tuple(projected),
        projection_hash=canonical_projection_hash(projected),
    )
```

`SemanticHashes.execution_invariants` still covers selection, full manifests, `numcopies`, plan/drive
identity and epoch, lifecycle/eligibility, policy, solver, capacity mode, and compression config. Only
allowlisted archived progress and current capacity evidence vary. Session parking/dependency readiness
changes only `schedule_state`, not the approved task set or completion truth.
`source_ready_pure` accepts a future source only while the exact approved
home task that produces it remains in the same projection and names that source drive. Once the home task
disappears as satisfied, the matching durable source fact must exist; no alternate copy is substituted.

### Start, resume, and terminal session lineage

```python
def start_session(con, proposal_id, predecessor_id, services):
    proposal = load_active_approval(con, proposal_id)
    if proposal is None:
        return Refusal("APPROVAL_MISSING", {"proposal_id": proposal_id},
                       ("preview_again",))
    relevant = proposal_drive_ids(proposal)

    with services.controller_flock.hold(), services.drive_fences.hold_all_sorted(relevant) as fences:
        evidence = observe_exact_capacity(proposal, fences, services.clock.now())
        current_config = services.config.read_graph_affecting_config()

        def acquire_tx(con):
            if live_session_exists(con):
                return Refusal("FILL_SESSION_ACTIVE", live_owner(con), ("wait_or_stop",))
            proposal = require_approval(con, proposal_id)
            require(proposal_hash(con, proposal.id)
                    == proposal.stored_hash, "PROPOSAL_HASH_MISMATCH")
            predecessor = None
            if predecessor_id is not None:
                predecessor = load_session(con, predecessor_id)
                if not is_resumable_terminal(predecessor.state, predecessor.terminal_code):
                    return Refusal("SESSION_NOT_RESUMABLE", session_terminal_evidence(predecessor),
                                   ("start_or_preview",))
                if predecessor.approved_proposal_id != proposal.id:
                    return Refusal("RESUME_APPROVAL_MISMATCH", approval_lineage_diff(...),
                                   ("start_or_preview",))

            current_facts = read_catalog_facts(con, proposal.plan_id, NO_CHANGE)
            current_input = assemble_input_pure(current_facts, current_config, evidence)
            current_graph = requirements_pure(current_input)
            projected = project_pure(
                proposal, current_input, current_graph, EMPTY_SESSION_OVERLAY
            )
            if isinstance(projected, Refusal):
                return projected

            token = allocate_next_fencing_token(con)
            session = insert_new_session(
                con,
                state="starting",
                proposal_id=proposal.id,
                resumed_from_session_id=(predecessor.id if predecessor else None),
                fencing_token=token,
                bound_planner_revision=planner_revision(con),
                lease_expiry=services.clock.now() + services.lease_ttl,
            )
            return SessionStart(session, projected)

        acquired = immediate_transaction(con, acquire_tx)

    if isinstance(acquired, Refusal):
        return acquired
    try:
        transition_session_token_cas(acquired.session, "starting", "running")
        require(services.worker.start(acquired.session, acquired.projection).ok,
                "PROCESS_LOCAL_WORKER_REFUSED")
        return acquired.session
    except BaseException as exc:
        terminalize_session_token_cas(
            acquired.session, "failed", "WORKER_START_FAILED", bounded(exc)
        )
        raise
```

The partial unique live-session constraint is the final defense against two starters. A predecessor row
is never updated back to live. A new session always gets a strictly greater fencing token, including
systemd auto-resume.

```python
def renew_lease(con, session, now, ttl):
    # owner_identity includes host boot id + pid + process start identity so PID
    # reuse cannot impersonate the controller. This is control state, not graph state.
    return immediate_transaction(con, lambda con: heartbeat_token_cas(
        con, session.id, session.fencing_token,
        expected_state={"starting", "running", "stopping"},
        owner_identity=session.owner_identity,
        heartbeat=now,
        lease_expiry=now + ttl,
    ))

def request_stop(con, session):
    return immediate_transaction(con, lambda con: transition_session_token_cas(
        con, session.id, session.fencing_token,
        expected_state={"starting", "running"}, new_state="stopping",
    ))

def end_session(con, session, state, code, evidence):
    require(state not in LIVE_STATES, "TERMINAL_STATE_REQUIRED")
    require(no_drive_lock(session), "NOT_AT_SAFE_BOUNDARY")
    return immediate_transaction(con, lambda con: update_session_token_cas(
        con,
        session_id=session.id,
        token=session.fencing_token,
        expected_state=LIVE_STATES,
        new_state=state,
        terminal_code=code,
        evidence=bounded(evidence),
        heartbeat=None,
        lease_expiry=None,
        terminal_at=clock.now(),
    ))
```

Terminalization clears live authority but does not bump the planner revision because it changes no graph
fact. Any preceding archive/dirty/anchor change already advanced the revision through `session_write`.

### Fixed-map executor and per-file divergence

```python
def execute_session(ctx, session, initial_projection):
    overlay = SessionOverlay()
    projection = initial_projection
    retry_task_id = None
    while True:
        if stop_requested(session):
            transition_to_stopping(session)
            return end_at_boundary(ctx, session, "stopped", "STOPPED_BY_REQUEST")

        runnable = [t for t in projection.tasks if t.schedule_state == "ready"]
        if not runnable:
            if projection.tasks and all(
                    t.schedule_state == "parked_gated" for t in projection.tasks):
                return end_session(ctx.con, session, "done", "PLAN_COMPLETE_WITH_FOLLOWUPS",
                                   gated_followups(projection))
            if projection.tasks:
                return end_session(ctx.con, session, "failed", "GRAPH_DEPENDENCY_DEADLOCK",
                                   dependency_diagnostics(projection))
            return end_session(ctx.con, session, "done", "PLAN_SATISFIED", {})

        # Proposal execution order groups ready work by exact approved target/source
        # drive set; a batch is the maximal next such group. This is scheduling,
        # never placement.
        batch = next_drive_batch(runnable, retry_first=retry_task_id)
        retry_task_id = None
        refresh_now = False
        for task in batch.tasks:
            required_drives = canonical_drive_ids(
                [task.target_drive] + ([task.source_drive] if task.source_drive else [])
            )
            unavailable = [d for d in required_drives if not drive_present(d)]
            if unavailable:
                if ctx.guided:
                    for drive_id in unavailable:
                        await_drive(drive_id)
                    refresh_now = True
                    break
                return end_session(ctx.con, session, "blocked", "DRIVE_UNAVAILABLE",
                                   evidence={"drives": unavailable})

            for file in tuple(task.missing_files_in_order):
                mutated_drives = transport_mutated_drives(task)
                with drive_mutation(session, mutated_drives) as mutation:
                    # The same sorted physical fence remains held from this df through
                    # staging, publish, annex/catalog writes, reconciliation, and anchor.
                    preflight = preflight_file_pure(
                        task, file, mutation.fresh_capacity_evidence(task.target_drive)
                    )
                    if preflight.shortfall:
                        outcome = typed_capacity_pause(preflight.evidence)
                    else:
                        outcome = transport_file(
                            exact_task=task,
                            exact_file=file,
                            target=task.target_drive,
                            source=task.source_drive,
                            mutation_writer=mutation,
                        )
                if outcome.is_repo_gated:
                    action = gated_decision(outcome.repo_id)
                    require(action in {"retry", "skip", "timeout"}, "GATED_ACTION_INVALID")
                    if action == "retry":
                        # DEC-047 owns prompt retry order and budget exemption.
                        # Retry this exact approved task before normal ordering advances.
                        retry_task_id = task.id
                    else:  # skip or timeout
                        overlay.parked_gated_repos.add(outcome.repo_id)
                    refresh_now = True
                    break
                if outcome.is_typed_pause_or_failure:
                    return end_from_transport(ctx, session, outcome)
                task = task.with_file_satisfied(file)  # cheap local monotonic progress
            if refresh_now:
                break

        # Full authoritative reconstruction occurs once per completed drive batch
        # and immediately after a gated/hot-swap/evidence-repair event, not per file.
        projection = refresh_projection(ctx, session, overlay)
        if isinstance(projection, Refusal):
            return end_from_refusal(ctx, session, projection)
```

`transport_file` retains DEC-046/047 and INC-018–021 behavior. Its arguments contain no
candidate set, solver, or alternate target. Per-file ENOSPC, compression-ratio overrun, raw-fallback
shortfall, or changed evidence stops at the approved target even when another drive could fit.
`gated_decision` remains the sole owner of DEC-047 matching retry/skip/timeout policy: retry consumes no
generic network/task-failure budget and re-attempts the same exact approved task before canonical
scheduling advances; skip/timeout parks the repository and allows other approved work.

`refresh_projection` performs the full fact read, semantic-hash recomputation, current-evidence capture,
and `project_pure` call. Start/resume already performs one before worker launch. During execution it runs
at completed drive-batch boundaries and typed state-changing events, while every file retains the cheap
identity/token/live-free preflight under its physical fence. Durable success updates the batch-local
missing set immediately, so coarser refresh never repeats a completed file. This cadence is a specified
performance contract: for one uninterrupted run, full projections are bounded by the initial projection
plus one per completed maximal drive batch and one per typed refresh event—not by task or file count.

### Drive mutation, progress, and clean-anchor publication

```python
@contextmanager
def drive_mutation(session, drive_ids):
    with exclusive_drive_flocks_sorted(drive_ids):
        generations = session_write(
            con, session.id, session.token,
            lambda con: advance_dirty_generations(
                con, drive_ids,
                owner_session_id=session.id,
                owner_fencing_token=session.token,
            ),
        )
        try:
            writer = MutationWriter(
                # Every archived/annex/catalog mutation validates token and rolls
                # planner_revision + session.bound_planner_revision atomically.
                catalog_write=lambda op: session_write(
                    con, session.id, session.token, op
                ),
                fresh_capacity_evidence=read_fenced_live_evidence,
            )
            yield writer
            reconciled = {
                d: reconcile_drive(d) for d in drive_ids
            }
            candidate_anchors = {
                d: read_reconciled_free(d) for d in drive_ids
            }
            session_write(
                con, session.id, session.token,
                lambda con: publish_anchors_cas(
                    con, drive_ids, generations, reconciled, candidate_anchors,
                ),
            )
        except BaseException:
            # No finally-clear: crash/error evidence must stay dirty.
            raise
```

The sorted drive locks span dirty publication through filesystem work, catalog/annex reconciliation, and
clean-anchor publication for every filesystem the transport may mutate. If token/revision/generation CAS
fails, the candidate anchors are discarded and affected drives stay dirty. Download staging,
publication, annex repair, and archived-row writes remain in their existing transport order inside this
boundary.

Non-Fill filesystem operations use the same machinery under operator authority:

```python
@contextmanager
def operator_drive_mutation(drive_ids):
    with same_host_controller_flock.hold(), exclusive_drive_flocks_sorted(drive_ids):
        generations = graph_write(
            con, lambda con: advance_dirty_generations(
                con, drive_ids, owner_session_id=None, owner_fencing_token=None
            )
        )
        try:
            yield MutationWriter(
                catalog_write=lambda op: graph_write(con, op),
                fresh_capacity_evidence=read_fenced_live_evidence,
            )
            reconciled, anchors = reconcile_and_observe_all(drive_ids)
            graph_write(
                con,
                lambda con: publish_anchors_cas(
                    con, drive_ids, generations, reconciled, anchors,
                ),
            )
        except BaseException:
            # Process death releases flocks; durable dirty generations survive.
            raise
```

Registration, restore, cleanup, explicit copy removal, lifecycle repair, and mutating probes enter this
scope. Catalog-only operator mutations need only `graph_write`.

### Plan switching and expired-session recovery

```python
def switch_active_plan(con, requested_plan_id):
    def tx(con):
        current = read_active_plan_id(con)
        if current == requested_plan_id:
            return current                       # true no-op: no revision bump
        if live_session_exists(con):
            return Refusal("FILL_SESSION_ACTIVE", live_owner(con),
                           ("stop_or_pause_fill",))
        if not plan_exists(con, requested_plan_id):
            return Refusal("PLAN_NOT_FOUND", {"plan_id": requested_plan_id},
                           ("list_plans",))
        supersede_approval(con, current)
        clear_approval(con)
        set_active_plan_exact(con, requested_plan_id)
        bump_planner_revision(con)
        return requested_plan_id
    with same_host_controller_flock.hold():
        return immediate_transaction(con, tx)

def recover_session(con, session_id, services):
    session = load_session(con, session_id)
    if session.state not in LIVE_STATES or not session.lease_expired:
        return Refusal("SESSION_NOT_EXPIRED", session_lease_evidence(session),
                       ("wait", "inspect_session"))

    # Dirty ownership records session id/token with the generation. If no dirty
    # generation belongs to this session, the dirty-before-allocation invariant
    # proves it published no filesystem mutation requiring recovery.
    touched = owned_dirty_drives(session.id, session.fencing_token)
    with services.controller_flock.hold():
        locks = services.drive_fences.try_hold_all_sorted(touched)
        if not locks.all_held or services.process_probe.owner_still_alive(session.owner):
            return Refusal("STALE_WRITER_NOT_EXCLUDED", lock_and_process_evidence(...),
                           ("wait", "operator_recovery"))

        with locks:
            def terminate_tx(con):
                current = require_expired_session(
                    con, session.id, session.fencing_token
                )
                for drive_id in touched:
                    preserve_dirty_generation(con, drive_id,
                                              reason="expired_session_recovery")
                bump = bump_planner_revision(con)
                close_session_row(
                    con, current, state="failed", code="SESSION_LEASE_EXPIRED",
                    bound_planner_revision=bump,
                )
            immediate_transaction(con, terminate_tx)

    return RecoveryRequired(
        drives=touched,
        actions=("identity_prove", "reconcile", "publish_clean_anchor", "resume_new_session"),
    )
```

Recovery never steals past a held physical lock and never reuses the expired token or row. Reconciliation
occurs after the old session is non-live, so it uses the normal operator graph-write path and cannot
deadlock on the execution lease.

### Canonical proposal serialization

```python
def canonical_proposal_payload(rows) -> bytes:
    payload = {
        "serializer_version": rows.serializer_version,
        "plan_id": rows.plan_id,
        "based_on_revision": rows.based_on_revision,
        "mutation": canonical_mutation(rows.mutation),
        "selection_before_hash": rows.selection_before_hash,
        "selection_after_hash": rows.selection_after_hash,
        "requirement_set_hash": rows.requirement_set_hash,
        "semantic_hashes": sorted_map(rows.semantic_hashes),
        "capacity_mode": rows.capacity_mode,
        "policy_versions": sorted_map(rows.policy_versions),
        "gate_b": canonical_gate_b(rows.gate_b),
        "derivation_mode": rows.derivation_mode,
        "tasks": [canonical_task(t) for t in sort_tasks(rows.tasks)],
        "files": [canonical_file(f) for f in sort_files(rows.files)],
    }
    # lifecycle, approved_at, superseded_at, heartbeat, and diagnostics display
    # timestamps are audit metadata, not reviewed planning identity.
    return utf8_json(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)

def proposal_hash(con, proposal_id):
    rows = read_proposal_rows(con, proposal_id, explicit_order_by=True)
    return sha256(canonical_proposal_payload(rows)).hexdigest()
```

Golden vectors include nulls, empty file sets, Unicode repository paths, large byte integers, shuffled
insertion order, every task kind, and all three derivation modes. A serializer-version change adds a new
reader; it never rewrites an already approved proposal.

### Control-layer failure taxonomy

| Code | Class | Required handling |
|---|---|---|
| `PREVIEW_STALE` | expected concurrency | Preserve draft diagnostics; preview again |
| `FILL_SESSION_ACTIVE` | expected exclusion | Wait or stop/pause Fill before graph mutation/approval/start |
| `PROPOSAL_NOT_DRAFT` / `PROPOSAL_NOT_FEASIBLE` | expected request state | Do not mutate selection; use/produce an approvable preview |
| `MUTATION_MISMATCH` | request mismatch | Refuse the unreviewed mutation |
| `APPROVAL_MISSING` | expected lifecycle/migration | Preview and approve before Fill |
| `APPROVED_INPUT_CHANGED` | semantic drift | Fresh preview; never reuse placement implicitly |
| `CAPACITY_EVIDENCE_UNKNOWN` | recoverable evidence | Release live session, mount/reconcile, then start a new session against the same approval if it still fits |
| `APPROVED_TARGET_IDENTITY_CHANGED` | identity drift | Correct the mount or perform explicit lifecycle action; otherwise preview again |
| `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE` | budget/evidence drift | Preserve progress and preview again; never remap |
| `APPROVAL_PROJECTION_VIOLATION` | integrity/implementation defect | Fail closed and inspect; no automatic recovery |
| `SESSION_NOT_RESUMABLE` / `RESUME_APPROVAL_MISMATCH` | expected lineage mismatch | Start from current approval or preview again; never reactivate the row |
| `SESSION_REVISION_DIVERGED` / `PROPOSAL_HASH_MISMATCH` | integrity/concurrency defect | Stop writes, retain dirty evidence, inspect the unsupported writer/corruption |
| `STALE_WRITER_NOT_EXCLUDED` | recovery safety | Do not take over while process/physical-lock evidence remains |
| `SESSION_LEASE_EXPIRED` | recovered crash terminal | Keep affected drives dirty until reconcile; resume in a new row/token |
| `GRAPH_DEPENDENCY_DEADLOCK` | graph defect | Fail closed with requirement/source diagnostics |

Existing Gate-B verdicts remain planning outcomes rather than execution-session terminals. Existing
transport/auth/gated/watchdog codes retain their DEC-defined meanings and are not renamed by this RFC.

### Pseudocode-to-phase ownership

| Delivery phase | Pseudocode introduced there |
|---|---|
| #35 | catalog fact/evidence split; `graph_write`; drive fences, dirty generations, clean anchors; nullable session/token ownership fields reserved for #39 |
| #36a | pure requirements, complete satisfaction, partial/fresh candidates, dependency source references |
| #38 | Gate-B ladder, bounded feasibility, deterministic `tiered_v2`, normalized assignment output |
| #37 schema/gating | lifecycle/eligibility facts and their writer guards; no lifecycle operations yet |
| #39 | proposal persistence/hash, approval CAS, `adopt_current`, batch/event projection, sessions, fixed-map executor entry; populate #35's reserved dirty-owner fields |
| #37 operations | reuse `graph_write` and the approved lifecycle invariants; no solver/executor changes |

Each phase may introduce compatibility façades around these functions, but it must not implement a later
phase's authority implicitly. In particular, #35 does not create proposal/session tables, #36a does not
choose placement, #38 does not alter the transport, and #39 does not redesign fetch/replica mechanics.

## Protected transport behavior

This RFC changes planning/control around the executor, not the transport implementation. Initial work
keeps `fetch.py`, download/compression workers, replica operations, and gated broker in place. Moving code
between files is not a goal and receives no credit unless required by a tested ownership boundary.

The following behavior is release-blocking preservation:

| Incident/decision | Behavior that must remain |
|---|---|
| DEC-019/023 | Stop at safe file boundaries; daemon/process death loses only ephemeral work; explicit resume |
| DEC-045 | Exact manifests; durable archived facts determine completion; per-file preflight |
| DEC-046 / INC-018/019 | Credential preflight; deterministic same-filesystem staging; hash/canary before atomic publish; proof-driven annex-placeholder repair; local errors are not network retries |
| DEC-047 / INC-020 | First gated notice, bounded matching retry/skip prompt, session park, typed Verify follow-up |
| INC-021 | Recursive nested `.incomplete` liveness and cleanup |
| DEC-022 | Bounded compression output and safe raw fallback |
| DEF-022 | Offline source/target is a resumable pause, not false missing/corrupt data |

### Regression/fault matrix

Before control-flow conversion, characterization tests freeze:

- stop/process death during download and compression;
- nested-path long download watchdog progress and orphan cleanup;
- invalid global credential versus repository-specific gate;
- gated retry, skip, timeout, and later successful resolution;
- hash mismatch, canary failure, compressor crash/hang/output-cap raw fallback;
- upstream-hashless downloaded file with an approved-size mismatch fails before publish;
- target ENOSPC/read-only/I/O error with no network cooldown;
- same-filesystem staging, absent/identical/proven-placeholder/conflicting publication targets;
- crash before publish, after publish/before annex, after annex/before archived row;
- replica source unavailable and target-UUID proof failure;
- hot-swap await, operator stop, throttle, and terminal persistence;
- per-file admission divergence returning reapproval rather than invoking placement.

Copied-catalog shadow replay proves graph/proposal/projection and migration behavior; it does not prove
transport/stall/crash behavior. The latter requires focused fault injection plus RFC-001-style attended
installed-runtime acceptance.

## Migration

This is the largest catalog/control change since the DuckDB→SQLite migration and is RFC-001-grade.

### Preconditions

- Complete review of this RFC, the binding DEC, issue rewrites, schema migration, rollback procedure,
  compatibility matrix, and fault-preservation tests.
- Prefer that the current GLM-5.2/BF16 Fill drain under the old executor.
- Stop systemd portal/Fill and every CLI writer at a safe boundary; confirm no worker/lock remains.
- Create raw SQLite+WAL/SHM preservation, a consistent SQLite backup, portable SQL dump, hashes, row
  counts, schema version, and immutable migration manifest.
- Exercise migration only on a copied catalog first.

### Publication behavior

- Apply schema changes transactionally with a monotonic `user_version`.
- Create `planner_state` with a revision but **no active approved proposal**.
- Preserve selection, files, archived facts, plan membership, and capacity mode exactly.
- Do not fabricate proposal rows from current planner output.
- Do not fabricate clean anchors or drive lifecycle evidence.
- Validate integrity, foreign keys, proposal constraints, counts, hashes, and idempotent migration replay.
- Publish the migrated catalog atomically; old binaries must refuse the newer schema.

### First start after cutover

- Start the installed portal without `--resume`.
- Surface “approval required after migration,” not a generic error or empty plan.
- Reconcile/mount required drives whose #35 evidence remains unknown.
- Derive a read-only preview of the current remaining 390-model/GLM work.
- Require explicit operator approval before enabling Fill.
- Confirm already archived files are absent from the projection and no target changed silently.

### Rollback

Before any post-migration archive write, rollback may restore the reviewed old binary plus consistent
pre-migration catalog snapshot. After new writes, rollback requires preserving those bytes/catalog facts
and a separately reviewed forward/repair procedure; never open the new schema with the old binary.

Rollback instructions name exact files, service state, hash checks, and the point of no simple return.
No destructive cleanup of backups occurs during RFC acceptance.

## Delivery sequence

0. Approve RFC-002; record the binding DEC/invariants; rewrite/split issues with failure codes,
   migrations, and test matrices.
1. Ship the independently reviewable portal mutation guard to `main`.
2. Deliver #35 immediately, introducing the evidence/fact-reader/write-mutation seams it needs rather
   than preceding it with architecture-only scaffolding.
3. Implement #36a while extracting pure requirement and candidate construction; retain compatibility
   façades and shadow comparisons.
4. Implement #38 as the pure feasibility/`tiered_v2` solver over explicit input and candidates.
5. Add the minimal #37 lifecycle/eligibility columns and planner gating with active/enabled migration
   defaults; defer operator operations.
6. Implement #39 proposal rows, canonical hash, revision CAS, pure projection, minimal session/lease, and
   explicit evidence-divergence UX.
7. Implement remaining #37 exclude/lost/reinstate/retire/drop-copy operations as separate reviewable PRs.
8. Run copied-catalog migration/shadow evidence, protected-transport fault matrix, installed-wheel tests,
   and operator-attended cutover/rollback acceptance before final integration.

Steps 2–6 live on the isolated fix branch. Until step 6 lands, the end-state guarantee does **not** exist:
the independent portal guard covers only portal selection finalize/removal/clear, not discover/manifest
refresh, protect/`numcopies`, plan selection/membership/capacity mode, drive edits, or external CLI
writers; the old executor also continues re-planning at drive-batch boundaries. Interim operation
therefore requires explicit single-operator discipline forbidding those mutations while Fill runs. This
is risk containment, not proof against the silent-drift class. State the residual in operator docs/tests,
merge `main` into the fix branch regularly, and publish no partial migration set to public `main` beyond
the independent guard.

## Compatibility and façade removal

- Existing CLI/API/UI projection shapes remain behind adapters while their authority moves to the pure
  core and normalized proposals.
- `reconcile_plan(con, ...)` may remain as a façade that invokes fact-reader + pure core; tests target the
  pure functions directly.
- `plan_capacity(con, ...)` may remain temporarily for diagnostics/shadow parity but cannot be called by
  the approved executor.
- `fill.execute()` retains incident behavior while planning entry is replaced by projection input.
- Legacy aliases remain only for their already-promised compatibility release.

Remove a façade only after call-site inventory proves no execution authority remains, CLI/API/UI
projections agree, copied-catalog replay passes, and the replacement has direct tests.

## Acceptance

### Pure-core acceptance

- Synthetic `PlannerInput` tests require no SQLite, config file, mount, clock, or network.
- Shuffled input/query order yields identical requirements, candidates, verdict, assignment, and hash.
- Adversarial packing distinguishes proven infeasible, evidence unknown, and bounded inconclusive.
- A requirement no larger than raw capacity but larger than every policy-permitted drive's **usable
  post-safety-floor capacity** is structural infeasibility, never `CAPACITY_EVIDENCE_UNKNOWN`; optimistic
  unknown-drive search uses that same usable maximum.
- 10k-candidate planning remains within the reviewed performance/memory bound.
- Candidate reuse never becomes a pin and infeasible reuse loses to a feasible fresh target.
- A pending protected-home requirement is the replica candidate's source reference; solving resolves it
  to one exact source drive without a pre-placement DB lookup.
- Optimizer truncation/fallback is deterministic and approval stores the exact chosen assignment.

### Proposal/control acceptance

- Draft rows/tasks/files publish atomically and hash deterministically.
- The immutable planning hash is unchanged by draft→approved→superseded lifecycle/timestamp updates.
- Revision, selection, mutation, hash, or evidence drift makes approval fail without selection change.
- `adopt_current` produces and approves an unchanged-selection proposal without a spurious selection
  edit, including the first post-migration approval; all ordinary CAS/semantic/capacity checks still run.
- Simulated graph-fact drift without a revision bump is rejected by authoritative requirement/hash
  recomputation at approval and start.
- Every graph writer bumps unless canonical equality proves a no-op; tests cover safe false-positive
  invalidation and reject a false-negative `proven_noop` implementation.
- Live-free/anchor evidence changes are evaluated by exact-assignment admission and do not fail merely
  because their values or observation timestamps differ from preview.
- Selection mutation + approval + active pointer + revision publish atomically.
- At most one approved-active proposal exists per plan; old approval becomes superseded.
- Approved proposal planning rows reject update/delete; draft GC cannot touch referenced/approved rows.
- Portal, CLI, second portal, and systemd resume share the same session exclusion.
- Start, approval, recovery, and operator drive mutation obey controller-flock → sorted-drive-flocks →
  short-transaction order; no path waits on a physical lock while holding a SQLite write transaction.
- A long operator filesystem mutation prevents session start without holding a SQLite transaction across
  filesystem work; a crash releases flocks and leaves its generation dirty.
- #35 migration creates the paired nullable dirty-owner fields before sessions exist; #39 session writes
  populate both, and expired-session recovery selects only the matching id/token generations.
- Portal/CLI processes using the same catalog but different state directories still contend on one
  catalog-derived controller lock; drive relabel/remount does not change its identity-derived lock key.
- Starting/running/stopping sessions reject every listed operator graph writer; paused/blocked sessions
  release lease/locks so reconciliation can clear evidence without deadlock.
- Resume never reactivates a terminal row: it creates one successor with a fresh fencing token, preserves
  the predecessor, and rejects a second live successor for the same or any other approval.
- Active-plan switch is rejected while live, then supersedes/clears approval and requires fresh approval
  when performed non-live.
- Expired session cannot bypass a held `flock`; forced recovery leaves dirty evidence.
- Worker graph writes advance planner and session-bound revisions atomically; heartbeat/terminal-only
  writes advance neither.

### Projection/runtime acceptance

- Progress removes work only; repeated projection is deterministic.
- Full projection occurs at start/resume, drive-batch boundaries, and typed state-changing events—not
  after each file/task. Before its implementation PR, #39 pins a numerical p95 budget from the copied
  catalog baseline; a 390-model/thousands-file run must meet it and prove the call count is bounded by
  initial start + completed maximal batches + typed refresh events.
- Gated session parking, hot-swap await, stop, and throttle do not remap or invalidate approval.
- DEC-047 retry re-attempts the same approved task before normal scheduling advances and consumes no
  generic failure budget; skip/timeout parks it and permits other approved work.
- Replica dependency readiness never substitutes a source: the exact approved source is either produced
  by its approved home task or proven by the matching durable fact, and offline source follows GATE-A.
- Dirty→clean evidence refresh continues the same target only if it fits.
- Known target shortfall returns `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE`, even when another drive fits.
- Compression-aware actual-ratio overrun reaches that same terminal and never invokes placement.
- Per-file authoritative free observation shares one sorted physical-fence scope with dirtying,
  transport mutation, reconciliation, and clean-anchor publication for every mutated drive.
- Changed identity/epoch/manifest/policy or expanded task returns fresh-preview failure.
- Executor has no import/call path to placement optimization.

### Migration/attended acceptance

- Backup-first copied migration, integrity/FK/count/hash checks, rollback rehearsal, and idempotent replay.
- Migrated catalog has no fabricated approval or clean anchor.
- First portal start is non-resuming and visibly requires approval.
- Operator reviews current remaining work and approves it through `adopt_current` once at the idle
  cutover boundary.
- Existing archived progress remains satisfied; no completed file is fetched again.
- Protected transport fault matrix passes before one controlled real Fill continuation.
- Operator reviews the first evidence-divergence terminal and recovery UX before broad resume.

## Stop conditions

Stop implementation/integration on any of:

- proposal rows, computed API projection, and stored canonical hash disagree;
- a projection expands or remaps approved work;
- commit/start invokes optimization;
- an unapproved/migrated selection starts Fill;
- an operator graph mutation succeeds while a live session owns authority, or a non-live paused session
  cannot run the reconciliation needed for recovery;
- a terminal session row is reactivated or two live sessions reference one approval;
- active-plan switch retains/reactivates an approval from either plan;
- a capacity/identity drift silently changes target;
- old transport behavior loses a typed terminal, safe boundary, hash/canary, watchdog, gated follow-up,
  or atomic publication guarantee;
- migration fabricates evidence, loses archived truth, cannot roll back, or requires opening a newer
  schema with an old binary;
- copied-catalog or attended evidence is unavailable for final integration.

## Deferred follow-ups

- Provider commit pinning and reproducible whole-repository acquisition.
- Per-plan/finer-grained planner revisions if global false invalidation becomes material.
- Distributed fencing/cross-host NAS writers.
- Automated draft proposal GC beyond a safe manual/retention policy.
- Persisted partial-continuation approvals (DEF-028).
- Multi-plan concurrent execution.

## Approval record

Architecture review approved all seven decisions below after the session-state mutation matrix,
active-plan switching, authoritative semantic recomputation, interim residual, compression-overrun
terminal, and usable-capacity precedence were made explicit:

1. the operator-visible stop-and-repreview behavior;
2. one-time post-migration reapproval at a Fill-idle boundary;
3. five-table normalized proposal/control schema and computed-hash rule;
4. pure projection monotonicity contract;
5. minimal same-host session/fencing model;
6. protected-transport no-rewrite boundary and fault/attended acceptance;
7. provider commit pinning as an explicit follow-up rather than this RFC.
