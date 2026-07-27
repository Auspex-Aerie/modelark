# PR-09 Gate-2 passback — F35 / F37 fail-open paths, F38 refresh cadence

> **SUPERSEDED** by `docs/plans/placement-capacity-pr09-gate2-passback-cut-list.md` (after pin `1939786`). Closed remediation scope is the cut-list only.

**Disposition:** WITHHELD
**Date:** 2026-07-26
**From:** human Gate-2 reviewer
**To:** implementer

## Authority

- Repository: `Auspex-Aerie/modelark`
- PR: `#54` — open, draft, unmerged
- Branch: `fix/placement-capacity-pr09-execution-projection`
- Reviewed tip: `768741f74e6c3f7533b83d6f30e3b0db47e8d286`
- Reviewed base: `bc33a0664d3e65e20c6843b0a9d5b1204d15502a`
  (`origin/fix/placement-capacity-hardening`, confirmed ancestor of the tip)
- Production range reviewed: `00ba101..3837347`; docs range `3837347..768741f`
- Gate 3: unauthorized. PR-10: unauthorized.

Commit this passback together with the remediation.

## Independently confirmed at this tip — no action required

- Local HEAD, `origin` branch head, and PR #54 head all equal `768741f`.
- Exact-tip checks: `Greptile Review`, `test (3.10)`, `test (3.12)`, `e2e` — all
  `success`.
- Fixture SHA-256 recomputed as
  `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f`, matching
  both identity blocks in the evidence JSON.
- Fixture counts 390 selected / 494 tasks / 4,120 models / 101,883 files match.
- No history rewrite occurred: the remote reflog is fast-forward only, and
  `663b207` was never pushed (GitHub returns 422 for it).

## Accepted and unchanged — do not relitigate

- Finding 33 — session-authorized physical mutation envelope.
- Finding 34 — connection-scoped session-write authority.
- Finding 36 — forced terminalization failure and worker-identity heartbeat CAS.
- Finding 38, in part — copied B12 fixture identity.

## F35 / F37 — re-probed at this tip

Substantially closed. Confirmed working, and not to be reopened or refactored:

- F35: `fetch._compression_from_ctx` (`modelark/fetch.py:205-229`) never calls
  `wishlist.compression()` once any freeze is attached, including an incomplete or
  malformed mapping.
- F35: a genuinely migrated shape — `semantic_input_hash` intact 64-hex,
  `execution_config_hash` NULL, `derivation_mode` a valid RFC-002 value — refuses
  start with `APPROVED_INPUT_CHANGED`. The regression at
  `tests/test_gate2_findings_33_38.py:186` correctly clears only the config hash.
- F35: `_migrate_execution_config_hash_v6` (`modelark/core/db.py:620-684`) is
  backup-first, rebuilds against the v6 DDL, coerces short hashes to NULL, and
  re-asserts the null-or-64 CHECK. The prior-unconstrained-v6 shape is repaired via
  the guard at `db.py:627`.
- F37: multi-file source authority holds. A requirement whose second approved file
  is stale, or absent from the source drive entirely, stays `waiting_dependency` —
  one matching file cannot authorize the requirement.
- B12 §5 fact list independently reconciled against the fixture: 390 selected,
  494 tasks, 4,120 models, 101,883 files, 5,567 archived rows, 65
  `baseline_satisfied` / 429 `executable`, 9,520 proposal-file rows,
  `integrity_check` ok, 0 foreign-key violations, `user_version` 6, and SHA-256
  `bac9bea8…1522f`.

Two fail-open paths remain open, below.

### F37-a — target-side null identity is satisfaction; source-side it is not

`modelark/fill.py:670-677` treats an approved file carrying no `orig_sha256`,
against a target `archived` row also carrying no `orig_sha256`, as durable
satisfaction. The requirement then shrinks out at `fill.py:678-679` and produces no
work unit at all — no copy, no verification, no evidence.

`_source_files_content_ready` (`fill.py:558-560`) refuses the identical shape, with
the comment "Approved file has no hash but archive also null — not content-proven."

Reviewer probe at this tip:

```text
f37c_null_null_target: SHRUNK_OUT_AS_SATISFIED  (no work unit)
f37d_null_null_source: NOT_READY  (ready=False)
```

The same codebase reaches opposite conclusions about identical evidence quality
depending only on which side of the copy it is evaluating. One of the two is wrong
by construction.

