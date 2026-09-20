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

## PR 88 — Codex cloud round 1/3

Outcome: **NOT ACCEPT** — three P1s, no P2. Codex found that inventory did
not admit an empty legacy parent directory removed with its last retired child;
separate drive batches tried to delete the same shared central-map path twice;
and a partial per-drive file workset could close the drive's entire layout-1-to-2
obligation while another raw path remained.

The common cause is scope promotion: a valid file-level transition was being
treated as proof of map-exclusive or clone-exhaustive completion. DEC-172 makes
those scopes explicit. Inventory now normalizes only actually vanished retired
ancestors; map deletion is idempotent only with the exact replacement already
present; and clone closure requires a frozen admission census plus final inventory
with no remaining raw catalog claims or unclaimed model-namespace paths. Otherwise
the operation closes while the clone obligation remains pending and tree-blocking.
New nested-path, partial-workset and two-drive shared-map regressions pass. Codex
cloud re-review is required on the correction commit. Final correction verification:
the focused retirement suite passed **8 tests** in 6m33s and the complete scoped
publication regression passed **123 tests** in 23m30s; Ruff and `git diff --check`
passed.

## PR 88 — Codex cloud round 2/3

Outcome: **NOT ACCEPT** — one P1 and one P2. Codex found that the map candidate
removed a legacy path because its name existed without comparing the map entry
to the source retirement's sealed old entry. It also found that two distinct
same-drive requests could claim one physical legacy path, allowing durable file
and catalog work before the second per-file retirement discovered the collision.

These findings share an ownership-proof boundary, recorded as DEC-173. A path is
not proof of the object owned at that path. Admission now requires one request per
`(drive_label, retired_path)`, before entering the operation. The verified source
retirement before-snapshot now supplies the exact old `TreeEntry`; map publication
uses compare-and-delete and refuses a divergent object. Cross-drive idempotence
from DEC-172 remains valid only after the first exact comparison and only when the
exact replacement is already present. New regressions cover both cases. Codex
cloud round 3 re-review is required on the correction commit. The complete
retirement end-to-end file passes **10 tests** in 7m09s; the two new cases plus
the cross-drive deduplication regression pass **3 tests** in 2m10s. Ruff and
`git diff --check` pass. The neighboring ArchivePublisher and map-candidate
normal/conflict/resume matrix also passes **18 tests** in 7m47s.
