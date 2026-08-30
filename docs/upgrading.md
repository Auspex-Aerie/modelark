# Upgrading ModelArk

ModelArk upgrades application code normally, but it never silently rewrites an existing catalog whose
schema is older than the installed release. Existing data is migrated through an explicit,
backup-first, side-by-side procedure so the old runtime remains a usable rollback point.

## Do I need to do anything?

| Existing installation | Required action |
|---|---|
| Fresh install with no catalog | None. The current schema is created on first use. |
| Existing catalog already at the current schema | Update/redeploy normally. |
| SQLite catalog at schema v1–v6, including catalogs created by the released ModelArk 0.2.0 | Run the one-time provenance migration before starting the new service. |
| Legacy checkout with DuckDB or pre-canonical runtime layout | Follow `legacy-cutover.md` first; install the `migration` extra when DuckDB conversion is required. |

If a new ModelArk binary is pointed at an existing pre-v7 catalog, it refuses before changing the
file and names `modelark-provenance-migrate`. This is expected protection, not catalog corruption.
For source checkouts and pre-release builds, do not use the Python package version string alone to
decide whether migration is needed: the catalog schema and the binary's refusal are authoritative.
ModelArk 0.3.0 is the first public release line carrying schema v7; it is intentionally distinct
from the released 0.2.0 schema-v2 line.

## What the provenance migration does

The migration:

1. reads a stopped, disposable copy of the existing data directory;
2. captures the complete SQLite main/WAL/SHM bundle before any recovery-capable open;
3. creates and validates a migrated clone;
4. classifies existing archive digest provenance without inventing missing evidence;
5. preserves the validated `library.json` git-annex map locator when present;
6. publishes a new schema-v7 catalog into a separate empty data directory without overwriting an
   existing destination; and
7. retains the old runtime plus migration snapshots/manifests for rollback.

It does not modify archive bytes, mount or format drives, initialize git-annex repositories,
register hardware, repair missing provenance, approve a placement proposal, or start Fill.

## What an existing user must do

An operator must schedule a stopped-writer window, retain a backup of the entire old data directory
including SQLite sidecars, run rehearsal and publication from a disposable copy, then repoint the
service to the new directory. The detailed commands and stop conditions are in
[`provenance-live-cutover.md`](provenance-live-cutover.md).

In practical terms, an existing user should expect one attended maintenance window:

1. stop ModelArk and copy the complete data/config/state runtime as rollback evidence;
2. rehearse the migration and review its integrity, classification, and locator evidence;
3. publish into a new empty data directory rather than overwriting the old catalog;
4. start the new service without automatic Fill resume; and
5. review drives, typed blockers, and the new plan before approving any work.

Keep the old runtime and migration capsule until post-upgrade drive reconciliation and at least one
operator-chosen recovery checkpoint have passed. Do not delete publication leftovers just because
the portal starts successfully.

After migration, start the portal without `--resume`, review the catalog and plan, and reconcile
attached archive drives before expecting capacity to become executable. Drives with missing or stale
identity-bound evidence remain visible but contribute no admitted capacity. This is intentional.

Most users should not need to re-download models or recreate their cart. They may need to:

- reconnect or mount archive drives through their normal operating-system procedure;
- explicitly reconcile each candidate drive so its current identity and free-space evidence are
  anchored;
- prepare the root permissions of a **new or replacement dedicated filesystem** when the onboarding
  preview says the unprivileged ModelArk service cannot write it; copy the exact commands shown by
  the preview, then refresh it, and do not recursively change an existing archive or shared mount;
- resolve any typed provenance, policy, identity, or capacity blocker shown by the planner; and
- approve a newly generated proposal before Fill can run.

The Drives view uses the mounted archive's filesystem UUID + git-annex UUID as the stable registered
identity. USB enclosures and iSCSI layers sometimes omit or replace a disk's hardware serial; when
the stable pair matches, ModelArk shows the observed serial discrepancy as supporting evidence only
and does not update the registered serial. A complete but different stable pair is an identity
conflict and cannot fall back to a matching serial.

Populated-drive reconciliation can take long enough to notice, but it should no longer be opaque.
The CLI reports catalog-claim, annex-membership, and worktree-scan milestones. It proves recorded
target-annex-UUID membership for all catalogued annex keys in one query and excludes `.git` metadata
from the filesystem walk. A failed query or missing claim still refuses the clean anchor; the
controller/drive fences and fresh final observation are unchanged.

Existing archive drives do not need a blanket ownership rewrite merely because the catalog was
migrated. The permission gate applies when registering a filesystem whose archive namespace is
absent. ModelArk shows the service identity, current owner/mode, and its planned hidden staging plus
final `modelark/` paths; it never runs the privileged commands or creates those directories until a
separate exact registration confirmation succeeds.

## Rollback boundary

Before any post-migration catalog action, rollback is simply: stop the new service, restore the old
service definition, and point the old executable at the untouched old data/state/config paths. Never
let an older binary open the new schema-v7 catalog.

Once operators declare drives lost, register replacement media, repair evidence, approve proposals,
or start Fill in the new runtime, rolling back to the old catalog also discards those newer graph
decisions. Preserve the migration evidence and review that divergence instead of copying the new
catalog over the old one.
