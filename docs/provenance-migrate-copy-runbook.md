# Provenance migration — repeatable copied-runtime runbook

Status: **copied-runtime acceptance, Drive #2 loss rehearsal and live declaration,
replacement-media read-only qualification, immutable candidate freeze, stopped side-by-side live
cutover, attended diagnostic-only application swaps, and preview-bound replacement onboarding
passed, including recovery from a cleanly refused archive-parent permission check and one successful
new-identity registration plus its dedicated-local capacity bootstrap; stopped before reconciling
the remaining active drives after separately bootstrapping the NAS and Drive #1 on 2026-08-28**.
LocalModelArk now runs schema v7 from permission-guidance candidate commit `e92c354`; the service
remains disabled for login startup and was started without Fill resume. The preserved schema-v2
runtime, cutover capsule, and earlier application candidates remain rollback/evidence points.
Remaining-drive capacity-evidence restoration, attached-identity presentation remediation, proposal
approval, and Fill remain separate gates.

Use this runbook to exercise the provenance migration from the current development branch against a
production-shaped, disposable copy of the existing LocalModelArk deployment. The process is designed
to be repeated from the same immutable seed after every correction, and then repeated once more from
a freshly captured seed before live cutover is considered.

This is not Fill, not a live cutover, and not `modelark-migrate` (the legacy ModelDump cutover; see
[`legacy-cutover.md`](legacy-cutover.md)). The migration CLI is `modelark-provenance-migrate`
(`scripts/migrate_provenance.py`). Its leftovers-list behavior is frozen at `b8895d2`; the branch when
this runbook was expanded was `fix/placement-capacity-pr10-content-satisfaction`.

## Executed acceptance record — 2026-08-28

The DEC-064 behavioral threshold was first met on disposable state and the exact accepted state was
then frozen as commit `11d9d6d` and a wheel with SHA-256
`1589a6168054ffd69b09aa82b8cbb34de76636b0d1ac6f2895718a3e03ecfb2a`:

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
- the frozen candidate passed **916 non-E2E tests**, isolated installed-wheel smoke, standalone portal
  E2E, focused migration/publication fault tests, and package build;
- live rehearsal from the stopped, disposable v2 capture reproduced the accepted classification,
  snapshot hash, and v7 logical identity, including the exact validated `library.json` locator;
- live publication succeeded into a new runtime directory and retained the immutable source seed,
  old service unit, rollback catalog/source bundle, migration report, and publication staging
  evidence;
- the deployed portal uses the pinned candidate checkout/environment, has schema v7 logical identity
  `084c144230d74077a36bc1af8a26a34d164bed8d99bba5e6878bfb64ea6926d1`, planner revision `0`, no
  approved proposal, and no running Fill. Library and proposal preview both fail closed on
  `CAPACITY_EVIDENCE_UNKNOWN` with zero executable tasks;
- passive inventory shows the Seagate as unregistered and takes no action. The migrated live catalog
  still shows Drive #2 as active/enabled until the operator repeats the reviewed loss declaration;
- the preserved old schema-v2 main/WAL/SHM bundle still matches its immutable seed. INC-043 records
  the pre-capture read-only SQLite sidecars; no catalog-content change occurred and no sidecar was
  deleted or normalized;
- after the attended Drive #2 declaration, the application-only `ce81288` swap preserved planner
  revision `1`, Fill idle, disabled startup, and the existing v7 data/config/state paths. It removed
  `drive-02` from unknown-capacity diagnostic targets without changing the compatibility summary:
  `316` planned, `79` done, `104` must, `212` bulk, `1` blocked, and `396` selected.
- after the first live replacement-registration request refused cleanly on a root-owned mount,
  application-only candidate `e92c354` made archive-parent write authority an explicit onboarding
  prerequisite and added backend-authored commands plus directory-layout guidance. The final suite
  passed 942 tests, the wheel SHA-256 is
  `2652c671ec2e2bc9fcd800424011819e6b2ced44668a9d01724f67355b4cf746`, and the attended swap
  retained revision `1`, identical canonical planning, idle Fill, and an untouched replacement
  filesystem.

