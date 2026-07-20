# Placement & capacity hardening

Working plan for the fix effort opened after the 2026-07-20 placement/capacity audit, revised across
review rounds toward an implementation-ready spec. The binding invariants go into the decision log, and
the GitHub issues are rewritten/split to agree with it, **before any implementation begins**.

## Origin

A mid-fill add of `zai-org/GLM-5.2` (BF16, 1.51 TB) exposed a cluster of drift-between-model-and-
reality problems. The immediate incident was resolved operationally (un-pinned GLM-5.2 by deleting 9
aborted-start stub files; restored `free_bytes` to its baseline contract). This effort addresses the
underlying classes so they stop recurring.

## Issues (GitHub — rewritten/split to match this plan + the DEC before implementation)

| # | Title | Split |
|---|---|---|
| [#35](https://github.com/Auspex-Aerie/modelark/issues/35) | Free-space is a mutable field, recompute never persisted | anchor-evidence model |
| [#36](https://github.com/Auspex-Aerie/modelark/issues/36) | Durable-partial pinning is disproportionate | → 36a (emit alternatives) / 36b (reuse preference) |
| [#37](https://github.com/Auspex-Aerie/modelark/issues/37) | No retire / un-archive / drive-loss recovery | → 37a–37d over orthogonal axes |
| [#38](https://github.com/Auspex-Aerie/modelark/issues/38) | Placement strands small drives / wedges large blocks | shared placement/feasibility engine + `tiered_v2` |
| [#39](https://github.com/Auspex-Aerie/modelark/issues/39) | Mid-fill add has no pre-commit preview | portal guard + catalog-backed atomic commit |

Deferred on the roadmap, not re-filed: cross-drive shard spanning; multi-RAID copy-#1 home.

## Invariants — recorded in the decision log FIRST (before any code, incl. the guard)

1. **Free-space is anchor-derived, never a mutable current-free field.** Per drive identity we keep an
   **evidence anchor**: `anchor_free_bytes`, the `identity+capacity epoch`, a **consumption watermark**
   (catalog/annex revision), a timestamp, and mount-identity proof. Current free is derived as
   `anchor_free_bytes + conservative_allocations − proven_releases` since the watermark. An empty
   registration is the simplest anchor; a **full mounted inventory/reconciliation establishes a fresh
   anchor for an already-populated drive** (this is the supported recovery from `unknown`, so
   fail-closed migration is never a dead end). Evidence is labelled `live` / `anchor_estimate` /
   `stale` / `unknown`.
2. **`live` requires positive mount-identity proof.** `df` is authority only after proving the path is
   the expected volume at the expected epoch — block: fs UUID/device identity; NAS/special-remote:
   expected mount source + remote identity. An absent NAS mount (df reporting the host fs), wrong volume
   at a known path, replaced filesystem, or stale mount path ⇒ `unknown`, never `live`.
3. **Dirty protocol (generation-based, single-writer, crash-safe).** A drive's estimate is dirtied
   **durably before any filesystem allocation** and stays dirty through staging → publish → annex →
   catalog commit; it clears **only** after all participating writes finish and reconciliation succeeds.
   A crash at every boundary leaves it dirty; **no writer may clear dirtiness set by another** (use a
   generation/counter or a durable single-writer lease). Applies to **every allocating path**: download,
   replication, restore, relocation, registration/annex-init, recovery. Dirty ⇒ offline free `unknown`.
   NULL `stored_bytes` is missing evidence, never a proven zero-length object.
4. **Durable truth is never auto-deleted** — a verified `archived` row is removed only by an explicit,
   scoped, confirmed operation.
5. **Drive properties are orthogonal, not one enum:** `lifecycle` ∈ {active, lost, retired};
   `eligibility` ∈ {enabled, excluded}; `presence` ∈ {mounted, offline, unknown}. A drive can be offline
   *and* excluded; plugging in an excluded drive must not silently re-enable it. A **found/reinstate**
   transition returns a `lost` drive to counting only after identity proof + reconcile/fsck. `retire`
   tombstones and **permanently reserves** the identity/label (never deletes). Offline ≠ lost.
6. **Gate B outcomes are graded and evidence-qualified** — `FEASIBLE`, `CAPACITY_EVIDENCE_UNKNOWN`
   (admission evidence is `unknown`), `PACKING_INCONCLUSIVE` (search bound exhausted),
   `INFEASIBLE_UNDER_ADMISSION_BUDGET` (won't fit the admitted evidence/policy budget), and structural
   failure (no eligible drive large enough). Never assert physical impossibility beyond the admitted
   budget. Gate B refuses start/commit on anything but `FEASIBLE`, naming which outcome.
7. **Placement is versioned (`tiered_v2`) with a stated optimization contract** — the exact/fallback
   search either performs lexicographic optimization over the objectives or finds feasibility then runs
   a deterministic improvement pass; "preserve large contiguous blocks" is defined mathematically (e.g.
   lexicographically maximize the sorted vector of remaining per-drive capacities) so tests can assert
   it. Search bound is a deterministic state-count over a canonical traversal; wall-clock is only an
   emergency cap.
8. **Serialized control is catalog-backed** — a monotonic `graph_revision` plus `BEGIN IMMEDIATE` (and a
   durable lease/fencing token) observed by **every supported graph writer** (portal threads, `modelark
   protect`, CLI capacity-mode/registration, a second portal, an external CLI Fill, config changes). The
   canonical revision **serialization is comparison evidence, not the atomic target**; the CAS is the DB
   transaction on `graph_revision`. The early portal guard is explicitly portal-scoped; a *universal*
   execution-lease guarantee holds only once CLI writers participate.

## Revised approach per issue

### #35 — anchor-derived, mount-proven, conservative, fail-closed free-space evidence
- Fields: `anchor_free_bytes`, `identity_capacity_epoch`, `consumption_watermark`, `anchor_at`,
  `mount_identity` proof; `observed_free_bytes`+`observed_at` (diagnostic only); `free_evidence` enum;
  a generation-based `dirty` marker. No writable current-free field.
- Offline estimate (only when clean + all `stored_bytes` known): `anchor_free − conservative_consumption`
  since the watermark, `conservative_consumption ≥ Σ stored_bytes` (per-object block round-up + fs/annex
  overhead margin) minus proven releases. Errs toward *less* free.
- **Recovery from `unknown`:** a mounted inventory/reconciliation (prove identity → inventory staging/
  orphans → reconcile catalog↔annex) sets a fresh anchor + watermark. Populated drives are supported;
  the empty-registration case is just the trivial anchor.
- `live` only with mount-identity proof (invariant 2). Consolidate every consumer onto one evidence path
  (`library_api.py:23` currently reports raw `free_bytes`).
- Migration fail-closed: unprovable provenance ⇒ `unknown`, then recover via the anchor protocol.

### #36 — feasibility-aware partial affinity, split to break the #38 dependency
- Root cause: `_choose_partial` (reconcile.py:353) sets a hard `pinned_target` before placement, honored
  even when the remainder can't fit (capacity.py:714).
- **36a — reconciler emits *alternatives*, chooses/pins nothing.** For each required copy it exposes all
  partial candidates with candidate-specific reuse budgets (reusable present files, missing-work
  identity). No local feasibility judgment, no pin.
- **36b — reuse preference (after the #38 engine exists).** Among assignments the shared engine proves
  **globally feasible**, prefer greatest reuse / least missing / annex-locality. Reuse is an objective
  *ranked after global feasibility*, never a local pre-packing pin — a partial that fits its own
  remainder but makes the fleet unpackable is not chosen.
- **Never delete verified rows** — a non-chosen partial's files are preserved as policy-drift until an
  explicit #37 op. Annex-to-annex relocation is **not required this phase** (fresh feasible target +
  preserve + re-download is acceptable; relocation is a follow-up).
- Enumerate: metadata-only stub, meaningful shard partial, multiple partial drives, GGUF/PyTorch/aux-
  only/zero-byte, one enormous completed shard, protected copy #2, target stability across replans.

### #38 — shared placement/feasibility engine + `tiered_v2`
- A single engine consumes 36a's alternatives and produces both the admission verdict (invariant 6
  graded outcomes) and the placement, so #36 and #38 aren't circular: **36a → engine (#38) → 36b**.
- Ordered objectives: (1) tier/failure-domain; (2) immovable work; (3) feasible whole-model arrangement;
  (4) minimize relocation/re-download; (5) preserve large contiguous blocks (defined per invariant 7);
  (6) minimize idle-drive count *low priority*; (7) deterministic label tiebreak. Exact fallback obeys
  the invariant-7 optimization contract (not just "first feasible").
- Correction from review: canonical path already has the label tiebreak (capacity.py:753); only the
  legacy comparison path lacks it, and that is not execution authority.
- `tiered_v2` is a named policy with a decision entry + operator acknowledgement (small-drive idleness
  was intentional consolidation). Protected homes, primaries, and replica grouping each specified.

### #37 — orthogonal axes + operations
- Model the three axes (invariant 5), not one column. Operations map onto them:
  **37a exclude** = set `eligibility=excluded` (existing verified copies still count);
  **37b mark-lost + re-home/repair** = set `lifecycle=lost` (copies stop counting; repair derived from
  remaining sources/Hub) + the **found/reinstate** inverse;
  **37c retire** = prove no required/unique bytes → `lifecycle=retired`, tombstone + reserve identity;
  **37d drop-copy / unarchive** = destructive, exact scope, dry-run dependency report, annex proof,
  explicit confirmation, idempotent DB recovery.
- Eligibility + lifecycle (not `plan_drives` membership) gate placement **and** bootstrap: `plan.bootstrap()`
  (plan.py:160) re-adds every registered drive on startup, so membership alone is insufficient.
- **Complements** (not subsumes) DEF-029.

### #39 — portal guard (early) + catalog-backed atomic preview/commit (final)
- **Early guard (portal-only, explicitly scoped):** refuse `finalize` and every removal path
  (`toggle(...,false)`, `bulk(...,false)`, `clear()` — selection_api.py:33) through a **single shared
  guarded-mutation primitive**, keyed on **the fill controller lease being live** — *not* the status
  string, which retains terminal states (`fill_worker.py:24`) and never resets to `idle` — including
  stopping-but-not-terminal, sharing the lock with `FillWorker.start()`. **Documented limitation:** an
  external CLI controller is not detected by this guard. Ships on its own branch to **`main`**.
- **Final atomic contract (catalog-backed, all writers):** monotonic `graph_revision` + `BEGIN
  IMMEDIATE` + durable lease/fencing token observed by every graph writer; while Fill owns a derived
  graph, its **execution lease** blocks operator graph mutation. Preview binds to a **versioned
  canonical serialization built from the actual immutable planner-input object** (finalized selection,
  manifests/files + archive-policy version, `numcopies`, plan membership, drive
  roles/RAID/lifecycle/eligibility, capacity/anchor facts + epoch, `dirty`/exclusivity evidence,
  compression config, margins/headroom policy, capacity mode, `tiered_v2`/solver-bound version, archived
  facts, and the mutation) — excluding only volatile display-only facts; the serialization is comparison
  evidence. Commit runs under the lock as a CAS on `graph_revision`, requiring **all three**: (a)
  revision unchanged; (b) live Gate B `FEASIBLE`; (c) the committed **execution-authority task set**
  materially equivalent to the preview — compared over {requirement/copy id, task kind, target drive,
  source drive where applicable, reused-present/missing-work identity}, not merely model→drive. Any
  divergence → reject + fresh preview. Preview covers add/remove/clear; no candidate bytes written
  before accepted admission.

## Sequencing (revised — breaks the #36/#38 circularity)

0. **Record the DEC** (invariants above).
0b. **Rewrite/split the GitHub issues** to agree with the DEC + this plan.
1. **Portal mutation guard** → **`main`** (independent; explicitly portal-only).
2. **#35** — anchor evidence + mount-identity + dirty protocol + migration/recovery (fix branch).
3. **#36a** — reconciler emits partial alternatives (no choosing/pinning) (fix branch).
4. **#38** — shared placement/feasibility engine + graded outcomes + `tiered_v2` (fix branch).
5. **#36b** — reuse preference among globally-feasible assignments (fix branch).
6. **#37** — 37a → 37b(+reinstate) → 37c → 37d (fix branch, multiple PRs).
7. **#39** — catalog-backed atomic preview/commit + execution lease (fix branch).

## Workflow

- **Decision log first**, then issue restructuring, then code.
- **Portal guard ships independently to `main`.**
- All migration work on the isolated long-lived branch `fix/placement-capacity-hardening`; **one
  reviewable phase per PR**, targeting the fix branch; merge commits (no squash), branches retained.
- **Sync `main` → fix branch** regularly; **never fix branch → `main` mid-effort** (public repo).
- **Final integration PR** (fix branch → `main`) with copied-catalog shadow evidence + rollback.

## Source of truth (pre-code blocker — accepted staging)

Issue bodies #35–#39 still hold original text and contradict this plan; that's fine while drafting.
Before any implementation PR: author the DEC; rewrite #35/#38/#39, split #36 → 36a/36b and #37 →
37a–37d, and add invariants, failure codes, migration behavior, and test matrices to each.

## Acceptance material (per issue, before its PR)

- **#35** — mounted/unmounted equivalence, fs overhead, external content, NULL vs proven-zero
  `stored_bytes`, dirty-volume→`unknown`, wrong-volume-at-known-path, missing-NAS-mount, replaced-fs,
  stale-mount-path → `unknown`, anchor recovery on a populated drive, capacity-epoch resize transition,
  unprovable-provenance→`unknown`, migrated-catalog replay, every CLI/API/UI consumer.
- **#36a/#38/#36b** — reconciler emits alternatives without pinning; global feasibility (a partial that
  fits itself but breaks the fleet is not chosen); graded Gate-B outcomes incl.
  `INFEASIBLE_UNDER_ADMISSION_BUDGET` vs `PACKING_INCONCLUSIVE` vs structural; deterministic output under
  a state-count bound; optimization contract (large-block preservation assertion); adversarial packing;
  10k-candidate performance.
- **#37** — offline vs excluded vs lost vs retired (orthogonal), found/reinstate, last/unique-copy
  refusal, two-copy policy, bootstrap eligibility, annex-success/DB-failure recovery, dry-run, idempotent
  reruns, tombstone reservation.
- **#39** — cross-writer atomicity (portal + CLI protect/capacity-mode/registration + external fill),
  `graph_revision` CAS, execution-authority-task equivalence under changed live free, live-lease
  predicate (not status string), all removal paths, no candidate bytes before accepted admission.

## Out of scope (tracked separately)

Operational continuation — restart the fill so GLM-5.2 lands on drive-01, then remove the mistakenly-
downloaded `zai-org/GLM-5.2-FP8` after the BF16 is archived — is independent of this code effort.
