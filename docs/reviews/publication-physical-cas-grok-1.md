# Disposable convert before-state CAS — Grok CLI review 1

Date: 2026-09-17
Reviewer: local Grok CLI. Section counter: **1/3**. Outcome: **ACCEPT**.
Reviewed commit: `a5efbf5`. Closes Greptile PR 86 P1s on `cae2f61` (DEC-169).
Grok did not run tests.

## Disposition

**ACCEPT**. Frozen `stored_relpath` is the only source. Commits are path-limited.
Catalog CAS matches inspect before-state. Retirement is required; already-keyed
resume still retires leftover source. Live catalog path remains forbidden.

## P2 (not blocking)

1. `_retire_source` treats a missing worktree file as done, so resume may skip a
   staged deletion if `git rm` succeeded and `git commit` failed.
2. Dest still defaults to `_mapping(rfilename)` when `proposed_stored_relpath`
   is absent; sealed inspect plans carry the proposed path.
3. `_convert_file` does not fail closed on a missing digest; inspect will not
   mark that row convertible.
