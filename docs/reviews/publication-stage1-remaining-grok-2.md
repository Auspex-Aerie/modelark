# Remaining Stage 1 items — Grok CLI review 2

Date: 2026-09-16
Reviewer: local Grok CLI. Section counter: **2/3**. Outcome: **ACCEPT**.
Reviewed commit: `642afdc`. Follow-up: `leftover(..., cataloged=True)` on mutating
retry paths so unpublished leftovers do not take the close-only no-op catalog.

Round 1 REJECTED: uniqueness guards ran before leftover hold.

## Disposition

**ACCEPT**. No P1 after `642afdc`.

## P2

1. Close-only leftover path should be cataloged-only — gated after accept.
2. No leftover-after-catalog test for `register_drive` / `register_nas`.
3. `register_drive` leftover success uses `library_root()` as archive.
