# PR-10 Gate-0 brief — implementer instructions

**Status:** ISSUED. Commit this file verbatim as PR-10's first commit. It lives outside
the repository until then.

- **Date prepared:** 2026-07-26
- **Date issued:** 2026-07-27, after PR-09 merged
- **From:** human Gate-2 reviewer
- **To:** implementer

## Where things stand

PR-09 is **complete and merged**. Gate 2 was accepted at
`aaf932ca1113820b356fcf91fa70993bc2769d05`, findings 33–38 closed, and Gate 3 merged it
into `fix/placement-capacity-hardening` as merge commit
`a2c3707dc129257733fabc015b688e9738d3dc51`, parents `bc33a066` + `aaf932c`, both
independently verified by the reviewer. PR #54 is MERGED and closed. `main` is untouched
at `88004d4`.

- **Branch PR-10 from `a2c3707`**, the current `origin/fix/placement-capacity-hardening`
  tip. Re-verify that is still the tip before branching; if it has moved, **STOP and
  report** rather than branching from something else.
- PR-11 is charted but not authorized. Its work — DEC-053 provenance column and
  DEC-054 automatic repair — is out of scope here.

## The ledger patch is PR-10's first content commit

DEC-053, DEC-054, DEC-055, and the DEC-051 status edit exist **only** as a patch file
outside the repository:

```text
/home/phaze/PycharmProjects/modelark-pr10-ledger.patch
```

Nothing else holds those three decisions. Treat the patch as the authority until it is
committed, and commit it early rather than late.

It was verified to apply cleanly against `aaf932c`, and the merge carried that same
`decision_log.md` through unchanged, so it applies to `a2c3707` as well. Confirm with
`git apply --check` first. **If it fails, STOP and report** rather than retyping — a
hand-retyped ledger is not the same artifact, and this is an append-only record.

## Sequence

```text
1.  PR-09 Gate 3 authorized                                     DONE
2.  PR-09 merged into fix/placement-capacity-hardening           DONE  a2c3707
3.  Branch PR-10 from a2c3707
4.  Commit this brief verbatim
5.  Apply the ledger patch, commit           DEC-053/054/055 + DEC-051 status
6.  Gate 0 — helper inventory, handback, STOP
7.  Gate 1 — contracts/tests only, STOP
8.  Gate 2 — implementation, STOP
```

Each gate ends with an explicit stop and a handback. Never infer authorization for the
next gate.

## Gate 0 — the mandatory helper inventory

**This step is new, and it exists because of a specific failure rather than as
generic diligence.** DEC-051 was authored against the raw `archived.orig_sha256`
column while `archive_hash.expected_sha256` already existed — documented in its own
module docstring as *"one restore-evidence rule shared by restore, verification, and
legacy repair"* — and already derived a digest from a raw copy's annex key. The rule
shipped correct but needlessly strict, parking 265 target rows and 497 replica-source
rows that the product could already prove. Nobody looked for an existing accessor
before writing a new rule.

Before writing or amending **any** rule in PR-10, produce an inventory and record it
in the Gate-0 handback:

- Grep for existing helpers on the same subject — digest resolution, satisfaction,
  identity, provenance, evidence — and **read them**, not just their names.
- For each rule you will touch, name the existing accessor it should use, or state
  explicitly that none exists.
- Check whether a sibling subsystem — restore, verification, repair, reconcile,
  execution_projection — already decides the same question, and whether your rule
  agrees with it. **A divergence between subsystems on identical evidence is a defect
  even when both sides are safe.**
- Prefer routing through the shared accessor over duplicating its logic.

Gate 0 produces the inventory and a scope confirmation. **No production, no tests.**

## PR-10 scope — four items, closed

The scope-freeze rule from PR-09 carries forward:

> Only a regression introduced by this PR blocks its gate. Anything else gets a ledger
> entry and moves to the next PR.

### 1. Implement DEC-055 — highest value, do it first

Route `fill._archive_content_satisfies` through
`archive_hash.expected_sha256(catalog_sha=…, orig_sha256=…, compressed=…, annex_key=…)`
instead of reading `archived.orig_sha256` directly. DEC-055 supersedes DEC-051 and
keeps its rule intact — only digest *resolution* changes.

- Approved hash present → the resolved digest must equal it.
- Approved hash absent → a digest must be resolvable for the stored copy.
- Neither resolvable → fails closed, on **both** source and target sides.
- A raw copy with a `SHA256`/`SHA256E` annex key resolves even when
  `archived.orig_sha256` is null. A **compressed** copy's annex key names the
  compressed bytes and certifies nothing about the original, so compressed copies with
  no recorded digest keep failing closed.

Acceptance: add regressions for the annex-key-derived case on both sides alongside the
four combinations PR-09 already pins, and demonstrate each fails when the rule is
reverted. Report the recomputed per-drive impact — the expectation is that the 265
target rows and 497 replica holds go to approximately zero, since all 1,122 unhashed
rows in the acceptance catalog are `SHA256E` with `compressed=0`. **If the real number
is not approximately zero, say so plainly** rather than restating the expectation.

### 2. Implement DEC-052 — acceptance-evidence identity

