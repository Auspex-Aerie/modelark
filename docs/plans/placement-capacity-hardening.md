# Placement & capacity hardening

Working plan for the fix effort opened after the 2026-07-20 placement/capacity audit, revised across
review rounds toward an implementation-ready spec. The binding invariants go into the decision log, and
the GitHub issues are rewritten/split to agree with it, **before any implementation begins**.

## Origin

A mid-fill add of `zai-org/GLM-5.2` (BF16, 1.51 TB) exposed a cluster of drift-between-model-and-
reality problems. The immediate incident was resolved operationally (un-pinned GLM-5.2 by deleting 9
aborted-start stub files; restored `free_bytes` to its baseline contract). This effort addresses the
underlying classes so they stop recurring.

## Issues (GitHub — to be rewritten/split to match this plan + the DEC before implementation)

| # | Title | Split |
|---|---|---|
| [#35](https://github.com/Auspex-Aerie/modelark/issues/35) | Free-space is a mutable field, recompute never persisted | append-only clean-anchor model |
| [#36](https://github.com/Auspex-Aerie/modelark/issues/36) | Durable-partial pinning is disproportionate | → 36a emits alternatives+costs (reuse ranking owned by #38) |
| [#37](https://github.com/Auspex-Aerie/modelark/issues/37) | No retire / un-archive / drive-loss recovery | → 37a–37d over orthogonal axes |
| [#38](https://github.com/Auspex-Aerie/modelark/issues/38) | Placement strands small drives / wedges large blocks | shared feasibility+placement engine, `tiered_v2` |
| [#39](https://github.com/Auspex-Aerie/modelark/issues/39) | Mid-fill add has no pre-commit preview | portal guard + catalog-backed lease/CAS commit |

Deferred on the roadmap, not re-filed: cross-drive shard spanning; multi-RAID copy-#1 home.

## Invariants — recorded in the decision log FIRST (before any code, incl. the guard)

1. **Free-space is anchor-derived via append-only *clean anchors*, with no delta arithmetic over mutable
   tables.** A *clean anchor* is a reconciled free-space observation taken when no managed write is in
   flight, carrying `anchor_free_bytes`, the `identity+capacity epoch`, `anchor_at`, mount-identity proof,
   and the write-exclusivity policy/evidence in force. Evidence selection is ordered: **(a)** a currently
   mounted, identity-proven volume uses live `df` even while dirty; **(b)** if it is not live and dirty,
   free is `unknown`; **(c)** if it is offline, clean, and exclusive-write control has held since the
   anchor, free is the latest clean anchor's `anchor_free_bytes`; **(d)** otherwise it is diagnostic
   `stale`/`unknown`, never admission authority. Dirty invalidates the *offline anchor*, not a fresh live
   read. A release or allocation is reflected by the next clean anchor after reconciliation — never by
   crediting a vanished catalog row or differencing a scalar watermark over mutable/tombstoned rows.
   Values clamp to `[0, usable_capacity_for_epoch]`. Empty registration is the trivial first clean
   anchor; a full mounted inventory/reconciliation appends a fresh clean anchor for an already-populated
   drive — the supported recovery from `unknown`. Each anchor row is immutable; a resize is an explicit,
   audited `identity+capacity` epoch transition that appends a new anchor.
2. **`live`/anchor require positive mount-identity proof.** `df` or a fresh anchor is authority only
   after proving the path is the expected volume at the expected epoch — block: fs UUID/device; NAS/
   special-remote: expected mount source + remote identity. Absent NAS mount (df on host fs), wrong
   volume at a known path, replaced filesystem, or stale mount path ⇒ `unknown`.
   **Dedicated/exclusive-volume assumption is explicit and fail-closed:** if another host, process, or
   non-ModelArk workflow may write while ModelArk cannot observe/fence it (especially a NAS/special
   remote), an offline anchor is not admission evidence. The exclusivity policy is persisted,
   identity/epoch-scoped, and part of the planner revision. Cross-host NAS writes are unsupported unless
   a real distributed writer lock/fence is configured.
3. **Dirty protocol (generation-based, single-writer, crash-safe).** Dirtied **durably before any
   filesystem allocation**, held through staging → publish → annex → catalog commit, cleared only after
   all participating writes finish and reconciliation appends a new clean anchor; a crash at every
   boundary leaves it dirty; **no writer clears dirtiness set by another** (generation/counter or durable
   single-writer lease). Covers every allocating path: download, replication, restore, relocation,
   registration/annex-init, recovery. Anchor publication is a generation CAS: snapshot the generation,
   prove no active writer, reconcile, then append+clear only if the generation is unchanged and the
   writer lock is still idle; otherwise discard the candidate anchor and remain dirty. NULL
   `stored_bytes` is missing evidence, never a proven zero.
4. **Durable truth is never auto-deleted** — a verified `archived` row is removed only by an explicit,
   scoped, confirmed operation.
5. **Drive properties are orthogonal:** durable `lifecycle` ∈ {active, lost, retired} and durable
   `eligibility` ∈ {enabled, excluded}; **`presence` ∈ {mounted, offline, unknown} is derived,
   timestamped, identity-proven observation — never persisted durable truth** (a stored "mounted" is
   false the moment the device/process disappears). A drive can be offline *and* excluded; re-plugging
   an excluded drive must not re-enable it. `found/reinstate` returns a `lost` drive to counting only
   after identity proof + reconcile/fsck. `retire` tombstones and **permanently reserves** the identity/
   label (never deletes). Offline ≠ lost.
6. **Gate B is a *safety* verdict about feasibility existence** (not optimality): `FEASIBLE` = a feasible
   whole-plan assignment exists; `PACKING_INCONCLUSIVE` = none found within the deterministic search
   bound and infeasibility not proven; `INFEASIBLE_UNDER_ADMISSION_BUDGET` = proven none fits the
   admitted evidence/policy budget; `CAPACITY_EVIDENCE_UNKNOWN` = admission evidence is `unknown`;
   structural = no eligible drive large enough. Never asserts physical impossibility beyond the admitted
   budget. Gate B refuses start/commit on anything but `FEASIBLE`, naming the outcome.
7. **Placement optimization is separate from safety, and is one concrete deterministic rule.** After a
   feasible assignment exists, a **deterministic best-effort improvement pass** ranks it
   **lexicographically** over the ordered objectives — it is *not* required to prove optimality (that
   would be far harder than proving feasibility at 10k candidates). "Preserve large contiguous blocks" is
   defined as **lexicographically maximizing the descending-sorted vector of remaining per-drive free**.
   `tiered_v2` versions this exact rule; quality beyond feasibility is advisory. If improvement exhausts
   its state/time/memory bound after feasibility was found, the safety verdict stays `FEASIBLE` and the
   result carries `optimization_truncated`; `PACKING_INCONCLUSIVE` is reserved for the feasibility search.
8. **Serialized control is catalog-backed with a real lease and physical writer exclusion.** A
   monotonic `graph_revision` plus a durable lease defining owner/session id, acquire/renew,
   heartbeat/expiry, crash recovery, safe
   operator takeover, a **monotonically increasing fencing token validated on every worker catalog
   write**. Because a DB token alone cannot fence filesystem side effects, every allocating/publish/
   annex path also holds a same-host process/per-drive writer lock and revalidates its token at safe
   boundaries. An expired catalog lease is not taken over while the prior physical lock is held; forced
   takeover marks affected drives dirty and requires reconciliation before new writes. The commit
   protocol does **not** hold `BEGIN IMMEDIATE` across the solve: acquire lease →
   snapshot `graph_revision` → compute → `BEGIN IMMEDIATE` (short) → recheck revision+token → commit or
   abort. The canonical serialization is comparison **evidence**; the CAS target is `graph_revision`.
   Every supported graph writer (portal, `protect`, capacity-mode/registration, a second portal, an
   external CLI Fill) validates the token; a *universal* execution-lease guarantee holds only once they
   all do. The early portal guard is explicitly portal-scoped.
9. **Approval and execution are distinct, revision-bound stages.** Preview/commit stores an approved
   execution fingerprint plus target/source constraints; it does not hold an execution lease while the
   operator is idle. Fill start acquires the execution lease, re-derives Gate B/tasks, and requires the
   current work to be equivalent to that approval before writing. Crash/auto-resume allows only
   **monotonic progress**: satisfied tasks may disappear, present-file sets may grow, and missing-work
   sets may shrink on the same approved targets; new requirements, expanded missing work, changed
   targets/sources/policy, or a non-`FEASIBLE` Gate B require a fresh preview. A legacy/migrated selection
   with no approval fingerprint is never silently grandfathered.

## Revised approach per issue

### #35 — append-only clean-anchor free-space evidence
- Per identity, append `clean_anchor` rows (`anchor_free_bytes`, `identity_capacity_epoch`, `anchor_at`,
  `mount_identity`, exclusivity policy/evidence); plus append-only/diagnostic observations and a
  generation-based `dirty` marker. `free_evidence` is **derived at read time**, not stored as mutable
  current truth. No writable current-free field.
- Evidence precedence follows invariant 1: identity-proven mounted `df` is `live` even while dirty;
  otherwise dirty ⇒ `unknown`; otherwise an offline clean anchor is `anchor_estimate` only while the
  identity/epoch-scoped exclusive-write guarantee remains valid. Non-exclusive/unfenceable volumes are
  `stale`/`unknown` offline. No allocation/release ledger and no watermark-over-mutable-tables
  differencing.
- **Recovery from `unknown`** = a mounted inventory/reconciliation (prove identity → inventory staging/
  orphans → reconcile catalog↔annex → generation-CAS a clean anchor). Populated drives fully supported.
- **Registration is an untrusted preparation phase**: clone/annex-init may allocate before a trusted
  anchor exists, so the **first clean anchor is published only after init + reconciliation succeed**; a
  crash before that leaves the drive `unknown`, never a half-trusted baseline.
- **Drift remains a diagnostic integrity signal:** when an identity-proven clean volume is remounted,
  compare live free with the latest anchor under the same epoch and surface unexplained disagreement;
  never silently rewrite/promote the anchor from that observation. A synchronous reconciliation may
  append a new anchor only after explaining or explicitly acknowledging the drift.
- Consolidate every consumer onto one evidence path (`library_api.py:23` currently reports raw
  `free_bytes`). Migration fail-closed: unprovable provenance ⇒ `unknown`, recover via the anchor path.

### #36a — reconciler emits partial *alternatives* + deterministic costs (no choosing/pinning)
- Root cause: `_choose_partial` (reconcile.py:353) sets a hard `pinned_target` before placement, honored
  even when the remainder can't fit (capacity.py:714).
- 36a exposes, for each required copy, all partial candidates **and eligible fresh targets** with
  deterministic costs (reusable present files, missing-work identity, supported finish-in-place vs
  fresh-target re-download cost). It makes **no**
  feasibility judgment and sets **no** pin. Reuse *ranking* is owned entirely by #38 (objective 4),
  which consumes these costs — there is no separate 36b implementation phase; "reuse preference" becomes
  #38 acceptance/policy validation, not its own PR.
- **Never delete verified rows** — a non-chosen partial's files are preserved as policy-drift until an
  explicit #37 op. Annex-to-annex relocation is **not an executable candidate in this phase** (omit it
  or price it as unsupported/infinite); it may be displayed only as a future advisory.
- Enumerate: metadata-only stub, meaningful shard partial, insufficient partial target with feasible
  fresh target, multiple partial drives, GGUF/PyTorch/aux-only/zero-byte, one enormous completed shard,
  protected/bulk/replica behavior, stop/crash durability, and target stability across unchanged replans.

### #38 — shared feasibility + placement engine, `tiered_v2`
- Owns **all** feasibility (invariant 6 graded outcomes) **and** objective ranking including reuse
  (invariant 7). Consumes 36a alternatives+costs. This removes the #36↔#38 circularity: **36a → #38**,
  with reuse cost defined in 36a and ranked in #38.
- Ordered objectives: (1) tier/failure-domain; (2) immovable work; (3) feasible whole-model arrangement
  (the safety verdict); (4) minimize supported movement/re-download cost (consumes 36a costs;
  annex-to-annex relocation enters only when executable); (5) preserve large
  contiguous blocks (invariant 7 definition); (6) minimize idle-drive count *low priority*; (7)
  deterministic label tiebreak. Objectives 4–7 are the deterministic best-effort improvement pass, not
  a Gate-B safety condition.
- Search bound is a deterministic state-count over a canonical traversal; wall-clock is only an
  emergency cap. Feasibility-search exhaustion with no assignment is `PACKING_INCONCLUSIVE`; resource
  exhaustion during the later improvement pass returns the best deterministic feasible assignment with
  `optimization_truncated`. Correction from review: canonical path already has the label tiebreak
  (capacity.py:753); only the legacy comparison path lacks it, and that is not execution authority.
- `tiered_v2` named policy + decision entry + operator acknowledgement (small-drive idleness was
  intentional consolidation). Protected homes, primaries, replica grouping each specified.

### #37 — orthogonal axes + operations
- Model the three axes (invariant 5). **37a exclude** = `eligibility=excluded` (existing verified copies
  still count); **37b mark-lost + re-home/repair** = `lifecycle=lost` (copies stop counting; repair
  derived) + the `found/reinstate` inverse; **37c retire** = prove no required/unique bytes →
  `lifecycle=retired`, tombstone + reserve identity; **37d drop-copy / unarchive** = destructive, exact
  scope, dry-run dependency report, annex proof, explicit confirmation, idempotent DB recovery.
- Eligibility + lifecycle (not `plan_drives` membership, and not `presence`) gate placement **and**
  bootstrap — `plan.bootstrap()` (plan.py:160) re-adds every registered drive on startup. Reinstate
  changes `lifecycle=lost→active` but preserves `eligibility` (an excluded drive never silently becomes
  enabled). **Complements** (not subsumes) DEF-029.

### #39 — portal guard (early) + catalog-backed lease/CAS commit (final)
- **Early guard (portal-only, explicitly scoped):** refuse `finalize` and every removal path
  (`toggle(...,false)`, `bulk(...,false)`, `clear()` — selection_api.py:33) through a **single shared
  guarded-mutation primitive**, keyed on the fill controller **lease being live** — not the status
  string, which retains terminal states (`fill_worker.py:24`) and never resets to `idle` — including
  stopping-but-not-terminal, sharing the lock with `FillWorker.start()`. **Documented limitation:** an
  external CLI controller is not detected here. Ships to **`main`** on its own branch.
- **Final atomic contract:** lease + `graph_revision` per invariant 8. Preview binds to a **versioned
  canonical serialization built from the immutable planner-input object** (finalized selection,
  manifests/files + archive-policy version, `numcopies`, plan membership, drive roles/RAID/lifecycle/
  eligibility, capacity/anchor facts + epoch, `dirty`/exclusivity evidence, **graph-affecting compression
  config copied into the snapshot**, margins/headroom policy, capacity mode, `tiered_v2`/solver-bound
  version, archived facts, and the mutation) — excluding only display-only volatiles. **Fill executes
  against this immutable snapshot and never rereads the config file**; a raw mid-lease edit is
  unsupported and only observed at the next boundary. Commit runs the invariant-8 protocol (short write
  txn after the solve) requiring **all three**: (a) `graph_revision` unchanged; (b) live Gate B
  `FEASIBLE`; (c) the committed **execution-authority task set** materially equivalent to the preview —
  compared over {requirement/copy id, task kind, target drive, source drive where applicable,
  reused-present/missing-work identity}. Any divergence → reject + fresh preview. Preview covers
  add/remove/clear; no candidate bytes written before accepted admission.
- **Approval→start handoff:** preview/commit persists the approved fingerprint/constraints but holds no
  long-lived lease. `/api/fill/start` (and CLI start) acquires the execution lease, recomputes against
  current live evidence, and accepts only exact approval equivalence before the first write. The lease
  is then held through the terminal worker boundary. A state change while the operator waits therefore
  causes start to reject rather than silently execute a different plan.
- **Crash/auto-resume equivalence is progress-aware, not strict task equality:** completed requirements
  may disappear, present-file sets may grow, and missing-work sets may shrink on the same approved
  targets. New/expanded work, changed target/source/policy, lost approval provenance, or non-feasible
  live Gate B requires a fresh preview. Pre-feature/migrated selections without an approval fingerprint
  fail closed; migration never fabricates operator approval.
- **Physical fencing:** the DB token is checked on every catalog write and at filesystem safe boundaries,
  while same-host controller/per-drive locks exclude a stale process from allocation/publish/annex.
  Lease expiry alone never authorizes takeover past a still-held physical lock. Forced takeover leaves
  affected drives dirty and blocks new writes until identity-proven reconciliation; cross-host NAS
  writers remain unsupported without distributed fencing.

## Sequencing (revised)

0. **Record the DEC** (invariants above).
0b. **Rewrite/split the GitHub issues** to agree with the DEC + this plan.
1. **Portal mutation guard** → **`main`** (independent; explicitly portal-only).
2. **#35** — append-only clean-anchor evidence + mount-identity + dirty protocol + registration-prep +
   migration/recovery (fix branch).
3. **#36a** — reconciler emits partial alternatives + deterministic costs (fix branch).
4. **#38** — shared feasibility+placement engine, graded outcomes, reuse ranking, `tiered_v2` (fix branch).
5. **#37** — 37a → 37b(+reinstate) → 37c → 37d (fix branch, multiple PRs).
6. **#39** — catalog-backed lease/CAS preview/commit + execution lease (fix branch).

## Workflow

- **Decision log first**, then issue restructuring, then code.
- **Portal guard ships independently to `main`.**
- All migration work on the isolated long-lived branch `fix/placement-capacity-hardening`; **one
  reviewable phase per PR**, targeting the fix branch; merge commits (no squash), branches retained.
- **Sync `main` → fix branch** regularly; **never fix branch → `main` mid-effort** (public repo).
- **Final integration PR** (fix branch → `main`) with copied-catalog shadow evidence + explicit rollback
  instructions.

## Source of truth (pre-code blocker — accepted staging)

Issue bodies #35–#39 still hold original text and contradict this plan; that's expected while drafting.
Before any implementation PR: author the DEC; rewrite #35/#38/#39, split #36 → 36a (reuse ranking to
#38) and #37 → 37a–37d, and add invariants, failure codes, migration behavior, and test matrices.

## Acceptance material (per issue, before its PR)

- **#35** — evidence precedence (identity-proven live `df` still authoritative while dirty; dirty only
  invalidates offline anchor), exclusive local volume vs externally-writable/NAS volume, offline latest-
  clean-anchor behavior, dirty⇒offline-unknown, mount-identity failures (wrong volume, missing NAS mount,
  replaced fs, stale path) ⇒ `unknown`, generation-CAS race against a writer starting, drift detection,
  anchor recovery on a populated drive, capacity-epoch resize, same-drive re-registration,
  **registration crashes** (before row creation, during clone, during annex-init, before first anchor),
  NULL vs proven-zero `stored_bytes`, unprovable-provenance⇒`unknown`, migrated-catalog replay, every
  CLI/API/UI consumer.
- **#36a/#38** — alternatives emitted without pinning; global feasibility (a partial fitting itself but
  breaking the fleet is not chosen); insufficient partial with a feasible fresh target; relocation is
  never selected while unsupported; candidate-specific reuse/workspace budgets; protected/bulk/replica;
  stop/crash durability; graded Gate-B outcomes; feasibility-existence vs optimization separation;
  shuffled input/query order; deterministic large-block metric; adversarial packing; feasibility-bound
  exhaustion vs post-feasibility `optimization_truncated`; time/memory/state exhaustion; 10k-candidate
  performance.
- **#37** — offline/excluded/lost/retired orthogonality, found/reinstate, last/unique-copy refusal,
  two-copy policy, bootstrap eligibility, annex-success/DB-failure recovery, dry-run, idempotent reruns,
  tombstone reservation.
- **#39** — lease acquire/renew/expiry/**crash recovery/operator takeover/fencing-token validation**,
  physical-lock exclusion of an expired-but-live writer, forced-takeover→dirty/reconcile, no `BEGIN
  IMMEDIATE` held across the solve, cross-writer atomicity (portal + CLI writers), config-in-snapshot /
  no file reread mid-lease, concurrent previews, approval→later-start state drift, changed live free,
  progress-compatible restart after one/many completed files, systemd auto-resume, missing legacy
  approval→fresh preview, execution-authority-task equivalence, live-lease predicate, all removal paths,
  no candidate bytes before accepted admission.

## Out of scope (tracked separately)

Operational continuation — restart the fill so GLM-5.2 lands on drive-01, then remove the mistakenly-
downloaded `zai-org/GLM-5.2-FP8` after the BF16 is archived — is independent of this code effort.
