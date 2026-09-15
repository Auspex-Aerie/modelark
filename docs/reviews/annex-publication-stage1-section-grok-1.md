# ArchivePublisher Stage 1 landing section — Grok CLI review 1

Date: 2026-09-15
Reviewer: local Grok CLI, session `01a0a387-9683-7fd0-b863-2aa71d1d0c07`.
Section counter: **1/3**. Outcome: **ACCEPT**.
Full Stage 1 remains incomplete. Conversion stays disabled.
Parent verification: ruff clean, `git diff --check` clean, scoped suite **1036 passed, 2 skipped**.
Grok did not run tests. Source hashes at review start: `annex-publication-stage1-section-grok-1-manifest.json`.
After ACCEPT, one stale `publication_store.py` module docstring was aligned with the v9 reader floor (not a P1/P2; reviewer noted it as documentation drift only).

## Invocation

```text
grok --cwd /home/phaze/PycharmProjects/modelark/claudedocs/operator-scratch/worktrees/codec-stage-d --permission-mode plan --no-subagents --disable-web-search --output-format plain --prompt-file /tmp/modelark-annex-stage1-section-grok-1.txt
# first headless pass cancelled shell tools in plan mode
grok --cwd ... --resume 01a0a387-9683-7fd0-b863-2aa71d1d0c07 --permission-mode plan --no-subagents --disable-web-search --output-format plain --tools read_file,grep,list_dir --prompt-file /tmp/modelark-annex-stage1-section-grok-1-continue.txt
```

## Reviewer verdict (verbatim)

**ACCEPT** — this section is safe to open as a PR (conversion still disabled; full Stage 1 still incomplete).

Read-only review of the listed coordinator, Fill/replica routing, v9 reader floor, serial-repair, registration gate, catalog CAS, and the matching tests. I did not run tests, ruff, or `git diff --check`; parent-reported 1036 passing is not evidence here.

## P1 (blocking)

None.

## P2 (should fix before land)

None that are live bypasses, conversion enablement, fabricated completion, lock-order reversal, v9-without-contract admission, or serial-repair downgrade.

## Residual unfinished work (not a defect)

These match the tracker and are fail-closed in this increment:

1. **Durable v9 registration setup adapter** — `require_legacy_registration` still raises `REGISTRATION_PUBLICATION_ADAPTER_REQUIRED` when `library()` is present (`register_drive`, `register_nas`, `register_new_identity`, `ensure_library` / `require_library_setup`). Catalog-facing registration cannot succeed on v9.
2. **Admission / clean-anchor / recovery route audit** — shared `require_publication_clear` is on serial repair, reconcile, and owned recovery; owner-bound continuation of sealed remaining steps is still later work.
3. **Failed / skipped / gated Fill vs frozen batch-child set** — per-repo policy, gated, and requirement failures can continue the loop; `finish()` still requires every selected child `CATALOG_PUBLISHED` and `__exit__` refuses enclosing completion if unfinished. The operation stays `PREPARED`. Children are not dropped to close.
4. **Full fault-injection** — not claimed.
5. **Live conversion / Fill restart / catalog migration apply** — `_install_schema` is an explicit graph-write primitive, not `connect()`. `prepare_operation` allows only `fill` / `replica`; other kinds raise `PUBLICATION_CONVERSION_DISABLED`. Bootstrap layout remains 7.

## What this section actually holds

**Conversion stays off.** `ArchivePublisher` construction and `store.prepare_operation` accept only `fill` / `replica`. Ordinary `db.connect()` does not install publication tables or stamp 9.

**v9 reader floor is not implicit install.** `SUPPORTED_CATALOG_VERSIONS` is `{7, 8, 9}`. `validate_publication_schema` → `library()`: older floors with publication tables refuse; v9 without the exact contract refuses (`PUBLICATION_SCHEMA_UNSUPPORTED` / `UNQUALIFIED` / `LIBRARY_UNPROVEN`). Tests show invalid contracts are refused before journal/schema writes.

**Serial repair is monotonic.** Floor is `max(current, 8)`, so 9 stays 9. Inspection of a v9 floor without the contract raises `PUBLICATION_SCHEMA_UNSUPPORTED`. Repair/reconcile recheck `require_publication_clear`.

**Fill/replica routing.** v9 uses `fetch_publication.scope` / `replica_tasks`, not the mutation envelope. Unexpected Fill errors become `PUBLICATION_ACQUISITION_FAILED` and never `_dest_writable`. `run_replica` refuses `PUBLICATION_EXACT_REPLICA_TASKS_REQUIRED`. Legacy v7/v8 still uses `drive_mutation` and `legacy_map_targets` before named annex sync.

**`resume_file` does not invent publication.** Skip re-download only for `CATALOG_PUBLISHED` plus a verified staging-release receipt (or release performed here). Missing file row or any earlier phase returns `None`. Resume-for-closure without re-download is covered.

**Lock order / CAS.** `locks.hold` is controller → map → sorted drives, then short adapter TX. `_advance_one` is catalog-only inside that TX, not a nested fence. Catalog pair CAS is the bound `CATALOG_PUBLISHED` callback (`require_catalog_transition` + same-TX `compare_and_swap`); no nested writer transaction. Closure requires the frozen child set, propagated batches, and every selected participant.

**Registration physical prep** still runs only after `register_new_identity` / `register_drive` / `ensure_library` hit the v9 refusal. Direct `prepare_new_identity_archive` remains a callback, not a catalog success path.

## Repeated architectural smell

Low-level store/lock/CAS APIs still accept an already-held scope and caller-built proof payloads. Fill/replica now bind that through `ArchivePublisher`; registration, conversion, and maintenance still must not treat those primitives as a public publisher. That is the known DEC-157 integration gap, not a new hole in this increment.

Secondary: `publication_store` still says schema 9 is not admitted by `catalog_versions`. The reader floor now includes 9 with `validate_publication_schema`. The code matches the tracker; the module docstring does not. Not a bypass.

Fill still isolates per-repo exceptions and relies on `finish()` as the completeness backstop. That keeps gated/failed children pending rather than optionalizing them. Correct for this section; still the batch-child follow-on.