The published SQLite container hash is evidence only at publication time. Opening a disposable
published catalog through the normal read-write portal/proposal runtime can convert its journal
header to WAL and change container bytes without changing any table row. After runtime replay the
four physical hashes differ, while integrity, foreign keys, schema, planner revision, and the exact
logical content identity above remain equal. Preserve both the original publication hash and the
post-open logical identity; do not misreport normal SQLite representation churn as a graph mutation.

DEC-072 accepts this live cutover. It does not authorize replacement registration, provenance repair,
proposal approval, Fill, old-label reuse, or deletion of any rollback/publication evidence. The
Drive #2 declaration remains an attended, exact-identity operator action in the migrated portal.

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
   Do not run even a URI `mode=ro` integrity/version query against the live path: WAL-mode SQLite may
   create `-shm` or `-wal` coordination files. Copy the stable physical bundle first and run every
   SQLite query against a disposable copy.
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

## Executed live Drive #2 loss declaration — 2026-08-28

The operator repeated the exact-confirmation workflow against the migrated live v7 portal. It
completed once at planner revision `1`: `drive-02` is now `lost + excluded`, remains visible as
historical membership at identity epoch `1`, and has zero planned bytes/targets. The canonical result
is `CAPACITY_EVIDENCE_UNKNOWN`, zero executable tasks, zero lost-drive targets, and 17.19 TB displayed
active capacity. Fill remained idle, the service remained healthy, and the attached Seagate remained
an unregistered observation with no action taken.

Read-only cross-surface verification found the non-authoritative diagnostic defect INC-044. Candidate
`11d9d6d` correctly excluded Drive #2 from the canonical candidate graph, active capacity, and
assignment, but its compatibility projection still emitted one
`capacity_failures[].eligible_drives` row for `drive-02`. DEC-073 corrected the diagnostic target set
at commit `d01cc6e`; the attended application-only swap proved six active-drive failure labels and no
lost-drive label. That first correction also exposed INC-045: associating each failure row with a
different candidate requirement changed the compatibility summary from `316` planned / `1` blocked
to `315` planned / `2` blocked. The service remained fail-closed and Fill stayed idle, but the
presentation regression was not accepted as the final state.

DEC-074 narrows the implementation at commit `ce81288`: failure labels still come only from canonical
placement candidates, while the fleet-wide unknown-evidence gate retains one deterministic
representative requirement for compatibility aggregation. The corrected candidate passed both
expected-red regressions, 112 focused lifecycle/capacity/planning tests, the full 918-test non-E2E
pipeline, standalone portal E2E, package build, and two isolated installed-wheel smokes. Its wheel
SHA-256 is `07ee8c097183c6637f96b2d98d0ab9f4dddc86bc8a44a9e640f45a2ddddd55d4`.

The final attended swap now runs `ce81288` against the unchanged migrated v7 data/config/state paths.
The service is active but disabled, starts with `resume=False`, and reports Fill idle. Planner revision
remains `1`; `drive-02` remains `lost + excluded` with zero usable, archived, and planned bytes; the
Seagate remains an unregistered passive observation with `action_taken=false`; preview remains
`CAPACITY_EVIDENCE_UNKNOWN`; and total planned bytes remain zero. Library now emits exactly six
failure labels (`drive-00`, `drive-01`, `drive-03`, `drive-04`, `drive-05`, `drive-06`), zero lost-drive
mentions, and the pre-correction totals `316/79/104/212/1/396`.

## Executed read-only replacement onboarding preview — 2026-08-28

DEC-075 adds the first DEF-029 replacement gate without adding a registration mutation. Candidate
`e71e234` provides a **Review onboarding** action for every passive unregistered observation. The
server rebinds the exact device path plus serial against a fresh attached-device inventory, inspects
block/filesystem topology without SMART, and then reports:

- a monotonically new `drive-NN` suggestion rather than filling a historical gap or reusing a lost
  label;
- the one existing filesystem target, UUID, mount state, and whether the running system depends on
  it;
- serial, filesystem UUID, existing annex UUID, and occupied archive-namespace collisions;
- every lost/excluded identity and its retained plan/archive/replica dependencies as separate history
  with an explicit `not_inherited` relationship;
