# PR-09 Gate-2 handback — findings 35–38 remediation

> **SUPERSEDED** for F38 refresh counts/generator by `docs/plans/placement-capacity-pr09-gate2-handback-38-cadence.md` (after tip `768741f`). The "6 calls" claim is obsolete.


**Date:** 2026-07-26
**Status:** Gate 2 remediation complete — **not accepted**. Gate 3 **not authorized**.
**Parent (withheld tip):** `490bf0135b14a5a08823c90daf371f46c8f8fb0c`
**Prior code tip:** `e3530667f96d2fcb2a7fef14e4d2455210f84bfd`
**Accepted previously:** findings 33–34
**Draft PR:** https://github.com/Auspex-Aerie/modelark/pull/54
**Branch:** `fix/placement-capacity-pr09-execution-projection`

---

## Findings addressed

| # | Finding | Closure |
|---|---------|---------|
| **35** | Frozen config still reread/self-compared; soft ALTER | `fetch._compression_from_ctx` never calls wishlist when freeze present; projection boundary uses authoritative `_config_reader` via `assert_frozen_unchanged`; `execution_config_hash` via **versioned backup-first v6 migration** (`db._SCHEMA_VERSION=6`); opportunistic ALTER removed |
| **36** | Terminalize fail with `ok=True`; no worker CAS | Terminalize exception forces `ok=False`/`state=failed`; heartbeat CAS on token+state+`worker_identity` |
| **37** | Fabricated 1TB evidence; catalog file fallback | `_catalog_projection_bundle` requires `observe_exact_capacity` and fails closed; approved path requires `proposal_files` (no catalog fallback); null/mismatched archive identity stays missing; regressions for config drift, missing files, null identity, thrown refresh |
| **38** | Synthetic org/m#### fixture; fake refresh count | Replaced with real acceptance-runtime copy: 390 selected, 494 tasks, real repo IDs, 5567 archived, schema v6; `measure_executor_refresh_boundaries` counts only `fill._refresh_projection` (6 event-boundary calls); rejects synthetic org/m#### at acceptance scale |

---

## B12 acceptance artifact

| Field | Value |
|-------|--------|
| Fixture | `docs/plans/evidence/b12_390_approved_fixture.sqlite` |
| Evidence | `docs/plans/evidence/b12_390_acceptance_wall_clock.json` |
| Source | Copied/migrated from Auspex acceptance runtime catalog (real models) |
| Generator | `gate2-b12-rfc001-copy-v2` |
| Structure | `approved_proposal` |
| Selected | 390 |
| Requirements / tasks | 494 / 494 |
| Models / files | 4120 / 101883 |
| Archived rows | 5567 |
| Source SHA-256 | `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f` |
| Canonical input hash | `323359830ab72c0e95e425f6fbcdd21d917464ca3b4c3e36176b888a2520bf20` |
| Projection hash | `6cdb39e2a1be0792bbf6090e9cce20c9c569679837a5f0a2ebce28a4ace0d010` |
| Pure / full p95 | ~0.327 s / ~0.698 s |
| Refresh instrumentation | **6** calls via `fill._refresh_projection` (3 batch + 3 typed events); source `fill._refresh_projection` |

Note: task counts (494 total) match RFC-001 requirement count (494). Baseline vs executable mix differs from the 2026-07-16 cart snapshot (102 satisfied / 392 executable) due to current archive state on the copied runtime catalog; fixture is not synthetic `org/m####`.

---

## Verification

| Check | Result |
|-------|--------|
| Full pytest | **584 passed** |
| `python tests/test_replan.py` | all passed |
| Ruff | clean |
| `git diff --check` | clean (trailing whitespace fixed on Gate-2 docs) |

---

## Unrelated dirt (untouched)

- `docs/plans/placement-capacity-pr09-review-restart-primer.md`
- `iscsi-login.sh`

---

## Explicit stop

Do **not** mark ready, merge, begin Gate 3, choose fork/spawn, or begin PR-10.
Await human Gate-2 re-review of the new tip after this commit.
