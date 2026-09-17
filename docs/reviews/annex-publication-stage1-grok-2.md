# ArchivePublisher Stage 1 — Grok round 2

Date: 2026-09-14 (operator timezone; CLI UTC session date September 15).
Local Grok CLI, session `01a0a270-126e-7280-b5c7-ab0885092ae5`, exit 0.
Stage counter: **2/3**; third unused. Outcome: **CHECKPOINT CORRECTIONS ACCEPT**.
Both round-1 P2s closed; no new substantive issues. Overall Stage 1 remains incomplete.

This was a focused correction review, not a new stage/counter or a completed-stage review.
Grok used read/search, did not execute tests and did not edit code. One automatic context
compaction occurred; no interruption/resume was needed in this round. Adjacent manifest
records the current implementation/test hashes; unchanged files are included for provenance,
not a claim that every unchanged file received another full review.

Parent verification after fixes: combined scoped regression **870 passed**, two existing Torch
deprecation warnings, 69.52 seconds. Ruff across changed runtime modules/selected tests and
`git diff --check` passed. All catalogs, native annex fixtures and test sockets were disposable;
no live archive/catalog/service/Fill action. No implementation commit or PR yet.

The corrections normalize incidental storage representation at the boundary: named SQLite
columns rather than their physical order; canonical JSON arrays rather than Python tuple shape.
This is a narrow implementation correction within DEC-157, not a new architecture decision.

## Reviewer verdict (verbatim)

**CHECKPOINT CORRECTIONS ACCEPT** (Stage 1 local Grok round **2/3**). This accepts the round-1 P2 fixes and the parent-queued adapter/restore/write-context regressions as a still-dormant implementation checkpoint. It does **not** accept Stage 1.

I did not run tests. Findings are from reading the listed modules and tests only.

---

## Prior P2s

**P2-1. Catalog pair CAS refuses migrated `archived` tables whose physical column order differs from current `CREATE TABLE`. — CLOSED**

`_schema` now requires matching column **count and name set**, not `PRAGMA table_info` order:

```31:32:modelark/publication_catalog.py
        if len(observed) != len(columns) or set(observed) != set(columns):
            raise PublicationRefused("PUBLICATION_CATALOG_SCHEMA_UNQUALIFIED", table=table)
```

With unique names, that is exactly “same names, any order; extra or missing still refuse.” `SELECT` / `INSERT` / `UPDATE` already bind `_COLUMNS[table]` by name, then `zip` in that named order, so physical order is not a CAS fact.

`test_historical_alter_column_order_still_binds_every_named_fact` rebuilds only a synthetic `archived` table: drop, `_canonical_table_sql(..., exclude=("stored_relpath", "orig_sha256_provenance"))`, the real `_MIGRATIONS` statement `ALTER TABLE archived ADD COLUMN stored_relpath VARCHAR`, then `ALTER` for provenance. It asserts `stored_relpath` sits after `verified_at` (current `CREATE TABLE` has it immediately after `stored_name`), then `ready` + `publish` of the complete pair. The CAS postcondition recaptures every named column. Replicas has no FK to `archived`, and the lifecycle `con` fixture is function-scoped, so the drop is isolated.

`test_raw_transaction_or_extended_schema_not_implicitly_adopted` still `ALTER TABLE files ADD COLUMN new_evidence` and still expects `SCHEMA_UNQUALIFIED`.

**P2-2. `LocalPayloadProof.record()` is not JSON-roundtrip stable. — CLOSED**

`record()` now normalizes the whole dataclass through `store.canonical` then `json.loads`. `canonical` is `json.dumps` (tuples encode as arrays); `json.loads` yields lists. A resumed `_unseal` dict and a fresh `proof.record()` can use raw equality.

The first payload test still hashes a real temporary locked tree, then checks `json.loads(canonical(proof.record())) == proof.record()` and that `root_identity`, `object_identity`, and `mapped_identity` are lists. In-memory `proof.object_identity` remains a tuple; only the durable record is arrays. That is the intended split.

---

## Parent-queued items (not round-1 P2s)

**Adapter inventory + restore shared expansion — addressed.**  
`test_every_physical_lock_adapter_uses_shared_expansion` now enumerates `publication_locks.py` and `restore.py`. The AST scan and the `modelark.drive_identity` / `proposal._fence_keys` requirement are unchanged. Current `hold_drives_sorted` / `hold_drive_reads_sorted` / `drive_lock_path` call sites in `modelark/` match that inventory (plus skipped `drive_fence.py`).

`restore._publication_source` uses `compatible_keys([captured])` instead of `captured.lock_keys()`. For one `FenceIdentity`, `compatible_keys` is the same sorted unique key set; the helper is the required shared expansion.

**Restore admission tests** cover the claimed checkpoint behavior: pending `d0` copy is `MAINTENANCE_REQUIRED` and never yields the reader; unrelated clean `d1` yields `may_mutate is False` with no catalog writes; `stored_relpath` change on the consumed `_rows` projection is `SOURCE_ROW_CHANGED`; `hold_drives_sorted` stays entered for the whole `with` body; `hold_controller` / `hold_map` are not acquired after the drive fence.

**Round-1 lower-priority `defer_revision_once` regression — covered.**  
`test_same_finalizer_key_with_conflicting_binding_rolls_back_owning_write` parametrizes graph and session. Same key, bindings `(10, False)` vs `(None, True)`, raises `CATALOG_WRITE_FINALIZER_CONFLICT`, finalizers do not run, `test_markers` rolls back, planner revision stays 10, and session `bound_planner_revision` stays 10. `defer_revision_once` itself is the existing conflict check; runtime write-context code was not part of this correction.

---

## New substantive issues

None. No new common architectural root cause in this correction set.

Round-1 lower-priority coordinator obligations remain open and **unfinished**, not closed by these tests: caller-supplied `require_scope` / `catalog_cas` callables, `publication_locks` fence-exception adaptation, and mutable in-memory `_FenceScope.identities`. Do not treat those as production proofs.

---

## Checkpoint vs Stage 1

`MAX_SUPPORTED_CATALOG_VERSION` is still 8 (`SUPPORTED_CATALOG_VERSIONS = {7, 8}`). Publication `VERSION = 9` is still only installed through `_install_schema` inside `graph_write`. These restore tests exercise that fixture path; they are not a production v9 reader/writer floor.

Still unfinished, and still blocking Stage 1: ArchivePublisher coordinator; qualified-profile / committed-tree / map proof factories; controller → map → drive-fence → short-TX wiring on the sync sites; `require_clear` on fetch/register and the rest of admission/recovery; additive catalog floor above 8 with v7/v8 guards; ordinary acquisition and replica publication; map quarantine / native merge / closure as production flows.

Conversion stays refused. There is no live archive, catalog service, Fill, or drive mutation in this increment.

**Overall Stage 1 remains incomplete.** Continue Stage 1 implementation on this checkpoint; do not treat this as stage acceptance.

