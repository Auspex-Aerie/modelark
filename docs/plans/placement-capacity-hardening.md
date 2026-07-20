# Placement & capacity hardening

Working plan for the fix effort opened after the 2026-07-20 placement/capacity audit, revised across
review rounds. This document is for local review; the authoritative problem statements live in the
GitHub issues, and the binding invariants will be recorded in the decision log **before any
implementation**.

## Origin

A mid-fill add of `zai-org/GLM-5.2` (BF16, 1.51 TB) exposed a cluster of drift-between-model-and-
reality problems. The immediate incident was resolved operationally (un-pinned GLM-5.2 by deleting 9
aborted-start stub files; restored `free_bytes` to its baseline contract). This effort addresses the
underlying classes so they stop recurring.

## Issues (problem statements in GitHub)

| # | Title |
|---|---|
| [#35](https://github.com/Auspex-Aerie/modelark/issues/35) | Free-space is a mutable field and the recompute is never persisted |
| [#36](https://github.com/Auspex-Aerie/modelark/issues/36) | Durable-partial pinning is disproportionate |
| [#37](https://github.com/Auspex-Aerie/modelark/issues/37) | No retire / un-archive / drive-loss recovery — **to be split** (see below) |
| [#38](https://github.com/Auspex-Aerie/modelark/issues/38) | Placement consolidation strands small drives / wedges large blocks |
| [#39](https://github.com/Auspex-Aerie/modelark/issues/39) | Mid-fill add has no pre-commit preview |

Deferred on the roadmap, not re-filed: cross-drive shard spanning; multi-RAID copy-#1 home.

## Invariants — recorded in the decision log FIRST (before any code, incl. the mutation guard)

A `DEC-###` entry is authored and merged **before** the first implementation PR, capturing:

1. **Free-space evidence model** — `baseline_free_bytes` is identity-scoped and immutable. Fixed-device
   `capacity_bytes` is identity-scoped; dynamic/special-remote (NAS) capacity changes only through an
   explicit observation/update path. Current free is always *derived* with a labelled `free_evidence`
   (`live` / `baseline_estimate` / `stale` / `unknown`); there is no writable current-free field. Drift
   detection correlates the observation against a catalog write-watermark (or a synchronous live
   observation) so archive writes *after* an observation are not mistaken for drift.
2. **Durable truth is never auto-deleted** — a verified `archived` row (complete, published file) is
   removed only by an explicit, scoped, confirmed operation. No threshold-triggered deletion.
3. **Offline ≠ lost** — a shelved/unmounted drive remains a valid archive copy (decision_log.md:813).
4. **Gate B never reports a false infeasible** — a heuristic that cannot find a packing must say so
   distinctly (`PACKING_INCONCLUSIVE`) from a proven-infeasible one.
5. **Placement policy changes are versioned** — the new distribution policy ships as a named
   `tiered_v2`, not a silent change to `tiered_v1`, with operator-facing acknowledgement.

## Revised approach per issue

### #35 — unified, derived, *conservative* free-space evidence
- Fields: `capacity_bytes` — identity-scoped; fixed for a fixed device, but dynamic/special-remote
  (NAS) capacity changes only via an explicit observation/update path. `baseline_free_bytes`
  (identity-scoped, immutable — free on the empty drive identity before ModelArk consumption).
  `observed_free_bytes` + `observed_at` (diagnostic only, **never** silently promoted to baseline).
  `free_evidence` enum.
- **Conservative offline estimate** — `baseline_free − conservative_consumption`, where
  `conservative_consumption ≥ Σ stored_bytes`: per-object round-up to filesystem block size plus an
  overhead margin (fs metadata, annex object/dir overhead). `Σ stored_bytes` is logical length and is
  **optimistic**; the estimate must err toward *less* free, never more.
- **Dedicated-volume assumption stated explicitly** — if external (non-ModelArk) writes are permitted
  on a drive, offline free is `unknown`, not estimated.
- Live `df` remains the authority when mounted; the offline estimate is a labelled fallback.
- **Drift is a diagnostic signal** — `observed` vs derived disagreement is surfaced for integrity, even
  though it is not admission authority (retracting the earlier "nothing to reconcile"). Comparison
  requires a catalog write-watermark or a synchronous live observation: `observed_free_bytes`/
  `observed_at` alone cannot distinguish real drift from archive writes made *after* the observation.
- **Consolidate every consumer** onto one evidence path — capacity/reconciler, librarian projections,
  Fill, CLI drive listings, and Library (`library_api.py:23` currently reports raw `free_bytes`).
- Migration: prove per existing row whether its value is a baseline or a stale observation. **If
  provenance cannot be proven, migrate that drive to `unknown` — never guess a baseline** (fail-closed).
  Identity-scoped facts are immutable; mount path / health / observations, and dynamic NAS capacity via
  the explicit update path, may change on same-identity re-registration.

### #36 — feasibility-aware partial affinity (no deletion)
- Root cause: `_choose_partial` (reconcile.py:353) sets a hard `pinned_target` before placement, which
  capacity honors even when the remainder can't fit (capacity.py:714).
- Replace hard pinning: expose all partial candidates + reusable bytes to placement; **prefer** a
  feasible partial target (greatest reuse / least missing); if it can't hold the remainder, allow a
  feasible fresh target; **preserve the old verified files as policy-drifted extra bytes** until an
  explicit #37 cleanup. Any threshold is a *preference*, never permission to delete or override
  feasibility.
- **Annex-to-annex relocation is a later optimization, not required in this phase** — the first
  implementation may select a feasible fresh target, preserve the old verified rows as policy-drift,
  and re-download; relocation is tracked as a follow-up.
- Enumerate behavior: metadata-only stub, meaningful shard partial, insufficient partial target w/
  feasible fresh target, multiple partial drives, GGUF / PyTorch / aux-only / zero-byte, one enormous
  completed shard, protected copy #2, and target stability across replans.

### #37 — split into distinct lifecycle/recovery operations
- **mark-lost / exclude-from-placement** — non-destructive; retain identity + provenance, stop counting
  its rows as satisfying desired copies.
- **re-home / repair** — derive new required work from remaining sources or Hub.
- **retire** — available-drive lifecycle; prove no required/unique bytes remain, clear plan/remote
  deps, then tombstone/remove identity.
- **drop-copy / unarchive** — destructive; exact model/file/drive scope, dry-run dependency report,
  annex proof, explicit confirmation, idempotent DB recovery.
- Needs a real **lifecycle state** governing eligibility and bootstrap: `plan.bootstrap()` (plan.py:160)
  re-adds every registered drive on startup, so a `plan_drives` delete alone is insufficient.
- **Complements** (not subsumes) DEF-029; identity-aware re-registration remains DEF-029's scope.

### #38 — `tiered_v2` placement policy
- Ordered objectives: (1) tier/failure-domain constraints; (2) honor truly immovable work; (3) find a
  feasible whole-model arrangement; (4) minimize relocation/re-download cost; (5) preserve large
  contiguous blocks; (6) minimize idle-drive count *as a low-priority objective*; (7) deterministic
  label tiebreak.
- Likely heuristic: largest-item-first onto the smallest remaining drive that fits — but a heuristic
  can emit a **false Gate-B failure**. Add a bounded exact/fallback search with explicit runtime/memory
  limits and three deterministic outcomes: **FEASIBLE** (placement found), **PROVEN_INFEASIBLE** (only
  after the exhaustive search completes within bounds), and **PACKING_INCONCLUSIVE** (bound exhausted).
  Gate B treats `PACKING_INCONCLUSIVE` distinctly — never as a false infeasible; "proven infeasible" is
  valid only once exhaustive search completes.
- Correction from review: the canonical path already has an explicit label tiebreak (capacity.py:753);
  only the legacy comparison path lacks it, and that is not execution authority.
- Small-drive idleness was intentional consolidation → the change needs a decision entry + operator
  acknowledgement; ships as named `tiered_v2`. Protected homes, ordinary primaries, and replica
  grouping each get separately stated behavior.

### #39 — mutation guard + revision-bound preview
- **Mutation guard (independent early protection):** refuse `finalize` / `remove` / `clear` of the
  finalized set whenever the worker is **not idle** — starting/auth/planning, downloading/compressing/
  publishing, awaiting a drive or operator decision, or stopping-but-not-yet-terminal — so an already-
  derived snapshot cannot resume after the selection changed (selection_api.py:33 mutations are
  currently immediate). **Scope:** this hotfix guards the portal's in-process worker state machine;
  detecting an *external CLI* fill controller is **deferred and documented as a limitation** (operators
  are already told not to run two controllers), not presented as universal. Ships on its own branch
  directly to **`main`**, not the long-lived fix branch — a genuinely shipped protection.
- **Revision-bound preview→commit:** the preview binds to a **versioned canonical serialization of
  every discrete reconciler and placement input** — finalized selection, canonical manifests/files +
  archive-policy version, `numcopies`, plan membership, drive roles + RAID flags, drive
  lifecycle/identity, capacity/baseline facts, capacity mode, placement-policy version, archived facts,
  and the proposed mutation itself — **explicitly excluding only volatile live `df`**, so free-space
  jitter never invalidates a preview. On commit, require **all three**: (a) the discrete revision is
  unchanged; (b) live Gate B is feasible (reading current `df`); and (c) the committed model-to-drive
  assignment is **materially equivalent** to the preview's (exact free-byte margins need not match; the
  reviewed model→drive assignment must). If the revision changed *or* the target map differs, reject
  and show a fresh preview. Preview covers add / remove / clear, and proves no candidate bytes are
  written before accepted admission.

## Sequencing (revised)

0. **Record the DEC** (invariants above) — before any implementation.
1. **Mutation guard** — own branch → **`main`** (independent early protection; not part of the
   migration set).
2. **#35** — unified, conservative capacity evidence + migration (fix branch).
3. **#36** — feasibility-aware partial affinity, no deletion (fix branch).
4. **#38** — `tiered_v2` placement policy (fix branch).
5. **#37** — lifecycle/recovery, non-destructive phase before destructive phase (fix branch, multiple PRs).
6. **#39** — full revision-bound preview/commit UX against the now-stable policy (fix branch).

## Workflow

- **Decision log first**, then code.
- **Mutation guard ships independently to `main`** (small, self-contained safety fix).
- All migration work on the isolated long-lived branch `fix/placement-capacity-hardening`; **one
  reviewable invariant/migration phase per PR**, targeting the fix branch; merge commits (no squash),
  branches retained.
- **Sync `main` → fix branch** regularly to avoid drift and keep phase PRs small; **never fix branch →
  `main` mid-effort** — the repo is public and `main` must not expose partial migration state.
- **Final integration PR** (fix branch → `main`) with copied-catalog shadow evidence + rollback
  instructions.

## Acceptance material (required per issue before its PR)

Explicit invariants, failure codes, migration behavior, and a test matrix:
- **#35** — mounted/unmounted equivalence, fs overhead, external content, NULL/zero stored sizes, NAS,
  same-drive re-registration, unprovable-provenance→`unknown`, migrated-catalog replay, every
  CLI/API/UI consumer.
- **#36** — metadata-only stub, meaningful shard partial, insufficient partial target w/ feasible fresh,
  multiple partials, stop/crash durability, protected/bulk/replica, target stability across replans.
- **#37** — offline vs lost, last/unique-copy refusal, two-copy policy, bootstrap behavior, annex-
  success/DB-failure recovery, dry-run, idempotent reruns.
- **#38** — the exact incident fleet, adversarial packing, shuffled query order, candidate-specific
  partial budgets, safety/workspace constraints, deterministic output, `PACKING_INCONCLUSIVE` on bound
  exhaustion, 10k-candidate performance.
- **#39** — stale preview after every mutable input, target-map divergence under changed live free,
  concurrent sessions, full non-idle worker boundary, add/remove/clear, proof no candidate bytes are
  written before accepted admission.

## Out of scope (tracked separately)

Operational continuation — restart the fill so GLM-5.2 lands on drive-01, then remove the mistakenly-
downloaded `zai-org/GLM-5.2-FP8` after the BF16 is archived — is independent of this code effort and
proceeds on the operator's cadence.
