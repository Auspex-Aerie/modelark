# Provenance migration — repeatable copied-runtime runbook

Status: **copied-runtime behavioral acceptance, Drive #2 loss rehearsal, and replacement-media
read-only qualification passed on 2026-08-28; live cutover not executed**. The tested source working
tree must still be frozen into an immutable candidate commit/package. The live deployment remains
stopped/disabled on schema v2 and has not been opened by the candidate runtime.

Use this runbook to exercise the provenance migration from the current development branch against a
production-shaped, disposable copy of the existing LocalModelArk deployment. The process is designed
to be repeated from the same immutable seed after every correction, and then repeated once more from
a freshly captured seed before live cutover is considered.

This is not Fill, not a live cutover, and not `modelark-migrate` (the legacy ModelDump cutover; see
[`legacy-cutover.md`](legacy-cutover.md)). The migration CLI is `modelark-provenance-migrate`
(`scripts/migrate_provenance.py`). Its leftovers-list behavior is frozen at `b8895d2`; the branch when
this runbook was expanded was `fix/placement-capacity-pr10-content-satisfaction`.

## Executed acceptance record — 2026-08-28

The DEC-064 behavioral threshold has been met on disposable state. The test session recorded the
branch, base commit, package version/module path, and dirty file set; freezing that exact state into
an immutable candidate artifact remains a pre-cutover gate:

- the final candidate pipeline passed **894 non-E2E tests** (5 deprecation warnings) and the
  standalone portal E2E passed every scenario;
- frozen-seed `run-005`, `run-006`, and `run-007`, plus fresh-capture `run-001`, all produced schema
  v7, integrity `ok`, no foreign-key violations, snapshot SHA-256
  `eec3a1f17326a8950ba7552a4b31b34385998cab9379eec11ab761171190a83a`, and logical content identity
  `084c144230d74077a36bc1af8a26a34d164bed8d99bba5e6878bfb64ea6926d1`;
- every migration classified `3286` rows as `hub_confirmed`, `1208` as `legacy_unknown`, `1122` as
  `null_digest`, and `0` as disagreement;
- current-code planning replay over all four published copies agreed exactly: Library is
  non-feasible with root `CAPACITY_EVIDENCE_UNKNOWN`, all seven drives have unknown admission
  evidence and zero planned bytes, proposal preview contains `183` proven baseline requirements and
  **zero executable tasks**, planner revision remains `0`, and the loopback portal exposes the same
  root gate;
- the copied lost-drive scenario visibly retains `drive-02[lost/excluded]`, assigns it no work, and
  reports 17.19 TB fleet capacity against a 20.39 TB guaranteed/raw forecast (119%);
- the operator repeated the Drive #2 workflow in the copied portal: cancel was a no-op, exact
  confirmation advanced the planner revision once, and the central replan returned
  `CAPACITY_EVIDENCE_UNKNOWN` with zero executable tasks and zero lost-drive targets;
- a separately authorized replacement-media qualification established the expected new Seagate 8 TB
  identity, clean SMART baseline/error log, passing short and conveyance tests, and a clean offline
  no-write ext4 check. It remains unregistered and contributes no capacity. DEC-070 accepts it as a
  replacement candidate; DEF-039 defers the optional extended SMART test;
- the live schema-v2 catalog remains byte-identical at SHA-256
  `07f8aa3907edb80c11d145341c2fb522afce181b7cd533a3df008ed21bf51c1e`; no live rollback was needed.

The published SQLite container hash is evidence only at publication time. Opening a disposable
published catalog through the normal read-write portal/proposal runtime can convert its journal
header to WAL and change container bytes without changing any table row. After runtime replay the
four physical hashes differ, while integrity, foreign keys, schema, planner revision, and the exact
logical content identity above remain equal. Preserve both the original publication hash and the
post-open logical identity; do not misreport normal SQLite representation churn as a graph mutation.

This acceptance authorizes preparation of a separate live-cutover plan only after the exact candidate
state is frozen and its focused identity checks are repeated. It does not authorize drive
qualification/registration, lifecycle mutation in the live catalog, provenance repair, live
publication, proposal approval, or Fill.

## Purpose and acceptance boundary

The run has two products:

1. **Migration evidence** — clone, migrate, validate, and publish a copied catalog without changing
   the seed or any live path.
