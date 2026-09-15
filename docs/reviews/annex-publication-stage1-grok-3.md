# ArchivePublisher Stage 1 — Grok round 3

Date: 2026-09-14 (operator timezone; CLI UTC session date September 15).
Local Grok CLI, session `a72fe127-0c4e-4fc5-a91a-a318dbe1110f`.
Stage counter: **3/3 used**. Outcome: **INCREMENT ACCEPT**, no P1/P2 or other actionable
findings, and no new shared architectural failure. **Full Stage 1 remains incomplete.**

The operator explicitly requested review of the follow-on tree/IO-guard increment now,
superseding the earlier intention to reserve the third round for completed integration.
This does not reset the stage counter. Stop here on this increment's approval; do not
start a fourth review or represent the unfinished whole stage as accepted.

Grok performed read/search only. Two context compactions occurred during its substantive
read pass. The parent interrupted only its identified headless client PID 2134529
(exit 130), then resumed the SAME saved session with a findings-only/no-tools request.
The continuation returned the verbatim verdict below and exited 0. This is one logical
review round, not a new review. No code or tests changed during the review; all source,
test and context file hashes matched the adjacent pre-review manifest before this report
and tracker update. Unchanged dependency hashes establish context, not another full review
of every unchanged module.

Parent's preceding verification: **916 scoped tests passed**, two existing Torch deprecation
warnings, 83.48 seconds; Ruff and `git diff --check` passed. Grok did not run those checks.
This turn did not rerun tests or change implementation. No implementation commit, push,
PR, live catalog/archive/service/Fill mutation or conversion activation occurred.

Optional nonblocking follow-up tests noted by Grok: session-token loss and identity changes
specifically during `require_io`, plus `CommittedTreeProof.record()` through store canonical
serialization. No additional architecture redesign was recommended. The qualified native
reader/profile, owning coordinator, map publisher and runtime/recovery/admission integration
remain required; see the tracker and original plan. Callback/profile digest inputs are not
production qualification merely because this observer increment was accepted.

## Reviewer verdict (verbatim)

**INCREMENT ACCEPT** (Stage 1 local Grok round **3/3**). This accepts the follow-on committed-tree observer, IO snapshot guard, frozen participant/authority view, store read-only binding split, and the real byte/tree composition tests as a still-dormant implementation increment.

**Overall Stage 1 remains incomplete.** This is not full Stage 1 acceptance.

I did not run tests. Findings are from the completed read of the named modules, tests, directly called helpers, the approved architecture, and the round-1/round-2 verdicts. Parent-reported **916 passed** is not review evidence.

---

## What this increment actually is

Round 2 accepted checkpoint corrections only. This round reviews the next on-disk increment:

- `publication_tree.py`: read-only HEAD/commit/tree/index observation, exact sealed delta, pointer parse, SHA-1 object check, NUL framing, size preflight, no `write-tree`.
- `publication_locks.py`: frozen `_FenceAuthority` plus `MappingProxyType` identities; `require_io` refuses an open caller TX, takes a short read snapshot, then rolls it back before IO.
- `publication_store.py`: `load_owned_operation` still requires the write adapter; `_load_bound_operation` is the read-only binding/revision check `require_io` needs.
- Tests: disposable Git/annex tree cases, IO-guard/immutability cases, and real payload+tree receipts under controller/map/drive scopes.

Catalog version 9 is still not globally admitted (`SUPPORTED_CATALOG_VERSIONS = {7, 8}`). Conversion stays refused. `publication_tree` is not imported by fetch, restore, or other runtime writers.

---

## 1. Verdict

**INCREMENT ACCEPT.**

**Full Stage 1 is incomplete.** The public coordinator, qualified bounded native reader/profile, map quarantine/native merge/ref/index/worktree publication, ordinary Fill/replica/registration wiring, and recovery/admission/v9 floor remain unfinished. Those absences are not treated as newly discovered defects in this increment.

---

## 2. Findings

None.

No P1 or P2 in this increment. I am not inventing issues to fill a quota.

Round-1 P2s stay closed (named catalog columns; payload `record()` JSON shape). The round-1 lower-priority mutable `_FenceScope.identities` is closed: identities live on frozen `_FenceAuthority` as `MappingProxyType` of frozen `FenceIdentity` values (`publication_locks.py` 25–32, 50–52, 200–201; lifecycle 158–169).

Residual layering that is **still unfinished, not a new defect**:

- Tree/payload `require_scope` remains a caller-supplied callable. Unit tree tests still pass `lambda: None`. Integration binds `scope.require_io`. The coordinator must own that binding.
- `require_io` / `require()` can still raise `Refusal` or `DriveMutationRefused` from `_require_owner` / `_fence_identity`. Same fence-exception adaptation gap as round 1; `require_io` does not make it a new bypass.

---

## 3. Missing tests

No substantive missing test for behavior this increment actually implements.

Covered with real Git or real locks: locked/unlocked pointers (including pinned annex 8.20210223), NUL/tab/newline/`é` paths, assume-unchanged/skip-worktree, unmerged stages, wrong branch, extra/intervening commits, retirement+add, wrong hash-dir pointer, ref/index/HEAD races, replacement refs, large non-pointer blob refused before `cat-file blob`, command allowlist, sealed-baseline decoder, IO snapshot without revision/`total_changes`, caller TX not rolled back, immutable participants, composition that retains PREPARED / LOCAL_VERIFIED / TREE_VERIFIED and never writes `archived` or a clean anchor.

Optional later tests, not blockers: session-token loss on `require_io`; identity change on `require_io` rather than only on `write()`; `CommittedTreeProof.record()` through `store.canonical` the way payload proofs are compared. Those would lock in already-visible behavior.

