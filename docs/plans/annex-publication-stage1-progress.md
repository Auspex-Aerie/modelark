# ArchivePublisher Stage 1 implementation tracker

Updated: 2026-09-15. Mutable work status, not a replacement for the decision ledger.

## Authority and scope

Latest operator instruction (2026-09-14): continue all remaining Stage 1 work without
another partial implementation checkpoint; review after Stage 1 is completed. This
explicitly authorizes a completed-stage review after the historical three checkpoint
rounds below. It does not erase or reset those consumed rounds. Keep conversion and
live deployment disabled; only a genuine blocker or materially new decision pauses work.

The operator authorized Stage 1 after Stage 0 qualification acceptance, then explicitly
required local Grok CLI review/fix cycles of at most three rounds after each implemented
stage. Stop on acceptance or after round three; summarize unresolved findings and repeated
root causes. Small commits, pushes or internal increments do not restart a stage's counter.

Stage 1 local Grok: **3/3 used — INCREMENT ACCEPT**, no new actionable findings or shared
architectural failure. The operator explicitly requested this follow-on tree/IO-guard
review now, superseding reserving round 3 for completed integration. Full Stage 1 is still
incomplete; acceptance does not reset the counter or make the full stage complete.
Rounds 1–2: checkpoint corrections accepted. Round 1 accepted the
accumulated implementation checkpoint as a base, with two P2 corrections and no P1. Round 2
closed both P2s and reported no new substantive issue; session `01a0a270-126e-7280-b5c7-ab0885092ae5`.
Round-3 session: `a72fe127-0c4e-4fc5-a91a-a318dbe1110f`, final exit 0. Stop here on this
increment's approval; do not automatically start a fourth review. Verdict and reviewed
file hashes are in `docs/reviews/annex-publication-stage1-grok-3.md` and adjacent JSON.
No full Stage 1 acceptance, commit, push or PR yet. Checkpoint acceptance does not imply the
integration gates below passed. These consume the stage counter; later integration/commits
do not restart it. Verbatim round-1 verdict and source hashes are in `docs/reviews/`.
Conversion remains disabled. No live archive/catalog, service, Fill or drive mutation.
Earlier Stage 0 qualification stopped on acceptance after 2/3 rounds and remains unchanged.

Active checkout: `claudedocs/operator-scratch/worktrees/codec-stage-d` within the main
ModelArk checkout. Current branch is `codex/annex-publication-stage1`, created from the
already-merged `origin/main` after verifying its tree exactly matched the previous base.
Existing dirty docs and unrelated acceptance/research files predate this stage and were preserved.
Use the top-level `.venv-dev/bin/python`, not a nonexistent worktree-local venv.

## Implemented foundations — not a completed publisher

- `modelark/publication_policy.py`: pure logical-path mapping, qualified SHA256 key and
  exact committed-pointer checks, timestamped location history and admitted replay states.
  These functions do not prove physical presence, caller authority, confinement or ownership.
- `modelark/publication_annotations.py`: separate native advisory-tag semantics. Validate
  exact assigned model/format/quant/params values, timestamped cell compaction, encoded
  values and a native merge's effective union. No tag establishes content identity/location.
  Source admission still needs a guarded source-write proof; parsing alone is insufficient.
- `modelark/catalog_write_context.py`, wired into existing `graph_write` and `session_write`:
  connection-scoped adapter identity and revision finalizers. Marker callbacks share the
  adapter's existing transaction and one revision bump; late failure rolls back all writes
  and the session's bound revision. Raw `con.in_transaction`, another connection, a stale
  session token, a copied expired context or a falsely claimed no-op cannot grant marker
  authority. This proves transaction ownership only, NOT drive/map fences or publication.
- Corresponding unit/regression tests and the isolated native annotation qualification
  harness `scripts/qualify_annex_annotations.py`.
- `publication_store.py`: explicit, still-unadmitted v9 schema; operation/participant/file/batch
  intents, chained receipts, adapter-finalized revisions and atomic selected-drive closure.
  Validates complete schema definitions; unknown/partial schemas are never an empty guard.
  These storage APIs still accept IO evidence from the unfinished coordinator; synthetic
  receipt tests do not establish real map, committed-tree or inventory proof.