2. **Copied-runtime acceptance** — run the development-branch CLI and a non-resuming loopback portal
   against the published copy, proving that migration, disk-residency evidence, capacity planning,
   placement projection, and typed blockers still agree.

“Full” in this runbook means full migration and planning acceptance on copied production state. It
does **not** include archive execution or physical-media mutation.

## Non-negotiable controls

1. **Never give a live/XDG path to this tool.** Source, work, destination, state, and configuration
   passed to the development branch are copies or scratch.
2. **Capture before mutation.** A live `catalog.sqlite` copied while WAL writers may exist is not a
   valid seed. Use an already verified consistent backup, or stop every writer and copy the complete
   stable data directory including SQLite sidecars before opening the copy.
3. **Freeze the seed.** Once captured, the source data/config/state copies and capture evidence are
   read-only. Every run starts from that same seed; scenario-specific mutations happen only in a
   per-run copy or the published destination.
4. **Use explicit literal paths.** Do not rely on XDG, `DB_PATH`, shell environment variables,
   `db.configure`, or an inherited working directory. Replace every `<PLACEHOLDER>` below with a
   reviewed literal path before execution.
5. **One run, one capsule.** Never reuse a work directory or destination across baseline runs. A
   refused or failed attempt remains evidence until reviewed.
6. **No execution authority.** Do not use `--apply`, `--resume`, Start Fill, `session start`, `fetch`,
   `repair-drive`, `repair-hashes --apply`, restore, drive registration/reconciliation, or
   `adopt_current`.
7. **Physical storage is observational.** Planning may read recorded residency, current mount
   availability, filesystem capacity, and other non-mutating evidence. It may not write, probe by
   writing, mount/remount, format, fetch annex content, alter annex metadata, or change archive bytes.
8. **Fail closed.** Unexpected mutation, disagreement, sidecar ambiguity, path aliasing, missing
   evidence, an untyped error, or a command trying to execute work ends the run.

The live service may resume after a stable seed has been captured only through its separately
reviewed operator procedure. Nothing in this runbook starts, enables, disables, or reconfigures it.

## Runtime capsule layout

Use a scratch filesystem with enough room for the immutable seed plus several catalog-sized working
copies. `/tmp` is illustrative; choose a same-filesystem location deliberately when testing atomic
publication.

```text
/tmp/modelark-provenance-copy/
  capture/
    source-data/              # required: stable copy of deployed data dir
    source-config/            # required: copied wishlist/config
    source-state/             # captured for fidelity; not used as writable run state
    evidence/                 # hashes, stats, deployed identity, capture notes
  runs/
    run-001/
      source-data/            # writable per-run mirror of the immutable seed
      work/                   # rehearsal clone, reports, rollback evidence
      dest-data/              # empty before publish; receives catalog.sqlite
      state/                  # new writable state/log tree for this run only
      config/                 # per-run config copied from the frozen seed
      evidence/               # command output, before/after identities, disposition
```

`capture/source-state` preserves the deployed runtime shape for analysis. Start ordinary acceptance
with a fresh `runs/<RUN>/state`; copy a selected state artifact into a scenario run only when that
artifact is the behavior under test and record the reason. Never point a test process at the live
state/log directory.

The migration tool receives `runs/<RUN>/source-data`, never `capture/source-data`. Publication
retains `BEGIN IMMEDIATE` on its reported source as a quiescence proof and may normalize SQLite WAL
state, so that source must be a disposable writable mirror. The capture seed remains byte-immutable.

## Preparation gate

Complete and review these items before the first rehearsal command:

- [ ] Record the development branch, exact commit, `git status --short`, and installed package
      identity that will execute the run.
- [ ] Inspect the deployed user service and record its checkout, data, state, and config paths in
      private capture evidence. Do not commit workstation paths or credentials.
- [ ] Record the deployed checkout commit and whether its service unit contains `--resume`.
- [ ] Choose the capture route below and record why it is transactionally valid.
- [ ] Confirm sufficient scratch free space for the seed, work clone, publication staging, published
      catalog, rollback artifacts, and at least one retained failed run.
- [ ] Choose an unused loopback port different from the deployed portal.
- [ ] Run the fresh test pipeline and focused migration contracts; do not infer readiness from an old
      log or a green badge.