- the active plan that a later registration would join and the separate reconciliation requirement;
- one typed next action. No mutation endpoint exists in this candidate.

Contracts first failed against the missing workflow, then passed after implementation. Focused
result: 40 tests passed. The complete suite passed 923 tests with five known deprecation warnings;
Ruff was clean; standalone portal E2E exercised the onboarding modal without SMART or mutation; the
isolated installed-wheel smoke passed; and the package built successfully. The wheel SHA-256 is
`177b37b5c41ade58de91c341c53c518c03dbe0ad48d7b5144c3cca3fc956178a`.

The application-only live swap retained the same schema-v7 data/config/state paths, planner revision
`1`, disabled startup, `resume=False`, idle Fill, and exact planning result (ignoring fresh
`observed_at` timestamps). The live read-only preview binds Seagate serial `ZR16L100` at `/dev/sda` to
one non-system ext4 volume `/dev/sda1`, filesystem UUID
`7db95a52-88f9-48c5-a39e-7a24f2d36588`, and proposed label `drive-07`. It is unmounted, so the sole
blocker/next action is `MOUNT_REQUIRED` / `mount_volume`. Drive #2 remains lost/excluded at identity
epoch 1 with zero archived and replica rows, and the preview inherits none of its facts.

## Prepared confirmation-bound new-identity registration — 2026-08-28

DEC-076 implements the separately gated mutation anticipated by DEC-075. Candidate `c190ce3`
introduced the registration boundary; presentation-complete candidate `8457c16` additionally shows
the operator every bound planning/storage fact: archive namespace state and path, new-identity role,
active plan, and the required post-registration reconciliation. It requires the exact phrase
`REGISTER NEW drive-07` and rebinds planner revision, device path, serial, volume path, filesystem
UUID, mount, absent/prepared archive namespace, proposed label, active plan, and role before any
write. Immediately before initialization it again proves the mounted source, filesystem type and
UUID, hardware serial, and non-system-disk relationship. It runs no SMART, format, or mount
operation.

The physical phase clones the existing git-annex map into a hidden sibling, initializes a new annex
identity, writes an exact local preparation receipt, and atomically promotes it to
`<mount>/modelark`. Unknown content or an annex without that receipt is preserved and refused. If
map synchronization succeeds but the catalog transaction fails, the receipt makes the exact retry
recoverable without blind adoption or cleanup guessing. The catalog phase runs under the central
graph-write boundary: it inserts `drive-07`, adds it once to the active `ark` plan, invalidates stale
approval, and advances planner revision once. It does not inherit Drive #2 facts, reconcile the new
identity, publish capacity authority, approve a proposal, or start Fill.

The expected-red Gate-2 contract first failed in 11 places. Final focused lifecycle/writer/security
result: 57 passed; the final full suite passed 938 tests with five known deprecation warnings in
287.55 seconds; Ruff was clean; standalone portal E2E passed the exact mounted-preview,
operator-visible binding, confirmation, result, and subsequent loss/replan flow; and the installed
wheel passed packaged-resource smoke. The production-shaped disposable rehearsal remains the
`c190ce3` backend proof because `8457c16` changes only static presentation and tests. That rehearsal
used a consistent copy of the migrated catalog and real temporary git-annex repositories: revision
`1` became `2`, `drive-07` joined `ark` exactly once, lost/excluded `drive-02` remained unchanged,
the new drive retained `unknown` evidence and zero executable capacity, a replay was a proven no-op,
integrity was `ok`, and foreign keys were clean. The final wheel SHA-256 is
`72341bf962e4a62c41be736fd87d7a00793ea5f78f8e41e61a485ae7029dcb0d`.

The final application-only live swap from `c190ce3` to `8457c16` retained schema-v7
data/config/state paths, planner revision `1`, disabled startup, `resume=False`, and idle Fill.
Pre/post drive inventory, proposal preview, onboarding preview, normalized Library planning, and
normalized Fill status are identical. Planning remains non-feasible on
`CAPACITY_EVIDENCE_UNKNOWN`, with zero planned bytes, seven catalog drives, no `drive-07`, and totals
`316/79/104/212/1/396`. The mounted Seagate preview is ready with zero blockers and an absent archive
namespace; at that stop point, the served modal exposed the complete binding and no registration
request had run.

