# RFC-002: First-class placement approval and execution control

- **Status:** accepted — implementation not started; binding DEC and issue rewrites next
- **Date:** 2026-07-20
- **Owners:** Auspex-Aerie + operator
- **Related:** DEC-019, DEC-022, DEC-023, DEC-026, DEC-030, DEC-031, DEC-034, DEC-036, DEC-037,
  DEC-040, DEC-042, DEC-045, DEC-046, DEC-047, DEF-022, DEF-028, DEF-029, INC-014,
  INC-018, INC-019, INC-020, INC-021, RFC-001, issues #35–#39
- **Working plan:** `docs/plans/placement-capacity-hardening.md`
- **Review boundary:** this RFC fixes architecture, behavior, migration, and acceptance scope. Detailed
  pseudocode and implementation sequencing inside each phase follow only after RFC review.

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
SHA-256. Store only that hash.

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
projection. If valid, it atomically acquires `execution_sessions`, binds the current revision/token, then
invokes existing exact-task scheduling. It does not call the solver.

At safe batch/file boundaries, durable progress may trigger another projection against the same approval.
That projection can shrink tasks only. Every non-live terminal releases the live-session constraint;
`paused`/`blocked` approval remains available for a valid later resume.

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

Every ModelArk filesystem mutation—including mutating probes, directory/temp/staging creation, cleanup,
annex bookkeeping, download, replica, restore, and registration—enters one write-mutation boundary:

1. acquire same-host drive `flock` and validate session token where applicable;
2. durably advance/mark the dirty generation before allocation;
3. perform existing operation and catalog writes with token checks;
4. reconcile filesystem/catalog/annex state;
5. publish a clean anchor and clear only the same generation while the lock remains held.

Await polling should use non-mutating presence checks. A required mutating writability probe occurs only
inside this boundary. Cross-host concurrent writers remain unsupported rather than motivating a
distributed-lock subsystem.

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
- Optimizer truncation/fallback is deterministic and approval stores the exact chosen assignment.

### Proposal/control acceptance

- Draft rows/tasks/files publish atomically and hash deterministically.
- Revision, selection, mutation, hash, or evidence drift makes approval fail without selection change.
- Simulated graph-fact drift without a revision bump is rejected by authoritative requirement/hash
  recomputation at approval and start.
- Selection mutation + approval + active pointer + revision publish atomically.
- At most one approved-active proposal exists per plan; old approval becomes superseded.
- Approved proposal planning rows reject update/delete; draft GC cannot touch referenced/approved rows.
- Portal, CLI, second portal, and systemd resume share the same session exclusion.
- Starting/running/stopping sessions reject every listed operator graph writer; paused/blocked sessions
  release lease/locks so reconciliation can clear evidence without deadlock.
- Active-plan switch is rejected while live, then supersedes/clears approval and requires fresh approval
  when performed non-live.
- Expired session cannot bypass a held `flock`; forced recovery leaves dirty evidence.

### Projection/runtime acceptance

- Progress removes work only; repeated projection is deterministic.
- Gated session parking, hot-swap await, stop, and throttle do not remap or invalidate approval.
- Dirty→clean evidence refresh continues the same target only if it fits.
- Known target shortfall returns `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE`, even when another drive fits.
- Compression-aware actual-ratio overrun reaches that same terminal and never invokes placement.
- Changed identity/epoch/manifest/policy or expanded task returns fresh-preview failure.
- Executor has no import/call path to placement optimization.

### Migration/attended acceptance

- Backup-first copied migration, integrity/FK/count/hash checks, rollback rehearsal, and idempotent replay.
- Migrated catalog has no fabricated approval or clean anchor.
- First portal start is non-resuming and visibly requires approval.
- Operator reviews current remaining work and approves once at the idle cutover boundary.
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
