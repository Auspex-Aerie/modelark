# ArchivePublisher Stage 1 — Grok round 1

Date: 2026-09-14 (operator timezone). Local Grok CLI; session `01a0a25c-7b7f-7112-8fca-e0fc5f6333df`.
Accumulated implementation checkpoint, **not completed Stage 1**. Stage counter: **1/3**.
Verdict: IMPLEMENTED CHECKPOINT ACCEPT, two P2s to fix before integration. No P1 findings.

The broad read/search invocation underwent three context compactions without a verdict. Parent
interrupted only its identified client (exit 130), then resumed the same saved session with a
findings-only, no-tools request. That continuation returned the verdict below (exit 0). No production
code changed during review. This is one logical review round, not two; no counter reset.
Grok did not execute tests. The exact reviewed source/test hashes are in the adjacent
`annex-publication-stage1-grok-1-manifest.json`. Four additional restore-admission tests were added
while review ran; they are not claimed as reviewed in that manifest. The branch moved to a new
branch at an identical base tree; no reviewed source content changed.

Parent separately found an architectural test's fixed lock-adapter inventory omitted the two new
adapters; explicit shared key expansion in restore and that inventory update are queued with the
review fixes. This was disclosed to Grok and is not attributed to its discovery.

## Reviewer verdict (verbatim)

**IMPLEMENTED CHECKPOINT ACCEPT** (Stage 1 local Grok round **1/3**). This accepts the accumulated components as a base to keep building. It does **not** accept Stage 1.

I did not run tests. Findings are from reading the listed modules, integrations, tests, and the Stage 1 / DEC-157 contracts. Parent’s queued `tests/test_drive_identity.py` adapter-inventory update and restore `compatible_keys` change are not treated as review findings.

No P1 authority, rollback, participant-reclassification, closure, or v7/v8 regression turned up in these components. Conversion stays refused (`prepare_operation` allows only `fill` / `replica`). `MAX_SUPPORTED_CATALOG_VERSION` is still 8. v9 is installable only through `_install_schema` inside `graph_write`; current readers/writers do not admit it.

---

## P1

None.

---

## P2

**P2-1. Catalog pair CAS will refuse migrated `archived` tables whose physical column order differs from current `CREATE TABLE`.**

```25:29:modelark/publication_catalog.py
def _schema(con):
    # A changed table cannot silently add facts outside our before/after binding.
    for table, columns in _COLUMNS.items():
        if tuple(row[1] for row in con.execute(f"PRAGMA table_info({table})")) != columns:
            raise PublicationRefused("PUBLICATION_CATALOG_SCHEMA_UNQUALIFIED", table=table)
```

`_COLUMNS["archived"]` is the current `schema.sql` order (`stored_relpath` immediately after `stored_name`; `orig_sha256_provenance` last). Named `SELECT` / `INSERT` / `UPDATE` would still bind every listed fact if only order differed. The guard compares `PRAGMA table_info` order, so extra columns are correctly refused (covered), but a same-set, different-order table is also refused.

Reproduction: create `archived` without `stored_relpath`; apply `modelark/core/db.py` `_MIGRATIONS` `ALTER TABLE archived ADD COLUMN stored_relpath VARCHAR` (that column lands last); add `orig_sha256_provenance`; install publication DDL; `publication_locks.hold` + `publication_catalog.capture`. Expect `PUBLICATION_CATALOG_SCHEMA_UNQUALIFIED`. Fixtures use current `CREATE TABLE` via `mem_con()`, so they do not see this. Any later cloned v7/v8 catalog that received `stored_relpath` by `ALTER` will.

This is fail-closed, not a silent partial CAS. Fix the qualification to require the named column set (and reject extras), not physical order, before pointing catalog CAS at a migrated catalog.

**P2-2. `LocalPayloadProof.record()` is not JSON-roundtrip stable.**

```41:47:modelark/publication_payload.py
    def record(self):
        from dataclasses import asdict
        return {"version": 1, "kind": "descriptor-stored-payload", **asdict(self)}


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
```

`root_identity`, `object_identity`, and `mapped_identity` are tuples. `publication_store.canonical` / `_unseal` persist them as JSON arrays. Resume that compares an unsealed receipt to a fresh `proof.record()` with raw dict equality will mismatch even when the bytes are unchanged. Store the identities as lists (or always compare through `canonical()`).

---

## Lower priority

