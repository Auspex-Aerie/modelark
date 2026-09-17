# v9 registration setup adapter — Grok CLI review 2

Date: 2026-09-15
Reviewer: local Grok CLI session `01a0a7cc-c987-7e11-8437-9e46e00bd6b1` (resumed after client crash; abort not counted).
Section counter: **2/3**. Outcome: **ACCEPT**.
Reviewed commit: `bc1d6fa`. Follow-up P2 fix committed after the verdict.
Parent verification: ruff clean; registration tests **34 passed** after the P2 fix.
Grok did not run tests.

Round 1 REJECTED: dual fence adapters reacquired controller/map flocks, skipped
`_CONTROLLER` so `map_write` failed, and `child_fds` were empty.

## Disposition

**ACCEPT**. Round-1 P1s closed. One P2 (v9 `register_drive` nested hold on a second
`db.connect()` prevented catalog CAS) was fixed after the verdict by passing the
outer `setup` into `_register_drive_archive`.

## P1

None.

## P2 (fixed after accept)

v9 `register_drive` outer `hold()` did not capture `setup`; `_register_drive_archive`
opened a second catalog connection and nested `hold()` raised `SCOPE_NESTED`.
`register_nas` already reused one connection.
