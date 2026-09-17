# Annex payload migration — local Codex CLI review 1

Date: 2026-09-13
Reviewer: installed codex-cli 0.153.4, gpt-6-astra, high reasoning, read-only sandbox.
Reviewed plan: docs/plans/annex-payload-migration.md
Reviewed SHA-256: dc992ccac418b41fb9f26c3ef7dd482c3765150d37edda4f9c536534e92cbe02
Review scope: one static plan/source review; no code or live state changes.
The plan's status headers were updated after review; finding line references still identify the reviewed body.
Verdict: NOT ACCEPT; three P1 findings. No follow-up review requested yet.

## Reviewer output (verbatim)

**NOT ACCEPT.** The plan has sound preservation goals, but its recovery and propagation rules need amendments before it is an implementable migration contract.

This was one static review pass of the plan and relevant local source. No files were changed and no workloads, migrations or external reviews were run.

1. **P1 — A private journal does not prevent ordinary recovery from prematurely clearing the drive.**  
   **Plan: lines 149–160, 194–198, 212–216.**

   The proposed adapter preserves the terminal session’s dirty owner and records maintenance ownership privately. However, `drive_bootstrap._recover_owned_generation()` currently accepts that terminal owner and publishes an anchor after inventory. The sessionless recovery branch can similarly close a failed maintenance generation. Neither checks migration state, old-path retirement or map propagation. Moreover, `_require_complete_inventory()` blocks missing claims but permits unexplained extras.

   Consequently, after a migration crash, ordinary reconciliation could publish clean evidence while an old control file remains active or required map propagation is incomplete.

   **Amendment:** Require a durable maintenance obligation, discoverable by every clean-publication and execution-admission path. Establish it before physical mutation; bind it to the captured generation and operation; clear it only with completed closure evidence and approval invalidation. Ordinary reconciliation must refuse or explicitly delegate to migration recovery. Test both terminal-session-owned and sessionless generations.

   This is a missing system invariant, beyond the deliberately deferred implementation of the recovery adapter.

2. **P1 — Map propagation and offline convergence need an explicit protocol, not just completion requirements.**  
   **Plan: lines 133–137, 181–191, 205–216.**

   Existing propagation is unsuitable for direct reuse: `fetch.run()` reports failed sync as a warning, and `run_replica_tasks()` ignores the map sync return code. Those paths do not establish a separate map exclusion mechanism. They exchange Git branches, whereas the plan promises propagation of exact intended mappings.

   Retiring a logical Git path also creates a deletion that can later reach other clones. An offline clone may still depend on that ordinary Git file and have no corresponding annex object. Its old `archived` mapping remains authoritative until its own conversion. Recording “pending compatibility work” alone does not define how to prevent an incoming tree update from removing that usable source.

   **Amendment:** Specify the map lock and lock order, captured map refs/index state, exact allowed tree delta, annex metadata refs, conflict policy and durable propagation receipt. Define how every returning clone is intercepted **before any tree-changing sync**, preserves its old sources, and completes or refuses local conversion. Include unclaimed materialized Git copies, not only `archived` rows. Require explicit outcomes for partial propagation and retries after either repository advanced.

3. **P1 — The catalog cutover must explicitly be one transaction across copy facts and revision authority.**  
   **Plan: lines 174–196, 212–213.**

   Step 5 describes a transaction updating `archived`; step 6 separately reconciles `replicas`. Resume then recognizes success from “the exact intended new row.” That leaves ambiguous whether an updated archive row with stale replica evidence counts as database publication.

   This matters to actual readers: `slice.catalog.read_catalog()` joins `archived` with `replicas.present`, while other planning paths consume archived evidence. `proposal.graph_write()` establishes the existing rule that graph changes and their planner revision bump commit together.

   **Amendment:** Define a single per-file transaction that CAS-checks the captured archive row, replica row or absence, and relevant source evidence; updates both copy representations; and bumps the planner revision. Journal the complete intended post-state. Resume must compare that complete state, not just `archived`. Specify approval supersession separately from revision advancement, including what remains blocked after an interrupted batch.

The following are legitimate **Stage 0 qualification gates**, rather than reasons to reject the plan merely because experiments have not yet run:

- **Inert representation and ownership — lines 75–100.** Hashing original filenames and restoring through `stored_relpath` fits the existing logical/stored separation. But the namespace remains beneath potentially active upstream ancestor attributes until retirement. Qualification must prove isolation throughout conversion, including the duplicate-representation interval. Define portable ownership evidence that another clone can verify; matching bytes alone must remain insufficient. Different content for the same logical filename across clones needs an explicit conflict outcome.
- **Replica delivery — lines 104–106, 228, 253.** `run_replica_tasks()` copies by key and copies source mapping fields into the target catalog without directly creating the target worktree path. Slice opens the recorded path; authoritative-drive restore refuses retrieval when that path is absent. Qualification must prove the mapped path becomes readable after propagation, or identify the necessary consumer/publication changes.
- **Verification strength — lines 203–204, 247.** Existing bootstrap inventory uses annex location logs and raw-file existence, not full physical content verification. Define changed-file byte hashing, unchanged-copy local-presence checks and treatment of extras separately. Do not describe reuse of that inventory as physical verification.

The common architectural cause is that representation changes span per-drive catalog mappings, shared Git trees and generation authority, while the proposed journal is private to the migration process. Safety requires those other subsystems to recognize its unfinished obligations.

**Before implementation, decide the durable maintenance interlock, the map/offline convergence protocol, and the atomic catalog cutover boundary. Stage 0 can then qualify the representation and consumer behavior against those explicit contracts.**