This is reachable on ordinary data, not corruption. `modelark/core/schema.sql:46`
documents `files.sha256` as "NULL for tiny git blobs", and
`modelark/proposal.py:840-850` copies it into `proposal_files.orig_sha256`
unchanged. In the operator-approved acceptance catalog:

```text
proposal_files with NULL orig_sha256 : 4879 / 9520  (51.3%)
archived rows  with NULL orig_sha256 : 1122 / 5567  (20.2%)
```

Acceptance:

- State one explicit rule for what constitutes durable content satisfaction when
  an approved file carries no content hash, and apply it identically on the source
  and target sides.
- If the rule is size/LFS-aware — for example, non-LFS blobs below a stated
  threshold satisfy on presence, everything else requires hash equality — state the
  threshold and its authority, and enforce it on both sides.
- Do **not** simply fail closed on null hashes without checking the consequence
  first: that parks a majority of approved file rows and would make normal
  operation impossible. Report the number of affected rows in the acceptance
  catalog under whatever rule you adopt.
- Add regressions for null-vs-null, null-vs-present, and present-vs-null on both
  the source and target sides.
- If the correct rule is not derivable from RFC-002/DEC-049, **stop and surface the
  conflict** rather than choosing one. Do not improvise a waiver.

### F35-a — `ecfg:` in `derivation_mode` still authorizes an unbound start

`modelark/execution_session.py:199-203` falls back to reading an `ecfg:<64 hex>`
binding out of `derivation_mode` when `execution_config_hash` is falsy. The comment
at `execution_session.py:196-197` claims there is "no field-by-field legacy soft
path"; these lines are one.

Reviewer probe at this tip — `execution_config_hash` set to NULL and
`derivation_mode` set to `ecfg:<current frozen hash>`:

```text
f35a_ecfg_bypass: PASSED_CONFIG_GATE  code=None reason=None
```

The session **started**. The control is the implementer's own regression at
`tests/test_gate2_findings_33_38.py:186`, which sets the same NULL
`execution_config_hash` without touching `derivation_mode` and correctly refuses.
One variable differs; the outcomes are opposite.

`schema.sql:308` declares `derivation_mode VARCHAR` with no CHECK, so any string is
writable. Nothing in production writes `ecfg:` — `grep` finds only these reads and
two tests asserting the approve path does not produce it. The path therefore buys
no compatibility and accepts a shape the product never creates, while contradicting
RFC-002's reservation of `derivation_mode` for placement audit evidence
(`optimized`, `state_truncated`, `canonical_fallback`).

Acceptance:

- A null, empty, or short `execution_config_hash` refuses start unconditionally.
  `derivation_mode` must not supply an execution-config binding.
- Either delete the fallback, or — if the operator requires a real migration path
  for catalogs that genuinely carry `ecfg:` — convert it in the v6 migration and
  refuse it at start.
- Constrain `derivation_mode` to its RFC-002 values, or state why it cannot be.
- Add a regression proving `derivation_mode='ecfg:<valid hash>'` with a NULL
  `execution_config_hash` refuses.
- Correct the comment at `execution_session.py:196-197`.

### F35-b — the unbind helper clears both hashes

`execution_config.mark_proposal_pre_pr09_unbound` (`modelark/execution_config.py:60-72`)
clears `semantic_input_hash` **and** `execution_config_hash`. Real v5→v6 migration
clears only the config hash. Reviewer probe:

```text
f35b_helper_clears_both: CLEARS_BOTH  semantic=NULL config=NULL
```

The F35 regression at `tests/test_gate2_findings_33_38.py:186` does not use this
helper, so the F35 proof itself is sound. But the helper is exported from
`execution_session.py:15-16` and used by
`tests/test_execution_config_binding.py:90-91`, so any future test reaching for it
silently stops testing the config gate — the earlier `semantic_input_hash` gate at
`execution_session.py:184-190` fires first.

Acceptance: narrow the helper to clear only `execution_config_hash`, or delete it
and have callers use explicit SQL. Also note `require_bound_execution_config`
(`execution_config.py:78-90`) validates `semantic_input_hash` while its docstring
says it returns the config hash; either fix the conflation or remove the function
if it is dead.

### F37-b — missing regression for multi-file source authority