- [ ] Review every expanded literal path in the commands. No command may resolve to the live data,
      state, config, checkout, portal port, or XDG defaults.
- [ ] Confirm the stop boundary: copied migration and planning acceptance only; no Fill, repair,
      restore, drive mutation, or live publication.

Suggested identity commands for later execution:

```bash
git branch --show-current
git rev-parse HEAD
git status --short
.venv/bin/modelark-provenance-migrate --help
systemctl --user cat modelark.service
```

If the console script is not installed in the branch venv, stop and determine which artifact is
being tested. The module form is valid for a deliberate source-checkout run:

```bash
.venv/bin/python -m scripts.migrate_provenance --help
```

Do not silently mix the deployed checkout's executable with the development branch's migration code.

## Fresh code pipeline

Run the project pipeline before using the captured state:

```bash
.venv-dev/bin/pytest -q --ignore=tests/test_e2e_portal.py
.venv-dev/bin/python tests/test_e2e_portal.py
.venv-dev/bin/pytest -q \
  tests/test_dec053_054_gate1_contracts.py \
  tests/test_dec053_054_gate2_remediation.py \
  tests/test_catalog_v5_proposal_migration.py \
  tests/test_inc029_gate1_contracts.py \
  tests/test_inc033_gate1_contracts.py \
  tests/test_inc034_gate1_contracts.py \
  tests/test_inc035_gate1_contracts.py \
  tests/test_inc036_gate1_contracts.py \
  tests/test_def035_gate1_contracts.py \
  tests/test_def038_gate1_contracts.py
```

Record exact counts and the commit tested. Any failure stops preparation unless it is an explicitly
authorized expected-red contract in a new gate cycle.

## Capture an immutable deployment-shaped seed

Choose exactly one route.

### Route A — existing verified consistent backup

Use a backup only when its manifest identifies the source, schema version, capture method, integrity,
foreign keys, content/row identity, and hashes. Copy that backup into `capture/source-data`; do not use
the backup artifact itself as writable input. The live service need not stop if this route never reads
the live runtime.

### Route B — attended copy of the local deployment

This route requires a short writer-stopped capture window.

1. Inspect the service unit and all other possible portal, Fill, CLI, and maintenance processes.
2. Stop the service through the operator's normal procedure. If its unit contains `--resume`, treat
   starting it again as a separate execution decision.
3. Prove there is no process holding or writing the deployed catalog and no worker remains.
4. Before any SQLite tool opens the live path, record byte hashes, sizes, modes, ownership, mtimes,
   inode/link information, and presence/absence for `catalog.sqlite`, `-wal`, `-shm`, and `-journal`.
5. Copy the **entire stable data directory** to `capture/source-data`, the exact config to
   `capture/source-config`, and the state directory to `capture/source-state` for fidelity evidence.
6. Re-record the live bundle metadata and hashes. They must be byte-identical to the pre-copy record.
7. From this point onward, no development-branch command may receive a live path.

Illustrative copy shape, after replacing placeholders with reviewed literals:

```bash
mkdir -p /tmp/modelark-provenance-copy/capture/evidence
cp -a <LOCALMODELARK_DATA_DIR> /tmp/modelark-provenance-copy/capture/source-data
mkdir -p /tmp/modelark-provenance-copy/capture/source-config
cp -a <LOCALMODELARK_CONFIG_FILE> \
  /tmp/modelark-provenance-copy/capture/source-config/wishlist.yaml
cp -a <LOCALMODELARK_STATE_DIR> /tmp/modelark-provenance-copy/capture/source-state
```

The `cp -a` above is admissible only after writer quiescence has made the whole directory stable. It
is not an online SQLite snapshot recipe. If quiescence cannot be proven, stop and create a reviewed
SQLite-backup capture procedure instead of improvising.

Freeze the completed capture tree after its evidence manifest is written:

```bash
chmod -R a-w /tmp/modelark-provenance-copy/capture/source-data
chmod -R a-w /tmp/modelark-provenance-copy/capture/source-config
chmod -R a-w /tmp/modelark-provenance-copy/capture/source-state
```

The evidence manifest should record:

