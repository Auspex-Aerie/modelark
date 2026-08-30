# Provenance migration — repeatable copied-runtime runbook

Status: **copied-runtime acceptance, Drive #2 loss rehearsal and live declaration,
replacement-media read-only qualification, immutable candidate freeze, stopped side-by-side live
cutover, attended diagnostic-only application swaps, and preview-bound replacement onboarding
passed, including recovery from a cleanly refused archive-parent permission check and one successful
new-identity registration plus its dedicated-local capacity bootstrap; stopped after separately
bootstrapping the NAS and every active local archive drive, exposing bounded reconciliation inventory
classification, and retaining a feasible central plan on 2026-08-29**. LocalModelArk now
runs schema v7 from corrected candidate commit `1d420e0`; the service remains disabled for login
startup and was started without Fill resume. The preserved schema-v2 runtime, cutover capsule, and
earlier application candidates remain rollback/evidence points. The deferred operator-facing
proposal approval surface (DEF-036), proposal approval, and Fill remain separate gates.

Use this runbook to exercise the provenance migration from the current development branch against a
production-shaped, disposable copy of the existing LocalModelArk deployment. The process is designed
to be repeated from the same immutable seed after every correction, and then repeated once more from
a freshly captured seed before live cutover is considered.

The copied-runtime rehearsal itself is not Fill or a live cutover, and this workflow is not
`modelark-migrate` (the legacy ModelDump cutover; see [`legacy-cutover.md`](legacy-cutover.md)). The
migration CLI is `modelark-provenance-migrate` (`scripts/migrate_provenance.py`). Its leftovers-list
behavior is frozen at `b8895d2`; the branch when this runbook was expanded was
`fix/placement-capacity-pr10-content-satisfaction`.

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
- after registering and reconciling Drive #7, then reconciling the separately identity-qualified NAS
  Drive #0 and USB-attached Drive #1, planner revision reached `5`; Drive #0/#1/#7 now have
  identity-bound dedicated-local capacity evidence and the unknown-evidence target set is exactly
  Drive #3 through Drive #6;
- corrected application candidate `890b1dc` resolves attached registered drives by the exact
  filesystem/annex UUID pair, treats serial as supporting/fallback evidence, batches annex membership
  proof, prunes `.git` before filesystem descent, emits bounded progress, and leaves all mutation
  fences intact. Its final wheel SHA-256 is
  `3c2bb7996fba7448ad3e83dceb758af4ebe4b36fa34fb2ccf242efe67959f762`;
- the first `e45c725` swap exposed one additional presentation defect: enriching every attached disk
  allowed an unreadable system mount such as `/boot/efi` to abort `/api/drives`. Candidate `890b1dc`
  records such unrelated topology as `inaccessible` and continues passive inventory. No drive,
  catalog, planner, approval, or Fill state changed during either application-only swap.

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

The walkthrough exposed two bounded issues without invalidating either reconciliation. Both are
remediated and live in application candidate `890b1dc`:

1. Passive inventory now enriches each disk with read-only mounted topology and routes correlation
   through one central resolver. An exact unique filesystem/annex UUID pair is authoritative; serial
   is supporting/fallback evidence only. A bridge mismatch is visible but never written back, and a
   complete conflicting pair cannot fall back to a matching serial. Contracts reproduce both live
   shapes: Drive #0's absent stored serial and Drive #1's `VR1KV4LK` → `5652314B56344C4B` bridge
   discrepancy.
2. Full bootstrap inventory now runs one `git annex whereis --all --in <target-uuid>` query, intersects
   its result with the catalogued key set, and fails every annex claim closed if that command fails.
   The worktree walk prunes `.git` before descent, so it never traverses `.git/annex/objects` merely
   to ignore the results. Bounded CLI milestones cover claims, target-UUID membership, and worktree
   scanning. Missing-copy refusal, extra/debris reporting, controller/drive fences, and the fresh
   final observation are unchanged. The real Drive #0 map returned 2,131 target keys from the new
   read-only query in about half a second.

