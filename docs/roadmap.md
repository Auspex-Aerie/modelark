# ModelArk — Roadmap & Backlog

Canonical build order and parked work. **Design rationale** lives in the append-only ledger
(`docs/decision_log.md`); **this file is the plan** (what's next, in what order, done-when) and the
**backlog** (what's parked and why).

> Task numbers (`#NN`) are stable cross-reference labels; a live task board may assign its own IDs.

## Public-release closeout — active

The external audit's numbered code blockers are fixed. Public visibility and the operator's archive
activation are deliberately separate gates: publishing an alpha does not authorize a catalog write,
restore output, service installation, or Fill.

| ID | Work | Done when | Execution gate |
|---|---|---|---|
| **RC-0** | Sanitize and approve release history before visibility | Current tree and every reachable ref are free of credentials, local/runtime data, hardware identifiers, and unintended identity linkage; the canonical remote is rescanned immediately before visibility changes | **Operator owns history/identity acceptance and the visibility switch** |
| **RC-1** | ✅ Harden destructive `drive register --format` | Complete block-device topology checks protect every active/system-backed device; unmount/wipe failures stop; destructive intent is explicit; command construction is covered without touching real disks | Canonical checkout + mocked/synthetic devices only |
| **RC-2** | Resolve roadmap task #30 (“resume re-fetch durability”) | ✅ Reconciled: durable per-(repo,file,drive) completion is covered by DEC-019 and regression tests; interrupted `hf_xet` file restart is the known INC-010 residual, explicitly deferred as DEF-026 | Canonical checkout only |
| **RC-3** | Documentation/governance reconciled; repository settings pending | README, changelog, governance, examples, roadmap, and RFC status match the reviewed implementation; local links and commands validate. Remaining: remove the GitHub description's loadability claim, disable the empty Wiki, and enable dependency alerts plus secret-scanning/push-protection/private-reporting where available | Merge the docs change, then perform an explicit repository-settings review before visibility |
| **RC-4** | ✅ Build the legacy-checkout migration/cutover tool + runbook | Dry-run-first inspection, non-overwriting database backup/manifest, copied-data migration, git-remote plan, validation, and rollback paths are tested without accessing the running checkout | Tooling and fixtures only; **do not touch the live legacy checkout** |
| **RC-5** | ✅ Deployable code release candidate | Minimal user-service deploy, reconciled capacity engine, schema-v2 migration, normal/dev and wheel installs, standalone/browser suites, packaging, and reviewed security/correctness fixes are green through PR #23 | Canonical checkout only; no live archive |
| **RC-6** | Complete operator-attended migrated-runtime acceptance | RFC-001 Phases A–F are complete. Refresh the isolated install from reviewed main; run the read-only legacy-hash audit; separately approve any repair and one real restore; then install the user service and approve the first reconciled Fill | **Phases G/H remain operator-attended; never run autonomously** (DEC-042 / RFC-001) |

Post-release deferrals remain deferrals: the Torch-free StreamZNN package split (`DEF-014`), the
privileged SMART/sudoers remainder of host setup (`DEF-025`), tensor/sub-shard checkpointing
(`DEF-026`), deeper pickle scanning and quarantine (`DEF-011`), and the public
[artifact-support backlog](deferred-artifact-support.md) for the 54 real repositories tracked by
`DEF-030`.

## The Plan epic (#33–#38) — ✅ DONE (2026-07-11)

**Shipped** and verified (unit tests + Playwright): #33 Plan entity +
copy-aware totals (**DEC-030**); #34 registration→plan_drives; #37 per-model capacity failsafe +
provisioning-aware fill + DEF-022 fail-soft replica (**DEC-031**); #35 Plans tab + gate / #36 two
capacity bars / #38 graduated catalog gate / DEF-023 loud oopsies (**DEC-032**); DEF-021 Verifier
(**DEC-033**); DEC-029 conservative RAID headroom folded into the capacity math. All three deferred
DEFs (021/022/023) resolved. The active next step is RFC-001 Phase G, followed by the separately
approved service/Fill work in Phase H.

The historical specification below retains the original `provisioning` terminology. Current code
uses capacity modes: `guaranteed` (formerly `uncompressed`) and `compression_aware` (formerly
`compressed`). DEC-045 replaces the legacy count-based planner with the reconciled work graph and
exact per-drive ledger; Phases 1–3 merged in PRs #10–#12, with the Phase 3 review follow-up in #13.

<details><summary>Original build-order spec (kept for reference)</summary>

**Concept (approved).** A first-class **"Plan"** = {run identity, a catalog selection, a fixed set of
registered drives, one git-annex, its download outputs + metrics}. It fuels a capacity model so the
fill is safe against a finite fleet. **Uncompressed size is the boundary currency** — what decides
"are we out"; compression is a *bonus*, never a bet you fail on. Default provisioning =
**uncompressed** (over-provision → smooth, never runs out); the operator may **override** to bet on
compression, at which point the per-model failsafe carries the risk. `plan.update()` recomputes
full-raw + full-compressed **after every model** (numbers/warnings per shard) in **both** modes, so
unexpected inflation (orphans/bug making actual > expected) is caught too. This is **level 1**
(always-correct failsafe); **level 2** (predictive per-file packing) is deferred → **DEF-016**, with
level 1 remaining the net underneath it. Full design + resolved open questions:
**`decision_log.md` → DEF-016**.

### Build order

| # | Task | Why | Done-when |
|---|------|-----|-----------|
| **33** | **Plan entity** (foundation): `plans` + `plan_drives` tables (schema.sql + `_MIGRATIONS`); new `modelark/plan.py` (create/list/get/active/set_active/add_drive + `totals()`); idempotent bootstrap of plan **`ark`** owning all 7 drives + the existing global selection/archived | Everything else reads these three numbers | `plan.totals(con,'ark')` returns sane numbers read-only vs the live catalog; bootstrap idempotent; tests pass |
| **34** | **registration → plan_drives**: `drive register` adds the drive to the active plan | The plan's drive set = the registered fleet = the only capacity that exists → this is what prevents the operator from exceeding | registering a drive puts it in `plan_drives`; capacity reflects it |
| **35** | **Plans tab (leftmost) + gate**: create + recall (no delete yet); all other tabs greyed until the operator **explicitly** selects a plan (no auto-select); select → sets active server-side + reloads the app | Force an explicit choice; no accidental fills against the wrong set | fresh load shows only Plans usable; create-from-empty works; select → tabs enable + reload; verified in the playwright harness. **Guard: never lock out** — bootstrapped `ark` is always selectable |
| **36** | **Two capacity bars**: always-on fully-COMPRESSED + fully-UNCOMPRESSED footprint, both vs the plan capacity line, on the Fill run page + catalog selection view | The gap = the compression dividend in drive terms; see how much you're betting | both bars render from `plan.totals()`, update per shard |
| **37** | **Per-model uncompressed failsafe**: at each model boundary, `plan.update()` then check the NEXT model's RAW size vs remaining live free across `plan_drives`; won't fit → STOP cleanly with state `plan-capacity-stop` + "add a drive to the plan, then re-run" | The always-on safety; never overflow, never surprise the end of a drive | a synthetic tiny-drive unit test stops at a boundary + emits the state; runs in both provisioning modes. **Edge:** a single model bigger than any one drive → GATE-B flags unplaceable up front |
| **38** | **Graduated catalog gate**: (1) under soft line → show bars; (2) compressed crosses threshold → refine by estimating ZipNN sizes for known-format models + soft warn; (3) climbing → warn harder; (4) compressed would exceed TOTAL capacity → **prevent** adding more | Stop over-committing at selection time | the four tiers behave; prevent actually blocks |

### #33 schema shape (so it is not lost)
- `plans(plan_id PK, name, annex_root, provisioning DEFAULT 'uncompressed', status, created_at, notes)`
- `plan_drives(plan_id, drive_label PK)`
- `plan.totals(con, plan_id)` → three numbers, computed **live** (not stored snapshots):
  - `uncompressed` = Σ raw sizes of the plan's finalized selection
  - `compressed`   = Σ (actual archived bytes + observed-ratio estimate for the rest)
  - `capacity`     = Σ (drive capacity − headroom) over `plan_drives`
- `selection` / `archived` stay **global** for the single plan; a future `plan_id` column = the
  multi-plan future (a DEF).

### Cadence — backend first, UI second, nothing disruptive until a restart
- **#33–34 (backend)** are additive/safe: `CREATE IF NOT EXISTS` + `_MIGRATIONS`, a new `plan.py`, a
  wire into registration. They don't touch existing data; the running fill keeps going on the old
  code — the new schema/entity only matter on the next restart. Test #33 by computing the three
  numbers for `ark` **read-only** against the live catalog; make bootstrap idempotent.
- **#35** is the one to get right (entry-flow change). Build + verify in the **playwright harness with
  mock data** (tab order, greyed state, select→reload) before it front-ends the live portal. Guard:
  the gate must never lock you out — `ark` always selectable, create works from the empty state.
- **#36 / #38** read `plan.totals()` live; check against the real numbers + the harness.
- **#37 (failsafe)** is a fill-loop change; unit-test with a **synthetic tiny-drive** so it stops at a
  model boundary and emits the "add a drive" state, rather than waiting to hit it for real.
- **Commit + push after each phase; tests green before each commit.** #33–34 can land while the fill
  runs; #35–38 go live on the next portal restart — batch the "restart to see it" moment rather than
  nagging per phase.
- Per DEF-016, **level 1 gets its own `DEC`** in the ledger when #33/#37 land (the concrete shape).

**Assumptions (operator can override):** plan name `ark`; `provisioning` default `uncompressed`; the
three numbers computed **live**.

</details>

## Parked backlog — demoted behind the Plan epic (still valid, just not next)

| # | Task | Why parked / where it resurfaces |
|---|------|----------------------------------|
| #9  | Scorer | Still valid; not next |
| #14 | Self-describing drives | ◐ annex description + per-key metadata (model/params/format/quant) shipped 2026-07-11 (register.py describe + fetch._annex_metadata); a full on-drive manifest file still deferred |
| #18 | Download-status view | The two-bar + Fill UI now cover much of it |
| #19 | SMART / write-surface hardening | Rides with registration (#34) later — remediation is spec'd in the ledger under "task #19" |
| #21 | Replication tab | Still valid; not next |
| #23 | Library audit | Now naturally plan-scoped |
| #30 | Resume re-fetch durability | **Closed as a duplicate label:** completed-file durability shipped in DEC-019; the only evidenced residual is interrupted-file `hf_xet` restart (INC-010), retained visibly as DEF-026 |

- **Closed: #22** (Library Fill core — done, incl. this session's resilience: supervision/resume,
  compress-crash isolation, download watchdog, SQLite/WAL cutover, reality-tracking placement).
