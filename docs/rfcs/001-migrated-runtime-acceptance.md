# RFC-001: Operator-attended migrated-runtime acceptance

- **Status:** in execution — Phases A–F passed; Phase G remediation merged and awaits installation
  plus operator-approved audit/restore
- **Date:** 2026-07-15
- **Owners:** Auspex-Aerie + operator
- **Related:** DEC-035, DEC-037, DEC-038, DEC-040, DEC-042, DEC-044, DEC-045,
  DEF-011, DEF-027, DEF-028, DEF-031, INC-014, INC-015, INC-016, INC-017
- **Execution record:** check boxes are completed only from observed evidence; an unchecked item is
  not implied by a later successful item.

## Summary

This RFC defines the release-candidate acceptance sequence for a backup-first migration from a
legacy ModelDump runtime to canonical ModelArk. It deliberately separates catalog and selection
acceptance from archive-writing fill acceptance.

The first attended session ends after the canonical loopback portal is healthy and the operator is
ready to inspect the migrated plan and choose the model cart. It does not start fill. A later,
separately approved session validates a real restore and eventually resumes fill.

## Why this is a separate RFC

The cutover crosses four boundaries at once: a new installed package, a migrated SQLite schema, a
durable seven-drive plan whose members need not all be online, and a portal that can mutate the model
selection or launch multi-terabyte work. Passing unit tests does not prove those boundaries agree on
the operator's real catalog.

The acceptance procedure must therefore be reviewable before it runs, preserve rollback evidence,
name every write, and stop at human decisions rather than treating a green health endpoint as consent
to resume archive work.

## Safety model

### Plan membership is not mount availability

`plan_drives` is the durable policy set. Nominal plan capacity is calculated from the registered
capacity of every plan member, including shelved drives. Current mount and write availability is
execution evidence, evaluated separately.

Consequences:

- all registered drives may remain members of the migrated plan while only a subset is mounted;
- catalog, plan, capacity-bar, selection-gate, and portal tests do not require every drive online;
- an offline plan member is expected to render unavailable, not disappear from the plan;
- read-only media may support an already-present restore source, but cannot satisfy a fill or replica
  write target;
- no new drive is required merely to test selection or planning;
- a new drive or a smaller cart is required only if the reconciled ledger proves the committed work
  is genuinely infeasible.

### No implicit fill

The acceptance portal is started without `--resume`. No checklist command includes `--apply`, and the
operator does not press **Start Fill** during the first session. Planning diagnostics may derive work
from the migrated catalog, but may not fetch, compress, replicate, register, format, mount, or remount
storage.

### Rollback authority

The stopped legacy catalog, its raw sidecars, the consistent pre-cutover snapshot, the SQL dump, and
the migration manifest remain immutable rollback evidence. The legacy executable must never open the
migrated schema-v2 catalog.

## Path contract

Use explicit, reviewed paths for every command:

```text
INSTALL_ROOT/
├── app/                  canonical clean checkout
├── venv/                 normal non-editable installation
├── runtime/
│   ├── config/           explicit wishlist
│   ├── data/             migrated schema-v2 catalog + library map
│   └── state/            logs and terminal state
├── backups/              pre-cutover and migration manifests/snapshots
└── rehearsal/            disposable restore output only
```

The private git-annex map and physical archive bytes retain their existing locations. A standard
systemd user unit, when later approved, is the only locator stored outside `INSTALL_ROOT`; it points
back to these explicit paths.

## Acceptance checklist

### A. Source freeze and rollback evidence

- [x] Legacy fill is stopped.
- [x] Legacy portal supervisor is stopped, not merely its child process.
- [x] No legacy portal, CLI fill, download, compression, or replica worker remains.
- [x] No process holds the source SQLite/DuckDB catalog or WAL sidecars open.
- [x] The old portal port has no listener.
- [x] A raw SQLite copy including present WAL/SHM sidecars exists.
- [x] A consistent SQLite backup snapshot exists and passes `PRAGMA integrity_check`.
- [x] A portable SQL dump exists.
- [x] SHA-256 values and row counts are recorded in a non-overwriting manifest.
- [x] Source hashes are unchanged by the safety-backup operation.

**Stop condition:** any writer, listener, missing backup, integrity failure, or changing source hash.

### B. Canonical install and identity

- [x] Canonical checkout is clean and equals the then-reviewed `origin/main`.
- [x] Origin is `Auspex-Aerie/modelark`.
- [x] GitHub CLI/API identity was `auspexlabs` before publication actions.
- [x] Installed package is non-editable and imports outside its source checkout.
- [x] `pip check` passes.
- [x] `modelark --help` and installed migration entry points load.
- [x] No Git operation beyond read-only inspection was performed during the recorded runtime
  acceptance pass.