## Archive-parent permission gate and operator guidance — 2026-08-28

The first exact-confirmation registration request reached no catalog or archive mutation. The
mounted ext4 root was `root:root 0755`, so the unprivileged portal process could not create
`.modelark.registering-drive-07`; `git clone` refused with
`DRIVE_REGISTRATION_PREPARATION_INCOMPLETE`. No staging or final archive directory remained,
planner revision stayed `1`, `drive-07` was absent, Drive #2 remained lost/excluded, and Fill stayed
idle. This is INC-047.

DEC-078 makes service write authority part of the registration contract. Candidate `0445eb0` first
added the fail-closed backend check and repeated it immediately before physical preparation.
Candidate `e92c354` adds the operator-facing contract:

- report the effective service UID/GID/user/group plus the mounted root's UID/GID/mode and
  write/execute result;
- when access is false, block registration with `ARCHIVE_PARENT_NOT_WRITABLE` and render **You do
  this now** commands generated by the backend for the observed service identity and mount;
- state that ownership preparation is only for a dedicated filesystem, never recursive, and is
  performed by the operator—not by ModelArk;
- show the hidden `.modelark.registering-drive-07/` preparation path and final `modelark/` path,
  while explicitly saying the operator creates neither; and
- require a fresh read-only preview after permission work rather than accepting a stale modal.

For this dedicated empty replacement filesystem, the operator assigned only the mounted root to
`phaze:phaze` and set mode `0750`. The fresh packaged/live probe now reports service identity
`phaze:phaze`, root UID/GID `1000/1000`, mode `0750`, write authority true, an absent archive
namespace, no blockers, and `ready_for_registration=true`. Because the problem is already corrected,
the command block is hidden; the ModelArk-managed directory transition remains visible.

The expected-red contract failed in four places before implementation. Final qualification is 23
focused tests passed, 942 full-suite tests passed with five known deprecation warnings, standalone
portal E2E passed both blocked and ready paths, Ruff is clean, and installed-wheel resource/backend
smoke passed. The immutable wheel SHA-256 is
`2652c671ec2e2bc9fcd800424011819e6b2ced44668a9d01724f67355b4cf746`.

The application-only live swap from `0445eb0` to `e92c354` reused the exact migrated v7
data/config/state paths and retained disabled startup, `resume=False`, and idle Fill. Drive inventory
and proposal preview are byte-identical; normalized Library and Fill output are identical; revision
is still `1`; and the replacement filesystem still contains only `lost+found`. No registration,
reconciliation, approval, archive-byte placement, or Fill state mutation occurred.

## Executed live new-identity registration — 2026-08-28

After a fresh read-only preview proved the corrected mount writable, the operator submitted the exact
phrase `REGISTER NEW drive-07` once. The portal returned: `drive-07` registered as a new identity at
revision `2`, joined plan `ark`, capacity evidence unknown, reconciliation not run, and zero inherited
lost-drive facts.

The immediate read-only acceptance proved:

- `drive-07` is active/enabled, primary, attached by exact serial `ZR16L100`, and joined to `ark`
  exactly once;
- filesystem UUID `7db95a52-88f9-48c5-a39e-7a24f2d36588` and annex UUID
  `6e40f9b6-7cf4-45e8-a32e-f87e6cc30885` agree across the catalog and promoted archive;
- the local receipt binds state `prepared`, label `drive-07`, that filesystem UUID, serial
  `ZR16L100`, and volume `/dev/sda1`;
- the mount contains `modelark/` plus the pre-existing `lost+found`, with no hidden registration
  staging sibling left behind;
- `drive-07` has zero archived and replica rows, identity fingerprint and filesystem-capacity
  authority remain unset, write authority remains `unknown`, and health is `unchecked` as designed;
- Drive #2 remains lost/excluded with its separate identity and historical `ark` membership;
- catalog integrity is `ok`, foreign-key violations are zero, active approval is null, planner
  revision is exactly `2`, and Fill is idle; and
- a repeated onboarding preview refuses with `DRIVE_ONBOARDING_IDENTITY_COLLISION` naming only
  registered `drive-07`.

