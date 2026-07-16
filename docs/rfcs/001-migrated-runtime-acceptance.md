# RFC-001: Operator-attended migrated-runtime acceptance

- **Status:** in execution — Phases A–C passed; Phase D follow-up pending
- **Date:** 2026-07-15
- **Owners:** Auspex-Aerie + operator
- **Related:** DEC-035, DEC-037, DEC-038, DEC-040, DEC-042, DEC-044, DEC-045,
  DEF-011, DEF-027, DEF-028, INC-014, INC-015
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

These checks must be repeated after the post-PR-16 follow-up is merged and installed; the checkmarks
record the completed initial canonical-install gate, not acceptance of a stale revision.

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
- [ ] `modelark library plan --json` derives placement without `--apply`.
- [x] `modelark library plan --explain` derives the DEC-045 graph/ledger read-only.
- [x] The explain payload has no phantom reservations for satisfied copies.
- [ ] Typed manifest/policy diagnostics are preserved rather than silently dropped.
- [x] Plan membership contains every migrated registered drive exactly once.
- [x] Nominal capacity includes every plan member, independent of current mounts.
- [x] Mounted, offline, read-only, primary, replica, and RAID-backed facts remain distinct.
- [x] No catalog count or capacity-mode value changed during the initial CLI diagnostics.
- [x] No archive file, annex key, drive registration, selection, or fill state changed.

**Stop condition:** schema write, policy drift, unexplained count change, missing plan member,
untyped blocker, archive mutation, or any command attempting execution.

### E. Loopback portal smoke

- [ ] Start canonical `modelark serve --no-open` with explicit paths and without `--resume`.
- [ ] Confirm the process executable and working tree are canonical.
- [ ] Confirm the listener is loopback-only on the reviewed port.
- [ ] Confirm the health endpoint responds.
- [ ] Confirm Host/Origin/content-type/CSRF protections remain active.
- [ ] Confirm Plans, Catalog, Disk, Library, Fill, and Verify views load.
- [ ] Confirm the migrated `ark` plan is present and selectable.
- [ ] Confirm all registered plan drives remain visible even when shelved.
- [ ] Confirm currently mounted drives resolve only where expected.
- [ ] Confirm Fill reports not running and no worker begins automatically.
- [ ] Confirm capacity forecasts and admission evidence use canonical terminology.
- [ ] Confirm no service unit is installed, enabled, or started by this manual smoke.

**Mandatory operator boundary:** stop automated work here. Leave the non-resuming portal available for
the operator to inspect the plan and choose the model cart. Do not select models or press **Start
Fill** on the operator's behalf.

### F. Operator cart selection (later continuation)

- [ ] Operator explicitly selects the intended plan for the browser session.
- [ ] Operator reviews the migrated selection before changing it.
- [ ] Operator chooses and confirms the cart.
- [ ] Capacity bars and graduated selection gate update after each change.
- [ ] Operator stops before **Start Fill** and hands control back for diagnostics.
- [ ] Re-run `library plan --json` and `--explain` against the chosen cart.
- [ ] Review exact tasks, targets, dependencies, capacity ledgers, and typed blockers.
- [ ] Explain any offline-drive dependency without removing that drive from the durable plan.

No new drive is a test prerequisite. If the chosen cart does not fit, the acceptance result is the
typed capacity failure; resolution is an explicit cart reduction or capacity addition, not a test
fixture disguised as production storage.

### G. Verified restore (later continuation)

- [ ] Operator chooses a small archived repository and disposable destination.
- [ ] Every required archived file has at least one currently readable recorded copy.
- [ ] Prefer already-present content; do not rely on annex retrieval onto read-only media.
- [ ] Restore stages atomically beneath `rehearsal/` and never overwrites an existing destination.
- [ ] Every restored file matches its canonical SHA-256.
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
