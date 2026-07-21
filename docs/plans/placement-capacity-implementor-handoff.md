# Placement/capacity implementor handoff

## Role and mission

You are the **implementor**, not the architecture owner and not the final reviewer. Implement the
accepted RFC-002/DEC-049 program in bounded, tests-first PRs. The operator and a separate reviewing agent
own scope changes, checkpoint approval, and merge authorization.

Your first assignment is **PR-01 only: the portal mutation guard**. Do not begin #35 in the same session
or PR. At every mandatory stop below, report evidence and wait for the operator and reviewer to answer.
Silence is not approval.

## Starting state and non-negotiable safety

- Repository: `Auspex-Aerie/modelark`
- Integration branch: `fix/placement-capacity-hardening`
- Handoff baseline when written: `24e2cf7` plus this handoff commit; use the current remote integration
  tip if it has advanced through an explicitly approved phase PR.
- The operator has a local, uncommitted `.gitignore` change. It is not yours: never stage, restore,
  rewrite, stash, or commit it.
- A real ModelArk Fill may be running under the user systemd service. Do not stop/restart it, signal its
  processes, touch its live catalog/state directories, mount/unmount its drives, run mutating probes, or
  test against real archive paths. Use `tmp_path`, fakes, synthetic facts, and deliberate copied-catalog
  fixtures only.
- Do not change issue titles or bodies. Append comments only. Once a PR exists, append its URL to the
  relevant issue in a new comment.
- Do not broaden scope into provider revision pinning, cross-host/distributed fencing, event sourcing,
  mutable work queues, multi-plan concurrency, transport redesign, or unrelated cleanup.

## Read this before making any edit

Read these sources **in order and in full**:

1. This handoff.
2. `docs/decision_log.md`: DEC-045, DEC-046, DEC-047, and DEC-049; also INC-018 through INC-021.
3. `docs/rfcs/002-first-class-placement-approval.md`, including the SVG/PNG operation graph,
   pseudocode, failure taxonomy, fault matrix, migration, acceptance, and stop conditions.