Zero production risk: `modelark/execution_benchmark.py` is imported only by
`tests/test_projection_performance_contract.py` and
`tests/test_gate2_findings_33_38.py`.

- Binding identity becomes the canonical content hashes —
  `prepared_canonical_input_hash`, `prepared_projection_hash`, and the recorded row
  counts. These are derived from ordered row values and survive a re-copy.
- `source_sqlite_sha256` becomes provenance: recorded, logged when it changes, never
  a gate.
- Open evidence catalogs **read-only**, or work on a copy where a write-capable path
  must run. All six `sqlite3.connect(str(path))` sites currently open read-write, and
  `measure_executor_refresh_boundaries` runs the real `fill._drain_projection` against
  the artifact with no copy. The fixture is `journal_mode: wal`, so a read-write handle
  can rewrite container pages on close — the measurement can invalidate the very hash
  it records.
- Resolve the fixture location from an operator-configured path **in config, not an
  environment variable**. When absent, skip with a typed reason and record the existing
  `skipped_measurement` field. Never synthesize at acceptance scale;
  `_reject_synthetic_org_m_fixture` is part of the accepted F38 closure and must keep
  refusing.
- Restate the amended F38 evidence basis in the handback. Identity moves from file hash
  to content hashes — stronger, but it changes what was accepted at Gate 2, so state it
  rather than assume it.

Rejected approaches, recorded so they are not re-proposed: auto-generating the fixture
(reopens F38); committing a manifest to rebuild it (barred by the publication boundary
— `.gitignore` deliberately excludes `files.jsonl`, `drives.jsonl`, `replicas.jsonl`,
`verifications.jsonl`); storing it in git-annex or on the NAS remote (annex belongs to
the drive fleet and the central map repo; this repository is not an annex repo and must
not become one).

### 3. Dispose of `derivation_mode`

RFC-002 reserves it for placement audit evidence — `optimized`, `state_truncated`,
`canonical_fallback` — but `schema.sql:308` declares it `VARCHAR` with no CHECK. With
the `ecfg:` read deleted in PR-09 this is audit hygiene, not an authority bypass.

Two acceptable outcomes: constrain it under a backup-first **v7** migration matching
the v6 pattern, or write **DEF-033** recording the deferral with an explicit revisit
condition. Note PR-11 already introduces v7 for the provenance column, so folding the
constraint in there may be cheaper than either — raise it in the Gate-0 handback rather
than deciding unilaterally.

### 4. Carry-forward hygiene

- `modelark/core/db.py:662-676` — the v6 short-hash probe wraps its INSERT in
  `except Exception: pass`, so it reports success if the INSERT fails for any unrelated
  reason. Make it assert the specific integrity error. This is the same
  "verification that cannot fail" class as the original F38 refresh count and the false
  `git diff --check` claim.
- Fix the 11 trailing-whitespace hits in
  `docs/plans/placement-capacity-pr09-gate2-handback-cut-list.md`.

## Explicit stops

- **No PR-11 work.** DEC-053's provenance column and DEC-054's automatic repair are
  out of scope, including any "while I'm here" partial implementation.
- **No restore.** DEC-054 amends only the repair half of INC-017's boundary, and it is
  not authorized yet regardless.
- **No hash-repair run** against any catalog.
- Do not delete, move, or re-add `docs/plans/evidence/b12_390_approved_fixture.sqlite`.
  It is untracked and ignored; the bytes stay on disk at
  `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f` because a re-copy
  is not byte-reproducible and DEC-052 has not landed yet.
- No history rewrite, no force-push, no amends of pushed commits.
- No merge to `main`. PR-10 merges into `fix/placement-capacity-hardening` only, and
  only under Gate-3 authorization.
- No production fork/spawn choice.

## Process rules

- **Do not push while a review is pinned.** Produce your tip, post the handback, then
  stop. Pushes during a review window split evidence across heads, which happened
  twice in PR-09.
- **Handbacks must not use trailing double-space Markdown hard breaks.** A handback is
  always the last commit, so it can never be covered by a check that ran before it
  existed. Use list items or a table.
- **No document states the SHA of the commit that contains it.** Reference only
  already-pushed SHAs. Two amend attempts failed to converge on this in PR-09; a commit
  cannot contain its own hash.
- Preserve untracked operator files. Do not stage, commit, delete, or rewrite:
  - `docs/plans/placement-capacity-reviewer-handoff.md`
  - `docs/plans/placement-capacity-pr09-review-restart-primer.md`
  - `docs/plans/placement-capacity-pr10-charter.md`
  - `iscsi-login.sh`

## Verification at each gate tip

- Focused suites for the items touched.
- Full pytest, `python tests/test_replan.py`, Ruff on touched files.
- `git diff --check` over the full reviewed range, **including your handback commit**.
- Exact-tip GitHub CI and Greptile. Do not cite a parent commit's checks for a changed
  head; if your handback commit is docs-only, say so and identify the code tip the
  checks ran on.
- Confirm untracked operator files are untouched and the fixture bytes are intact.

## Greptile

At most three rounds per gate, triggered with `@greptileai review`. Greptile is
evidence, not authority — it ranks below RFC-002, the DEC ledger, and this brief. A
clean Greptile closes nothing and confers no gate. If a comment conflicts with any of
those, record the conflict and stop rather than complying.
