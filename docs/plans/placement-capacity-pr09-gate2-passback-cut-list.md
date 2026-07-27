# PR-09 Gate-2 passback — closed cut list

**Disposition:** WITHHELD, with a **closed** remediation scope

- **Date:** 2026-07-26
- **From:** human Gate-2 reviewer
- **To:** implementer

This document supersedes `placement-capacity-pr09-gate2-passback-38-cadence.md`.
Commit it verbatim as the first commit of this cycle; do not edit it in place.

## Authority

- Repository: `Auspex-Aerie/modelark`
- PR: `#54` — open, draft, unmerged
- Branch: `fix/placement-capacity-pr09-execution-projection`
- Pinned tip: `1939786b0742352df96563cea0d438d9eb6f5e7b`
  (local, `origin`, and PR head agree)
- Base: `bc33a0664d3e65e20c6843b0a9d5b1204d15502a`
- Code tip under review: `fadc937a549482d445aa17b17f72bfd3f0174458`
- Gate 3: unauthorized. PR-10: planned but unauthorized.

## The scope of this cycle is closed

PR-09 stands at 23 commits past Gate-1 acceptance, 48 files, and 8,148 insertions,
with Gate 2 never accepted. The cause is scope absorption: each review round
discovered something and the PR absorbed it, including defects that predate the PR.

The five items below are the **entire** remaining scope. The governing rule for the
rest of this PR:

> Only a regression introduced by this PR blocks its gate. Any other defect
> discovered from here gets a ledger entry and moves to PR-10.

If you find something outside this list, report it in the handback and **do not fix
it here**. That is not a licence to ignore it; it is a licence to schedule it.

## Confirmed closed — do not revisit

- Findings 33, 34, 36 — accepted previously.
- **F35-a** — the `ecfg:` fallback is deleted and the misleading comment corrected.
  Verified: `start_session` returns
  `Refusal(code='APPROVED_INPUT_CHANGED', evidence={'reason': 'execution_config_unbound'})`.
- **F35-b** — `mark_proposal_pre_pr09_unbound` clears only `execution_config_hash`,
  and `require_bound_execution_config` was correctly rewritten to read
  `execution_config_hash` with a matching docstring.
- **F37-b** — multi-file source authority regression added.
- **F38 fixture identity** — the §5 fact list reconciles: 390 selected, 494 tasks,
  4,120 models, 101,883 files, 5,567 archived rows, 65 `baseline_satisfied` / 429
  `executable`, 9,520 proposal-file rows, `integrity_check` ok, 0 foreign-key
  violations, `user_version` 6.

**F38-a is accepted with one condition** (item 4 below). The invariant is
`total_refreshes = batch_boundary_refreshes + typed_event_refreshes`, with
`batch_boundary_refreshes ≤ transport_batches`. Your B−1 explanation is consistent
with the drain's loop structure. Recorded honestly: the reviewer did **not**
instrument it.

## Reviewer errors now in history — correct them

The previous passback was left untracked in the working tree and was committed at
`fadc937` as an in-progress draft. Two reviewer errors landed with it:

1. `schema.sql:145` is wrong for the "NULL for tiny git blobs" comment. The correct
   citation is **`modelark/core/schema.sql:46`** (`files.sha256`). Line 145 is
   `archived.orig_sha256`, a different field with a different comment.
2. The document's meta lines lack hard breaks and render as one paragraph — the same
   defect that passback raised as F38-c.

Correcting these is item 5. This is the reviewer's mistake, not yours.

## Locked decisions to implement and commit

Three ledger entries were appended by the operator/reviewer this cycle and are
**already in your working tree** as an uncommitted modification to
`docs/decision_log.md`:

- **DEC-051** — Fill content satisfaction requires a recorded digest; presence alone
  never proves durability.
- **DEC-052** — Acceptance-evidence identity is the canonical content hashes, not the
  container's byte hash. **Implementation is deferred to PR-10.**
- **DEF-032** — Defer reclaiming committed acceptance-evidence blobs from git history.