Canonical planning remains fail-closed on `CAPACITY_EVIDENCE_UNKNOWN`: `drive-07` has zero usable and
planned bytes until reconciliation, appears once among the seven active unknown-evidence targets,
and lost `drive-02` appears zero times. Compatibility totals remain `316/79/104/212/1/396`.

## Executed Drive #7 dedicated-local reconciliation — 2026-08-28

The operator explicitly confirmed that the replacement filesystem is dedicated to ModelArk. The
candidate CLI then ran one `drive reconcile drive-07 --dedicated` against the exact migrated
data/config/state paths, without `--accept-drift`. It returned `bootstrapped`, identity epoch `1`,
generation `1`, and observed free space `7,537,248,694,272` bytes.

Read-only acceptance proved:

- planner revision advanced exactly once from `2` to `3`; active approval remains null;
- identity fingerprint is
  `bd311b67d33a56341d0d7883b47229aad19721a5e8c29a73c636c479285d3329`;
- filesystem capacity is `7,937,390,178,304` bytes, write authority is `dedicated_local`, and the
  catalog drive generation is `1`;
- one append-only `bootstrap` dirty-generation row and one matching clean anchor exist at epoch
  `1` / generation `1`, with exact filesystem and annex identity proofs;
- the clean anchor records `7,537,248,694,272` free bytes and the central planner admits
  `7,378,031,317,044` usable bytes after its headroom policy;
- `drive-07` now reports `evidence_kind=live`, no evidence error, zero archived/planned bytes, and
  zero archived/replica rows;
- catalog integrity is `ok`, foreign-key violations are zero, Fill is idle, archive ownership and
  namespace are unchanged, and no model bytes were copied; and
- the fleet root remains `CAPACITY_EVIDENCE_UNKNOWN`, now with exactly six active failure labels:
  `drive-00`, `drive-01`, `drive-03`, `drive-04`, `drive-05`, and `drive-06`. Lost `drive-02` and
  reconciled `drive-07` are absent from that failure set. Compatibility totals remain
  `316/79/104/212/1/396`.

## Executed Drive #0 and Drive #1 capacity bootstraps — 2026-08-28

The operator brought the single-host Synology iSCSI LUN online at `/dev/sdc` and mounted it at
`/mnt/drive-00`; Drive #1 was also attached through a USB enclosure as `/dev/sdb` and mounted at
`/media/phaze/drive-01`. Read-only qualification proved each registered identity by the stable pair
of filesystem UUID plus annex UUID before mutation:

- Drive #0: filesystem `c43bdf6b-d0e2-4e5a-8785-3f0bee5e8c46`, annex
  `f0a8abe8-06e1-4f67-b084-3759a07fdd36`;
- Drive #1: filesystem `a4f43d95-055a-479c-90e6-a96c8c95a078`, annex
  `adfa44c7-70f4-4cd5-b14a-f8c6fd5f2675`.

The operator explicitly confirmed that the iSCSI LUN remains exclusive to this workstation under
DEC-016's single-host topology and that Drive #1 is dedicated ModelArk storage. Each drive then ran
one separately fenced `--dedicated` reconciliation without `--accept-drift`:

- Drive #0 returned `bootstrapped`, epoch/generation `1/1`, free `187,395,854,336` bytes, advanced
  revision `3 → 4`, recorded fingerprint
  `5b99b36edd36498e97da7f99523d5dd27e3a2ddff96d7deee4b393a3a57f7a3e`, and admits
  `43,618,522,727` usable bytes after RAID headroom;
- Drive #1 returned `bootstrapped`, epoch/generation `1/1`, free `548,071,542,784` bytes, advanced
  revision `4 → 5`, recorded fingerprint
  `e9101ca18c421d65b7d77ec0fdabbf52fe7f9f60a5be3abd299740ba54a6604c`, and admits
  `388,855,249,972` usable bytes after primary-drive headroom.

Both have one matching append-only clean anchor and `dedicated_local` authority. Catalog integrity
is `ok`, foreign-key violations are zero, approval remains null, Fill is idle, compatibility totals
remain `316/79/104/212/1/396`, and the unknown-evidence target set is now exactly `drive-03` through
`drive-06`.