- capture route, operator, UTC time, deployed checkout commit, and service state;
- exact source bundle members and physical hashes before/copy/after;
- source data/config/state paths in private evidence only;
- catalog schema version, integrity, foreign-key result, row/content identity, and catalog size;
- copied configuration hash;
- scratch filesystem identity and free space;
- development branch commit and package identity.

Do not publish secrets, usernames, private absolute paths, drive serials, filesystem UUIDs, annex
UUIDs, private network addresses, or credentials.

## Prepare one fresh run

Create a new capsule for every attempt. `run-001` is an example, not a reusable name.

```bash
mkdir -p /tmp/modelark-provenance-copy/runs/run-001
cp -a /tmp/modelark-provenance-copy/capture/source-data \
  /tmp/modelark-provenance-copy/runs/run-001/source-data
chmod -R u+w /tmp/modelark-provenance-copy/runs/run-001/source-data
mkdir -p /tmp/modelark-provenance-copy/runs/run-001/work
mkdir -p /tmp/modelark-provenance-copy/runs/run-001/dest-data
mkdir -p /tmp/modelark-provenance-copy/runs/run-001/state
mkdir -p /tmp/modelark-provenance-copy/runs/run-001/config
mkdir -p /tmp/modelark-provenance-copy/runs/run-001/evidence
cp -a /tmp/modelark-provenance-copy/capture/source-config/wishlist.yaml \
  /tmp/modelark-provenance-copy/runs/run-001/config/wishlist.yaml
```

Before rehearsal, prove the per-run source matches the frozen seed and independently record both
bundles' hashes and metadata into the run's evidence. After the run, compare the **frozen seed** again;
the publisher is allowed to normalize physical SQLite representation only in the per-run source. The
migration tool also captures and verifies its per-run source, but independent evidence protects the
testing boundary itself.

## Execute the copied migration

This section is intentionally ready for a later authorized session. **Do not execute it during
preparation.**

### 1. Rehearse

```bash
.venv/bin/modelark-provenance-migrate rehearse \
  --source-data-dir /tmp/modelark-provenance-copy/runs/run-001/source-data \
  --work-dir /tmp/modelark-provenance-copy/runs/run-001/work \
  --run-id run-001
```

Do not pass `--confirm-stopped` to `rehearse`. It is a parser error by design.

Require all of the following from stdout and the on-disk `report.json` before publication:

- `status=ok` and `manifest_status=validated`;
- source and clone integrity `ok` and empty foreign-key violation lists;
- expected source and clone `user_version` values;
- source and clone content identities present and independently reproducible;
- measured `classification` counts, with `disagreement=0` unless an explicit disposition gate exists;
- a matching snapshot SHA-256 and manifest;
- byte-exact per-run source bundle identity before and after rehearsal;
- byte-exact immutable-seed identity before and after the complete run;
- exact migrated schema, provenance CHECK, `derivation_mode` CHECK, and row/content preservation;
- `runtime_companions.library.json` exactly records the optional source locator's presence, size, and
  SHA-256; malformed, symlinked, or changing locators refuse rehearsal;
- no live, XDG, config, state, archive, or physical-drive mutation.

Contradictory evidence is a successful fail-closed test result, not a reason to guess a provenance
classification or weaken validation.

### 2. Publish to the disposable destination

No process may have opened `run-001/dest-data`. It must be quiet and contain no catalog or SQLite
sidecars.

```bash
.venv/bin/modelark-provenance-migrate publish \
  --work-dir /tmp/modelark-provenance-copy/runs/run-001/work \
  --dest-dir /tmp/modelark-provenance-copy/runs/run-001/dest-data \
  --confirm-stopped MODELARK-STOPPED
```

`MODELARK-STOPPED` means the copied source/destination publication namespace is quiescent; it is not
proof by itself. The operator verifies quiescence before entering it.

Require `status=ok`, `manifest_status=validated`, a published `catalog.sqlite`, an exact
`published_companions.library.json` result, a retained rollback artifact/source bundle, and a
`staging_report`. Publication must fail closed on catalog or locator destination occupancy, sidecars,
insufficient free space, source/destination aliasing, source-locator drift, path replacement, or
staging-state ambiguity. A failed attempt that created any destination member is retained as one
refused capsule; never reuse that destination.

### 3. List publication leftovers

