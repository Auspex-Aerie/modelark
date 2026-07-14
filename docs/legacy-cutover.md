# Legacy ModelDump → canonical ModelArk cutover

This is the operator-attended runbook required by `DEC-042`. It is intentionally split into a safe
rehearsal and a maintenance-window cutover. Do not point the migration tool at the running ModelDump
checkout, stop its service, change its remotes, or start the final validation without the operator
present.

The migration tool never replaces its source. It creates a consistent catalog snapshot, preserves
raw database/WAL sidecars for rollback evidence, copies `library.json`, migrates only from the backup,
validates the current schema and foreign keys, and atomically publishes a new data directory. Its
default mode is read-only inspection; `--execute` requires the exact `MODELARK-STOPPED` assertion.

## Phase 0 — canonical rehearsal (safe now)

These checks use only synthetic fixtures in the canonical checkout:

```bash
.venv-dev/bin/python tests/test_legacy_runtime_migration.py
.venv-dev/bin/python -m scripts.migrate_legacy_runtime --help
```

The release-candidate clean install, wheel/sdist, test suite, browser smoke, and PR review also happen
in the canonical checkout. They do not authorize access to the legacy checkout.

## Phase 1 — open the maintenance window (operator required)

Record these paths together; do not assume them:

- legacy checkout (currently the working `ModelDump` directory);
- active catalog data directory (`catalog.sqlite` or the older `catalog.duckdb`);
- active config/wishlist and state/log directories;
- git-annex library-map root and mounted archive drives;
- service unit name and its exact `ExecStart`;
- new, absent destination data directory and backup directory on reliable storage.

Before stopping anything, capture sanitized output from `git status`, `git rev-parse HEAD`,
`git remote -v`, the service status, and the active ModelArk command line. Keep this with the private
cutover notes; remote URLs and local paths may be sensitive and do not belong in a public issue.

The operator then stops the fill from its normal control surface and stops the supervising service.
Wait for the current file boundary if a graceful stop is possible. Verify there is no portal, CLI
fill, download worker, compression worker, or database writer left. The migration tool independently
tries to acquire SQLite's immediate-writer lock (or a DuckDB read-only open) and refuses if it cannot,
but that guard supplements rather than replaces process inspection.

**Gate:** do not continue until the operator confirms the fill is stopped and the maintenance window
is active.

## Phase 2 — inspect, back up, and migrate a copy

From the canonical checkout, first run inspection mode with the agreed paths:

```bash
.venv-dev/bin/python -m scripts.migrate_legacy_runtime \
  --source-data-dir <LEGACY_DATA_DIR> \
  --destination-data-dir <NEW_DATA_DIR> \
  --backup-root <BACKUP_ROOT>
```

Review the detected engine, source catalog, table counts, runtime config list, and destination. If
both SQLite and DuckDB are present, stop and determine which one the service actually opened; the tool
refuses to guess. DuckDB input requires `.[migration]`.

After review, execute against the stopped source:

```bash
.venv-dev/bin/python -m scripts.migrate_legacy_runtime \
  --source-data-dir <LEGACY_DATA_DIR> \
  --destination-data-dir <NEW_DATA_DIR> \
  --backup-root <BACKUP_ROOT> \
  --execute --confirm-stopped MODELARK-STOPPED
```

Keep both generated manifests. The backup manifest contains hashes for the consistent snapshot, raw
source database/WAL files, and copied runtime config. The destination manifest records schema and
foreign-key checks, source/destination row counts, the bootstrapped active plan, and the published
catalog hash. The source remains in place and is the rollback authority.

**Gate:** any failed integrity check, foreign-key violation, unexpected row-count change, missing
`library.json`, or surprising annex root stops the cutover. Diagnose from the backup; do not repair the
source in place during the window.

## Phase 3 — validate the canonical install against the migrated copy

Use a fresh environment built from canonical `main`; do not carry the legacy `.venv` forward. With
the service still stopped, validate:

1. CLI import/help and `library plan` against `<NEW_DATA_DIR>`.
2. Catalog counts and active-plan drive membership against the migration manifest.
3. `library.json` points at the intended private git-annex map.
4. Registered drives resolve only when expected; no formatting or registration command is run.
5. The portal starts loopback-only with explicit `--data-dir`, renders all six views, and remains
   stopped as a fill controller.
6. With the operator choosing a small archived model and destination, run one real verified restore.
   Check its final hashes and then remove only that disposable restore output.

Do not resume the fill yet. A read-only portal smoke and an explicit restore are the acceptance tests;
starting archive work would make rollback harder.

## Phase 4 — replace the working copy without combining unrelated histories

First determine whether the legacy and public repositories share ancestry:

```bash
git -C <LEGACY_CHECKOUT> fetch https://github.com/Auspex-Aerie/modelark.git main
git -C <LEGACY_CHECKOUT> merge-base HEAD FETCH_HEAD
```

If they share the intended history and the checkout is clean, the operator may rename the old remote,
add `https://github.com/Auspex-Aerie/modelark.git` as `origin`, switch to its `main`, and pull only with
`--ff-only`. Never create a merge commit between release histories just to make the command succeed.

If they are unrelated—as expected for a sanitized public re-origin—the safer cutover is a clean-clone
directory swap:

1. Clone canonical `main` to a new sibling directory.
2. Build its fresh environment and repeat CLI help before changing paths.
3. Rename the stopped legacy checkout to a timestamped rollback name; do not delete it.
4. Rename the canonical clone to the desired `ModelDump` working path.
5. Point the service at the new executable plus explicit migrated data/state/config paths.
6. Verify `git remote -v`, `git status`, and `gh auth status` show `Auspex-Aerie/modelark` and the
   `auspexlabs` identity before any GitHub write.

This produces the requested canonical working copy without force-resetting an unrelated history or
accidentally deleting checkout-local runtime state. The old remote/history remains available only in
the rollback directory until acceptance.

**Gate:** remote changes, directory renames, service-unit edits, pulls, and service starts are each
operator-approved actions. They are not performed by an unattended agent.

## Phase 5 — final run and acceptance (operator required)

Start the canonical portal once, still without auto-resume. Re-run the Phase 3 checks through the
service. Then, with the operator watching logs, enable the intended resume setting and start the fill.
Confirm it recognizes completed files, current drive capacity, the active plan, and the next expected
work item before leaving it unattended.

Acceptance requires all of the following:

- migrated catalog integrity and foreign keys are green;
- source/destination counts and intentional `ark` bootstrap additions are explained;
- annex map and mounted-copy resolution agree with the pre-cutover record;
- one real restore verifies byte-for-byte;
- portal and service use canonical code and explicit runtime paths;
- GitHub/git identity is `auspexlabs` and origin is `Auspex-Aerie/modelark`;
- the resumed fill advances from durable completion instead of restarting completed files;
- backup manifest, source data, and rollback checkout remain intact.

## Rollback

Stop the canonical service before rollback. Point the service back to the untouched source data and
the timestamped legacy checkout, using the captured pre-cutover command line. Do not copy a partially
used migrated database back over the source. If the canonical fill wrote new archive records before a
rollback, preserve its destination and logs for reconciliation rather than trying to merge databases
during the incident.

Retire the old checkout, source database, or backup only in a later maintenance window after the
operator explicitly accepts the canonical run.
