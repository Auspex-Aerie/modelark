# Native command fallback containment — Grok CLI review 1

Date: 2026-09-15
Reviewer: local Grok CLI (`--no-plan`, tools `read_file,grep,list_dir`).
Section counter: **1/3**. Outcome: **ACCEPT**.
Reviewed commit: `40e900f` on `codex/annex-publication-stage1`.
Parent verification: ruff clean; `tests/test_publication_native.py` **30 passed**.
Grok did not run tests.

The first prompt stated why this changed: Greptile extra-3x oscillated between
hard `CGROUP_REQUIRED` (qualify outage) and session-loop fallback (`setsid`
escape). DEC-159 makes the fallback the ppid tree plus a temporary
`PR_SET_CHILD_SUBREAPER`, not another session scan.

## Disposition

**ACCEPT**. No P1. Three P2s recorded below; not blocking this section.

## P1

None.

## P2

1. `_owned_session_members` matches **pgrp**, not session id (stat field 5 vs 6).
   Third pass is mislabeled; tree still holds `setpgid` descendants.
2. `killpg` also runs after a successful `pidfd_open` when `_proc_starttime`
   returns `None`. DEC-159 limits `killpg` to instant `pidfd_open` failure.
3. Fallback reap is exception-only. A 0-exit leader without a cgroup can leave
   descendants; timeout tests do not cover that path.

## Residual (not a defect)

Subreaper is armed at timeout cleanup, not for the whole command. A descendant
that already double-forked/`setsid` and reparented to init *before* reap is
outside both this fallback and a late-attached cgroup. Installing subreaper at
spawn would steal unrelated orphans process-wide.

## Verbatim verdict

See `/tmp/modelark-native-containment-grok-1.out`. Closing token: **ACCEPT**.