```bash
.venv/bin/modelark-provenance-migrate leftovers \
  --work-dir /tmp/modelark-provenance-copy/runs/run-001/work \
  --dest-dir /tmp/modelark-provenance-copy/runs/run-001/dest-data
```

This command is read-only. It lists `catalog.sqlite` plus the reserved staging main, `-wal`, `-shm`,
and `-journal` names. It has no dispose subcommand and must not follow unbound paths.

Useful fields:

| Field | Meaning |
|-------|---------|
| `present` / `missing` | Whether the named member exists now |
| `dest_relation` | `same_attempt_inode`, `absent`, or `different_inode` relative to destination |
| `allocated_bytes_estimate` | `st_blocks * 512`; an allocation estimate, not logical size |
| `st_nlink` | Link-count evidence used to distinguish a second name from another copy |
| `unbound` | Recorded path is outside the five-name destination bundle and was not followed |

The reserved main is `DEST/.catalog.sqlite.publish-staging`. A successful publication may leave it as
a second hardlink to the published catalog; this consumes a name but normally no additional data
blocks. A failed pre-link publication may leave one catalog-sized copy.

Do not glob-delete. For baseline repetition, retain the refused capsule and start a new run. If an
operator later chooses manual disposal, first rerun `leftovers`, inspect `ls -li` while the destination
is quiet, and follow DEC-063/DEF-038: never remove a name when destination identity or link-count
evidence is ambiguous.

## Copied-runtime planning acceptance

Only after migration and publication evidence pass, use the development-branch executable with all
three explicit runtime paths. These commands target the disposable destination and must omit
`--apply`:

```bash
.venv/bin/modelark \
  --data-dir /tmp/modelark-provenance-copy/runs/run-001/dest-data \
  --state-dir /tmp/modelark-provenance-copy/runs/run-001/state \
  --config /tmp/modelark-provenance-copy/runs/run-001/config/wishlist.yaml \
  plan list

.venv/bin/modelark \
  --data-dir /tmp/modelark-provenance-copy/runs/run-001/dest-data \
  --state-dir /tmp/modelark-provenance-copy/runs/run-001/state \
  --config /tmp/modelark-provenance-copy/runs/run-001/config/wishlist.yaml \
  plan show

.venv/bin/modelark \
  --data-dir /tmp/modelark-provenance-copy/runs/run-001/dest-data \
  --state-dir /tmp/modelark-provenance-copy/runs/run-001/state \
  --config /tmp/modelark-provenance-copy/runs/run-001/config/wishlist.yaml \
  library plan --json

.venv/bin/modelark \
  --data-dir /tmp/modelark-provenance-copy/runs/run-001/dest-data \
  --state-dir /tmp/modelark-provenance-copy/runs/run-001/state \
  --config /tmp/modelark-provenance-copy/runs/run-001/config/wishlist.yaml \
  library plan --explain
```

Validate that:

- the active plan and every durable plan member are preserved exactly once;
- registered identity, role, tier, mount availability, and write availability remain distinct;
- nominal capacity and live available capacity are not conflated;
- already satisfied exact-file work remains satisfied from digest-backed evidence;
- missing, offline, blocked, unknown, and contradictory states remain typed rather than disappearing;
- placement/projection uses the approved/canonical file set and does not invent policy-unfetchable work;
- record overall feasibility, typed blockers, task counts, and per-drive admission evidence from the
  canonical CLI/library projection; do not treat a packing-only `gate_b_code` as overall feasibility;
- repeated diagnostics produce the same logical result and do not bootstrap or mutate the catalog;
- no archive file, annex metadata, selection, proposal approval, execution session, or Fill state changes.

### Non-resuming loopback portal

Choose a reviewed unused port different from the deployed service and do not pass `--resume`:

```bash
.venv/bin/modelark \
  --data-dir /tmp/modelark-provenance-copy/runs/run-001/dest-data \
  --state-dir /tmp/modelark-provenance-copy/runs/run-001/state \
  --config /tmp/modelark-provenance-copy/runs/run-001/config/wishlist.yaml \
  serve --port <UNUSED_LOOPBACK_PORT> --no-open
```

Confirm loopback-only binding, health, and that Plans, Catalog, Disk, Library, Fill, and Verify render
the copied runtime consistently. Fill must show not running and no worker may begin. Do not mutate the
selection, approve a proposal, dismiss blockers, press Start Fill, or exercise any endpoint that
executes or repairs work. Stop the copied portal before inspecting final database and state evidence.