---

## 4. Architectural root cause

No new shared architectural failure in this increment.

The remaining DEC-157 split is unchanged and still the Stage 1 gap: low-level store/IO primitives accept coordinator-supplied reader, `require_scope`, profile digest, and proof dicts. This increment does **not** treat a callable as qualification. `_read` always prepends `--no-replace-objects` and `--literal-pathspecs`; post-allocation size checks are defense in depth; the future reader must still bound output before allocation. Do not wire a test reader or a caller-supplied profile digest in as production qualification.

No additional redesign is warranted by this increment. Build the owning coordinator and typed proof factories next; do not relax these primitives to infer publication from Git exit status or a bumped revision.

---

## 5. Why the increment is sound (checked concerns)

**Tree/index/ref/lineage.** `capture` requires index `H` + stage `0`, index==tree, stable HEAD and index over the read, then attachment identity (`publication_tree.py` 202–228). `verify` applies the sealed mapping, recaptures, requires same root/mount/ref, exact entries, single-parent lineage for a non-empty delta, unchanged HEAD for an empty delta, full pointer blob via `check_committed_pointer`, then a final recapture (`231–275`). Unrelated edits, extra commits, branch moves, and post-commit staging cannot hide behind a valid pointer.

**Objects and replacement.** Raw `cat-file` is size-preflighted, length-checked, and SHA-1-matched as `type size\\0content` (`188–199`). `--no-replace-objects` is on every read (`123–129`). Replacement refs cannot substitute another commit (`tests/test_publication_tree.py` 303–314). Pointer blobs are capped at 4096 before content is requested (`267`, `269–285`).

**Framing.** NUL-split records, first-tab path split, UTF-8 paths, ASCII headers, duplicate-path and file/dir collision refusal (`145–170`). Policy `relative_path` still rejects `.git` / `..` / NUL. Unlocked mode remains `100644` + `/annex/objects/{key}\\n`; locked is symlink bytes of the qualified object path (`publication_policy.py` 78–97).

**IO vs transactions.** Catalog connections are `isolation_level=None`. `require_io` raises if `in_transaction`, then `BEGIN` / recheck owner+binding / `rollback` (`publication_locks.py` 92–109). It cannot stamp markers or `planner_revision`. Tests show a caller `BEGIN` is left intact and `scope.write(lambda _: scope.require_io())` is `IO_TRANSACTION_ACTIVE`. Unrelated `graph_write` makes the next `require_io` `OPERATION_STALE` without clearing the obligation.

**Store split.** Write paths still go through `load_owned_operation` → `require_transaction` (`publication_store.py` 212–215). `require_io` uses `_load_bound_operation` only after fence + owner inside the short snapshot (`218–236`). That matches the plan: no Git/hashing under an adapter write TX; later phase CAS still does the full revision check.

**Proof boundary.** Tree proof is index/tree/ref/pointer, not object presence. Payload proof is the reverse. Composition test: missing object stays PREPARED with no tree receipt; unrelated tree edit stays LOCAL_VERIFIED; success is TREE_VERIFIED with real hashes; `archived` count 0; generation 2 clean anchors 0; `require_clear` still `MAINTENANCE_REQUIRED`.

**Rollback/ownership.** `scope.write` still joins `graph_write` / `session_write`, one revision bump, operation stamp restore on exception (`publication_locks.py` 111–150). Prepare failure still restores `operation_id is None` and rolls back rows.

**Production effect.** No runtime importer of `publication_tree`. v9 still only via `_install_schema` inside `graph_write` in tests.

---

## 6. Review limitations (this is still round 3/3)

- Read/search only. No shell, tests, edits, web, subagents, or live catalogs/archives/drives.
- I did **not** execute the parent 916-test suite, Ruff, or `git diff --check`.
- Uncommitted/untracked files were read as on-disk sources, not as a git diff.
- Unchanged checkpoint modules were consulted at the called helpers (policy pointer, payload `verify`, write adapters, `BoundTree`, `_fence_identity`, owner checks), not re-reviewed as a full Stage 1 codebase.
- Native Git/`ls-files -v --stage -z` framing is accepted from the tests plus parent’s disposable Git/annex run, not from a reviewer-executed git.
- Two context compactions and a headless-client interrupt occurred; this continuation used the completed read pass. No source or test change was reported between those events.

---

## 7. Gates this increment acceptance does **not** close

1. ArchivePublisher coordinator; typed qualified-profile / committed-tree / map factories at the public boundary (store still accepts opaque proof dicts).
2. Qualified bounded native command/profile adapter (this observer is not that reader; it does not prove worktree bytes or helper/filter/config).
3. Controller → map → drive-fence → short TX on the six sync sites and registration; surviving-child recovery for the map lock on ordinary Fill/`drive_mutation` (that envelope still inherits drive FDs only).
4. `require_clear` on fetch/register and the rest of admission/recovery; current owner may finish only sealed remaining steps.
5. Additive catalog floor above 8; v7/v8 guards; **do not admit v9 in current readers**.
6. Ordinary acquisition and replica publication, including mapped destination paths, codec proofs, and per-key advisory tags.
7. Map quarantine / native merge / ref CAS / index/worktree replay, verified no-op receipts, enclosing-generation closure, selected participant loss.
8. Fill failed/skipped/stopped children vs the store’s frozen complete batch-child set (integration work item, not a defect here).

Conversion stays disabled. No live archive, catalog service, Fill, or drive mutation is authorized by this verdict.

Round 3/3 is used. Stop the stage counter here: increment accepted, Stage 1 not accepted. Continue implementation of the coordinator and remaining gates; do not start a fourth review of this increment.
