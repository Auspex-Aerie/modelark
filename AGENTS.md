# ModelArk (modelark)

An ark for open model weights: catalog model metadata broadly, archive supported artifacts from a
curated set across an offline git-annex drive library, and record distinct remote-header, ingestion,
copy, and physical-verification evidence. Do not collapse those evidence levels into a claim that
every catalog entry is loadable or every offline copy is currently verified. See `README.md` for the
current product contract and `catalog_discussions.md` for non-normative catalog research.

Package: `modelark.core` (shared catalog/db) + `modelark` (discovery, archive, portal, restore).
Tooling: `.venv` for runtime, `.venv-dev` for tests/builds, `hf` CLI for Hub auth, and
`git-annex` for bytes. DuckDB is optional and used only for legacy migration.

## Decision log

Decisions, deferrals, and hypotheses for this project are recorded in
`docs/decision_log.md` — an append-only,
[ADRLight](https://github.com/Indubitable-Industries/ADRLight)-style ledger (this
repo is `Auspex-Aerie/modelark`). Record architecture/policy decisions there as
you make them, following the format at the top of that file; append only, never
rewrite past entries (status updates excepted).

## Review automation

Trigger Greptile reviews with the exact canonical mention `@greptileai review`.
GitHub API identities such as `greptile-apps[bot]` identify reactions and review
authors; they are not substitutes for the trigger mention, even if a push causes
an automatic review at the same time.

After every push to an open PR, request both `@greptileai review` and
`@codex review` on the resulting head, covering every newly pushed commit.
Documentation-only changes, merge commits and base-branch reconciliation are
not exemptions. Verify both reviewers' results against the current head;
earlier-head approval and CI success do not substitute for current-head review.
Apply at most three review/fix rounds per scoped stage, not three fresh rounds
per push. Batch known changes before requesting a round. After round three,
stop and summarize unresolved findings, review state, and any common
architectural cause before further changes or another review cycle. Do not
push an unreviewed final fix or silently reset the counter. A user-authorized
exception applies only to its explicitly named scope; PR #73's one-time
no-review-wait merge authorization does not carry forward to other PRs.

## Operator approval continuity (2026-09-10)

When the operator replies "ok", "begin", "go", "continue", or equivalent to an
activity just described, treat that as authorization to perform that activity
and its normal, scoped supporting steps across subsequent turns. Do not ask for
the same approval again merely because a new turn or implementation step begins.
Ask again only for a materially different target, risk, destructive action, or
scope not covered by the described activity, or a genuinely required user choice.
Missing credentials or sudo access are execution blockers, not missing consent:
provide the exact necessary command instead of asking for redundant authorization.
This preference does not bypass tool permissions or safety boundaries.

## Explicit merge handoff (2026-09-10)

When required current-head reviews and CI establish that a PR is ready to
merge, promptly lead with a **bold, explicit request for the operator to merge
it**, including the PR link. Do not bury the merge request in a status summary.
If a ready PR is still open when encountered again, stop advancing dependent
work and **tell the operator clearly and in bold that it is awaiting their
merge**. Do not silently let merge-ready PRs linger. Verify current readiness
before repeating that claim; report actual blockers for PRs that are not ready.
The operator retains merge authority unless explicitly delegated for that PR.