- `publication_payload.verify(..., require_scope=...)` only calls a caller-supplied callable. `lambda: None` satisfies it (tests do this). Write context binds connection/token/thread; this primitive does not bind `_FenceScope.require`. Documented layering, but a boolean-shaped hole if the coordinator is wired carelessly.
- `advance_file(..., phase="CATALOG_PUBLISHED", catalog_cas=noop)` records `CATALOG_PUBLISHED` without `require_catalog_transition`. Lifecycle tests use that on purpose. The store does not prove the pair CAS ran; the unfinished coordinator must.
- `publication_locks.hold()` can raise `DriveMutationRefused` from `_fence_identity` or unwrapped `FenceUnavailable`, unlike `drive_mutation`’s typed fence mapping.
- `_FenceScope.identities` is a mutable dict. Tampering fail-closes on `load_owned_operation` / `require()`, but the selected set is not frozen in memory.
- No test for `defer_revision_once` `CATALOG_WRITE_FINALIZER_CONFLICT` when the same operation key is rebound with a different `(previous, created)` in one adapter transaction.

---

## What is sound to build on

Write-context authority is connection-scoped, not `con.in_transaction` or a copied expired context: nested same-connection enter is refused; another connection cannot use the ambient context; a stale session token never runs the callback; proven no-op cannot keep markers; late finalizer failure rolls back graph rows, markers, planner revision, and session `bound_planner_revision` together; two markers share one bump.

Publication locks take controller → map → sorted drives with no DB transaction open; `child_fence_fds` includes controller and map; a child that inherits those FDs keeps exclusion after parent exit. Session vs graph writer identity is rechecked on each `scope.write`. Terminal-owned dirty generations refuse the graph adapter (`PUBLICATION_MAINTENANCE_ADAPTER_REQUIRED`).

Durable records freeze participants at prepare; continuation rechecks live dirty-generation owners against that freeze; dropping a drive from a resume scope is `BINDING_CHANGED`; unrelated `graph_write` makes `last_revision` stale without clearing the obligation; per-file / per-batch success cannot close; failed second-anchor close rolls back the first anchor. `_CLOSING` is `(connection is, operation_id)`, not a boolean.

Catalog CAS (on current `CREATE` order) compares the full files/archived/replicas pair plus the selected replica source pair, freezes intended after-state, refuses other-drive inserts and replica deletion, preserves timestamps when copied into the frozen pair, and rolls back first-table writes on postcondition/trigger failure. Only the bound file’s `CATALOG_PUBLISHED` callback may invoke the pair swap.

Payload IO hashes the confined object and the mapped locked pointer or unlocked bytes; object-symlink, parent-symlink, FIFO, truncated, and mid-hash replacement are refused. Pointer checks are full committed blobs, not key substrings. Location logs and advisory tags are separate; map-UUID content claims and drop claims refuse.

v7/v8 behavior: `library()` is `None` without publication tables, so `require_clear` is a no-op. Restore’s new path yields existing `_may_mutate` and does not take map/controller locks. Slice still uses its existing nonblocking drive fence and does not invert lock order. Serial repair stamps `max(user_version, 8)` instead of forcing 8.

---

## Unfinished integration (not defects in this checkpoint)

These remain open and still block Stage 1 acceptance:

1. ArchivePublisher coordinator; typed qualified-profile / committed-tree / map proof factories at the public boundary (store/lifecycle still take synthetic proofs).
2. Physical proof factories and authority adapters in controller → map → drive-fence → short TX order on the six existing sync sites; surviving-child recovery for the new map lock on the Fill/`drive_mutation` path (that envelope still inherits drive FDs only).
3. Shared obligation-guard integration at tree-changing sync/registration (`fetch.py` / `register.py` still have no `require_clear`); audit of every admission/clean-anchor/recovery route; current owner may finish only sealed remaining steps.
4. Additive catalog floor above 8, v7/v8 migration, read-only compatibility, source admission. **Do not admit v9 in current readers before those guards are wired.**
5. Ordinary acquisition and replica publication through the publisher, including mapped target paths, codec proofs, and per-key advisory tags.
6. Map quarantine / native merge / ref CAS / index/worktree replay, verified no-op receipts, enclosing-generation closure, selected participant loss as production flows.

Synthetic lifecycle receipts do not prove map, committed-tree, inventory, or real payload IO. Clone-obligation rows have a guard but no prepare API.

---

No repeated review-driven architectural smell is claimed beyond the usual DEC-157 split: persistence and IO helpers still accept caller-supplied proof/CAS/scope callables, while adapter-owned transactions and frozen participant records do not. That gap is the unfinished coordinator, not a new bypass in these components.

**Overall Stage 1 remains incomplete** because the integration gates above are unfinished. Conversion stays disabled; there is no live archive, catalog, service, Fill, or drive mutation in this increment. Continue Stage 1 implementation; do not treat this checkpoint as stage acceptance.