As a read-only cross-surface contract, compare `/api/library/plan` and `/api/plan/preview` with the
CLI/library result for the same stopped copied snapshot. They must agree on whether work is executable
and must preserve the same root typed blockers. A reduced proposal preview may expose fewer details,
but it must not report `FEASIBLE` merely from legacy `free_bytes` when canonical reconciliation,
provenance, identity, or capacity evidence fails closed. Preserve the mismatched capsule and stop if
one surface is green while another has zero executable tasks.

Portal startup may write only its isolated run-state artifacts. Any unexplained logical catalog
mutation without an explicit copied-state action is a finding and stops acceptance.

## Controlled copied-state scenarios

Baseline acceptance uses the immutable captured seed without manual database edits. Each failure or
edge scenario starts from a new capsule or an explicitly named scenario-source copy; never edit the
frozen seed.

Keep scenarios limited to migration, evidence, disk residency, and planning:

- stable source with WAL/SHM sidecars captured before recovery;
- destination main/sidecar occupancy and late no-clobber races;
- interrupted publication, reserved-slot leftovers, and retry refusal;
- insufficient destination free space;
- null digest/provenance, column-versus-annex-key disagreement, and orphan/integrity failure;
- offline, shelved, read-only, lost, or identity-mismatched recorded drives;
- exact-file content satisfaction, policy-blocked selection, and structurally infeasible capacity;
- repeated CLI/portal open and restart against the copied destination without execution.

Use reviewed fixtures or test seams for race/path-injection cases. Do not improvise SQL directly
against the deployed catalog or change real drive state to create a scenario.

## Finding and correction loop

For each finding:

1. Preserve the failed capsule and its exact branch commit, inputs, reports, and typed outcome.
2. Decide whether it is a migration defect, evidence-semantics defect, planning defect, or a test
   control defect. Do not treat a symptom-specific patch as the architectural authority.
3. Inventory the existing helper and sibling subsystem that already answers the same question
   (DEC-057 Gate 0).
4. Add an expected-red contract reproducing the copied-state failure.
5. Implement the smallest architecture-level correction within provenance/disk/planning scope.
6. Run the fresh pipeline, then repeat the entire baseline from a new capsule.
7. Record durable events in `docs/decision_log.md`: DEC for an accepted direction, INC for a defect
   and root cause, DIS for corrected signal interpretation, HYP for a systematic experiment, and DEF
   only for deliberate postponement with a revisit condition.

Discoveries outside this runbook's scope are logged and dispositioned; they do not silently expand
the run into Fill, physical repair, UI redesign, deployment management, or unrelated cleanup.

## Readiness threshold for later live-cutover consideration

Copied-runtime preparation is complete only when all of the following hold:

- the non-E2E, isolated portal E2E, and focused migration pipelines pass on the exact candidate
  commit;
- three consecutive baseline capsules from the same immutable seed produce identical schema,
  classification, content-identity, planning/projection, and typed-blocker evidence;
- a final baseline from a newly captured deployment seed also passes;
- all injected migration/publication failures preserve seed/live state and fail with typed,
  reviewable evidence;
- CLI and non-resuming portal acceptance agree on plan membership, disk residency, capacity,
  satisfaction, and blockers without logical catalog mutation;
- no command relied on XDG/default paths, touched the deployed runtime, or wrote physical storage;
- refused/failed capsules and publication leftovers have explicit retained/quarantined/disposed
  dispositions;
- every architectural decision, defect, interpretation correction, experiment, and deferral found
  during the loop is recorded in the decision ledger;
- DEF-036's operator-facing proposal approval requirement remains visible before any eventual Fill
  resume.

Passing this threshold authorizes only a review of a separate live-cutover plan. It does not authorize
live migration, publication, repair, `adopt_current`, Fill, drive work, service replacement, PR merge,
or deletion of rollback evidence.

## Executed drive-loss UI rehearsal (replacement remains separate)

DEC-069 adds a copied-state operator walkthrough without making attached hardware authoritative. The
walkthrough completed successfully against a fresh copied capsule. Physical-media qualification was
then authorized and completed separately under DEC-070; it did not register, initialize, or bind the
replacement.