The behavior is correct but unpinned. The handoff required it explicitly. Add
regressions for a multi-file requirement whose second source file is (i) stale and
(ii) absent, asserting the requirement stays `waiting_dependency`.

## Cross-cutting — self-verification that cannot fail

Three separate checks in this branch are structurally incapable of reporting
failure. Treat this as a class defect, not three coincidences:

1. The F38 refresh measurement recorded 1 call over 1 batch and still reported
   `ok: true` (F38-a).
2. `modelark/core/db.py:662-676` — the v6 short-hash probe wraps its INSERT in
   `except Exception: pass`. If the INSERT fails for any unrelated reason (a NOT
   NULL column, a renamed column), the probe reports success without ever proving
   the CHECK rejects a short hash. Make it assert the specific integrity error.
3. The reported `git diff --check` clean result was false (F38-c).

For every check added in this cycle, demonstrate that it fails when the behavior it
claims to verify is broken.

## Remediation required

### F38-a — refresh evidence does not demonstrate production cadence

`docs/plans/evidence/b12_390_acceptance_wall_clock.json` at this tip records:

```text
projection_refresh_count                          = 1
projection_refresh_instrumentation.calls          = 1
projection_refresh_instrumentation.source         = 'fill._drain_projection'
projection_refresh_instrumentation.breakdown.drain_refresh_calls = 1
projection_refresh_instrumentation.breakdown.transport_batches   = 1
```

The seam moved to the production drain as required, but the measurement collapsed
from 6 recorded refreshes to 1, over a single transport batch, with no typed-event
component present at all. One refresh over one batch on a 390-repository,
429-executable fixture cannot demonstrate cadence — it cannot distinguish
per-batch refresh from per-run refresh, and it provides no evidence for refresh at
typed state-changing events.

Acceptance:

- Run the production drain (`fill._drain_projection`, or the installed
  equivalent) against the approved 390-repository fixture, with transport mocked
  only enough to avoid physical writes and network.
- The acceptance scenario must actually exercise **more than one** completed
  maximal drive batch and **at least one real typed state-changing event**.
- Record a breakdown that separates: initial/resume full projection,
  per-completed-batch refreshes, and typed-event refreshes.
- The reported total must equal the sum of the breakdown, and must reconcile
  against the observed number of completed batches and emitted typed events.
  Report both observed numbers in the artifact.
- Prove no full projection occurs per file or per task.
- Include adversarial proofs: patching `_drain_projection` to raise must make the
  measurement fail rather than record a low count; refresh exceptions must not be
  swallowed and counted as evidence; the count must not derive from the benchmark
  run count.
- If the fixture's current archive state genuinely admits only one batch, treat
  that as an acceptance-scenario defect and say so explicitly. Construct a
  scenario that exercises multi-batch and typed-event cadence. Do not report a
  degenerate single-batch run as cadence evidence.

### F38-b — the same fact is stated three different ways at one tip

- `docs/plans/placement-capacity-pr09-gate2-handback-35-38.md:41` — "**6** calls
  via `fill._refresh_projection` (3 batch + 3 typed events); source
  `fill._refresh_projection`".
- `docs/plans/placement-capacity-pr09-gate2-passback-35-38.md:36,48` — source
  `fill._drain_projection`.
- Evidence JSON — 1 call, source `fill._drain_projection`.

Generator version also disagrees: the handback says `gate2-b12-rfc001-copy-v2`,
the JSON says `gate2-b12-rfc001-copy-v3`.

Acceptance: at the final tip, every document statement of refresh source, count,
breakdown, and generator version matches the evidence JSON exactly. Superseded
handbacks are either corrected or marked superseded, naming the tip that
supersedes them.

### F38-c — a reported verification result is false

`git diff --check bc33a066 768741f` is **not** clean. It reports four
trailing-whitespace hits, all in
`docs/plans/placement-capacity-pr09-gate2-passback-35-38.md` lines 3, 4, 5, and
65, while the passback asserts the check is clean. This is the second consecutive
cycle in which a "clean" self-report did not hold.

Acceptance:

- `git diff --check` is genuinely clean over the reviewed range at the final tip.
  Use list items or a table rather than trailing double-space Markdown hard
  breaks, so the fix does not silently reflow the rendered text.
- Every verification line in the handback must be a command the reviewer can
  re-run verbatim and obtain the same result. Do not report a check you did not
  run at the tip you are reporting.

### F38-d — contract authority and evidence portability