These checks were repeated after PR #19 was merged and installed from reviewed public `main`.

### C. Migration publication

- [x] Destination was absent before migration.
- [x] Migration selected the proven active engine explicitly when multiple legacy catalogs existed.
- [x] Migration worked from a consistent backup and never replaced the source.
- [x] Destination was published atomically.
- [x] Backup and destination manifests report `status=published`.
- [x] Source/destination row counts match, except only documented bootstrap additions.
- [x] Destination `PRAGMA user_version=2`.
- [x] Destination has `plans.capacity_mode` and no `plans.provisioning` column.
- [x] Legacy `uncompressed` maps to `guaranteed`; legacy `compressed` maps to
  `compression_aware` without policy drift.
- [x] Destination integrity and foreign-key checks pass.
- [x] The migrated library map matches the reviewed annex root.

### D. CLI and catalog acceptance

All commands use explicit `--data-dir`, `--state-dir`, and `--config` values.

- [x] CLI help/import passes from the installed environment.
- [x] `modelark plan` shows the migrated active plan and canonical capacity mode.
- [x] `modelark library plan --json` derives placement without `--apply`.
- [x] `modelark library plan --explain` derives the DEC-045 graph/ledger read-only.
- [x] The explain payload has no phantom reservations for satisfied copies.
- [x] Typed manifest/policy diagnostics are preserved rather than silently dropped.
- [x] Plan membership contains every migrated registered drive exactly once.
- [x] Nominal capacity includes every plan member, independent of current mounts.
- [x] Mounted, offline, read-only, primary, replica, and RAID-backed facts remain distinct.
- [x] No catalog count or capacity-mode value changed during the repeated CLI diagnostics.
- [x] No archive file, annex key, drive registration, selection, or fill state changed.

**Stop condition:** schema write, policy drift, unexplained count change, missing plan member,
untyped blocker, archive mutation, or any command attempting execution.

### E. Loopback portal smoke

- [x] Start canonical `modelark serve --no-open` with explicit paths and without `--resume`.
- [x] Confirm the process executable and working tree are canonical.
- [x] Confirm the listener is loopback-only on the reviewed port.
- [x] Confirm the health endpoint responds.
- [x] Confirm Host/Origin/content-type/CSRF protections remain active.
- [x] Confirm Plans, Catalog, Disk, Library, Fill, and Verify views load.
- [x] Confirm the migrated `ark` plan is present and selectable.
- [x] Confirm all registered plan drives remain visible even when shelved.
- [x] Confirm currently mounted drives resolve only where expected.
- [x] Confirm Fill reports not running and no worker begins automatically.
- [x] Confirm capacity forecasts and admission evidence use canonical terminology.
- [x] Confirm no service unit is installed, enabled, or started by this manual smoke.

**Mandatory operator boundary:** stop automated work here. Leave the non-resuming portal available for
the operator to inspect the plan and choose the model cart. Do not select models or press **Start
Fill** on the operator's behalf.

### F. Operator cart selection (later continuation)

- [x] Operator explicitly selects the intended plan for the browser session.
- [x] Operator reviews the migrated selection before changing it.
- [x] Operator chooses and confirms the cart.
- [x] Capacity bars and graduated selection gate update after each change.
- [x] Operator stops before **Start Fill** and hands control back for diagnostics.
- [x] Re-run `library plan --json` and `--explain` against the chosen cart.
- [x] Review exact tasks, targets, dependencies, capacity ledgers, and typed blockers.
- [x] Explain any offline-drive dependency without removing that drive from the durable plan.

No new drive is a test prerequisite. If the chosen cart does not fit, the acceptance result is the
typed capacity failure; resolution is an explicit cart reduction or capacity addition, not a test
fixture disguised as production storage.

### G. Verified restore (later continuation)

- [ ] Operator chooses a small archived repository and disposable destination.
- [ ] Every required archived file has at least one currently readable recorded copy.
- [ ] A read-only `repair-hashes --repo ...` audit reports every legacy hash gap and proves each
      proposed digest against the archive's committed Git blob.
- [ ] If repair is needed, operator separately approves `--apply`, confirms the consistent catalog
      backup, and re-runs the read-only audit before restore.
- [ ] Prefer already-present content; do not rely on annex retrieval onto read-only media.
- [ ] Restore stages atomically beneath `rehearsal/` and never overwrites an existing destination.
- [ ] Every restored file matches its recorded original-byte SHA-256, and every Hub-provided
      canonical SHA-256 remains identical to that evidence.
