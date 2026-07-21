# Placement & capacity hardening

Working plan for the fix effort opened after the 2026-07-20 placement/capacity audit, revised across
review rounds toward an implementation-ready spec. The binding invariants go into the decision log, and
the GitHub issues are rewritten/split to agree with it, **before any implementation begins**.
RFC-002 (`docs/rfcs/002-first-class-placement-approval.md`) is the architecture and migration authority;
this file remains the issue-level working plan.

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
| [#39](https://github.com/Auspex-Aerie/modelark/issues/39) | Mid-fill add has no pre-commit preview | guard + normalized proposal/approval/session control |

Deferred on the roadmap, not re-filed: cross-drive shard spanning; multi-RAID copy-#1 home.

## Invariants — recorded in the decision log FIRST (before any code, incl. the guard)

1. **Free-space is anchor-derived via append-only *clean anchors*, with no delta arithmetic over mutable
   tables.** A *clean anchor* is a reconciled free-space observation taken when no managed write is in
   flight, carrying `anchor_free_bytes`, the `identity+capacity epoch`, `anchor_at`, mount-identity proof,
   and the write-exclusivity policy/evidence in force. Evidence selection is ordered: **(a)** a currently
   mounted, identity-proven **and write-fenced** volume uses live `df` even while dirty (a shared remote
   needs the same distributed-fence guarantee as an offline anchor; identity proof without write
   exclusion is diagnostic only); **(b)** if it is not admission-live and dirty, free is `unknown`;
   **(c)** if it is offline, clean, and exclusive-write control has held since the anchor, free is the
   latest clean anchor's `anchor_free_bytes`; **(d)** otherwise it is diagnostic `stale`/`unknown`, never
   admission authority. Dirty invalidates the *offline anchor*, not a fresh fenced live read. A release
   or allocation is reflected by the next clean anchor after reconciliation — never by
   crediting a vanished catalog row or differencing a scalar watermark over mutable/tombstoned rows.
   `anchor_free_bytes` is the raw identity-proven filesystem `available` observation; admission applies
   the versioned safety floor exactly once when deriving usable free. An anchor outside
   `[0, filesystem_capacity_for_epoch]` fails validation and yields `unknown`—it is never silently
   clamped. Empty registration is the trivial first clean anchor; a full mounted inventory/
   reconciliation appends a fresh clean anchor for an already-populated drive — the supported recovery
   from `unknown`. Each anchor row is immutable; a resize is an explicit, audited `identity+capacity`
   epoch transition that appends a new anchor.
2. **`live`/anchor require positive mount-identity proof.** `df` or a fresh anchor is authority only
   after proving the path is the expected volume at the expected epoch — block: fs UUID/device; NAS/
   special-remote: expected mount source + remote identity. Absent NAS mount (df on host fs), wrong
   volume at a known path, replaced filesystem, or stale mount path ⇒ `unknown`.
   **Dedicated/exclusive-volume assumption is explicit and fail-closed:** if another host, process, or
   non-ModelArk workflow may write while ModelArk cannot observe/fence it (especially a NAS/special
   remote), an offline anchor is not admission evidence. The exclusivity policy is persisted,
   identity/epoch-scoped, and part of the planner revision. Cross-host NAS writers remain unsupported;
   this effort does not introduce a distributed-fence framework. For a mounted remote to be authority,
   exclusive write control must be enforceable from this host and held continuously from before the
   authoritative `df` through allocation, publish, catalog commit, and clean-anchor publication.
   Observation-only exclusion does not close the TOCTOU window and is not admission authority.
3. **Dirty protocol (generation-based, single-writer, crash-safe).** Dirtied **durably before any
   filesystem allocation**, held through staging → publish → annex → catalog commit, cleared only after
   all participating writes finish and reconciliation appends a new clean anchor; a crash at every
   boundary leaves it dirty; **no writer clears dirtiness set by another** (generation/counter or durable
   single-writer lease). Covers **every ModelArk filesystem mutation**, not only durable payloads:
   download, replication, restore, relocation, registration/annex-init, recovery, directory/staging/
   temp creation, git-annex bookkeeping, cleanup, and mutating writability probes. Prefer non-mutating
   presence checks while awaiting a drive; any write/read/delete probe runs only after the writer fence
   is held and the dirty generation is durable. Anchor publication is a generation CAS: snapshot the
   generation, prove no active writer, reconcile, then append+clear only if the generation is unchanged
   and the writer lock is still idle; otherwise discard the candidate anchor and remain dirty. NULL
   `stored_bytes` is missing evidence, never a proven zero. #35 reserves nullable owner-session-id and
   fencing-token fields on each dirty generation (both null or both present); they remain null until #39
   begins attributing Fill mutations, avoiding a second dirty-state migration for session recovery.
4. **Durable truth is never auto-deleted** — a verified `archived` row is removed only by an explicit,
   scoped, confirmed operation.
5. **Drive properties are orthogonal:** durable `lifecycle` ∈ {active, lost, retired} and durable
   `eligibility` ∈ {enabled, excluded}; **`presence` ∈ {mounted, offline, unknown} is derived,
   timestamped, identity-proven observation — never persisted durable truth** (a stored "mounted" is
   false the moment the device/process disappears). A drive can be offline *and* excluded; re-plugging
   an excluded drive must not re-enable it. `found/reinstate` returns a `lost` drive to counting only
   after identity proof + reconcile/fsck. `retire` tombstones and **permanently reserves** the identity/
   label (never deletes). Reinstate applies only to `lost→active`: a retired identity that reappears
   remains retired and is rejected as a registration target; resurrecting it would require a separately
   designed explicit operation outside this effort. Offline ≠ lost.
6. **Gate B is a *safety* verdict about feasibility existence** (not optimality), with explicit
   mixed-evidence precedence. Unknown-evidence drives contribute **zero admissible free** to an
   executable assignment—they never poison a plan that fits on known evidence and never make a plan
   `FEASIBLE`. Every maximum in this ladder is **usable capacity for the epoch after the same safety-
   floor/headroom policy used by execution—never raw device capacity**. The ladder is: **(1)** report a
   structural/policy failure immediately when it is independent of current free (for example, one
   requirement exceeds every policy-permitted candidate drive's maximum usable capacity); **(2)**
   search using known-evidence drives plus zero for unknown drives—an assignment found here is
   `FEASIBLE`, bound exhaustion
   is immediately `PACKING_INCONCLUSIVE` (do not blame unknown evidence while a known-only packing may
   exist), and proven infeasibility with no relevant unknown drive is
   `INFEASIBLE_UNDER_ADMISSION_BUDGET`; **(3)** only after known-only infeasibility is proven and a
   relevant unknown drive exists, run a non-executable optimistic sensitivity search that gives each
   unknown drive its maximum **usable** capacity; an assignment relying on that capacity is
   `CAPACITY_EVIDENCE_UNKNOWN` (name the drives to resolve); **(4)** if even the optimistic usable space
   is exhaustively impossible, return `INFEASIBLE_EVEN_AT_OPTIMISTIC_USABLE_CAPACITY` with action
   add-capacity/trim-selection/change-hard-constraints—not “resolve evidence”; **(5)** if the optimistic
   search exhausts its deterministic bound, return `PACKING_INCONCLUSIVE` with the unknown-drive
   diagnostics. Never assert physical impossibility beyond the admitted/optimistic policy bounds. Gate B
   refuses start/commit on anything but `FEASIBLE`, naming the outcome **and the capacity mode under
   which it was derived**. `guaranteed` is raw-bounded admission; `compression_aware` is explicitly an
   estimate-backed admission that may pause safely if actual durable bytes exceed the forecast. The
   latter is never presented as a completion guarantee. Planned-raw codec paths budget raw durable bytes,
   and every compression fallback proves the raw result fits before publication; neither mode may cross
   the safety floor.
7. **Placement optimization is separate from safety, and is one concrete deterministic rule.** After a
   feasible assignment exists, a **deterministic best-effort improvement pass** ranks it
   **lexicographically** over the ordered objectives — it is *not* required to prove optimality (that
   would be far harder than proving feasibility at 10k candidates). "Preserve large contiguous blocks" is
   defined as **lexicographically maximizing the descending-sorted vector of remaining per-drive free**.
   `tiered_v2` versions this exact rule; quality beyond feasibility is advisory. The semantic
   improvement bound is **only a deterministic state-count**: exhausting it returns the deterministic
   best-so-far assignment with `optimization_truncated`. Time/memory limits are emergency caps, not
   semantic truncation; hitting one discards nondeterministic best-so-far state, surfaces
   `optimization_resource_exhausted`, and falls back to the canonical first-feasible assignment. The
   preview shows and fingerprints the resulting exact assignment plus derivation mode (`optimized` /
   `state_truncated` / `canonical_fallback`). **Optimization runs only to produce the preview; commit,
   Fill start, and resume never rerun it.** They validate the exact approved assignment against the
   approved proposal, monotonic progress, and current **admission-authoritative evidence** (live `df` or
   a valid clean anchor—not “live capacity” only). This preserves the reviewed placement without load-
   dependent reproduction/retry behavior. `PACKING_INCONCLUSIVE` remains reserved for the feasibility
   search.
8. **Serialized control is catalog-backed with a real lease and physical writer exclusion.** A
   monotonic catalog `planner_revision` (the current `graph_revision` concept) plus a durable lease
   defining owner/session id, acquire/renew, heartbeat/expiry, crash recovery, safe operator takeover,
   and a **monotonically increasing fencing token validated on every worker catalog write**. Because a
   DB token alone cannot fence filesystem side effects, every allocating/publish/annex path also holds a
   same-host process/per-drive writer
   lock and revalidates its token at safe boundaries. An expired catalog lease is not taken over while
   the prior physical lock is held; forced takeover marks affected drives dirty and requires
   reconciliation before new writes. The commit
   protocol does **not** hold `BEGIN IMMEDIATE` across the solve: acquire lease →
   snapshot `planner_revision` → compute → `BEGIN IMMEDIATE` (short) → recheck revision+token → commit or
   abort. Every graph-affecting catalog mutation—including archive progress, anchor/dirty transitions,
   identity/epoch/policy changes, and selection changes—increments it. The canonical serialization is
   comparison **evidence**; `planner_revision` is the fast preview→commit CAS pre-check, not semantic
   authority. Approval and every start/resume reconstruct current planner input and recompute the
   requirement set plus selection/manifest/identity/policy/config hashes. Those comparisons are
   authoritative even if a supported writer accidentally misses a revision bump.
   Every supported graph writer (portal, `protect`, capacity-mode/registration, a second portal, an
   external CLI Fill) validates the token; a *universal* execution-lease guarantee holds only once they
   all do. A live `starting`/`running`/`stopping` session refuses operator graph mutations. `paused` and
   `blocked` are non-live historical states: the lease/heartbeat and every process/per-drive lock are
   released so identity-proven reconciliation and other mutations cannot deadlock; resume performs the
   full authoritative validation and creates a new `starting` session with a fresh fencing token and an
   audit link to the immutable terminal predecessor. Terminal rows are never reactivated; multiple
   historical rows may reference one approval, but at most one session may be live. The early portal
   guard is explicitly portal-scoped. Graph writers conservatively advance `planner_revision` unless
   they prove canonical before/after equality; a false-positive bump is safe over-invalidation, while a
   false-negative is an implementation defect caught by authoritative semantic recomputation.
9. **Preview CAS, durable approval, and execution projection are three distinct contracts.**
   **(a) Preview→commit:** the preview is bound to one `planner_revision`; commit requires strict CAS and
   atomically applies the mutation and approves one immutable, normalized **PlacementProposal**; there is
   no separately writable approval blob/task copy. **(b) Approved proposal:** contains desired
   requirements, exact target/source map, baseline missing/present-work identity, drive identities+
   capacity epochs, manifest hashes/task-relevant file evidence, and every graph-affecting policy/config
   version. It remains durable while the operator is idle and is not invalidated merely because the
   catalog revision advances. **(c) Execution projection:** Fill start/resume acquires the
   execution lease and derives current remaining work constrained to that approved proposal; it never reruns
   placement optimization. The projection may differ only by an allowlisted monotonic rebase: satisfied
   requirements disappear, present-file sets grow, missing-work sets shrink on the same approved targets,
   or capacity evidence refreshes while identity, epoch, exclusivity policy, manifest/policy hashes, and task
   mapping stay fixed. It must remain `FEASIBLE` using current admission-authoritative evidence. On
   success, the execution lease binds to the current `planner_revision` and fencing token; new/expanded
   work, changed identity/epoch/target/source/policy/manifest, or infeasibility requires a fresh
   preview. Thus dirty→clean reconciliation and a clean offline target's later remount do not themselves
   erase approval when the constrained task map still fits; an offline approved target follows GATE-A's
   await path. A legacy/migrated selection with no approved proposal is never grandfathered. Switching
   the active plan is allowed only without a live execution session; it atomically supersedes and clears
   the active approval, changes the plan, and bumps `planner_revision`. The newly active plan requires a
   fresh preview/approval, and switching back never reactivates an old proposal.
10. **Approval integrity is manifest-bound without expanding into provider versioning.** Each proposal
   stores a hash of the full canonical manifest and normalized rows only for missing files plus reused
   content that affected its tasks/costs. Existing upstream LFS SHA-256 and archived original hashes are
   bound when available; structural manifest change requires a fresh preview. Normalized rows are the
   sole source of truth, canonical serialization is computed in stable order, and only its hash is
   stored. Explicit tasks cannot broaden to upstream additions; missing/renamed paths fail lookup; every
   download must match its approved byte length before publish. Accepted residual: a same-path, same-size
   content change to an upstream-hashless Git-tracked file may move with provider HEAD; provider commit
   pinning is a separately reviewed follow-up.

## Revised approach per issue

### #35 — append-only clean-anchor free-space evidence
- Per identity, append `clean_anchor` rows (`anchor_free_bytes`, `identity_capacity_epoch`, `anchor_at`,
  `mount_identity`, exclusivity policy/evidence); plus append-only/diagnostic observations and a
  generation-based `dirty` marker. `free_evidence` is **derived at read time**, not stored as mutable
  current truth. No writable current-free field.
- Evidence precedence follows invariant 1: identity-proven **and write-fenced** mounted `df` is
  admission-`live` even while dirty; a mounted shared remote without distributed fencing is diagnostic
  only. Otherwise dirty ⇒ `unknown`; otherwise an offline clean anchor is `anchor_estimate` only while
  the identity/epoch-scoped exclusive-write guarantee remains valid. Non-exclusive/unfenceable volumes
  are `stale`/`unknown` offline. No allocation/release ledger and no watermark-over-mutable-tables
  differencing. Out-of-range anchors are integrity failures, never silently clamped.
- **Recovery from `unknown`** = a mounted inventory/reconciliation (prove identity → inventory staging/
  orphans → reconcile catalog↔annex → generation-CAS a clean anchor). Populated drives fully supported.
- **Registration is an untrusted preparation phase**: clone/annex-init may allocate before a trusted
  anchor exists, so the **first clean anchor is published only after init + reconciliation succeed**; a
  crash before that leaves the drive `unknown`, never a half-trusted baseline.
- **Drift remains a diagnostic integrity signal:** when an identity-proven clean volume is remounted,
  compare live free with the latest anchor under the same epoch and surface unexplained disagreement;
  never silently rewrite/promote the anchor from that observation. Alert only when the absolute delta
  exceeds a versioned, filesystem-aware `drift_tolerance_bytes` (allocation-unit rounding plus a bounded
  metadata/journal allowance); record sub-threshold deltas as diagnostics so benign filesystem metadata
  fluctuation is not presented as corruption. The tolerance is not admission headroom. A synchronous
  reconciliation may append a new anchor only after explaining or explicitly acknowledging the drift.
- Consolidate every consumer onto one evidence path (`library_api.py:23` currently reports raw
  `free_bytes`). Migration fail-closed: unprovable provenance ⇒ `unknown`, recover via the anchor path.
- Reserve nullable `owner_session_id` + `owner_fencing_token` as a paired dirty-generation field in #35.
  Operator/pre-#39 writes leave both null; #39 populates and validates them without requiring #35 to
  create an early FK to the later session table.

### #36a — reconciler emits partial *alternatives* + deterministic costs (no choosing/pinning)
- Root cause: `_choose_partial` (reconcile.py:353) sets a hard `pinned_target` before placement, honored
  even when the remainder can't fit (capacity.py:714).
- 36a exposes, for each required copy, all partial candidates **and eligible fresh targets** with
  deterministic costs (reusable present files, missing-work identity, supported finish-in-place vs
  fresh-target re-download cost). Reuse binds available upstream LFS SHA-256 or archived original-hash
  evidence and the proposal's baseline present/missing identity; unknown legacy provenance is not
  treated as proven free reuse. It makes **no**
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
- Both feasibility and improvement use deterministic state-count semantic bounds over canonical
  traversal. Feasibility-search exhaustion with no assignment is `PACKING_INCONCLUSIVE`; deterministic
  improvement-bound exhaustion returns the deterministic best-so-far assignment with
  `optimization_truncated`. Wall-clock/memory caps are emergency aborts: discard nondeterministic
  best-so-far state, surface `optimization_resource_exhausted`, and use the canonical first-feasible
  assignment in the preview under a fingerprinted fallback mode. Once the operator accepts the preview,
  its exact execution-authority assignment—not a later optimizer run—is validated and executed.
  Correction from review: canonical path already has the label tiebreak (capacity.py:753); only the
  legacy comparison path lacks it, and that is not execution authority.
- Every verdict and task budget carries its capacity mode. `guaranteed` remains raw-bounded;
  `compression_aware` exposes estimate risk through fenced per-file admission and full remaining-map
  revalidation at drive-batch/event boundaries. Codec choice is part of the snapshotted config: a codec
  planned as raw uses a
  raw durable budget, and crash/canary/output-cap fallback may publish raw only after a current-evidence
  guard proves the raw result and workspace preserve the safety floor. An estimate overrun pauses the
  approved plan as `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE`; completed bytes remain durable, and it never
  authorizes an optimizer rerun or target substitution.
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

### #39 — portal guard (early) + normalized proposal/approval/execution control
- **Early guard (portal-only, explicitly scoped):** refuse `finalize` and every removal path
  (`toggle(...,false)`, `bulk(...,false)`, `clear()` — selection_api.py:33) through a **single shared
  guarded-mutation primitive**, keyed on the fill controller **lease being live** — not the status
  string, which retains terminal states (`fill_worker.py:24`) and never resets to `idle` — including
  stopping-but-not-terminal, sharing the lock with `FillWorker.start()`. **Documented limitation:** an
  external CLI controller is not detected here. Ships to **`main`** on its own branch.
- **Final atomic contract:** lease + `planner_revision` per invariants 8–10. Preview binds to a **versioned
  canonical serialization built from the immutable planner-input object** (finalized selection,
  full-manifest hashes + task-relevant files/evidence + archive-policy version, `numcopies`, plan membership,
  drive roles/RAID/lifecycle/eligibility, capacity/anchor facts + epoch, `dirty`/exclusivity evidence,
  **graph-affecting compression config copied into the snapshot**, margins/headroom policy, capacity
  mode, `tiered_v2`/solver-bound
  version, archived facts/provenance, and the mutation) — excluding only display-only volatiles. The
  preview fingerprint binds the resulting exact **execution-authority task set** and the solver's
  placement-derivation mode (the latter is audit evidence, not an instruction to rerun the optimizer).
  **Fill uses the immutable approved proposal plus its constrained execution projection and never
  rereads the config file**; a raw mid-lease edit is unsupported and only observed at the next boundary.
  Commit runs the
  invariant-8 protocol (short write txn after the solve) requiring **all three**: (a)
  `planner_revision` unchanged; (b) the exact approved
  assignment itself—not merely some newly-found alternative—remains Gate-B `FEASIBLE` under current
  admission-authoritative evidence; (c) the committed **execution-authority task set** is materially
  equivalent to the preview, compared over
  {requirement/copy id, task kind, target drive, source drive where applicable, reused-present/missing-
  work identity}. Commit validates the approved assignment against current evidence without rerunning
  placement optimization. If it no longer fits even though another assignment might, reject + fresh
  preview; never substitute the new assignment silently. Preview covers add/remove/clear; no candidate
  bytes are written before accepted admission. The mutation and proposal approval are persisted
  atomically; an accepted selection can never exist without its approval provenance. Proposal header,
  task, and task-file rows are authoritative; API/canonical JSON is derived and only its hash is stored.
- **Revision is an optimization, not the trust root:** approval and every start/resume reconstruct
  current `PlannerInput`, recompute the desired requirements and all semantic hashes, and compare those
  facts with normalized proposal rows. Revision equality permits the detailed check; it never replaces
  it. A missed bump is a writer defect but cannot authorize stale or expanded work.
- **Unchanged-selection approval is explicit:** `adopt_current` previews and approves the already
  finalized selection with equal before/after hashes and no selection-row mutation. It retains every
  normal semantic check, CAS, exact assignment, approval lifecycle, pointer, and revision rule; this is
  the required migration/reapproval path, not a bypass.
- **Approval→start handoff:** commit approves the immutable proposal but holds no long-lived lease.
  `/api/fill/start` (and CLI start) acquires the execution lease, derives the execution projection
  **constrained to the approved target/source map**, applies only invariant 9's allowlisted monotonic
  rebase, and validates current admission-authoritative evidence before the first write; it does not
  invoke the optimizer. It then binds the lease to the current `planner_revision` + fencing token and
  holds it through the terminal worker boundary. A non-allowlisted state change while the operator waits
  therefore causes start to reject rather than silently execute a different plan.
  `optimization_resource_exhausted` can occur while producing a preview; that preview may instead show
  the canonical fallback for approval. It is not a commit/start reproduction outcome because those
  boundaries validate the already-approved assignment rather than replaying its derivation.
- **Projection cadence is bounded:** perform full authoritative projection at start/resume, completed
  drive-batch boundaries, and typed state-changing events (gated park/retry, hot-swap return, evidence
  repair/recovery)—not after every file/task. Every file still performs cheap token/identity/live-free
  admission while holding its drive fence, and successful durable files shrink the batch-local missing
  set immediately. Full projection count scales with batches/events, not total tasks/files.
- **Session-state mutation authority:** `starting`, `running`, and `stopping` retain the live lease and
  refuse every operator graph writer. `paused`/`blocked` are non-live historical terminals with the
  heartbeat/lease cleared and every physical lock released, so the operator can mount/reconcile an
  evidence-blocked target or make other graph edits. Resume then runs the authoritative proposal/
  projection checks; evidence-only repair may preserve an unchanged feasible map, while relevant graph
  edits require a fresh preview. An expired live session is not made non-live until process/lock audit or
  forced-dirty recovery proves stale-writer exclusion. Resume creates a new `starting` row and fresh
  fencing token linked to the immutable terminal predecessor; it never reactivates that row.
- **Crash/auto-resume equivalence uses the approved proposal, not strict revision equality:** completed
  requirements may disappear, present-file sets may grow, missing-work sets may shrink on the same
  approved targets, and dirty/anchor evidence may be refreshed. New/expanded work, changed identity/
  epoch/target/source/policy/manifest hash, lost approval provenance, or non-feasible admission under
  current authoritative evidence requires a fresh preview. Pre-feature/migrated selections without an
  approved proposal fail closed; migration never fabricates operator approval. A same approved target
  that is merely offline retains approval and follows GATE-A's await-drive path when its clean anchor is
  sufficient. If it is dirty/unknown, request mount+reconciliation first; require re-preview only if the
  resulting constrained projection violates the approved proposal or no longer fits.
- **Active-plan switching:** reject it while a session is live. Otherwise supersede the current active
  proposal, clear the active-approval pointer, switch the plan, and bump the revision atomically. The
  newly active plan needs a fresh approval; switching back never resurrects a prior approval.
- **Physical fencing:** the DB token is checked on every catalog write and at filesystem safe boundaries,
  while same-host controller/per-drive locks exclude a stale process from allocation/publish/annex.
  Lease expiry alone never authorizes takeover past a still-held physical lock. Forced takeover leaves
  affected drives dirty and blocks new writes until identity-proven reconciliation. Cross-host NAS
  writers remain unsupported; supported same-host exclusion is held from authoritative free-space
  observation through clean-anchor publication.
- **Manifest approval boundary:** full-manifest hash catches added/removed/renamed/resized work;
  task-relevant rows bind exact executable files and reusable hashes, and downloaded length must match
  the approved size before publish. Provider commit pinning is out of scope; document the same-path/
  same-size/hashless-file residual and track it separately.

## Sequencing (revised)

0. **Record the DEC** (invariants above).
0b. **Rewrite/split the GitHub issues** to agree with the DEC + this plan.
1. **Portal mutation guard** → **`main`** (independent; explicitly portal-only).
2. **#35** — append-only clean-anchor evidence + mount-identity + dirty protocol + registration-prep +
   migration/recovery plus only the fact/evidence/write-mutation seams it needs (fix branch).
3. **#36a** — reconciler emits hash/provenance-aware partial alternatives + deterministic costs while
   requirement/candidate construction becomes pure (fix branch).
4. **#38** — pure shared feasibility+placement engine, graded outcomes, reuse ranking, `tiered_v2` (fix
   branch).
5. **#37 schema/gating** — minimal lifecycle+eligibility columns with safe active/enabled migration
   defaults (fix branch; operator operations remain later).
6. **#39** — normalized proposal rows/hash + catalog-backed revision CAS + pure execution projection +
   minimal session/lease and evidence-divergence UX (fix branch).
7. **#37 operations** — exclude → lost/re-home(+reinstate) → retire → drop-copy, multiple PRs.

## Workflow

- **Decision log first**, then issue restructuring, then code.
- **Portal guard ships independently to `main`.**
- All migration work on the isolated long-lived branch `fix/placement-capacity-hardening`; **one
  reviewable phase per PR**, targeting the fix branch; merge commits (no squash), branches retained.
- **Sync `main` → fix branch** regularly; **never fix branch → `main` mid-effort** (public repo).
- **Final integration PR** (fix branch → `main`) with copied-catalog shadow evidence + explicit rollback
  instructions.
- Until step 6 lands, the portal guard covers only portal selection finalize/removal/clear. It does not
  fence discover/manifest refresh, protect/`numcopies`, plan or drive edits, external CLI writers, or the
  old executor's batch-boundary re-planning. Interim safety therefore depends on explicit
  single-operator discipline; the guard is containment, not the final no-drift guarantee.

## Source of truth (pre-code blocker — accepted staging)

Issue bodies #35–#39 still hold original text and contradict this plan; that's expected while drafting.
Before any implementation PR: approve RFC-002; author the binding DEC; rewrite #35/#38/#39, split #36
→ 36a (reuse ranking to #38), and #37 → schema/gating then 37a–37d operations; add invariants, failure
codes, migration behavior, and test matrices.

## Acceptance material (per issue, before its PR)

- **#35** — evidence precedence (identity-proven live `df` still authoritative while dirty; dirty only
  invalidates offline anchor), exclusively controlled vs unfenced remote, same-host exclusion held
  continuously from authoritative `df` through clean-anchor publication, exclusive local volume vs
  externally-writable/NAS volume, offline latest-clean-anchor behavior, dirty⇒offline-unknown, mount-
  identity failures (wrong volume, missing NAS mount, replaced fs, stale path) ⇒ `unknown`, out-of-range
  anchor rejected (not clamped), generation-CAS race against a writer starting, drift below/above the
  versioned filesystem-aware tolerance (and proof the tolerance grants no admission headroom), anchor
  recovery on a populated drive, capacity-epoch resize, same-drive re-registration,
  **registration crashes** (before row creation, during clone, during annex-init, before first anchor),
  mutating writability probe/staging-directory/temp/annex bookkeeping dirties before allocation and
  remains dirty across a crash (or the await probe is non-mutating),
  paired nullable dirty-owner fields migrate in #35 with both-null/both-present enforcement, pre-#39
  writes remain null, and #39 matching session/token attribution drives expired-session recovery,
  NULL vs proven-zero `stored_bytes`, unprovable-provenance⇒`unknown`, migrated-catalog replay, every
  CLI/API/UI consumer.
- **#36a/#38** — alternatives emitted without pinning; global feasibility (a partial fitting itself but
  breaking the fleet is not chosen); insufficient partial with a feasible fresh target; relocation is
  never selected while unsupported; LFS/original-hash-proven partial reuse vs unknown legacy
  provenance; candidate-specific reuse/workspace budgets; protected/bulk/replica;
  stop/crash durability; graded Gate-B outcomes; feasibility-existence vs optimization separation;
  **mixed known/unknown fleet precedence** (known-only feasible wins; known-only inconclusive remains
  packing-inconclusive; proven-known-infeasible then unknown may-help; optimistic still-impossible;
  optimistic inconclusive; a requirement below raw capacity but above every policy-permitted drive's
  usable **post-safety-floor** capacity is structural, never evidence-unknown, and optimistic unknown-
  evidence search uses the same usable maximum), shuffled input/query order; deterministic large-block
  metric;
  adversarial packing; feasibility-bound exhaustion vs deterministic post-feasibility
  `optimization_truncated`; emergency time/memory `optimization_resource_exhausted` canonical fallback;
  preview fingerprints optimized/state-truncated/canonical-fallback output; commit/start validate the
  exact approved assignment without an optimizer rerun; approved assignment no longer fitting while an
  alternative might fit requires a fresh preview; mode-labelled verdicts; planned-raw codec budget,
  raw-fallback fit guard, compression-aware estimate overrun returns
  `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE` without remapping; remaining approved projection revalidation
  after durable progress; 10k-candidate performance.
