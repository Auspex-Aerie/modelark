# Guarded map retirement — Grok CLI review 1

Date: 2026-09-19
Reviewer: local Grok CLI in read-only sandbox; no subagents or web.
Section counter: **1/3**.
Outcome: **NOT ACCEPT** — one P1, no P2.

## P1: per-file final-snapshot equality breaks same-drive multi-file maintenance

`publication_map_files._source_proofs` required every retirement receipt's
`after` snapshot to equal the repository's one final HEAD. In a batch with two
files, only the last retirement can satisfy that. The inventory verifier also
followed catalog commit order as if it must equal Git pointer/retirement commit
order. A multi-file operation could therefore annex, catalog-CAS and retire its
old sources, then remain unable to propagate or close.

Required correction: prove every pointer and retirement as an exact transition
and compose their one linear commit chain; at map time require each retirement
commit to be an ancestor of the final source HEAD rather than equal to it. Add a
same-drive two-file end-to-end test. The reviewer also noted that post-commit
lost-reply recovery existed but lacked a direct test.

## Disposition in progress

The map proof now validates the retirement action's frozen before/after tree and
bounded first-parent ancestry. Inventory builds all selected pointer/retirement
transitions and follows the exact chain from the frozen baseline to the final
tree. New tests cover two files published before either retirement and both
before-commit and after-commit interruption. Re-review is required; this file
does not claim acceptance.

## Round 2/3

Outcome: **NOT ACCEPT** — one P1, no P2. Grok confirmed the round-1 transition
composition fix, retirement replay states, clone-obligation fencing and live
cutover refusal, then found that `source_retirement` had been added to the exact
v9 `publication_actions` DDL without raising the physical catalog version. An
already-installed PR-86 v9 catalog would therefore fail exact schema validation,
and its old SQLite CHECK could not store the new action.

Disposition in progress: preserve the exact v9 DDL, add an explicit additive v10
action-table migration, support exact v9 and v10 readers, and require v10 for
maintenance/source retirement. This is DEC-171. Round 3 re-review is required;
round 2 does not claim acceptance.

## Round 3/3

Outcome: **ACCEPT** — no P1/P2 correctness issues. Grok verified that the frozen
v9 action DDL is byte-identical to released HEAD, the remainder of v9 DDL is
unchanged, v9-to-v10 rebuilds only `publication_actions` inside `graph_write`,
copies all rows, checks foreign keys and receives the normal atomic revision
bump. It also confirmed that ordinary connect, Slice and the existing schema
ladder do not implicitly upgrade v9; maintenance requires v10; future floors
remain refused; and the retirement/map/clone-obligation corrections remain
fail closed.

Across the three cycles, the two substantive findings shared one architectural
smell: an immutable historical state was being compared with or replaced by the
latest aggregate representation. Round 1 confused an intermediate per-file Git
commit with final repository HEAD. Round 2 changed a released exact-match schema
under the same physical version. The implemented architectural correction is to
model explicit monotonic transitions: compose Git before/after edges and assign
new physical schema capabilities a new catalog version. No further common-cause
refactor was recommended after those corrections.

Final verification after the v10 correction: 80 store/catalog reader and migration
tests passed; 44 action/store/retirement tests passed; 134 additional focused
reader/repair/Slice tests passed in the restricted sandbox, with the 13 lease-socket
cases then passing as part of the complete 50-test Slice operator file outside that
socket restriction; and the full scoped publication regression passed **120 tests**
in 19m54s. Ruff across every changed Python file and `git diff --check` passed.