- `contract.source = 'harness'` with `pure_p95_seconds = 0.5` and
  `full_p95_seconds = 2.0`. The harness must not be its own contract authority.
  Cite the RFC-002, DEC-049, or `docs/decision_log.md` location that establishes
  those thresholds, or record them explicitly as provisional harness targets
  pending a decision entry.
- `fixture_descriptor.sqlite_path` is the absolute path
  `/home/phaze/PycharmProjects/modelark/docs/plans/evidence/b12_390_approved_fixture.sqlite`.
  Make it repo-relative.
- The p95 values changed between candidates — approximately 0.327 s / 0.698 s at
  `00ba101` versus 0.3527 s / 0.7322 s in the JSON at this tip. Both remain within
  contract, but the previously accepted timing was silently re-measured. State
  both values in the handback so the reviewer is not re-accepting different
  numbers without notice.
- Record in the evidence artifact, computed from the fixture at measurement time,
  the figures the reviewer must reconcile: archived row count, the
  `baseline_satisfied` and `executable` split, `PRAGMA integrity_check`, and the
  foreign-key check.

### DOC-1 — a document names an unreachable SHA as branch HEAD

`docs/plans/placement-capacity-pr09-gate2-passback-35-38.md:15` names
`663b207cb681deb9d9c76838468ea177304788f6` as "Branch HEAD (passback tip)". That
object exists only in the local object database, is unreachable from every ref,
was never pushed, and is the pre-amend version of `768741f`. A commit cannot
contain its own hash, so amending to "correct" the value can never converge; two
attempts have already failed this way.

Acceptance:

- No document states the SHA of the commit that contains it.
- Documents reference only already-pushed SHAs — normally the code remediation
  tip and its parent.
- Do not amend or force-push. Each round is a new fast-forward commit.
- If a current-head reference is genuinely needed, add it in a later commit that
  names the earlier one, or omit it.

## Greptile loop — at most three rounds

After the remediation commit is pushed:

1. Push fast-forward only.
2. Comment `@greptileai review` on PR #54 to trigger the review.
3. Triage every comment. Fix real defects.
4. If a comment conflicts with RFC-002, DEC-049, an accepted finding (33, 34,
   36), or this passback, **do not comply**. Record the conflict and stop for the
   human reviewer.
5. If fixes were made, produce one commit, push, and re-trigger.
6. Stop at three review rounds total, or earlier as soon as a round produces no
   actionable comments.

Constraints:

- Greptile is evidence, not authority. It ranks below RFC-002/DEC-049 and below
  this passback. A clean Greptile does not close F38 and does not confer Gate 2.
- Greptile suggestions must not cause scope drift: no new features, no
  opportunistic refactors, and no production fork/spawn choice. Contract changes
  are limited to the F35-a, F35-b, and F37-a closures required above.

## Verification at the final tip

- Focused F35/F37/F38 suites, including `tests/test_gate2_findings_33_38.py`.
- `tests/test_catalog_v6_execution_config_hash.py` and the v5→v6 migration tests.
- `tests/test_projection_performance_contract.py`.
- Full pytest.
- `python tests/test_replan.py`.
- Ruff on touched files.
- `git diff --check` over `bc33a066..<final tip>`.
- Exact-final-tip GitHub CI and Greptile. Do not cite a parent commit's checks.
- Confirm untracked operator files are untouched.

## Explicit stop and out-of-scope

- Do not mark the PR ready, do not merge, do not begin Gate 3, do not begin
  PR-10, and do not make a production fork/spawn decision.
- Do not rewrite history and do not force-push.
- Do not touch `docs/plans/evidence/b12_390_approved_fixture.sqlite`. Do not
  remove it, re-add it, or move it into git-annex. It is a 50,556,928-byte plain
  git blob introduced at `00ba101`, and its disposition is an operator decision
  deliberately held outside this cycle.
- Do not retitle the PR. The stale Gate-1 title is Gate-3 scope.
- Preserve the untracked operator files
  `docs/plans/placement-capacity-reviewer-handoff.md`,
  `docs/plans/placement-capacity-pr09-review-restart-primer.md`, and
  `iscsi-login.sh`. Do not stage, commit, delete, or rewrite them.

Produce one remediation tip, run the Greptile loop as bounded above, then one
concise handback naming the final tip and its parent. Stop for human Gate-2
re-review.
