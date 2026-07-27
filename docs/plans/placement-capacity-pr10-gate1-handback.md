# PR-10 Gate-1 handback — contract rewrite (re-review)

**Date:** 2026-07-27
**Status:** Gate 1 remediation complete — **not accepted**. Gate 2 unauthorized.
**From:** implementer
**To:** human Gate-1 reviewer
**Branch:** `fix/placement-capacity-pr10-content-satisfaction`
**Prior reviewed tip:** `74de679c863f0c5b2f674d91a7e85fa7b2daa525`
**Base:** `a2c3707dc129257733fabc015b688e9738d3dc51`

## What changed this cycle (closed scope only)

| Item | Change |
|------|--------|
| `test_dec052_measure_refresh_*` | Replaced source-grep with **outcome pin**: SHA-256 before/after + no `-wal`/`-shm` sidecars. |
| Demonstration | `test_dec052_measure_unmutated_contract_fails_when_function_writes` — pin raises when a writer mutates the artifact. |
| `test_dec052_recompute_opens_sqlite_read_only` | Docstring: deliberately stricter than DEC-052 general (mode=ro only; no write path). Plus digest-unmutated outcome. |
| Demonstration | `test_dec052_recompute_unmutated_contract_fails_when_writer_runs` — pin raises on mutation. |
| Totals wording | File-level and co-run scopes labeled separately below (no adjacent contradiction). |

No production code. No INC-023 remediation. No PR-11 / derivation_mode CHECK / repair / restore.

## Contract suite results

**File-level** (`pytest tests/test_pr10_gate1_contracts.py` only):

| Result | Count | Notes |
|--------|------:|-------|
| Failed (expected red until Gate 2) | **9** | DEC-055 annex/routing (5); DEC-052 identity + recompute mode=ro + config path (3); v6 probe (1) |
| Passed | **7** | DEC-055 compressed + both-null fail-closed (4); measure outcome pin (1); two fail-when-broken demos (2) |

**Breakdown of the 7 greens**

| Test | Why green now |
|------|----------------|
| Compressed annex fail-closed (target + source) | Already true under DEC-051 |
| Both-null no-annex fail-closed (both sides) | Already true under DEC-051 |
| `test_dec052_measure_refresh_leaves_evidence_bytes_unchanged` | Outcome pin: real measure on a **copy** of the acceptance fixture left container hash unchanged and no sidecars |
| Writer-demo tests (recompute + measure helpers) | Assert `AssertionError` when a deliberate writer mutates — proves the pin can fail for the right reason |

**Still red (Gate 2)**

- DEC-055 behavioural annex pins + shared-accessor routing spy + revert pin
- DEC-052: content-hash identity not gated on `source_sqlite_sha256`; `recompute` must use `mode=ro`; config fixture path + typed skip
- v6 probe CHECK-specific IntegrityError

**Note on measure green:** the outcome pin is implementation-agnostic. It does **not** claim production already uses RO/copy-first; only that this measurement run did not rewrite the copied artifact. Gate 2 still owes RO or copy-first so a write-capable drain cannot invalidate evidence.

## Fail-when-broken demonstrations (this cycle's point)

| Pin | Broken behaviour | Result |
|-----|------------------|--------|
| Artifact unmutated helper | INSERT into evidence SQLite after snapshot | `AssertionError` matched (`test_dec052_recompute_unmutated_contract_fails_when_writer_runs`) |
| Same helper for measure shape | WAL write + table insert | `AssertionError` (`test_dec052_measure_unmutated_contract_fails_when_function_writes`) |

## Dispositions unchanged

| Topic | Status |
|-------|--------|
| `derivation_mode` CHECK | PR-11 v7 only (option 3) |
| INC-023 / projection fabrications | Open in ledger; out of PR-10 |
| Call-shape | `expected_sha256(..., catalog_sha=None)` from archive-row fields |

## Verification

| Check | Result |
|-------|--------|
| `pytest tests/test_pr10_gate1_contracts.py` | **9 failed, 7 passed** |
| Production files changed | none |
| `git diff --check a2c3707..<tip>` | clean (including this handback) |
| Fixture on disk | intact; not re-added to git |
| Four untracked operator files | untouched |

## Stop

One tip for human Gate-1 re-review. **No pushes after this handback** until dispositioned. Gate 2 unauthorized.