- [ ] Nested Hugging Face paths are reconstructed correctly.
- [ ] Missing/offline copies are reported without false success.
- [ ] Operator reviews evidence before disposable output cleanup.

### H. Deployment and real fill (separate approval)

- [ ] Resolve any intended write target that is mounted read-only.
- [ ] Review the generated systemd user unit before installation.
- [ ] Install the unit without `--start` and without `--resume-fill`.
- [ ] Confirm executable, data, state, config, port, and resume settings in `ExecStart`.
- [ ] Start the portal once without auto-resume and repeat the health check.
- [ ] Obtain explicit operator approval before enabling resume or pressing **Start Fill**.
- [ ] Confirm the first reconciled task recognizes durable completed files and does not re-fetch them.
- [ ] Confirm live free-space and per-file preflight agree with the selected target.
- [ ] Confirm the next requested drive matches drive-affinity scheduling.
- [ ] Preserve legacy source, rollback checkout, and both backup sets through acceptance.

## Evidence and reporting

The final report records:

- checklist items passed, failed, deferred, or not run;
- canonical commit and package environment;
- sanitized catalog counts, schema version, integrity, and policy mapping;
- durable plan membership separately from current mount/write state;
- graph/ledger diagnostics and any typed blockers;
- portal command line, listener address, and explicit proof that fill did not start;
- restore repository, source drive class, output hashes, and cleanup disposition;
- every operator gate and the exact point at which automation stopped.

Private usernames, absolute workstation paths, drive serials, filesystem UUIDs, annex UUIDs, private
network addresses, and service credentials do not belong in the public RFC or committed evidence.

## Acceptance disposition

The migrated runtime is not accepted merely because the catalog opens. Acceptance requires the CLI,
portal, plan membership, capacity evidence, operator-selected cart, and one real restore to agree.
Real fill remains a separate, operator-approved production action.

## Execution record — 2026-07-15

Disposition: **stopped in Phase D; portal not started.**

| Phase | Result | Evidence |
|---|---|---|
| A — source freeze and rollback | Pass | Supervisor and portal stopped; no listener or open catalog descriptor; raw sidecars, consistent snapshot, SQL dump, hashes, and manifests preserved |
| B — canonical install and identity | Pass | Reviewed canonical install and explicit runtime paths validated; Git remained read-only during this acceptance pass |
| C — migration publication | Pass | Schema v2 published atomically; integrity and foreign keys clean; row counts exact; legacy `uncompressed` mapped to `guaranteed`; library map preserved |
| D — CLI/catalog | **Blocked** | CLI help and plan list/show passed. Reconciled `library plan --explain` returned typed diagnostics and exact ledgers. Legacy-compatible `library plan --json` raised the first `ArchivePolicyError` instead of returning a safe typed response |
| E — portal | Not run | `/api/library/plan` and `/api/library/queue` share the failing legacy projection; starting the portal would not satisfy the six-view smoke gate |
| F–H | Deferred | Operator cart selection, restore, deployment, and fill remain behind the failed Phase-D gate |

Observed reconciled evidence was internally coherent: 444 selected repositories produced 390 valid
manifests, 494 requirements, 102 satisfied requirements, 392 missing-work intents/tasks, seven drive
ledgers, and no unassigned task. Admission was correctly infeasible because 54 manifest-policy
diagnostics were blocking; 95 additional copy-policy-drift diagnostics remained visible. The defect
is the UI/compatibility projection, not silent executor continuation.

Required correction before resuming this RFC:

- make CLI/portal plan and queue JSON consume the reconciled result or a reviewed adapter;
- preserve the existing UI contract while surfacing every blocking typed diagnostic;
- never omit invalid selected repositories in a way that makes a partial plan look fulfillable;
- add CLI, HTTP, and browser regressions for a mixed valid + pickle-only migrated cart;
- repeat Phase D from a reviewed canonical build before starting the portal.

Published remediation — 2026-07-15 (not yet installed into the canonical acceptance tree):

- the CLI and both portal projections now adapt the canonical reconciled graph and capacity ledger;
- the response retains the legacy drive/queue fields plus typed diagnostics, capacity failures,
  feasibility, placement policy, and per-repository blocker codes;
- blocked repositories remain visible in the queue and make **Start Fill** unavailable;
- unit, CLI, real loopback HTTP, and isolated browser regressions cover a mixed valid/pickle-only cart;
- the full suite passes (210 tests), and a read-only migrated-catalog replay returns all 444 queue
  rows, all 54 manifest-policy blockers, no capacity failures, and sub-second plan/queue projections.