- **#37** — offline/excluded/lost/retired orthogonality, found/reinstate, last/unique-copy refusal,
  two-copy policy, bootstrap eligibility, annex-success/DB-failure recovery, dry-run, idempotent reruns,
  tombstone reservation, retired identity reappearance stays retired and reinstate refuses it.
- **#39** — lease acquire/renew/expiry/**crash recovery/operator takeover/fencing-token validation**,
  physical-lock exclusion of an expired-but-live writer, forced-takeover→dirty/reconcile, no `BEGIN
  IMMEDIATE` held across the solve, cross-writer atomicity (portal + CLI writers), config-in-snapshot /
  no file reread mid-lease, concurrent previews, approval→later-start state drift, changed current
  admission-authoritative evidence, strict preview→commit `planner_revision` CAS plus authoritative
  requirement/hash recomputation when the revision matches (including a simulated missed-bump writer),
  conservative graph-write change detection (bump unless a canonical no-op is proven),
  atomic mutation+
  proposal approval, rows-authoritative canonical hash with no writable blob, draft→approved→superseded
  lifecycle, explicit `adopt_current` unchanged-selection preview/approval,
  exact-approved-assignment capacity validation without optimizer rerun, alternate-feasible placement still
  requiring fresh approval through an explicit evidence-divergence terminal/UX, progress/evidence-
  compatible pure projection after one/many completed files and dirty→clean reconciliation, rejection
  of expanded/remapped work, batch/event projection cadence with no per-file/full-catalog O(tasks²)
  behavior plus a copied-catalog numerical p95 fixed before the #39 PR, DEC-047 same-task retry priority
  with no generic failure-budget consumption, execution lease binding to
  the rebased revision+fencing token, systemd auto-resume, missing legacy approval→fresh preview, same
  approved target offline→GATE-A await without re-preview, dirty offline target→mount/reconcile then
  compare, full-manifest structural drift and task-file/hash mismatch typed before execution, documented
  same-path/same-size/hashless-file provider-HEAD residual, execution-authority-task equivalence,
  one-time migrated-selection reapproval at a Fill-idle boundary, live-lease predicate, all removal
  paths, `starting`/`running`/`stopping` mutation refusal, `paused`/`blocked` lease+lock release and
  reconciliation without deadlock, resume-as-new-session lineage with a fresh fencing token and no
  concurrent live successor, plan-switch supersede/clear/fresh-approval semantics,
  compression-aware actual-ratio overrun typed as `APPROVED_PLACEMENT_NO_LONGER_FEASIBLE`, no candidate
  bytes before accepted admission.

## Out of scope (tracked separately)

Operational continuation — restart the fill so GLM-5.2 lands on drive-01, then remove the mistakenly-
downloaded `zai-org/GLM-5.2-FP8` after the BF16 is archived — is independent of this code effort.