Two further tree changes are staged/modified and must be committed in this cycle:

```text
 M .gitignore                                          docs/plans/evidence/*.sqlite{,-wal,-shm}
 M docs/decision_log.md                                DEC-051, DEC-052, DEF-032
 D docs/plans/evidence/b12_390_approved_fixture.sqlite  untracked via git rm --cached
```

The fixture bytes remain on disk at hash
`bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f`. **Do not delete
or move them, and do not re-add the file.** A re-copy of a live SQLite catalog is not
byte-reproducible, so deleting the bytes would make the accepted F38 identity
unverifiable before DEC-052 lands.

## The five items

### 1. Implement DEC-051 — the regression this PR introduced

At `768741f` the source side was strict: "Approved file has no hash but archive also
null — not content-proven." At `fadc937` it was unified in the **permissive**
direction, so PR-09 currently ships a newly weakened integrity check. That is the one
thing that must not merge.

Implement the DEC-051 rule in `fill._archive_content_satisfies`, applied identically
by `_source_files_content_ready` and by target evaluation in
`_projection_work_units`:

- approved hash present → the `archived` row must carry the same non-null hash;
- approved hash absent, archived hash present → satisfied;
- **both absent → fails closed**, on both sides.

Remove the `DEC-022` citation from the docstring and cite **DEC-051**. DEC-022
governs compression codec selection by RAM budget and establishes nothing about
content-hash satisfaction; `schema.sql:46` states only that `files.sha256` may be
null, which is a data-shape fact rather than a durability policy.

Acceptance: regressions covering all four hash combinations
(present/equal, present/unequal, present/null-archive, absent/present,
absent/absent) on **both** the source-readiness and target-satisfaction paths. Each
regression must fail if the rule is reverted.

### 2. Independently verify the DEC-051 impact counts

The reviewer has recomputed these per target drive and corrected DEC-051's `impact`
in the ledger. The earlier estimate of roughly 1,541 affected target rows was wrong by
about six times, because the join ignored `drive_label` and therefore counted archived
rows on any drive. Corrected figures, over the approved proposal's 9,520
proposal-file rows:

| target drive | no archive row | archive hashed | archive NULL → fails closed | hashed |
|---|---|---|---|---|
| `drive-00` | 1,112 | 69 | **265** | 1,190 |
| `drive-01` | 1,926 | 213 | 0 | 1,998 |
| `drive-02` | 1,141 | 0 | 0 | 1,034 |
| `drive-03` | 141 | 0 | 0 | 136 |
| `drive-06` | 12 | 0 | 0 | 283 |
| **all** | 4,332 | 282 | **265** | 4,641 |

Replica source side, joined on `source_drive`: 497 of 1,887 replica-task file rows are
unhashed against a null source digest and will hold at `waiting_dependency`.

The join shape is the one the production check uses — `archived` keyed on
`(repo_id, rfilename, drive_label)`, with `drive_label` bound to `target_drive` for
satisfaction and to `source_drive` for replica readiness, and `repo_id` taken from
`proposal_tasks` because `proposal_files` does not carry it. Restrict to the approved
proposal; `baseline_satisfied` rows are skipped by `_projection_work_units` before this
check is reached.

Acceptance: reproduce these numbers independently and report them in the handback. If
yours differ, **yours are probably right** — the reviewer has already been wrong once
here. Say so plainly rather than restating these figures.

### 3. Reconcile the contradictory refresh statements

At the pinned tip the tree asserts the same fact three ways:

- `placement-capacity-pr09-gate2-handback-35-38.md:41` — "6 calls via
  `fill._refresh_projection` (3 batch + 3 typed events)";
- `placement-capacity-pr09-gate2-passback-35-38.md` — source
  `fill._drain_projection`;
- the evidence JSON — the current instrumented counts.

Generator version also disagrees: `gate2-b12-rfc001-copy-v2` in one handback versus
`gate2-b12-rfc001-copy-v3` in the JSON.

Acceptance: every document statement of refresh source, count, breakdown, and
generator version matches the evidence JSON exactly. Superseded handbacks are
corrected or marked superseded, naming the tip that supersedes them.