PR #16 merged with green Python 3.10/3.12 and isolated Playwright CI. Greptile's delayed 4/5 review
confirmed the safety behavior and found three bounded projection defects: pending/blocked totals can
overlap for a capacity-blocked valid manifest, the singular compatibility `source` field is lossy
for plural sources, and an unassigned replica can lose its copy-2 eligibility hint. `INC-015` carries
those fixes. Their focused regressions, the exact direct-file core-CI loop, a strengthened isolated
Playwright flow, and the full 212-test suite now pass locally. The RFC remains stopped in Phase D
until that follow-up is reviewed, public `main` is installed into the hidden canonical tree, and all
Phase-D checks are repeated there.

Installed-quality continuation — 2026-07-16:

- PR #17's projection corrections and PR #18's wheel-installed test contract were installed from
  reviewed public `main`;
- the exact direct-file suite against site-packages, the full 212-test pytest suite, Ruff, a fresh
  wheel's resource/runtime-path/schema-migration smokes, and isolated Chromium E2E passed;
- before opening the migrated catalog for Phase D, inspection found that `plan list`, `plan show`,
  and non-apply `library plan --json` still used an RW/bootstrap connection;
- acceptance stopped without invoking those paths against the migrated catalog. `INC-016` requires
  enforced read-only connections and no bootstrap for diagnostics before Phase D resumes.

## Execution continuation — 2026-07-16

Disposition: **Phase D passed; stopped before Phase E; portal not started.**

| Phase | Result | Evidence |
|---|---|---|
| B — canonical install and identity | Pass | Clean reviewed public `main`; non-editable site-packages import; dependency consistency and focused installed-wheel regressions passed |
| D — CLI/catalog | Pass | Help, plan list/show, JSON projection, and graph/ledger explanation completed with explicit paths and no `--apply` |
| E — portal | Operator boundary | Not started; service remained inactive and no production/test portal listener remained |
| F–H | Deferred | Cart decisions, restore, deployment, and real fill remain operator-attended continuations |

Sanitized Phase-D evidence:

- the schema-v2 catalog passed integrity and foreign-key checks with one active `guaranteed` plan,
  seven unique plan members, and 444 selected repositories;
- 390 valid manifests produced 494 requirements, 102 satisfied requirements, and 392 exact tasks
  across seven ledgers;
- 54 typed `MANIFEST_POLICY` blockers and 95 `COPY_POLICY_DRIFT` diagnostics remained visible;
- no task was unassigned, no byte-capacity failure existed, and no satisfied requirement appeared
  in the scheduled task set;
- all seven durable plan members were currently offline. Planning retained their registered capacity
  without presenting any as a live write target;
- catalog/WAL/SHM, library-map, and config hashes, sizes, mtimes, logical row counts, schema version,
  capacity mode, selection, and archive state were identical before and after the commands;
- both rollback snapshots still passed immutable read-only integrity checks with the expected
  schema-v0 model and selection counts.

No portal, service, fill worker, fetch, restore, replica, registration, mount, or archive mutation was
started by this continuation.

## Execution continuation — 2026-07-16 Phase E/F

Disposition: **Phases E and F passed; stopped before verified restore and Start Fill.**

| Phase | Result | Evidence |
|---|---|---|
| E — loopback portal smoke | Pass | Canonical explicit-path process, loopback listener, packaged assets, hostile-web boundary, all six views, active `ark`, durable fleet, live mount resolution, canonical terminology, and idle worker were exercised |
| F — operator cart | Pass | Reviewed UI build and read-only CLI diagnostics agree on the 390-repository cart; exact graph, placement, dependencies, ledgers, legacy differences, and live/offline drive evidence were reviewed before Start Fill |
| G–H | Deferred | Restore, installed service, and real fill remain separately approved operator continuations |

Sanitized Phase-E/F evidence:

- the portal served only on loopback and rejected untrusted Host, missing/untrusted Origin, invalid
  CSRF capability, and non-JSON mutation envelopes; all packaged scripts/styles and read-only API
  surfaces loaded;
- Fill remained `idle` throughout and no user service or automatic resume worker started;
- seven durable plan members remained visible; live resolution distinguished mounted media from
  shelved members without removing any plan capacity;
- the 54 exclusions comprised 50 pickle-only repositories refused by the safe public default and
  four unsupported-only repositories. Their public product backlog and cumulative reasons are in
  `docs/deferred-artifact-support.md` under DEF-030;