4. `docs/plans/placement-capacity-hardening.md` in full.
5. The append-only binding comments on issues
   [#35](https://github.com/Auspex-Aerie/modelark/issues/35#issuecomment-5037967073),
   [#36](https://github.com/Auspex-Aerie/modelark/issues/36#issuecomment-5037967195),
   [#37](https://github.com/Auspex-Aerie/modelark/issues/37#issuecomment-5037967307),
   [#38](https://github.com/Auspex-Aerie/modelark/issues/38#issuecomment-5037967462), and
   [#39](https://github.com/Auspex-Aerie/modelark/issues/39#issuecomment-5037967599). For PR-01, #39 is
   the active issue contract.
6. `.github/workflows/ci.yml`, `pyproject.toml`, and the current tests relevant to the slice.
7. For PR-01 specifically: `modelark/web/selection_api.py`, `modelark/web/fill_worker.py`,
   `modelark/web/fill_api.py`, their server wiring, and the focused unit/E2E tests. Trace every portal
   finalize/deselect/bulk-remove/clear entry point and the worker start/stop lifecycle before proposing a
   guard location.

Authority order is: current operator instruction → DEC-049/RFC-002 → latest binding issue comment →
hardening plan → this PR map → current code. Pseudocode names are illustrative; its authority,
transaction ordering, failure semantics, and invariants are normative. Use concise domain names rather
than sentence-length functions.

## Frozen integration-branch workflow

`fix/placement-capacity-hardening` is the PR base and integration branch for the entire series.

1. Begin each slice from its current reviewed tip and create a dedicated branch such as
   `agent/pc-01-portal-guard`.
2. Open a **draft PR targeting `fix/placement-capacity-hardening`**, never `main`.
3. Use merge commits when the operator eventually authorizes merge; do not squash or rebase the reviewed
   commit sequence. Retain the phase branch.
4. Do not merge/rebase/cherry-pick `main` into the integration branch or a phase branch. If a mainline
   change appears necessary, stop and ask. The operator has deliberately chosen review continuity over
   continuous synchronization.
5. Do not merge your own PR. Do not begin the next PR merely because CI/Greptile is green. Wait for
   explicit operator/reviewer authorization, then re-read the next issue contract from the updated
   integration tip.
6. Only the final evidence-backed integration PR targets `main`.

Intermediate integration commits are not production-ready releases. Do not run this branch against the
live catalog until the attended cutover gate is explicitly opened.

## Tests-first and mandatory stop gates

Every PR repeats this protocol.

### Gate 0 — orientation; stop before edits

After reading, report:

- current branch/base SHA and worktree state;
- the exact slice and explicit non-goals;
- the current code paths and tests you expect to touch;
- the safety invariants/failure behavior that cannot regress;
- the proposed test cases and commit breakdown;
- any RFC/code mismatch or ambiguity.

Then **stop and wait** for the operator and reviewer. Do not create the phase branch or edit code until
they approve this orientation.

### Gate 1 — test contract; stop before production implementation

After Gate 0 approval:

1. Create the phase branch.
2. Add characterization tests that pass on the old behavior and change-contract tests that fail for the
   intended missing behavior. A failing test must fail for the reviewed reason, not because of a broken
   fixture.
3. Commit the tests separately, push, open a draft PR to the integration branch, and append its link to
   the issue as a new comment.
4. Show the focused test output, the expected red tests, and the PR diff/commit.
5. Run the Greptile loop below.

Then **stop and wait**. Production implementation begins only after the human review confirms that the
tests express the right contract. Never weaken a reviewed test merely to make implementation easier.

### Gate 2 — implementation complete; stop before merge

Implement only the approved slice in one or more clearly named commits. Preserve characterization tests.
Run focused tests continuously, then the proportional full checks below. Push, run the Greptile loop
again, and report:

- commits and changed files;
- focused/full test results and CI links;
- migration, failure-injection, copied-catalog, or performance evidence required by the slice;
- every Greptile finding and its disposition;
- remaining risks, compatibility façades, and follow-ups;
- confirmation that no live service/catalog/drive and no unrelated file was touched.

Then **stop and wait**. Green CI and zero Greptile findings do not authorize merge.

### Gate 3 — post-approval handback; stop before the next slice

Only after explicit merge authorization, merge using the reviewed method or let the operator merge.
Verify the integration tip and issue PR-link comment. Give a short handback and **stop**. The next slice
starts in a fresh session or only after explicit instruction.

## Greptile loop

At each draft/test and implementation checkpoint:

1. Ensure the PR is pushed and Greptile review has been requested/triggered.
2. Poll in **five-minute increments, at most four times**.
3. Read the entire review, not just priority labels. Classify findings by correctness, safety, scope, and
   severity. Treat a labelled P2 that is genuinely a nit as a nit, but fix it when the change is safe and
   in scope.
4. Fix every actionable in-scope finding. If a suggestion contradicts DEC-049/RFC-002, weakens a safety
   invariant, or materially expands scope, do not apply it silently: explain the conflict and stop for
   human judgment.
5. Push fixes, reply with evidence where useful, and trigger/poll Greptile again. Never hide, delete, or
   rewrite review history.
6. Include unresolved or rejected findings verbatim enough for the human reviewers to decide.

## PR series and boundaries

These are review boundaries, not permission to begin the next row automatically.

| PR | Issue | Scope | Required checkpoint evidence |
|---|---|---|---|
| 01 | #39 | Portal-only mutation guard. One shared guarded mutation primitive; finalize/deselect/bulk-remove/clear vs. worker start/starting/running/stopping. No durable session yet. | Race/entry-point tests, portal E2E, documented CLI/other-writer residual. |
| 02 | #35-A | Catalog v3 columns/tables/triggers, backup-first migration, typed capacity evidence and pure precedence in shadow/advisory mode. No authority cutover. | Migration rollback/idempotence, no fabricated evidence, golden evidence matrix. |
| 03 | #35-B | Controller/drive fences, dirty-generation start, clean-anchor publish, generation-scoped reconciliation, registration/recovery, inherited child FDs. Protected transport changes only at reviewed seams. | DEC-046/047 characterization, crash/fault matrix, parent-death child-fence proof, no full-drive scan per file. |
| 04 | #35-C | Switch every admission consumer to the shared authority; remove legacy `free_bytes` and `capacity-SUM(stored)` authority. | Planner/API/UI/per-file equivalence, copied-catalog shadow evidence, #35 acceptance complete. |
| 05 | #36 | Logical #36a pure requirements/candidates: all partial and fresh alternatives, exact reuse/provenance/costs, no pin/delete. | Synthetic matrix and shuffled-order determinism; no I/O in pure path. |
| 06 | #38 | Pure Gate B + deterministic `tiered_v2`, mixed-evidence ladder, bounded feasibility/improvement and canonical fallback. | Adversarial/property/10k-candidate tests; outcome and capacity-mode golden vectors. |
| 07 | #37 | Lifecycle/eligibility schema and planner/bootstrap gating only; no lifecycle operations. | Safe-default migration and orthogonal state/permission matrix. |
| 08 | #39-A | Five planning/control tables, canonical serializer/hash, planner revision, preview/approval CAS, `adopt_current`. Keep execution behind compatibility façade. | DDL/migration, hash golden vectors, CAS races, exact-assignment validation, no optimizer in approval. |
| 09 | #39-B | Pure monotonic projection, global sessions/tokens, frozen config, fixed-map executor, worker/child recovery, typed divergence UX/API. | Projection properties, session/recovery fault matrix, protected-transport suite, pinned p95 benchmark. |
| 10 | #39-C | Portal/CLI integration, compatibility façade removal where proven, migration/rollback tooling and copied-catalog shadow replay. No live cutover. | Installed-wheel/E2E, call-site inventory, migration rehearsal and rollback evidence. |
| 11 | #37 | Exclude/include operation. | Non-destructive transition and bootstrap tests. |
| 12 | #37 | Lost/re-home/found-reinstate operation. | Source derivation, offline≠lost, retired refusal, idempotence. |
| 13 | #37 | Retire operation. | Required/unique-byte and active-dependency proofs; tombstone behavior. |
| 14 | #37 | Drop-copy/unarchive operation. | Dry-run/confirmation, annex proof, exact scope, annex-success/DB-failure recovery. |
| 15 | RFC-002 | Final acceptance/evidence documentation and integration readiness. No new product scope. | Full CI/wheel/E2E, copied catalog, fault matrix, performance, attended cutover/rollback plan. |

If a row proves too large, stop with a proposed **smaller** boundary that preserves transactional and
authority invariants. Do not combine rows or move work earlier merely to reduce PR count. A partial PR
must keep tests green, retain compatibility façades, and avoid exposing half-authoritative behavior.

## Validation floor

Use the smallest focused test during development, then before Gate 2 run the CI-equivalent checks
applicable to the slice:

- every non-E2E `tests/test_*.py` file as CI runs it;
- `ruff check modelark scripts tests`;
- `python tests/test_e2e_portal.py` for portal/API/session behavior;
- wheel build and installed-wheel smoke/migration checks for schema, packaged resources, or runtime-path
  changes;
- the issue-specific migration/fault/property/performance evidence.

Never point a test at the operator's live SQLite/WAL, state directory, systemd service, annex map, or
mounted archive. Destructive/failure tests use disposable temp trees and copied fixtures.

## Immediate PR-01 boundary

PR-01 changes only the process-local portal guard. The accepted behavior is:

- one shared server-side primitive guards finalize and every removal path;
- it shares synchronization with worker start so mutation and start cannot both win;
- it refuses during starting/running/stopping based on live controller ownership, not a terminal status
  string;
- read-only operations and safe additions remain as currently specified; do not broaden the guard;
- external CLI and other graph writers remain a documented residual until #39;
- refusal is typed and rendered clearly without mutating selection;
- no durable session/schema, solver, placement, capacity, transport, or live-runtime change belongs here.

At Gate 0, the implementor must enumerate the actual selection endpoints and demonstrate where the one
primitive can cover them without duplicating policy. That report is the next expected work product.