Expected-red commit `92ced97` failed all seven initial contract points. The first implementation
candidate `e45c725` then exposed a third bounded issue during its live application-only swap:
read-only topology enrichment attempted to inspect the archive namespace on every mounted disk, and
a protected unrelated system mount (`/boot/efi`) raised `PermissionError`, degrading only the Drives
API/UI. Planner and Fill endpoints remained healthy and no mutation ran. Expected-red commit
`e661ae3` reproduces that production shape; corrected candidate `890b1dc` converts the inaccessible
topology to passive evidence and keeps the rest of inventory available.

Final qualification passes 44 correction-focused contracts, 951 non-E2E tests with five known
deprecation warnings, Ruff, JavaScript syntax, installed-wheel topology/resource smoke, and the
standalone portal E2E. The retained final wheel and source archive have SHA-256
`3c2bb7996fba7448ad3e83dceb758af4ebe4b36fa34fb2ccf242efe67959f762` and
`4db6e59538af28ed0df7b00087e164476288aa1d21a2e71927145184f9e9ecfd`. The in-app browser backend
was unavailable for a final attended visual replay, so that check is explicitly not claimed; the
standalone portal E2E and live installed-wheel APIs passed.

The final application-only swap uses the exact existing schema-v7 data/config/state paths. The
service is active but disabled for login startup; approval remains null and Fill is idle and not
resumed. Planner revision remains `5`, plan preview remains bound to selection hash
`b8eb1154a66ec52d8aef85846a8dfdb677cbc471491354b319be579228990dd5`, and both pre/post plan and
preview payloads are byte-identical. Normalized Library planning (excluding only observation time)
and stable Fill state are equal. Live passive inventory maps Drive #0, Drive #1, and Drive #7 by
their exact filesystem/annex pairs, presents Drive #1's bridge serial discrepancy without rewriting
the stored `VR1KV4LK`, and no longer duplicates Drive #0/#1 under unregistered devices. No archive
bytes, drive rows, planner rows, approval, or Fill state were changed by this remediation or swap.

## Executed Drive #3 dedicated-local reconciliation — 2026-08-29

After Drive #7 was cleanly unmounted from the single-drive dock, Drive #3 mounted at
`/media/phaze/drive-03`. Passive inventory and an independent block-device read proved the exact
registered identity before mutation: ext4 filesystem UUID
`c0c1f4d6-c671-490f-ba5b-5cb1907bea04`, annex UUID
`a1121672-fc1e-42fa-93f4-2e7c6be13af4`, and supporting hardware serial `ZKP2L15R`. The operator
explicitly confirmed that Drive #3 is dedicated ModelArk storage.

One `drive reconcile drive-03 --dedicated` then ran against candidate `890b1dc` and the exact live
schema-v7 data/config/state paths. It completed as `bootstrapped`, established epoch/generation
`1/1`, advanced planner revision exactly `5 → 6`, recorded fingerprint
`184063cfc78945208875bc7bdb13102a6c4267f737216ed568e9625300a152d8`, anchored free space at
`983,319,678,976` bytes, and admits `934,152,122,164` usable bytes after primary-drive headroom.
The selection hash remains
`b8eb1154a66ec52d8aef85846a8dfdb677cbc471491354b319be579228990dd5`; Fill remains idle and no
execution started. `CAPACITY_EVIDENCE_UNKNOWN` now names exactly Drive #4, Drive #5, and Drive #6.
Offline Drive #7 remains active/enabled with its prior anchor and was not declared lost.