The executed walkthrough used a fresh copied/migrated capsule and a portal explicitly bound to that
capsule:

1. Preserve the source and frozen seed. Create a fresh scenario capsule; never reuse a capsule after
   its lifecycle mutation.
2. Open **Drives**. The screen may perform passive attached inventory only when the walkthrough is
   deliberately underway. It must show the old registered Drive #2 separately from any unregistered
   Seagate. Opening this screen must not run SMART, change lifecycle, bind identity, or count the
   unregistered disk as capacity.
3. Confirm the initial catalog remains unchanged: old Drive #2 is `active + enabled`; the Seagate has
   no registered label, plan membership, identity epoch, or capacity authority. “Not attached” means
   offline/missing only.
4. Click **Uh oh — review this drive** for the old identity. Record the preview's drive label, planner
   revision, identity epoch/fingerprint, plan membership, archived rows, replica rows, and exact
   confirmation phrase. Cancel once and prove that cancel is a no-op.
5. Reopen, type `DECLARE LOST drive-02`, and submit. A stale revision/identity binding, wrong phrase,
   or live Fill must refuse without a partial change.
6. On success, require exactly one planner-revision increment; old Drive #2 becomes
   `lost + excluded`; any active approval is superseded/cleared; its plan membership, residency,
   replicas, identity, and provenance remain. The central planner runs once and the UI shows its root
   blocker, executable-task count, updated plan capacity, and zero targets for Drive #2.
7. Stop. No SMART action or replacement registration occurred inside the walkthrough. The later
   separately authorized qualification passed. Replacement registration still requires a new label
   by default, identity-aware admission evidence, and a distinct operator-confirmed workflow; old-label
   reuse remains prohibited without an exceptional decision.

Test the inventory view with injected/mock observations before the physical walkthrough. A mock
unregistered 8 TB Seagate must remain unregistered and must not alter planner revision. The physical
walkthrough is the first point where passive enumeration is allowed; SMART remains separately gated.

## Current stop point

Copied-runtime execution is complete and remains copy-only. DEC-067 supplies one planning authority,
INC-039/INC-040/INC-041 are remediated, the full pipeline is green, three frozen-seed baselines plus
one fresh capture agree, and the lost-drive scenario fails closed visibly. DEC-069's real copied-state
operator walkthrough also passed: cancel made no change, the exact declaration produced revision 1,
and the central replan excluded Drive #2. The physical Seagate was qualified only afterward under a
separate authorization; DEC-070 accepts it as a distinct replacement candidate and DEF-039 defers the
optional extended SMART test.

Stop here before live cutover. The remaining gates are operational and evidence-bound:

1. Freeze the reviewed working tree into an immutable candidate commit/package and repeat the focused
   migration/planning identity checks against that exact artifact. The accepted behavior is green,
   but the current checkout is intentionally uncommitted and is not yet a cutover artifact.
2. Follow the separate side-by-side live procedure in `docs/provenance-live-cutover.md`: preserve the
   stopped schema-v2 deployment as rollback evidence, publish v7 into a new destination, repoint the
   service only after validation, and start without Fill resume.
3. Repeat the Drive #2 loss declaration in the migrated live portal. The copied scenario is evidence,
   not a mutation to the live catalog.
4. Implement and rehearse the remaining DEF-029 replacement operation separately: show the accepted
   candidate's exact identity and media state, require a new label by default, split preview from
   initialization/registration/reconciliation, and never inherit Drive #2 facts.
5. Restore identity-bound capacity evidence for candidate drives. Until that evidence exists,
   `CAPACITY_EVIDENCE_UNKNOWN` correctly permits no executable proposal.
6. Resolve capacity only from admitted registered evidence. The accepted Seagate contributes zero
   planning capacity until registered under its own label, placed in the plan, and given current
   identity-bound capacity evidence. Recompute the fleet centrally after that work and never add an
   alternate planner.
7. No Fill or proposal approval may follow until
   the freshly migrated live preview is feasible, cross-surface identical, and explicitly approved.

Publication staging hardlinks and every failed/refused capsule remain retained evidence under
DEC-063/DEF-038. No live migration/publication, repair, `adopt_current`, Fill, drive
format/mount/register, replacement label reuse, service replacement, PR merge, or rollback-artifact
deletion has yet occurred.
