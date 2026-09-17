# Admission/clean-anchor/recovery audit — Grok CLI review 1

Date: 2026-09-16
Reviewer: local Grok CLI (`--no-plan`, tools `read_file,grep,list_dir`).
Section counter: **1/3**. Outcome: **ACCEPT**.
Reviewed commit: `e24a267`. Docstring P2 fixed after the verdict.
Parent verification: ruff clean; store/authority/lifecycle tests green.
Grok did not run tests.

DEC-161: PREPARED map-only registration is a library-wide `require_clear` block.
Expired-session recovery rechecks the guard before terminalizing. `_CLOSING`
inside `close_operation` remains the only clean-anchor exemption.

## Disposition

**ACCEPT**. No P1.

## P2

1. Session-recovery test stubs the guard rather than inserting a PREPARED row
   (coverage, not a bypass). Left as-is.
2. `require_clear` docstring omitted the library-wide registration block.
   Fixed after accept.