The successful run exposed a bounded operator-reporting defect. The inventory loaded zero catalogued
Drive #3 claims and scanned 2,109 non-Git worktree entries, but the `Reconciliation` result discarded
the returned `Inventory`, so the CLI did not state their classification. A separate read-only count
found 1,929 git-annex symlinks, 180 regular files, and zero recognized temporary/debris names; under
the existing reconciliation contract all 2,109 entries are therefore retained extras, not
catalogued residency. Nothing was deleted or adopted, and the final free-space observation already
accounts for their occupied bytes, so the Drive #3 identity/capacity anchor remains valid and must
not be repeated merely to recover presentation evidence.

Expected-red commit `60d0990` proves the lost domain result and absent bounded CLI summary.
Implementation commit `1d420e0` threads the same classified `Inventory` through every successful
bootstrap, refresh, accepted-drift, epoch-transition, and recovery outcome, then prints bounded
`present / missing / debris / extra` counts and explicit retain/not-catalogued/not-deleted language.
It adds no scan, planner, adoption, cleanup, or mutation path. Final qualification passes 46 focused
contracts, 951 non-E2E tests with the same five known deprecation warnings, Ruff, JavaScript syntax,
the standalone portal E2E, package build, and an installed-wheel reporting smoke. The retained wheel
and source archive have SHA-256
`5a21df9b80d8bbcd6c038ec7f60621e4ca3a73f88a7843551805d5226a2599ea` and
`2ef261d0bac28f5f0de48fd7578ce414bcfce88c00b8a092770b4d17afe98d25`.

The application-only replacement to `1d420e0` reused the exact live schema-v7 data/config/state
paths. Its deployment check passes; the service is active but remains disabled for login startup,
resume is absent, and Fill is idle. Pre/post Drives, plan, and preview payloads are byte-equivalent;
Library output is equal after removing only observation time, and Fill output is equal after removing
only live network-rate telemetry. Revision remains `6`, the selection hash is unchanged, and
`CAPACITY_EVIDENCE_UNKNOWN` still names exactly Drive #4/#5/#6. No disk scan, reconciliation, catalog
write, approval, archive-byte action, or Fill action ran during the application swap. Drive #4 may
now proceed through the same read-only identity gate.

## Executed Drive #4 dedicated-local reconciliation — 2026-08-29

Drive #3 was cleanly unmounted from the single-drive dock and Drive #4 mounted at
`/media/phaze/drive-04`. Passive inventory plus a host-visible read-only block check proved the exact
registered identity before mutation: ext4 filesystem UUID
`cb040366-62fd-411f-a80f-92ab6367b67a`, annex UUID
`4289b38d-50a6-4662-9214-a15fd74f1f17`, and matching model/serial
`WDC WD10EZEX-08WN4A0` / `WD-WCC6Y2VNC10Z`. The operator explicitly confirmed that Drive #4 is
dedicated ModelArk storage.

One `drive reconcile drive-04 --dedicated` ran through live candidate `1d420e0`. The new bounded
reporting contract rendered `present=0 missing=0 debris=0 extra=2109` and stated that the extras are
retained, not catalogued, and never deleted automatically. The operation completed as
`bootstrapped`, established epoch/generation `1/1`, advanced planner revision exactly `6 → 7`,
recorded fingerprint `25b3c5b8aef1274e676c9df78b9da30289ec7cf76320dfa159dddafa1f61205f`,
anchored free space at `983,320,113,152` bytes, and admits `934,152,556,135` usable replica bytes.
No catalogued claim was missing and no on-disk entry was deleted or adopted.

The central planner now returns `FEASIBLE` with no capacity failures: `317` planned, `79` done,
`104` must, `213` bulk, `0` blocked, and `396` selected. The selection hash remains
`b8eb1154a66ec52d8aef85846a8dfdb677cbc471491354b319be579228990dd5`; Drive #4 currently receives
`653,986,447,916` planned bytes. Drive #5 and Drive #6 remain active/enabled with unknown evidence and
zero planned bytes, but they are not required by the current plan and therefore are not blockers.
Lost/excluded Drive #2 likewise receives zero work. Fill remains idle and no execution started.