- `publication_locks.py`: controller → shared map UUID → compatible physical-drive locks,
  inherited controller/map/drive FDs, same-token session or sessionless graph writes.
  Own progress writes advance the operation revision once; unrelated writes make it stale.
  Terminal-owned maintenance adoption remains explicitly refused, not implicitly assumed.
  Follow-on integration work adds an immutable authority/participant view and `require_io`:
  reject any caller-owned transaction, briefly recheck owner/operation in a read snapshot,
  then end that snapshot before filesystem/subprocess work. No revision or marker changes.
- `publication_catalog.py`: full archived/replica/source-fact before/after CAS, including
  expected absence and the selected replica source's full pair. Only the current file's
  CATALOG_PUBLISHED callback can invoke its exact frozen pair. No filesystem IO, transaction
  nesting, revision bump or fabricated timestamps; pair/postcondition failure rolls back all.
- `publication_payload.py`: actual descriptor-confined stored-object hash and mapped-path
  checks for the pinned SHA256 grammar, locked/unlocked forms, stable inode/content, and
  retained root/mount identity. Separate from codec/original-byte, native profile, committed
  tree, location and catalog evidence. The caller still must supply a qualified native object
  path and actual scope; the public publisher has not yet integrated these primitives.
- `publication_tree.py`: actual native read observations of HEAD, commit lineage, full
  committed file tree and index; exact prepared delta and locked/unlocked pointer checks.
  NUL framing preserves tabs/newlines; assume-unchanged/skip-worktree flags, unmerged stages, branch changes,
  unrelated edits, unexpected extra commits and unqualified object/path shapes refuse.
  Object-size preflight rejects a large raw blob before reading it as a pointer. No
  `write-tree`, filters, replacement-object interpretation or publication command is used
  by this observer. Before-state has strict JSON round-trip decoding for sealed resume.
  This still requires the coordinator's **qualified native reader**: the callable itself
  is not qualification or authority, and the reader must bound command output before
  allocation. The factory is not a production runner and does not prove worktree bytes.
  Initial observation grammar is SHA-1 Git objects, regular/symlink blob entries and a
  normal existing file branch; other formats, submodules and unborn/detached/annex branches
  are not implicitly admitted. Production profile/admission integration remains required.
- `tests/test_publication_evidence_integration.py`: composes real stored-byte and Git-tree
  receipts with durable phases under real controller/map/drive scopes. Test native children
  inherit fence FDs; all native IO stays outside catalog transactions. Missing objects and
  unrelated tree edits retain the last durable phase and obligation, without archive rows
  or clean anchors. Catalog-to-repository attachment and command profile are test fixtures;
  this is not a claim that the unfinished public coordinator or live attachment proof exists.
- Shared obligation guard added to normal mutation/clean publication, proposal/start,
  reconciliation/identity repair, selected hash repair, drive loss and source admission.
  Restore uses a fresh fenced v9 read snapshot and never implicitly retrieves into a v9
  archive. Complete registration/map-sync integration is still outstanding.

The native tag integration gap was found by tracing existing `fetch._annex_metadata`.
Stage 0 had qualified location logs, not per-key `.log.met` annotation records. Tags
compact superseded cells and repeated `-s` commands may produce new clocks; do not apply
location-log byte-history inclusion rules to them. Existing advisory tags must neither be
dropped nor admitted as an arbitrary metadata exception. The new pure validators and fresh
fixtures cover this gap without modifying the accepted Stage 0 harness/results.

## Verification so far

- Expanded synthetic-catalog regression batch: **232 passed** in 19.63 seconds. Covers
  new policy/context checks, existing sessions/writer exclusion, drive mutation, terminal
  recovery, session transport, annex tags, fetch resume, proposal CAS and catalog floors.
- The first sandboxed expanded run was **224 passed, 4 failed**. All four failures were
  CLI launch guards denied AF_UNIX socket access, before reaching the target code. Rerun
  with socket permission and a process-local unique test-only `instance._ADDRESS`; no live
  singleton was bypassed or contacted. The subsequent count also includes four added tests.
