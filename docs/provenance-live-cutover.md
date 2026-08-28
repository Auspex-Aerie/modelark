# Provenance migration — stopped side-by-side live cutover

This is the operator procedure for moving an existing SQLite ModelArk runtime from schema v1–v6 to
the current schema. It publishes into a new data directory and repoints the service only after
validation. It never migrates a running catalog in place.

Use a reviewed release/candidate that has already passed the copied-runtime runbook. Replace every
example path below with an explicit reviewed literal path. Keep the complete cutover capsule on the
same filesystem as the final destination because publication uses an atomic no-clobber hard link.

## Stop boundary

This procedure authorizes catalog migration and a non-resuming portal start only. It does not
authorize Fill, proposal approval, provenance repair, drive mount/format/initialization,
registration, replacement binding, or rollback-evidence deletion.

## 1. Record the installed and deployed identities

Record:

- current and candidate package versions/commits;
- the service unit's executable, data, state, config, port, and resume flag;
- service enabled/active state;
- exact catalog, WAL, SHM, journal, `library.json`, and configuration metadata/hashes; and
- free space and filesystem identity for the cutover destination.

Stop the user service and prove no portal, Fill, migration, or maintenance writer remains:

```bash
systemctl --user stop modelark.service
systemctl --user status modelark.service --no-pager
systemctl --user cat modelark.service
```

If another writer exists, stop. Do not infer quiescence from the confirmation token alone.

## 2. Capture an immutable rollback seed

Copy the complete stable data directory, configuration, state, and current service unit into a dated
cutover capsule. Copy directory contents, not just `catalog.sqlite`; SQLite sidecars are part of the
source bundle.

Illustrative layout:

```text
/path/on-final-filesystem/modelark-cutover-YYYYMMDD/
  seed/
    source-data/
    source-config/
    source-state/
    modelark.service
    evidence/
  run/
    source-data/
    work/
    new-runtime/
      data/
      state/
      config/
    evidence/
```

After copying and hashing the seed, make `seed/source-data`, `seed/source-config`, and
`seed/source-state` read-only. Recheck the original live bundle: its hashes and metadata must match
the pre-copy record.

## 3. Create the disposable publication source

Copy the immutable seed data into `run/source-data` and make only that per-run mirror writable. Copy
the configuration into `run/new-runtime/config`; start the new runtime with a fresh state directory.
The migration CLI must receive the disposable mirror, never the old live directory or immutable
seed.

The destination `run/new-runtime/data` must contain no `catalog.sqlite`, SQLite sidecars,
`library.json`, or publication staging names before publication.

## 4. Rehearse

```bash
/path/to/candidate/venv/bin/modelark-provenance-migrate rehearse \
  --source-data-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/source-data \
  --work-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/work \
  --run-id live-cutover-001
```

Require:

- `status=ok` and `manifest_status=validated`;
- source version equal to the stopped deployment and clone version equal to the candidate schema;
- source and clone integrity `ok` with no foreign-key violations;
- `classification.disagreement=0`;
- a nonempty source/clone logical content identity;
- exact source-bundle identity before and after rehearsal; and
- `runtime_companions.library.json` matching the captured source's presence and SHA-256.

Any disagreement, unexpected classification, source drift, or malformed map locator stops cutover.

## 5. Publish to the new data directory

Reconfirm that all writers are stopped and the destination is unused, then publish:

```bash
/path/to/candidate/venv/bin/modelark-provenance-migrate publish \
  --work-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/work \
  --dest-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/new-runtime/data \
  --confirm-stopped MODELARK-STOPPED
```

Require `status=ok`, a published schema-v7 `catalog.sqlite`, a matching
`published_companions.library.json` result, retained rollback artifacts, and a staging report. If
publication refuses after creating any destination member, retain that entire run and use a new run
and destination; do not delete or overwrite ambiguous leftovers.

## 6. Validate before service replacement

Run the candidate with explicit new runtime paths and no apply/resume action:

```bash
/path/to/candidate/venv/bin/modelark \
  --data-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/new-runtime/data \
  --state-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/new-runtime/state \
  --config /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/new-runtime/config/wishlist.yaml \
  plan list

/path/to/candidate/venv/bin/modelark \
  --data-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/new-runtime/data \
  --state-dir /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/new-runtime/state \
  --config /path/on-final-filesystem/modelark-cutover-YYYYMMDD/run/new-runtime/config/wishlist.yaml \
  library plan --json
```

Verify schema/integrity/foreign keys, catalog counts, active plan membership, typed blockers, the
git-annex map locator, and that no proposal is approved and no Fill session is active. Unknown
identity-bound capacity is a safe expected blocker until drives are separately reconciled.

## 7. Install and start without resume

Render the service with the candidate executable and the new explicit data/state/config paths. Review
the generated unit and confirm `--resume` is absent. Install/reload it without starting, compare it to
the reviewed preview, then start the loopback portal.

Validate `/api/meta`, Drives, Plans, Catalog, Library, Fill, and Verify. Fill must show stopped. Compare
the portal planning result with the CLI result and stop if feasibility, executable-task count, root
blocker, or drive eligibility differs.

## 8. Preserve rollback and continue through separate gates

Keep the old service definition, executable, data, state, config, immutable seed, migration work,
publication leftovers, and hashes. Do not delete them merely because the portal opens.

Drive-loss declaration, replacement onboarding, capacity reconciliation, proposal approval, and Fill
are separate operator decisions after this cutover. The copied-runtime result never substitutes for
repeating consequential actions against the migrated live catalog.

## Rollback

Before post-migration graph or drive actions:

1. stop the new service;
2. restore the saved old service unit;
3. reload the user service manager;
4. start the old executable against the untouched old data/state/config paths if service restoration
   is desired; and
5. retain the failed/new runtime and evidence for diagnosis.

Never copy the schema-v7 catalog over the old catalog and never let the old executable open it.
