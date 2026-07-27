# Passback to Gate-2 reviewer — F35/F37 fail-open + F38 cadence

> **SUPERSEDED** by `docs/plans/placement-capacity-pr09-gate2-passback-cut-list.md` (after pin `1939786`). Historical body retained; live refresh/generator/DEC-051 authority is the evidence JSON and cut-list.

**Date:** 2026-07-26
**From:** implementer
**To:** human Gate-2 reviewer
**Implements:** `docs/plans/placement-capacity-pr09-gate2-passback-38-cadence.md`

---

## Authority

| Item | Value |
|------|--------|
| PR | [#54](https://github.com/Auspex-Aerie/modelark/pull/54) — open, **draft**, unmerged |
| Branch | `fix/placement-capacity-pr09-execution-projection` |
| **Code remediation tip** | `fadc937a549482d445aa17b17f72bfd3f0174458` |
| Handback-pin tip (docs) | `0f897448b602db9c6cd30e53a8ab054eba61f06e` |
| Parent of remediation | `768741f74e6c3f7533b83d6f30e3b0db47e8d286` |
| Production range | `00ba101..fadc937` (docs pin after) |
| Written handback | `docs/plans/placement-capacity-pr09-gate2-handback-38-cadence.md` |

Gate 3 remains unauthorized. PR-10 unauthorized.
No ready mark, no merge, no history rewrite, no force-push.

---

## Accepted previously (unchanged — do not relitigate)

- Finding **33** — session-authorized physical mutation envelope
- Finding **34** — connection-scoped session-write authority
- Finding **36** — terminalize failure forces unsuccessful outcome; worker-identity heartbeat CAS
- Finding **38** (partial) — copied B12 fixture identity and wall-clock p95 within provisional contract

---

## What this cycle closed

### F35-a — no `ecfg:` config binding via `derivation_mode`

- Start requires a 64-char `execution_config_hash` alone.
- `derivation_mode='ecfg:<hash>'` with NULL config hash **refuses** start.
- Regression: `tests/test_gate2_findings_33_38.py::test_finding_35a_ecfg_derivation_mode_does_not_authorize_start`

### F35-b — unbind helper matches real migration

- `mark_proposal_pre_pr09_unbound` clears **only** `execution_config_hash`.
- `require_bound_execution_config` reads `execution_config_hash` (not semantic).
- Regression: `test_finding_35b_unbind_helper_clears_only_config_hash`

### F37-a — one content-satisfaction rule on source and target

Explicit rule (`fill._archive_content_satisfies`):

| Approved file | Archive row | Result |
|---------------|-------------|--------|
| Has content hash | Matching non-null hash | Satisfied |
| Has content hash | Missing / null / wrong hash | Not satisfied |
| **No** content hash (tiny git blob / NULL sha) | **Present** (null or any) | **Satisfied by presence** on both sides |

Authority: `schema.sql` documents `files.sha256` NULL for tiny git blobs; proposal_files copies that field.
Failing closed on all null hashes would park the majority of approved files.

Acceptance catalog counts under this rule:

| Fact | Count |
|------|------:|
| `proposal_files` total | 9,520 |
| `proposal_files` with NULL `orig_sha256` | **4,879** (51.3%) |
| `archived` total | 5,567 |
| `archived` with NULL `orig_sha256` | **1,122** (20.2%) |

Regressions: null-null source+target symmetry; present-vs-null and stale hash cases.

### F37-b — multi-file source authority

Regressions for multi-file requirement where second source file is (i) stale hash or (ii) absent → stays `waiting_dependency`.

### F38 — production refresh cadence (not degenerate / not invented)

Measurement runs **real** `fill._drain_projection` with transport mocked only enough to avoid network/disk writes. Projection re-solve is stabilized so cadence dispatch is observable.

| Metric | Value |
|--------|------:|
| Source | `fill._drain_projection` |
| Initial full projection | 1 (start path; not a refresh) |
| Batch-boundary refreshes | **2** |
| Typed-event refreshes | **1** (`typed_event:gated_retry`) |
| Total refreshes | **3** (= 2 + 1) |
| Transport batches observed | 3 |
| Generator | `gate2-b12-rfc001-copy-v4` |

Adversarial: blocking `_drain_projection` raises rather than recording a low count.
Insufficient batch/typed counts raise `ACCEPTANCE_FIXTURE_INVALID`.

### F38 evidence identity (fixture **not** modified this cycle)

| Field | Value |
|-------|--------|
| Fixture | `docs/plans/evidence/b12_390_approved_fixture.sqlite` (repo-relative) |
| Evidence JSON | `docs/plans/evidence/b12_390_acceptance_wall_clock.json` |
| Source SHA-256 | `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f` |
| Selected / tasks | 390 / 494 |
| Models / files | 4,120 / 101,883 |
| Baseline / executable | 65 / 429 |
| Archived | 5,567 |
| integrity_check | ok |
| foreign_key_violations | 0 |
| user_version | 6 |

### p95 timing (re-measured this cycle — both within provisional contract)

| Measurement | Pure p95 | Full p95 |
|-------------|----------|----------|
| Prior accepted at `00ba101` | ~0.327 s | ~0.698 s |
| This cycle (evidence JSON) | ~0.365 s | ~0.770 s |
| Provisional contract | ≤ 0.5 s | ≤ 2.0 s |

Contract `source` is **`provisional_harness_pending_decision`** — harness thresholds are not claimed as RFC-002/DEC-049 product gates.

### Docs consistency

- Superseded “6 synthetic refresh” claims marked on older handback.
- Evidence generator, path, refresh source/count/breakdown match the JSON.
- Handback does not self-name its own commit SHA as branch HEAD.

---

## Exact-tip verification (re-runnable)

Commands run at tip `0f89744` (docs pin) / code `fadc937`:

| Check | Result |
|-------|--------|
| Full pytest | **593 passed** |
| `python tests/test_replan.py` | all passed |
| Focused F35/F37/F38 + v6 + projection contract | passed |
| Ruff | clean |
| `git diff --check bc33a066 HEAD` | clean |
| GitHub `test (3.10)` | SUCCESS |
| GitHub `test (3.12)` | SUCCESS |
| GitHub `e2e` | SUCCESS |
| Greptile Review | SUCCESS — 47 files, **0 comments** (loop stopped round 1/3) |

---

## Greptile loop

| Round | Tip | Result |
|-------|-----|--------|
| 1/3 | `0f89744` | SUCCESS, 0 comments — stop |

Greptile is evidence only; does not close Gate 2.

---

## Untouched / out of scope

- Fixture sqlite not removed, re-added, or moved to git-annex
- Untracked operator files preserved:
  `docs/plans/placement-capacity-pr09-review-restart-primer.md`
  `docs/plans/placement-capacity-reviewer-handoff.md`
  `iscsi-login.sh`
- No PR retitle, no ready mark, no merge, no Gate 3, no PR-10, no fork/spawn choice

---

## Requested reviewer action

Re-review **code tip `fadc937`** (branch head `0f89744` includes handback pin only) against:

1. F35-a / F35-b
2. F37-a rule + catalog null-hash counts
3. F37-b multi-file source regressions
4. F38 multi-batch + typed-event cadence evidence vs JSON

Human Gate-2 disposition required. Automation green is not acceptance.