## Executed Drive #5 dedicated-local reconciliation — 2026-08-29

Drive #4 was cleanly removed from the single-drive dock and Drive #5 mounted at
`/media/phaze/drive-05`. Passive inventory plus a host-visible read-only block check proved the exact
registered identity before mutation: ext4 filesystem UUID
`2d6429fb-8bd2-466f-8a5b-b16f76f7102a`, annex UUID
`b5bbc45e-b15d-4cc4-b7e3-04fe29f75273`, and matching model/serial
`WDC WD10EZEX-08WN4A0` / `WD-WCC6Y1JPZ5KH`. The operator explicitly confirmed that Drive #5 is
dedicated ModelArk storage.

One `drive reconcile drive-05 --dedicated` ran through live candidate `1d420e0`. The bounded report
rendered `present=0 missing=0 debris=0 extra=2109` and stated that the extras are retained, not
catalogued, and never deleted automatically. The operation completed as `bootstrapped`, established
epoch/generation `1/1`, advanced planner revision exactly `7 → 8`, recorded fingerprint
`37c485aab86bbadf45cbc4a7785d30985fb48cf920ec47eeb61a6234f31061dd`, anchored free space at
`983,319,982,080` bytes, and admits `934,152,425,063` usable primary bytes. No catalogued claim was
missing and no on-disk entry was deleted or adopted.

The central planner remains `FEASIBLE` with no capacity failures: `317` planned, `79` done, `104`
must, `213` bulk, `0` blocked, and `396` selected. The selection hash remains
`b8eb1154a66ec52d8aef85846a8dfdb677cbc471491354b319be579228990dd5`; Drive #5 currently receives
`917,246,746,803` planned bytes across `27` models. Drive #6 remains active/enabled with unknown
evidence and zero planned bytes, but it is not required by the current plan and therefore is not a
blocker. Lost/excluded Drive #2 likewise receives zero work. Fill remains idle and no execution
started.

Two follow-up API probes from the restricted execution environment reported connection refusal even
though the same portal process remained active and listening and the operator could still reach it
in the browser. Repeating the read-only API checks through the host-visible path succeeded and
returned revision `8`, the Drive #5 anchor, and the feasible plan above. No service restart occurred;
this is retained as a probe-path observation, not claimed as a ModelArk outage.

## Executed Drive #6 dedicated-local reconciliation and fleet closure — 2026-08-29

Drive #5 was removed from the single-drive dock and Drive #6 mounted at
`/media/phaze/drive-06`. Passive inventory plus a host-visible read-only block check proved the exact
registered identity before mutation: ext4 filesystem UUID
`0331b951-30c6-4fa5-98a9-e3665b3f4d4f`, annex UUID
`e3c184a5-c9d5-4225-9738-d4c16e0b223c`, and matching model/serial
`ST2000DM001-1CH164` / `Z240GBF3`. The operator explicitly confirmed that Drive #6 is dedicated
ModelArk storage.

One `drive reconcile drive-06 --dedicated` ran through live candidate `1d420e0`. The bounded report
rendered `present=0 missing=0 debris=0 extra=2109` and stated that the extras are retained, not
catalogued, and never deleted automatically. The operation completed as `bootstrapped`, established
epoch/generation `1/1`, advanced planner revision exactly `8 → 9`, recorded fingerprint
`93b9aba22adfc9dab4a6dd73e4382d5ac5326f335f2c118f69ec2b8a15e7c5c3`, anchored free space at
`1,967,815,880,704` bytes, and admits `1,898,458,937,959` usable primary bytes. No catalogued claim
was missing and no on-disk entry was deleted or adopted.