- Native annotation qualification runs and exact limits are recorded separately in
  `docs/acceptance/annex-annotations-stage1-2026-09-14.md` and its retained result JSON.
- Ruff and whitespace checks are required again before any review/commit. This is not
  a claim that the full project suite, production publisher or crash recovery passed.
- Follow-on focused storage/lifecycle/catalog/payload/mutation batch: **92 passed** before
  the last callback-capability test addition. The prior expanded run was **416 passed,
  1 failed** solely on the architecture import allowlist; narrowed helper-only imports and
  a clean-closure call-site invariant fixed that failure (59-test rerun passed).
- Real pinned annex 8.20210223 disposable-repository payload test: **1 passed**. It obtains
  the object path from native examinekey and exercises locked → unlocked → locked, then
  removes only the temporary mapped link and confirms the still-present object is insufficient.
  An initial fixture incorrectly prefixed the already-complete native object path; fixing
  the fixture (not relaxing the production grammar) made the test pass.
- Latest expanded regression: **453 passed**, two pre-existing torch deprecation warnings,
  43.00 seconds. Includes the catalog callback-capability case and native payload check.
  Separate fresh v9 restore-admission batch: **4 passed**, 0.74 seconds. Total distinct
  cases across these two runs: 457. These are scoped suites, not the full project suite.

## Remaining Stage 1 work (all required before stage acceptance)

1. Finish the actual ArchivePublisher coordinator around the implemented durable records,
   local payload primitive and catalog CAS. Typed qualified-profile/committed-tree/map proof
   factories must replace caller-supplied evidence at its public boundary.
2. Complete physical proof factories and authority adapters under the approved controller →
   map/library → sorted drive-fence → short DB-transaction order. Child inheritance and
   surviving-child recovery must cover the new map lock; do not reenter non-reentrant locks.
3. Complete shared persistent obligation-guard integration at tree-changing sync/registration
   and audit every admission/clean-anchor/recovery route; current owner may finish only its
   sealed remaining steps.
4. Explicit additive catalog floor above 8, v7/v8 migration, monotonic serial repair,
   read-only compatibility and source admission. **Do not admit v9 in current readers before
   all guards are wired.** No live migration or conversion enablement in this stage.
5. Shared ordinary acquisition and replica publication; prove mapped target paths as well
   as keys/objects; retain codec proofs and per-key advisory tags. Integration must cover
   the six existing sync sites and registration tree creation, not optional checks alone.
6. Staged map quarantine/native merge/ref CAS/index/worktree replay and durable receipts,
   verified no-op receipts, enclosing-generation closure and selected participant loss.
7. Full scoped fault-injection/regression verification remains required for completed
   integration. Local Grok rounds **3/3 are already used**, with increment acceptance,
   not full-stage acceptance. Do not relabel further review as an unused third round
   or silently reset the counter; establish any subsequent review scope explicitly.

## Review findings and fixes

- Round 1 P2-1: column-order equality incorrectly rejected historically migrated archived
  tables. Compare the exact named column set/count, retaining extra/missing-column refusal;
  SQL already names every field. Added a real legacy-ALTER-order synthetic-table CAS test.
- Round 1 P2-2: tuple-valued payload identities differed from their persisted JSON lists.
  `LocalPayloadProof.record()` now emits canonical JSON-shaped values; fresh/persisted records
  compare identically. Regression checks all three identity arrays and the full round trip.
- Parent regression finding: the architecture test's fixed physical-lock-adapter list omitted
  the new restore/publication adapters. Added both without weakening its scan, and made restore
  explicitly call the shared `compatible_keys` expansion. Separate fenced read-only tests pass.
- Added the review-requested missing regression for same finalizer key with conflicting binding
  under both graph/session adapters: all writes and revisions roll back.
- Focused post-fix batch: **88 passed**, 5.71 seconds; Ruff and whitespace checks clean.
- Combined post-fix regression: **870 passed**, 69.52 seconds; two pre-existing Torch
  deprecation warnings. This combines the previously separate scoped suites, including
  the repaired architecture inventory, historical ALTER-order and finalizer-conflict cases.
  Ruff across all changed runtime modules and the selected tests, plus `git diff --check`,
  also passed. No full-project suite or live end-to-end claim is made.
