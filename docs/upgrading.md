# Upgrading ModelArk

ModelArk upgrades application code normally, but it never silently rewrites an existing catalog whose
schema is older than the installed release. Existing data is migrated through an explicit,
backup-first, side-by-side procedure so the old runtime remains a usable rollback point.

## The 0.3.x → 0.4.0 Beta boundary

ModelArk 0.4.0 includes the reviewed Slice, guarded-codec and serial-evidence work.
An ordinary application update preserves an existing v7 or v8 catalog version;
it does not run serial repair, rewrite archive bytes, approve work or start Fill.
Pre-v7 catalogs still require the separate provenance migration described below.

Stop all ModelArk writers, retain a consistent backup of the catalog and its
SQLite sidecars, application/configuration, and private Slice state before updating.
Slice journals live separately in `~/.local/state/modelark/slice`, not just in the
configured catalog data directory. Its private store version 8 and new approval
envelopes require compatible readers; an old executable is not necessarily a
safe rollback after opening or creating newer Slice state. Preserve the old
runtime and state together, and do not discard later work to force a downgrade.

Deploy the reviewed release against the same explicit data/state/config paths,
without automatic Fill resume. Check `modelark --version` reports `0.4.0`, verify
the installed catalog reader, and review the plan, drives, pending approvals and
idle Fill state. Stop the portal before using the separately guarded Slice CLI.
Fresh Slice previews are needed for the new decode policy; existing seals retain
their old policy and are never silently rewritten.

Only use [explicit serial repair](archive-serial-repair.md) for a proven matching
legacy condition after its backup/rehearsal and stopped-writer gates. Repair may
invalidate affected approvals and raises the catalog reader floor to 8. Rolling
back the application alone after that repair is unsafe. Beta does not remove
these boundaries or promise compatibility with arbitrary older development builds.

## Explicit archive serial repair and reader floor 8

Builds carrying the reviewed serial-identity repair support the unchanged catalog
layout at versions 7 and 8. Normal open leaves the version alone; only explicit
repair raises the reader floor to 8 atomically with corrected identity evidence.
This is distinct from the older provenance/table-layout migration below. Verify
the exact installed build's read-only opener. Version 0.4.0 carries this support;
the original 0.3.3 package version alone cannot identify a development build with it.

Follow [archive-serial-repair.md](archive-serial-repair.md) for quiescence, backups,
copied rehearsal, bound inspection/repair and fresh approvals. Old binaries must
refuse repaired version-8 catalogs. An application-only rollback is not sufficient
after repair; never lower the version stamp by hand. Existing v7 catalogs without
this particular mismatch do not need automatic mass repair or a version upgrade.

## The 0.3.2 → 0.3.3 boundary

ModelArk 0.3.3 retains schema v7 and requires no catalog migration. Retain the 0.3.2 runtime and
unit as rollback, then deploy 0.3.3 against the same explicit data, state, and config paths with
automatic Fill resume disabled.

Any immutable draft based on the current planner revision is preserved. On the next Fill-page load,
the portal restores that exact review and keeps Start Fill unavailable until the operator approves
or discards it. Discarding a draft does not change the active approval or planner revision.

## The 0.3.1 → 0.3.2 boundary

ModelArk 0.3.2 retains schema v7 and requires no catalog migration. Stop the current service, retain
its unit and runtime as rollback, and deploy 0.3.2 against the same explicit data, state, and config
paths without automatic Fill resume. Confirm the existing proposal, plan, Drives, and idle Fill
state after restart.

The new replacement-drive action creates a separate bounded successor proposal from the current
approval. Creating or approving that proposal does not start Fill. Review its exact target changes,
approve it explicitly, and use the separate Start Fill action only while an operator can respond to
drive-loading prompts.

## The 0.3.0 → 0.3.1 boundary

ModelArk 0.3.1 uses the same schema-v7 catalog as 0.3.0. This is an application-only update: retain
the current runtime as rollback, stop the service, install or deploy 0.3.1 against the same explicit
data/state/config paths, and start it without automatic Fill resume. Confirm the current proposal,
plan, Drives, and idle Fill state after restart. No provenance migration is required.

## The 0.2.0 → 0.3.0 boundary

ModelArk 0.3.0 must not be started against a 0.2.0 data directory. Install the new release in a
separate checkout/environment, stop every old writer, capture the complete old runtime, rehearse the
provenance migration against a disposable copy, and publish schema v7 into a new empty data
directory. Start the new portal without automatic Fill resume. Reconcile storage evidence, review
one fresh immutable placement proposal, approve it explicitly, and start Fill only as a later
operator action.

The reusable commands and stop conditions are in
[`provenance-live-cutover.md`](provenance-live-cutover.md). The longer
[`provenance-migrate-copy-runbook.md`](provenance-migrate-copy-runbook.md) is the maintainer evidence
record behind that public procedure; ordinary upgrades should begin with the live-cutover guide.

## Do I need to do anything?

| Existing installation | Required action |
|---|---|
| Fresh install with no catalog | No migration. A fresh catalog is created at v7; only explicit serial repair raises its reader floor to 8. |
| Catalog v7 or explicitly repaired v8 moving to 0.4.0 | Follow the 0.4.0 boundary above; preserve private Slice state and do not perform blanket serial repair. |
| SQLite catalog at schema v1–v6, including catalogs created by the released ModelArk 0.2.0 | Run the one-time provenance migration before starting the new service. |
| Legacy checkout with DuckDB or pre-canonical runtime layout | Follow [`legacy-cutover.md`](legacy-cutover.md) first; install the `migration` extra when DuckDB conversion is required. |

If a new ModelArk binary is pointed at an existing pre-v7 catalog, it refuses before changing the
file and names `modelark-provenance-migrate`. This is expected protection, not catalog corruption.
For source checkouts and pre-release builds, do not use the Python package version string alone to
decide whether migration is needed: the catalog schema and the binary's refusal are authoritative.
ModelArk 0.3.0 is the first public release line carrying schema v7; 0.3.1 through 0.3.3 retain that schema
and are intentionally distinct from the released 0.2.0 schema-v2 line.

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