The central planner remains `FEASIBLE` with no capacity failures or blocking diagnostics: `317`
planned, `79` done, `104` must, `213` bulk, `0` blocked, and `396` selected. The selection hash
remains `b8eb1154a66ec52d8aef85846a8dfdb677cbc471491354b319be579228990dd5`; Drive #6 currently receives
`1,700,401,294,559` planned bytes across `159` models. Every active/enabled drive now has live or
clean-anchor capacity evidence. Lost/excluded Drive #2 alone remains evidence-unknown, correctly
contributes zero usable capacity, and receives zero planned work. Fill remains idle and no execution
started.

Fleet closure also reconfirmed that Drive #1 was already successfully bootstrapped at revision `5`.
It retains fingerprint `e9101ca18c421d65b7d77ec0fdabbf52fe7f9f60a5be3abd299740ba54a6604c`,
`388,855,249,972` usable bytes, exact attached filesystem/annex identity, and `378,942,012,288`
planned bytes across `8` models. Its enclosure-reported serial mismatch remains supporting-only and
does not change the registered identity.

Disk/capacity admission is therefore complete for the intended fleet, but Fill is not yet cleared.
The live planner state is revision `9`, `active_approved_proposal_id=NULL`, fencing token `0`, and the
proposal tables contain zero rows. `proposal.create_draft()` and `proposal.approve()` exist and are
tested, while the installed portal/CLI exposes preview and start but no operator-facing draft/review/
approval action. This is the exact deferred gate already recorded as DEF-036, whose revisit condition
is now reached. Starting Fill would correctly refuse `APPROVAL_MISSING`; it must not be bypassed by a
direct database call or alternate planner.

## Current stop point

The immutable migration candidate, stopped side-by-side live cutover, attended Drive #2 loss
declaration, diagnostic-only application correction, read-only replacement preview, and disposable
new-identity registration rehearsal, live registration, and Drive #7/#0/#1/#3/#4/#5/#6 capacity
bootstraps are complete under DEC-072 through DEC-080. The portal is active on `1d420e0` against the
same migrated v7 runtime at revision `9`; the service remains disabled and Fill resume is absent.
The central plan is feasible with no blocked requirements and every active/enabled drive has admitted
capacity evidence. The old v2 runtime, prior service units/candidates (including the bounded
`e45c725` Drives-API failure),
immutable seed, migration work, rollback bundle, and publication leftovers remain retained and
matched. No data rollback was required.

Stop here before any automatic work. The remaining gates are operational and evidence-bound:

1. Deliver DEF-036 through the existing proposal domain: one operator-facing action must create an
   immutable draft from the canonical central planning result, display the exact assignments and
   evidence bound to revision/selection hash, and approve only that reviewed draft through the
   existing fenced CAS/revalidation path. Do not add an alternate planning mode or let the frontend
   reconstruct placement.
2. Adding/removing selection or changing the active plan/capacity policy may proceed while Fill is
   idle, but each real graph mutation advances revision and invalidates any stale draft or approval.
   Re-preview after the final intended edit.
3. After the approval surface is qualified and swapped in, generate and review a fresh proposal over
   the settled revision, approve it explicitly, and only then start or resume Fill through the normal
   execution-session service. Never fabricate approval for the migrated selection.
4. Before public distribution, assign the schema-v7 release a package version distinct from the
   released 0.2.0 source version (DEF-040); development package strings are not a safe migration gate.

Publication staging hardlinks and every failed/refused capsule remain retained evidence under
DEC-063/DEF-038. No provenance repair, `adopt_current`, Fill, drive format, replacement label reuse,
proposal approval, PR merge, or rollback-artifact deletion has occurred.

When adding later application-swap evidence to a retained cutover capsule, always use an explicit
generation-prefixed destination filename and refuse an existing destination; never group-copy a
generic basename such as `modelark.service` into the shared evidence directory. INC-046 records one
immediately detected collision: the original `11d9d6d` unit was restored from its byte-preserved
pre-swap copy and hash-verified, while the final unit was stored separately as
`ce81288-modelark.service`. No live service or catalog state was affected.

## Deployed proposal gate; final catalog choice and human approval pending — 2026-08-30

