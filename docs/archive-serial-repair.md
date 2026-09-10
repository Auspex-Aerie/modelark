# Archive serial-identity repair: attended handoff

This repairs one evidence mismatch: the catalog already holds the correct disk
serial, but an older partition-only observation recorded a `serial: null` archive
fingerprint. It does not change the saved serial or adopt a different disk.
Registration without a canonical serial remains supported and is not eligible
for this particular repair.

Implementation/review approval is not permission to deploy or repair an operating
installation. Schedule a separate attended window. Replace the executable,
data/state/config paths and drive label in the examples with approved targets.

## What changes, and what stays

| Item | Result |
| --- | --- |
| Identity epoch | Unchanged; this is not replacement or capacity transition |
| Archive evidence | New generation and serial-bearing fingerprint/clean anchor |
| Old generations, anchors and owner/session history | Retained |
| Catalog reader floor | Explicitly becomes 8 during enrichment; table layout stays unchanged |
| Affected Fill approvals | Superseded; fresh Preview → Approve → Start required |
| Completed task/copy results, archive bytes and hash provenance | Unchanged; no implicit redownload |
| Unrelated Slice seals | Remain valid when sealed facts are unchanged |
| Seals referencing changed source evidence | Stale execution refuses; fresh preview required |

Slice still owns an output folder, not a whole drive. This source repair does not
change destination profiles, reservation rules, protected-device checks or receipts.

## 1. Establish the maintenance boundary

1. Verify the exact installed build/commit and supported catalog versions. A package
   version string or healthy portal metadata endpoint is insufficient.
2. Stop the service and every other old ModelArk client against this installation
   or copied catalogs. Check surviving transfer children too. The launch singleton
   does not quiesce already-open old readers or children.
3. Retain a consistent catalog backup and the runtime/config/state plus old
   executable/service definition. Follow the established [deployment procedure](deployment.md);
   do not copy only a live SQLite main file while discarding its WAL.
4. Rehearse on a disposable consistent copy with separate data/state paths.
   Compare preserved tables, owner/token/history, fingerprints, generations,
   affected approvals and reader version. Never pass a live connection to a
   rehearsal intended to mutate only the copy.

Repair also retains a consistent SQLite backup and transaction rehearsal beside
the selected catalog, in a unique `.serial-repair-*` directory, before publication.
Failure output lists attempted artifact paths, not certification that incomplete
files are valid backups. Retain and validate those artifacts.

## 2. Inspect the exact drive

Inspection reads saved evidence in one catalog snapshot without changing it:

```bash
/absolute/qualified/bin/modelark \
  --data-dir /absolute/runtime/data \
  --state-dir /absolute/runtime/state \
  --config /absolute/runtime/config/wishlist.yaml \
  drive reconcile drive-XX --inspect-serial-identity
```

Review `status`, epoch/generation, both fingerprints, `affected_approvals` and the
exact `binding`. Inspection alone does not prove mounted hardware or authorize
archive reads. Never reuse a binding from a clone, another installation or an old
attempt for live repair.

- `legacy_clean`: eligible for fresh verification and enrichment.
- `legacy_dirty`: requires the guarded legacy recovery bridge first. Prior proof,
  ended owner/child state and claims must remain provable.
- `already_correct`: no further identity correction needed.
- `correct_identity_dirty`: not this legacy transition; use its appropriate
  ordinary recovery path without rewriting proof, fingerprint or owner fields.

## 3. Repair only after explicit live approval

Use the binding from the immediately reviewed inspection of this installation:

```bash
/absolute/qualified/bin/modelark \
  --data-dir /absolute/runtime/data \
  --state-dir /absolute/runtime/state \
  --config /absolute/runtime/config/wishlist.yaml \
  drive reconcile drive-XX --repair-serial-identity \
  --expected-binding REPLACE_WITH_EXACT_INSPECTION_BINDING --writers-stopped
```

`--writers-stopped` is an operator assertion, not automatic shutdown. Do not combine
this mode with `--dedicated` or `--accept-drift`; it cannot adopt authority or accept
changed capacity/identity. Known serial, UUIDs and capacity must match fresh
attachment-bound observations. Compatible old/new physical locks remain mandatory.
Contention, stale intent, ambiguous topology and failed observations refuse.

Inventory is report-only presence evidence, not full-byte verification. Extras and
debris remain untouched; successful inventory does not authorize their deletion.

For dirty legacy state, recovery first commits an old-identity clean anchor without
changing its generation owner/history. Enrichment is a separate transaction. If
output reports committed recovery followed by failed enrichment, retain that safe
milestone, inspect again and use a new binding. Do not undo it with manual SQL or
blindly restore an older catalog.

## 4. Verify before resuming work

Validate backup/rehearsal artifacts and database integrity. Confirm the unchanged
epoch and saved serial, new generation/fingerprint/anchor, version 8, selective
approval supersession and preserved historical rows/archive claims. A fresh
inspection after successful clean repair should report `already_correct`.

Validate readability using the **installed build's read-only opener**, not checkout
constants alone. Updated readers accept 7 and 8; old readers must refuse 8. Ordinary
open must neither upgrade 7 nor downgrade 8. Never change `PRAGMA user_version` by
hand to make an old binary run.

Restart the approved build without automatic Fill resume. Review a fresh proposal:
completed archive work should be recognized as satisfied, not erased to force
replanning. Approve and Start remain separate actions. Explicit stale proposal IDs
refuse even after a new approval exists; only omission may select the active
approval. Old-session Resume must not silently adopt a new proposal.
Start resolves that omission under its controller lock and verifies the selected
approval again in the session-creation transaction. An approval change during
admission refuses rather than rebinding the projected work.

For Slice, preview changed source evidence afresh, review it, then approve/start
separately. Preserve existing output and completed receipts. Begin with a tiny
disposable output folder only after physical acceptance is separately approved.
Software fixtures and copied-catalog rehearsal are not USB qualification or
permission to format a device.

## Stop conditions and rollback

Stop on changed/missing identity, missing claims, live/child/fence contention, stale
binding, unknown proof/version, failed backup/rehearsal or unexpected preserved-table
changes. Do not weaken checks, clear ownership or modify the saved serial to proceed.

Failed enrichment rolls back that transaction, including its reader-floor change.
A committed legacy bridge remains committed. After successful repair, rolling back
only the application is unsafe: old code must reject version 8. Restoring an older
catalog after later archive writes can discard their history. Catalog restoration
requires a separate recovery decision under quiescence; it is not automatic.

See the [serial-identity plan](plans/archive-serial-identity-consistency.md) for the
implementation and exact qualification/review record (DEC-133 through DEC-137).