The walkthrough exposed two bounded issues without invalidating either reconciliation:

1. Passive portal inventory correlates only stored and observed hardware serial. Drive #0 has no
   migrated stored serial, while Drive #1's USB enclosure reports bridge serial `5652314B56344C4B`
   instead of stored disk serial `VR1KV4LK`. The UI therefore presents both proven mounted archives
   as registered-but-unobserved plus unregistered observations. Onboarding still refuses because the
   filesystem/annex UUIDs collide, so this is misleading presentation rather than an identity bypass.
   The architectural fix is one shared observation resolver that prioritizes exact filesystem plus
   annex identity, treats serial as supporting evidence, and exposes bridge mismatch explicitly.
2. Full bootstrap inventory performs a separate `git annex whereis --key` subprocess per catalogued
   row and then walks the complete archive tree without progress reporting. It remained fenced and
   fail-closed, but took minutes for each populated drive. Batch annex membership and bounded
   progress reporting should replace the N+1 opaque operator wait before public migration readiness.

## Current stop point

The immutable migration candidate, stopped side-by-side live cutover, attended Drive #2 loss
declaration, diagnostic-only application correction, read-only replacement preview, and disposable
new-identity registration rehearsal, live registration, and Drive #7/#0/#1 capacity bootstraps are
complete under DEC-072 through DEC-078. The portal is active on `e92c354` against the same migrated
v7 runtime at revision `5`; the service remains disabled and Fill resume is absent. The old v2
runtime, prior service units/candidates, immutable seed, migration work, rollback bundle, and
publication leftovers remain retained and matched. No data rollback was required.

Stop here before any automatic work. The remaining gates are operational and evidence-bound:

1. Record and remediate the central attached-identity presentation defect before declaring the
   migration experience user-ready. Reuse one stable resolver across inventory and onboarding;
   never update a stored serial or bind identity merely because a bridge reports a different value.
2. Record and disposition the N+1/opaque full-inventory cost. Optimization must preserve the exact
   per-key target-UUID proof, missing-copy refusal, extra/debris report, held fences, and fresh final
   observation; do not trade correctness for speed.
3. Attach and mount one remaining active drive at a time—never lost `drive-02`—using its normal
   operating-system procedure. First perform read-only filesystem/annex identity qualification;
   attached device names and serial observations alone are not registration or reconciliation
   authority.
4. Reconcile only the proven matching registered identity. Assert `--dedicated` only for storage
   whose exclusivity policy is actually dedicated-local; shared/NAS/unfenceable storage must not be
   promoted by convenience. Preserve each response and require exactly one revision increment per
   successful bootstrap.
5. Continue until the remaining failure labels `drive-03`, `drive-04`, `drive-05`, and `drive-06`
   have either valid identity-bound evidence or an explicit lifecycle/eligibility decision. Do not
   manufacture evidence for unavailable media.
6. Recompute capacity only through the central planner after each accepted reconciliation. Legacy
   `capacity_bytes` and `free_bytes` remain diagnostic until their identity-bound gate passes.
7. Do not approve a proposal or start Fill until the migrated live preview is feasible,
   cross-surface identical, explicitly reviewed, and approved through the normal fenced workflow.
8. Before public distribution, assign the schema-v7 release a package version distinct from the
   released 0.2.0 source version (DEF-040); development package strings are not a safe migration gate.

Publication staging hardlinks and every failed/refused capsule remain retained evidence under
DEC-063/DEF-038. No provenance repair, `adopt_current`, Fill, drive format, remaining-drive
reconciliation, replacement label reuse, proposal approval, PR merge, or rollback-artifact deletion
has occurred.

When adding later application-swap evidence to a retained cutover capsule, always use an explicit
generation-prefixed destination filename and refuse an existing destination; never group-copy a
generic basename such as `modelark.service` into the shared evidence directory. INC-046 records one
immediately detected collision: the original `11d9d6d` unit was restored from its byte-preserved
pre-swap copy and hash-verified, while the final unit was stored separately as
`ce81288-modelark.service`. No live service or catalog state was affected.