DEC-082 resolves DEF-036 with exact candidate `a574d6b`. The portal now creates a backend-authored
immutable proposal, displays its complete assignment docket, requires the exact backend phrase for
approval, and keeps approval separate from Fill Start. Exact-source qualification passed 957
non-E2E tests with five known warnings, 18 focused proposal/security/version tests, Ruff, JavaScript
syntax, package build, and the prior standalone portal E2E review/approval path. The retained 0.3.0
wheel SHA-256 is `841cba53896e99bb9973438195d61ef7af53394641eead20cf8ea412800a5e7a`; the source archive SHA-256
is `b17d1223820c67c3ba307fbd629156b3af8a6c273764dc30160adf55a57077f3`.

The application-only swap preserved the existing schema-v7 data, state, and config paths. The first
deployer invocation installed the new unit but its child `systemctl` lacked the user-session bus and
stopped before daemon reload or restart; the old service remained healthy throughout. Direct
`systemctl --user daemon-reload` and `restart` then completed the intended boundary. The documented
deployment check passes. The service is active on `a574d6b`, remains disabled for login startup,
logs `resume=False`, and serves proposal JavaScript byte-identical to candidate source. Pre/post plan
evidence is unchanged: revision `9`, selection hash
`b8eb1154a66ec52d8aef85846a8dfdb677cbc471491354b319be579228990dd5`, gate `FEASIBLE`; Fill is idle
and proposal status is `missing`. No proposal was created or approved and no Fill work ran.

DEC-083 also resolves DEF-040: the schema-v7 line is now package version `0.3.0`, distinct from the
released schema-v2 `0.2.0` line. No tag or public package publication occurred.

The operator chose the tested revision-9 migration/release path before Spark catalog expansion.
DGXSpark's current `config/inference/weight_catalog.yml` names 24 kept or queued repository IDs
checked against the live ModelArk catalog. Only `LiquidAI/LFM2.5-8B-A1B` is currently catalogued,
and it is already selected; the other 23 remain a later catalog/Usable Slice wave under
DEF-CATALOG-005. They are already resident on the Sparks and their weights/recipes are changing
daily, so they do not delay revision-9 review. Any later Spark selection change must advance the
planner revision and invalidate the old approval; revision 9 never covers those additions.

## Executed live revision-9 proposal approval; Fill deliberately idle — 2026-08-30

The operator opened **Review exact placement** in candidate `a574d6b` and personally reviewed the
complete 500-row docket. Stored proposal `8f41c6b6-211a-4f90-9d5f-54ffbc75da2a` is based on revision
`9`, uses `guaranteed` capacity and `state_truncated` derivation, reports `FEASIBLE`, and has canonical
seal `471b2ec32bda25a1eefb0b1773f1112e54d96d0a3b637854ffe1787bb6b91769`. Its totals are 396
repositories, 500 requirements, 183 baseline-satisfied requirements, 317 executable requirements,
20,014,224,990,197 guaranteed bytes, and 18,975,902,645,626 expected bytes.

The operator entered the backend-issued exact phrase and selected **Approve exact placement**. The
live status is `approved_current`; semantic input and execution configuration both match. Approval
advanced the planner revision to `10` atomically while retaining the immutable proposal's
`based_on_revision=9`, as designed for `adopt_current`. Fill remains `idle`, no execution session was
started, and the service remains without automatic resume. **Start Fill** is deliberately postponed
until the operator is physically present to answer drive-loading prompts.

The attended review also exposed two follow-ups without invalidating approval. DEF-042 records the
future operator-directed ability to advance replacement Drive #7 into lost Drive #2's former role.
INC-053 records that the Fill chart retained Drive #2, correctly, but rendered no visible
`lost/excluded` distinction even though the backend already supplied both fields. The UI remediation
is prepared separately and changes presentation only; the approved proposal assigns Drive #2 zero
requirements and receives no execution authority from the chart.
