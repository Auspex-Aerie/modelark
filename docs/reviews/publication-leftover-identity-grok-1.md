# Leftover durable identity — Grok CLI review 1

Date: 2026-09-16
Reviewer: local Grok CLI. Section counter: **1/3**. Outcome: **ACCEPT**.
Reviewed commit: `31c5e32`.
Parent verification: ruff clean; registration/lifecycle/register-safety **70 passed**.

DEC-163: leftover identity is cataloged `serial` / `fs_uuid` / `annex_uuid`, not `/dev` or path spelling.

## Disposition

**ACCEPT**. No P1.

## P2

1. Durable check is in `leftover()`, not `hold()`. Operator register paths call `leftover()` first; a later same-label `hold()` could resume without re-checking facts if the drives row is missing.
2. `observe_locator` (lsblk SERIAL/UUID) is not the same probe as SMART serial / `lsblk -dno UUID` used at first registration. Fail-closed if they disagree. Annex-only rows cannot match because observe does not return annex_uuid.
3. Tests cover the `leftover()` predicate, not `register_drive()` with a renamed `/dev`.