- Grok round 2: **CHECKPOINT CORRECTIONS ACCEPT**, both P2s closed, no new substantive
  findings. Verbatim verdict, precise limits and source/test manifest are in
  `docs/reviews/annex-publication-stage1-grok-2.md` and adjacent JSON.

The two P2s share incidental-representation equality (physical SQLite column order and Python
tuple-vs-JSON-array shape) rather than semantic data comparison. Normalize at the boundary;
no additional broad architectural redesign is indicated by those findings. Grok did not find
a new P1 or repeated review-driven architecture failure in the implemented foundation.

The larger DEC-157 integration gap remains explicit: low-level store/IO primitives accept
caller-provided scope/proof/CAS capabilities. The unfinished coordinator must own and bind the
actual qualified capabilities, not expose no-op callbacks or synthetic receipts as publication.
Scope participant/authority views are now frozen; exception adaptation still needs audit.
None of these integration gates is waived by checkpoint acceptance or passing tests.

## Follow-on implementation after the accepted checkpoint

This work was subsequently reviewed at the operator's request: **round 3/3 INCREMENT
ACCEPT**, with no P1/P2 or other actionable findings. Full Stage 1 remains incomplete.
No implementation commit, push, PR, live migration, conversion, Fill restart, or live
archive mutation was performed for this increment or its review.

- Added the committed-tree observer, strict saved-baseline decoder, IO authority guard,
  frozen participant view and real byte/tree/storage composition described above.
- First native Git/tree pass: **30 passed**, including actual pinned annex 8 locked and
  unlocked commitments. Later negatives cover large non-pointer blobs, replacement refs,
  a ref moving during the final index read, and malformed resumed baseline records.
- First combined guard/tree run: **57 passed, 1 failed** because the new test fixture
  referenced a nonexistent `planner_state.plan_name` column. Replaced that fixture write
  with the existing graph adapter's revision-bumping no-op; no production check relaxed.
- Subsequent tree/guard/real-evidence composition pass: **61 passed** in 9.10 seconds.
- Expanded focused pass after saved-baseline cases: **114 passed** in 11.71 seconds.
  Ruff for all files changed in this increment and `git diff --check` passed.
- Latest combined regression: **916 passed**, two existing Torch deprecation warnings,
  **83.48 seconds**. Same isolated test-only singleton address strategy as the previous
  870-test run, adding 40 native-tree/parser cases, three IO-authority cases and three
  real byte/tree/storage composition cases. This remains a scoped suite, not full-project
  or live end-to-end acceptance. All test subprocesses from this increment have finished.
- Grok round 3 completed read/search only. After two context compactions, the parent
  interrupted only the identified headless client (exit 130), then resumed the same
  session for a no-tools final verdict (exit 0). All reviewed source/test/context hashes
  matched the pre-review manifest before updating this tracker; no implementation changed.
- Nonblocking optional tests: session-token/identity loss specifically on `require_io`,
  and the tree proof record through store canonical serialization. Grok recommended no
  additional redesign; the still-unfinished owning coordinator/native profile is the
  existing architectural integration work, not a newly identified defect.

Next implementation work: qualified bounded native reader/command profile and the owning
coordinator must select the actual scope, sealed file intent/baseline and typed proof
factories. Do not wire a caller-supplied reader/profile digest into production as if it were
qualification. The staged map publisher, ordinary Fill/replica and registration integration,
maintenance recovery and guarded catalog-floor admission still remain. No new architecture
policy or review counter reset was introduced by the observer/IO-guard work.

## Resume here

Operator cadence 2026-09-15: land a section, tests, local Grok CLI up to 3x, PR on
accept, then `@greptileai review` up to 3x. This is a **per-section** counter, not a
fourth Stage 1 increment review. No live migration, conversion, or Fill restart.

Section landing (conversion-disabled ArchivePublisher increment): tests **1036 passed,
2 skipped**; ruff/`git diff --check` clean; local Grok section round **1/3 ACCEPT**
(`docs/reviews/annex-publication-stage1-section-grok-1.md`). Full Stage 1 remains
incomplete. Continue remaining Stage 1 work on the next section after this PR.

