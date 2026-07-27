# PR-10 Gate-2 handback — production complete

**Date:** 2026-07-27
**Status:** Gate 2 remediation complete — **not accepted**. Gate 3 unauthorized.
**From:** implementer
**To:** human Gate-2 reviewer
**PR:** https://github.com/Auspex-Aerie/modelark/pull/55 (draft)
**Branch:** `fix/placement-capacity-pr10-content-satisfaction`

## Tips

| Item | SHA |
|------|-----|
| **Code tip (CI/Greptile production)** | `82e3f901bcc0b2b5c7e2250d046b18de8552bb0e` |
| Parent (Gate-1 accepted) | `0c51227380324dd44e0a47853b33cf91bb881e1c` |
| Base | `a2c3707dc129257733fabc015b688e9738d3dc51` |

This handback commit is docs-only after the code tip above.

## Step-0 automation baseline (on accepted tip `0c51227`)

Recorded before any production code:

| Check | Result |
|-------|--------|
| test (3.10) | pass |
| test (3.12) | pass |
| e2e | pass |
| Greptile | pass — 3× P2 on contracts only (not counted against production rounds) |

## Production Greptile round 1 (on `82e3f90`)

| Check | Result |
|-------|--------|
| test (3.10) | pass |
| test (3.12) | pass |
| e2e | pass |
| Greptile | pass — no new production-file comments |

Baseline P2s were addressed in the production tip (measure outcome capture; probe classifier; `skipped_measurement` via `run_acceptance_wall_clock`). One remapped “skip evidence” note still points at the resolver lines; the same test now asserts `skipped_measurement`/`skip_reason` immediately below. No further Greptile rounds.

## Six items

| # | Item | Disposition |
|---|------|-------------|
| 1 | **DEC-055** | `fill._archive_content_satisfies` routes through `archive_hash.expected_sha256(catalog_sha=None, …)`; SELECTs load `orig_sha256, compressed, annex_key` on source and target. |
| 2 | **DEC-052** | Content hashes bind identity; `source_sqlite_sha256` provenance-only (logged, not gated). Evidence opens RO via `_connect_readonly`; measure uses tempfile copy. `resolve_acceptance_fixture_path` + wishlist `acceptance.fixture_sqlite_path`; typed skip + `skipped_measurement`. |
| 3 | **v6 probe** | `_is_execution_config_hash_check_error`; both probes refuse non-CHECK IntegrityError. |
| 4 | **Gate-1 reds → green** | `tests/test_pr10_gate1_contracts.py`: **16 passed, 0 failed**. |
| 5 | Measure outcome pin | No silent `except Exception: pass`; success keys or typed exception set. |
| 6 | Fixture skip typing | Resolver + wall-clock path record `skipped_measurement` / `skip_reason`. |

## DEC-055 impact (acceptance fixture, independent)

Unhashed approved files with an archive row present:

| Side | DEC-051 fail-closed | DEC-055 fail-closed | Rescued by annex resolution |
|------|--------------------:|--------------------:|----------------------------:|
| Target (`drive-00`) | 265 | **0** | 265 |
| Target (`drive-01`) | 0 | 0 | 0 |
| Replica source (`drive-00`) | 497 | **0** | 497 |

Matches expectation (~265 / ~497 → approximately zero).

## Fixture contracts that need / do not need the on-disk B12 catalog

| Needs fixture bytes on disk | Does not |
|-----------------------------|----------|
| `test_dec052_measure_refresh_leaves_evidence_bytes_unchanged` (skips if absent) | Pure matrix / annex unit paths, recompute RO, config skip, v6 probe, identity gate |

Fixture SHA intact at `bac9bea8…1522f`; not in tree.

## `derivation_mode`

Scheduled with **PR-11 v7** (option 3). No DEF-033. No PR-10-only migration.

## Out of scope (unchanged)

INC-023 open; no `execution_projection` changes. No DEC-053/054. No repair/restore. No ready/merge.

## Verification

| Check | Result |
|-------|--------|
| `pytest tests/test_pr10_gate1_contracts.py` | 16 passed |
| Focused DEC-051/055 + v6 + projection contracts | pass |
| Full pytest | **618 passed** |
| `python tests/test_replan.py` | all passed |
| Ruff on touched files | clean |
| `git diff --check a2c3707..<tip>` | clean (handback included) |
| Exact-tip CI on `82e3f90` | 3.10 / 3.12 / e2e green |
| Untracked operator files | untouched |

## Stop

Do not mark ready, merge, or begin Gate 3. Pin re-engages: **no pushes after this handback** until human Gate-2 disposition.
