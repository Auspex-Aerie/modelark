# Serial-less registration / observed-serial anchor repair

Approved scope: fresh PR from merged main `986241e`; local Grok up to three
rounds, then cloud Codex up to three rounds. No Greptile for this PR; operator
requested no Greptile for three more days on 2026-09-11. Operator merges.

## Problem and boundary

Drive 00 physical Slice preview refused before decoding or creating output:
catalog serial NULL, current epoch/generation 1/1, but saved fingerprint and
identity/fence proofs encode the connected NAS serial. UUIDs and capacity agree.
Historical bootstrap code before `43ec31e` could produce this exact shape by
using observed serial in a fingerprint without changing the registration column.
This is a plausible producer, not proof of which invocation created the record.
Current optional-serial producers already prevent this mismatch.

NAS read-only mounting was explicitly requested with `mount -o ro`, not observed
as an automatic failure transition. Leave it read-only. No NAS health claim,
remount, archive rewrite, service rollout or live repair is part of this PR.

## Implementation

1. Strictly recognize NULL catalog serial plus a **current clean** serial-bearing
   anchor: exact v1 JSON, identity/fence proof agreement, saved UUIDs, capacity,
   authority and hash reproduction. Reject dirty state and empty-string serials.
2. Derive both old observed-serial and canonical null-serial exclusion keys only
   inside this explicit, evidence-bound repair. Keep ordinary FenceIdentity strict.
3. Require the anchor's exact serial in fresh live observations before/after full
   presence inventory and after actual SQLite writer-lock acquisition.
4. Reuse controller/physical locks, writer-quiescence assertion, exact-state binding,
   durable SQLite backup and catalog-only rehearsal. Append a new generation and
   canonical null-serial anchor atomically; retain actual serial in fence proof.
5. Preserve catalog serial, UUIDs, epoch, old anchors/generations, copy/provenance
   and owner history. Reuse selective Fill approval supersession, revision bump,
   reader floor 8 and fresh Slice preview requirements. No new schema/layout.

The shared cause with Drive 07 is disagreement over **which serial facts form
identity**, not damaged model bytes. One strict proof parser and the existing
repair transaction owner handle both directions; no relaxed reader checks,
implicit serial enrichment or manual live SQL workaround.

## Validation and handoff

- Regression-first: inverse repair failed on the old code before implementation.
- Exercise malformed/duplicate proof, mismatched hardware, both physical aliases,
  stale binding, backup/inventory/publication failures, rollback and idempotence.
- Verify selective approvals and unchanged historical records on real disposable
  SQLite; public Slice preview must refuse before repair and deliver verified
  original bytes after repair with synthetic hardware and real filesystem I/O.
- Run existing serial repair, optional-serial and broader regression suites.
- Local Grok then fresh PR / cloud Codex; every pushed head gets cloud review.
- After merge, separately qualify the installed artifact and rehearse the exact
  catalog copy before any live application. Then retry the physical Slice test.

Review counters and current test outcomes live in the ignored local handoff/evidence,
not in the append-only decision ledger. Implementation is not live acceptance.