Use `annex-publication-writer-map.md` for remaining entry points.

Completed after the last Codex 5.6 SOL interruption (still uncommitted, conversion disabled):

- Reader floor now includes catalog 9 with `validate_publication_schema`. Serial repair
  is monotonic (never stamps 8 over 9) and refuses a v9 floor without the publication
  contract. Reconcile/repair/recovery recheck `require_publication_clear`.
- Fill resume no longer re-downloads a CATALOG_PUBLISHED file whose staging duplicate
  is already released. `publication_staging.resume_file` returns None when acquisition
  is still required and never fabricates completion. Unexpected v9 acquisition errors
  raise `PUBLICATION_ACQUISITION_FAILED` instead of probing `_dest_writable`.
- Focused verification: `tests/test_fetch_publication.py` 19 cases (add the unexpected
  error case when running), plus 123 related publisher/install/registration/catalog
  tests passing. Grok local review counter remains 3/3 used; do not start a fourth.

Stage 1 remaining coordinator items (DEC-162): frozen batch children cannot be
dropped to close; injected stop at each durable phase resumes without clearing
the obligation; leftover PREPARED registration after `CATALOG_PUBLISHED` resumes
and closes. Conversion remains disabled. Live Fill/catalog migration remain off.

Stage 2 inspect is read-only. Disposable apply freezes a plan and, with
`--archive label=path`, annex-converts convertible files and CASes `annex_key`
against the frozen before-state (DEC-168, DEC-169). Source is frozen
`stored_relpath` only; commits are path-limited; retirement is required and
resume still closes leftover source. Live catalog path remains forbidden.
Physical conversion of the production library is not authorized.

Next remaining work:

1. Guarded map retirement is implemented on the current post-PR-86 branch: exact
   source retirement is two-action journalled and resumable; the map candidate
   removes the old path only with its receipt; every registered clone receives a
   layout obligation before map publication; selected clones close with physical
   inventory/generation closure while offline clones remain pending and blocked
   from ModelArk tree changes. Local Grok round 1/3 returned one P1: per-file
   retirement snapshots were incorrectly treated as the final source HEAD for a
   same-drive multi-file batch. The implementation now composes exact Git
   transitions; its focused cases and the 118-case publication regression pass.
   Local Grok round 2/3 confirmed that correction but returned one new P1: the
   new retirement action had mutated the already-qualified v9 action CHECK in
   place. DEC-171 freezes exact v9, introduces explicit additive v10, keeps both
   reader contracts, and requires v10 for retirement. Focused migration/reader
   tests pass. Local Grok round 3/3 **ACCEPTED** with no P1/P2 after verifying the
   exact released-v9 match, atomic v10 rebuild, v10-only maintenance gate, composed
   Git transitions and clone-obligation enforcement. PR publication remains pending.
   Final post-fix scoped publication regression: **120 passed** in 19m54s; exact
   migration/reader batch: **80 passed**; action/store/retirement batch: **44
   passed**; full Slice operator file: **50 passed** with its local lease socket;
   Ruff and `git diff --check` are clean.
   PR 88 Codex cloud round 1/3 then returned three P1s sharing a scope-promotion
   cause: nested empty-parent normalization, shared-map retirement deduplication,
   and partial work closing a clone-wide obligation. DEC-172 implements the
   architectural correction. The three direct regressions pass; the full scoped
   publication rerun passes **123 tests** in 23m30s; the focused retirement suite
   passes **8 tests** in 6m33s. Codex cloud round-2 review is pending.
2. Attended returning-clone inspect/apply/closure remains: capture the returned
   clone's actual old refs/bytes, convert claimed or unclaimed materialized paths
   without inventing catalog rows, then close only that clone's pending obligation.
3. Live Fill restart / catalog migration apply, only when explicitly authorized.

Current branch is `codex/returning-clone-publication` in
`claudedocs/operator-scratch/worktrees/codec-stage-d`, based on merged PR 86. Unrelated
bridge/DGXSpark files remain untracked.