### 4. State the B−1 batch/refresh relationship in the artifact

Record in the evidence artifact why `batch_boundary_refreshes` is expected to be at
most `transport_batches` — the drain re-enters the loop, finds nothing ready, and
exits through the terminal path before another batch-boundary refresh. State the
required equality explicitly:
`total_refreshes = batch_boundary_refreshes + typed_event_refreshes`.

This exists so the relationship is not reopened later as a counting bug.

### 5. Fix whitespace and the reviewer's citation errors

- `git diff --check` genuinely clean over `bc33a066..<final tip>`. Use list items or
  a table rather than trailing double-space Markdown hard breaks, so the fix does not
  silently reflow rendered text.
- Correct `schema.sql:145` → `schema.sql:46` where the previous passback cites it.
- No document may state the SHA of the commit that contains it. Reference only
  already-pushed SHAs — normally the code tip and its parent. Two amend attempts have
  already failed to converge on this; a commit cannot contain its own hash.

## Known limitation to state in the handback

DEC-051 will cause roughly 1,541 approved file rows in the acceptance catalog to fail
closed — rows where neither the approved nor the archived hash exists. These are
INC-017's defect class. They **park** rather than being silently waved through, which
is fail-safe and visible, but the handback must state it as a known limitation.
Whether `repair-hashes` actually covers them is **PR-10 scope** — do not investigate
it here, and do not assume it.

## Also required

Update the PR title and body now, not at Gate 3. The title still reads "PR-09 (#39-B)
Gate 1: execution projection/session contracts (tests-only)" while the PR carries 48
files and 8,148 insertions of production. Greptile reads that description.

## Greptile loop — at most three rounds

1. Push fast-forward only.
2. Comment `@greptileai review` on PR #54.
3. Triage every comment; fix real defects **inside the five items only**.
4. If a comment conflicts with RFC-002, DEC-049, DEC-051, DEC-052, an accepted
   finding, or this passback, **do not comply** — record the conflict and stop.
5. If fixes were made: one commit, push, re-trigger.
6. Stop at three rounds, or earlier when a round produces no actionable comments.

Greptile is evidence, not authority. A clean Greptile closes nothing and confers no
gate. Suggestions must not widen scope beyond the five items.

## Verification at the final tip

- Focused F35/F37/F38 suites, including `tests/test_gate2_findings_33_38.py`.
- `tests/test_catalog_v6_execution_config_hash.py` and the v5→v6 migration tests.
- `tests/test_projection_performance_contract.py`.
- Full pytest.
- `python tests/test_replan.py`.
- Ruff on touched files.
- `git diff --check` over `bc33a066..<final tip>`.
- Exact-final-tip GitHub CI and Greptile. Do not cite a parent commit's checks.
- Confirm the untracked operator files are untouched.

## Process constraints for this cycle

- **Do not push while a review is pinned.** The head moved from `768741f` to
  `1939786` during the last review, which split evidence across heads. Produce your
  tip, then stop.
- Do not rewrite history and do not force-push.
- Do not mark ready, do not merge, do not begin Gate 3, and do not begin PR-10 work —
  including DEC-052 implementation, the `derivation_mode` disposition, and the
  `repair-hashes` coverage question.
- No production fork/spawn choice.
- Preserve these untracked operator files. Do not stage, commit, delete, or rewrite
  any of them — the previous cycle committed an in-progress reviewer draft that had
  been left in the tree, which is how two reviewer errors entered history:
  - `docs/plans/placement-capacity-reviewer-handoff.md`
  - `docs/plans/placement-capacity-pr09-review-restart-primer.md`
  - `docs/plans/placement-capacity-pr10-charter.md`
  - `iscsi-login.sh`

  This passback is the **only** reviewer document you commit this cycle.

Produce one remediation tip covering exactly these five items plus the three tree
changes, run the bounded Greptile loop, then one concise handback naming the final tip
and its parent. Stop for human Gate-2 re-review.
