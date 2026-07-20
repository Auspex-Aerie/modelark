# Placement & capacity hardening

Working plan for the fix effort opened after the 2026-07-20 placement/capacity audit, revised across
review rounds. This document drives the effort; the binding invariants will be recorded in the
decision log, and the GitHub issues will be rewritten/split to agree with it, **before any
implementation begins**.

## Origin

A mid-fill add of `zai-org/GLM-5.2` (BF16, 1.51 TB) exposed a cluster of drift-between-model-and-
reality problems. The immediate incident was resolved operationally (un-pinned GLM-5.2 by deleting 9
aborted-start stub files; restored `free_bytes` to its baseline contract). This effort addresses the
underlying classes so they stop recurring.

## Issues (GitHub — to be rewritten/split to match this plan + the DEC before implementation)

| # | Title |
|---|---|
| [#35](https://github.com/Auspex-Aerie/modelark/issues/35) | Free-space is a mutable field and the recompute is never persisted |
| [#36](https://github.com/Auspex-Aerie/modelark/issues/36) | Durable-partial pinning is disproportionate |
| [#37](https://github.com/Auspex-Aerie/modelark/issues/37) | No retire / un-archive / drive-loss recovery — **splits into 37a–37d** |
| [#38](https://github.com/Auspex-Aerie/modelark/issues/38) | Placement consolidation strands small drives / wedges large blocks |
| [#39](https://github.com/Auspex-Aerie/modelark/issues/39) | Mid-fill add has no pre-commit preview |

Deferred on the roadmap, not re-filed: cross-drive shard spanning; multi-RAID copy-#1 home.

## Invariants — recorded in the decision log FIRST (before any code, incl. the mutation guard)

A `DEC-###` entry is authored and merged **before** the first implementation PR, capturing:

1. **Free-space evidence** — `baseline_free_bytes` is identity-scoped and immutable **per identity-and-
   capacity epoch**; a filesystem/NAS resize is an explicit, audited epoch transition (so an expanded
   volume can gain free space without a mutable current-free field). Current free is *derived* with a
   labelled `free_evidence` (`live` / `baseline_estimate` / `stale` / `unknown`); no writable current-
   free field. A volume's offline estimate is marked **dirty** before the first potentially-allocating
   write and cleared only after a mounted reconciliation proves staging/orphans clean and catalog↔annex
   synchronized. **Dirty ⇒ `unknown`.** Any `unknown`/missing `stored_bytes` ⇒ offline free `unknown`;
   a NULL `stored_bytes` is *missing evidence*, never treated as a proven zero-length object. Drift
   comparison correlates observations against a catalog write-watermark (or a synchronous live read).
2. **Durable truth is never auto-deleted** — a verified `archived` row (complete, published file) is
   removed only by an explicit, scoped, confirmed operation. No threshold-triggered deletion.
3. **Drive states are distinct** — `available/offline(shelved)` (valid + eligible under hot-swap),
   `excluded` (no new placement, existing verified copies still count), `lost` (copies no longer count,
   repair generated), `retired` (proven empty of deps, **tombstoned and identity/label permanently
   reserved — never deleted**). Offline ≠ lost (decision_log.md:813).
4. **Gate B never reports a false infeasible** — three deterministic outcomes: `FEASIBLE`,
   `PROVEN_INFEASIBLE` (only after exhaustive search within bounds), `PACKING_INCONCLUSIVE` (bound
   exhausted). Gate B refuses start/commit on `PACKING_INCONCLUSIVE` reporting "not proven" — it neither
   proceeds nor calls the plan infeasible.
5. **Placement policy is versioned** — ships as named `tiered_v2`, not a silent change to `tiered_v1`,
   with operator-facing acknowledgement.
6. **Serialized control** — operator graph/selection mutations, preview-commit, and Fill start are
   serialized under a shared controller lock/lease. Preview→commit is atomic (CAS on the revision).
   While Fill owns a derived graph, an **execution lease** blocks operator graph mutation — not merely
   the early finalized-selection guard.

## Revised approach per issue

### #35 — unified, derived, *conservative*, fail-closed free-space evidence
- Fields: `capacity_bytes` — identity-scoped, per capacity epoch (fixed for a fixed device; NAS/
  special-remote or any resize changes only via an explicit audited epoch transition).
  `baseline_free_bytes` (identity-scoped, immutable per epoch). `observed_free_bytes` + `observed_at`
  (diagnostic only, **never** silently promoted to baseline). `free_evidence` enum. A per-volume
  **`estimate_dirty`** flag.
- **Conservative offline estimate** — only when the volume is *clean* (`estimate_dirty=false`) and all
  `stored_bytes` are known: `baseline_free − conservative_consumption`, where `conservative_consumption
  ≥ Σ stored_bytes` (per-object round-up to fs block size + overhead margin for fs metadata + annex
  object/dir overhead). `Σ stored_bytes` is logical length and optimistic; the estimate errs toward
  *less* free.
- **Uncatalogued ModelArk bytes are handled by the dirty flag, not a margin** — interrupted staging
  (fetch.py:302), annex objects published before the DB write (fetch.py:695), and other orphans can be
  shard-sized, so no percentage margin is conservative. Mark dirty before the first allocating write;
  clear only after mounted reconciliation proves staging/orphans clean and catalog↔annex synchronized.
  **Dirty ⇒ offline free `unknown`.**
- **Dedicated-volume assumption** — if external (non-ModelArk) writes are permitted, offline free is
  `unknown`, not estimated.
- **Drift is a diagnostic signal** — surfaced for integrity (not admission authority); comparison needs
  a catalog write-watermark or synchronous live read (retracting the earlier "nothing to reconcile").
- **Consolidate every consumer** onto one evidence path — capacity/reconciler, librarian, Fill, CLI
  drive listings, Library (`library_api.py:23` currently reports raw `free_bytes`).
- **Migration is fail-closed** — prove per row whether the value is a baseline or a stale observation;
  **if provenance cannot be proven, migrate that drive to `unknown` — never guess a baseline.**

### #36 — feasibility-aware partial affinity (global feasibility, no deletion)
- Root cause: `_choose_partial` (reconcile.py:353) sets a hard `pinned_target` before placement, honored
  even when the remainder can't fit (capacity.py:714).
- **Feasibility is global, not per-candidate.** "Prefer a feasible partial" means *participates in a
  feasible whole-plan assignment* — a partial that fits its own remainder but makes the rest of the
  fleet unpackable is not feasible. Reuse (greatest reuse / least missing / annex-locality) is an
  **objective ranked *after* global feasibility**, never a local pre-packing pin. Otherwise the original
  bug survives in a subtler form.
- **Never delete verified rows** — if the partial target can't participate feasibly, use a feasible
  fresh target and **preserve the old verified files as policy-drifted extra bytes** until an explicit
  #37 cleanup.
- **Annex-to-annex relocation is not required this phase** — the first implementation may pick a
  feasible fresh target, preserve old rows, and re-download; relocation is a tracked follow-up.
- Enumerate: metadata-only stub, meaningful shard partial, insufficient partial target w/ feasible
  fresh, multiple partial drives, GGUF / PyTorch / aux-only / zero-byte, one enormous completed shard,
  protected copy #2, target stability across replans.

### #37 — split into 37a–37d (distinct states + operations)
- **37a exclude-from-placement** — non-destructive; accept no new placements, **existing verified copies
  remain valid and still count toward `numcopies`**; identity + provenance retained.
- **37b mark-lost + re-home/repair** — bytes presumed unavailable; provenance retained but **copies no
  longer satisfy `numcopies`**, and repair work is derived from remaining sources or Hub.
- **37c retire** — available-drive lifecycle; prove no required/unique bytes remain, clear plan/remote
  deps, then **tombstone and permanently reserve the identity/label (never delete)**.
- **37d drop-copy / unarchive** — destructive; exact model/file/drive scope, dry-run dependency report,
  annex proof, explicit confirmation, idempotent DB recovery.
- Underlying all: a real **lifecycle state** governing eligibility *and* bootstrap — `plan.bootstrap()`
  (plan.py:160) re-adds every registered drive on startup, so a `plan_drives` delete alone is
  insufficient.
- **Complements** (not subsumes) DEF-029; identity-aware re-registration stays DEF-029's scope.

### #38 — `tiered_v2` placement policy
- Ordered objectives: (1) tier/failure-domain constraints; (2) honor truly immovable work; (3) feasible
  whole-model arrangement; (4) minimize relocation/re-download; (5) preserve large contiguous blocks;
  (6) minimize idle-drive count *as a low-priority objective*; (7) deterministic label tiebreak.
- Heuristic (largest-item-first onto smallest fitting drive) can emit a **false Gate-B failure**, so add
  a bounded exact/fallback search. **The semantic bound is a deterministic expansion/state-count limit
  over a canonical traversal** (so the same input always yields the same outcome); a wall-clock limit is
  only an emergency operational cap. Outcomes: `FEASIBLE` / `PROVEN_INFEASIBLE` (only after exhaustive
  search completes within the deterministic bound) / `PACKING_INCONCLUSIVE` (bound exhausted). Gate B
  refuses start/commit on `PACKING_INCONCLUSIVE` as "not proven."
- Correction from review: the canonical path already has an explicit label tiebreak (capacity.py:753);
  only the legacy comparison path lacks it, and that is not execution authority.
- Small-drive idleness was intentional consolidation → change needs a decision entry + operator ack;
  ships as `tiered_v2`. Protected homes, ordinary primaries, and replica grouping each get separately
  stated behavior.

### #39 — mutation guard + revision-bound, atomic preview→commit
- **Mutation guard (independent early protection):** refuse `finalize` and every removal path
  (`toggle(...,false)`, `bulk(...,false)`, `clear()` — selection_api.py:33) of the finalized set
  whenever the **fill controller thread/lease is live** — *not* inferred from the status string, since
  `fill_worker.py:24` retains terminal states (`done`/`paused`/`blocked`/`stopped`/`error`) and never
  resets to `idle`. "Live" includes stopping-but-not-yet-terminal. Route all these mutations through a
  **single shared guarded-mutation primitive** (not duplicated checks), and make the check-then-mutate
  atomic with `FillWorker.start()` under the **same lock/lease** (else the check→mutate window races).
  **Scope:** guards the portal's in-process worker; detecting an *external CLI* controller is deferred
  and **documented as a limitation**. Ships on its own branch directly to **`main`**.
- **Revision-bound, atomic preview→commit:** the preview binds to a **versioned canonical serialization
  of every discrete reconciler/placement input** — finalized selection, canonical manifests/files +
  archive-policy version, `numcopies`, plan membership, drive roles + RAID flags, drive
  lifecycle/identity, capacity/baseline facts + epoch, capacity mode, placement-policy version, archived
  facts, and the proposed mutation — **excluding only volatile live `df`**. Commit is executed under the
  shared controller lock as a **CAS**, requiring **all three**: (a) the discrete revision unchanged;
  (b) live Gate B `FEASIBLE`; (c) the committed model→drive assignment **materially equivalent** to the
  preview's (exact free-byte margins need not match; the reviewed assignment must). Atomic relative to
  another preview-commit, Fill start, selection mutation, plan/capacity-mode/`numcopies`/archive-policy
  changes, drive role/RAID/lifecycle changes, and the #37 operations. Revision changed, target-map
  differs, or Gate B not `FEASIBLE` → reject + fresh preview. Preview covers add/remove/clear and proves
  no candidate bytes are written before accepted admission.

## Sequencing (revised)

0. **Record the DEC** (invariants above) — before any implementation.
0b. **Rewrite/split the GitHub issues** to agree with the DEC + this plan (see source-of-truth below).
1. **Mutation guard** — own branch → **`main`** (independent early protection).
2. **#35** — unified, conservative, dirty-aware capacity evidence + migration (fix branch).
3. **#36** — globally-feasible partial affinity, no deletion (fix branch).
4. **#38** — `tiered_v2` placement policy (fix branch).
5. **#37** — 37a→37b→37c→37d, non-destructive phases before destructive (fix branch, multiple PRs).
6. **#39** — full atomic revision-bound preview/commit UX (fix branch).

## Workflow

- **Decision log first**, then issue restructuring, then code.
- **Mutation guard ships independently to `main`** (small, self-contained safety fix).
- All migration work on the isolated long-lived branch `fix/placement-capacity-hardening`; **one
  reviewable invariant/migration phase per PR**, targeting the fix branch; merge commits (no squash),
  branches retained.
- **Sync `main` → fix branch** regularly; **never fix branch → `main` mid-effort** — the repo is public
  and `main` must not expose partial migration state.
- **Final integration PR** (fix branch → `main`) with copied-catalog shadow evidence + rollback.

## Source of truth (blocker to resolve before implementation)

The GitHub issue bodies (#35–#39) still hold their original text and now **contradict this plan** —
e.g. #36 still states never-re-homing a real partial is correct, #37 is not split, and the new
invariants/failure-codes/migration/test-matrices are absent. Before any implementation PR:
- Author the DEC.
- **Rewrite** #35, #36, #38, #39 to match; **split #37 into 37a–37d**; add explicit invariants, failure
  codes, migration behavior, and test matrices to each.
- Only then is the mutation guard (and subsequent phases) started.

## Acceptance material (required per issue before its PR)

- **#35** — mounted/unmounted equivalence, fs overhead, external content, NULL vs proven-zero
  `stored_bytes`, dirty-volume→`unknown`, NAS/resize epoch transition, same-drive re-registration,
  unprovable-provenance→`unknown`, migrated-catalog replay, every CLI/API/UI consumer.
- **#36** — global feasibility (partial that fits itself but breaks the fleet is rejected), metadata-
  only stub, meaningful shard partial, multiple partials, stop/crash durability, protected/bulk/replica,
  target stability across replans.
- **#37** — offline vs excluded vs lost vs retired, last/unique-copy refusal, two-copy policy, bootstrap
  eligibility, annex-success/DB-failure recovery, dry-run, idempotent reruns, tombstone reservation.
- **#38** — the exact incident fleet, adversarial packing, shuffled query order, candidate-specific
  partial budgets, safety/workspace constraints, deterministic output under a state-count bound,
  `PACKING_INCONCLUSIVE` on bound exhaustion + Gate-B refusal, 10k-candidate performance.
- **#39** — atomicity vs concurrent preview-commit/Fill-start/mutation, target-map divergence under
  changed live free, live-controller-lease predicate (not status string), all removal paths
  (toggle/bulk/clear), proof no candidate bytes are written before accepted admission.

## Out of scope (tracked separately)

Operational continuation — restart the fill so GLM-5.2 lands on drive-01, then remove the mistakenly-
downloaded `zai-org/GLM-5.2-FP8` after the BF16 is archived — is independent of this code effort and
proceeds on the operator's cadence.