- the authorized cart mutation changed only the selection from 444 to 390. The canonical portal
  projection then reported 383 ready, seven done, zero blocked, zero capacity failures, and
  `feasible=true`; the worker remained idle;
- acceptance exposed two bounded UI defects: Library lacked repository/drive filtering, and durable
  occupancy pushed Drive 00's planned progress colors to the right. The accompanying implementation
  adds clickable multi-drive filters plus repository search and restores left-aligned planned
  progress, with isolated Chromium, XSS, projection, Ruff, and full-suite coverage;
- DEF-029 records the separate drive-identity lifecycle gap found while distinguishing shelved,
  same-identity re-registration, mount-path drift, and genuinely stale/replaced media.
- PR #21's Greptile-5/5 reviewed build was installed non-editably from public `main`. Live Chromium
  confirmed 140 archived Library rows, exact repository search, synchronized multi-drive facets with
  search-relative counts, and planned Fill segments to the left of durable archived occupancy;
- repeated `library plan --json` and `--explain` used explicit paths without `--apply`. The diagnostic
  catalog hash, size, mtime, schema version, integrity/FK result, selection, archive, plan-membership,
  and capacity-mode evidence were identical before and after;
- the chosen cart produces 390 manifests, 494 requirements, 102 satisfied requirements, and 392
  assigned tasks: 288 fetches plus 104 replicas, including nine replica tasks dependent on unfinished
  protected homes. The four active batches contain 13, 83, 192, and 104 tasks respectively; no task
  is unassigned and no typed blocker or byte-capacity failure remains;
- the shadow comparator deliberately reports seven reconciled-only protected-home tasks. Those
  repositories have only `.gitattributes` or `.gitattributes` plus `README.md` recorded while their
  canonical safetensors/config/tokenizer files are absent; the legacy completeness shortcut omitted
  that work. Correcting those requirements and using exact file budgets transparently reflows 16
  downstream bulk targets instead of normalizing the differences away;
- Drives 00, 01, and 04 currently resolve; Drives 00/01 are writable while Drive 04 is read-only.
  Drives 02/03/05/06 remain offline but retain durable plan membership. The accepted batch order will
  eventually require Drive 02, and replica execution requires Drive 04 to be writable; these are
  future execution preconditions, not reasons to delete capacity from the plan;
- the refreshed process remained loopback-only and non-resuming. Host, Origin, CSRF, content-type,
  and CSP checks passed; Fill stayed `idle`, and **Start Fill** was never clicked. A tab kept open
  across the restart safely rejected its stale per-process CSRF capability; a hard refresh restored
  mutation access. DEF-031 tracks replacing that raw error with explicit refresh guidance without
  replaying the rejected action.

Phase F is complete. Phase H and any real Fill remain separate explicit approvals.

## Execution continuation — 2026-07-16 Phase G preflight

Disposition: **stopped before restore; INC-017 requires a reviewed repair build.**

The read-only candidate audit found seven otherwise complete repositories with every recorded file
physically present on a readable drive. Their ordinary Git-tracked metadata lacked restore-hash
evidence: `.gitattributes` in all seven, plus three nested evaluation YAML files in one repository.
The smallest candidate had ten files and approximately 2.37 GB of original bytes, but restore would
correctly fail closed on its hashless `.gitattributes` before publishing any output.

The cause is the legacy fetch contract: it computed and persisted `orig_sha256` only when Hugging
Face supplied `files.sha256`. Git-tracked files commonly have no Hub sha256, and unlike annexed raw
content they have no SHA256 annex key fallback. Physical presence therefore could not establish the
expected digest required by DEC-037 restore verification.

No restore, annex retrieval, fill, catalog update, archive write, or disposable-output cleanup ran.
PR #23 now supplies universal ingestion hashing and the reviewed dry-run-by-default repair. It
validates legacy bytes against the blob committed at the same Git path, creates a consistent catalog
backup before explicit apply, and has end-to-end repair-plus-restore regression coverage. Phase G
resumes only after this reviewed main revision is installed into the isolated acceptance runtime.
The real migrated-catalog dry run, any explicit apply, and restore remain separate operator approvals.

## Execution continuation — 2026-07-16 Phase G remediation review

Disposition: **code gate passed; runtime gate not yet run.** PR #23 merged after Python 3.10/3.12 and
Playwright CI passed, with Greptile 5/5 on the final revision. No real catalog or archive was opened by
that publication step. The next action is to refresh the isolated install, repeat the read-only
`repair-hashes` audit against the migrated catalog, and stop for operator review before any apply.
