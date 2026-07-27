# PR-10 Gate-1 handback — contracts only (expected red)

**Date:** 2026-07-27
**Status:** Gate 1 complete — **not accepted**. Gate 2 unauthorized.
**From:** implementer
**To:** human Gate-1 reviewer
**Branch:** `fix/placement-capacity-pr10-content-satisfaction`
**Gate-0 accepted tip:** `144235986e0b3ade53765eadcb5843343bcb4af3`
**Base:** `a2c3707dc129257733fabc015b688e9738d3dc51`

## Commits this gate

| Order | Subject |
|-------|---------|
| 1 | INC-023 appended verbatim; trailing WS cleared on Gate-0 handback and PR-09 cut-list handback |
| 2 | `tests/test_pr10_gate1_contracts.py` — DEC-055 / DEC-052 / v6 probe contracts |
| 3 | This handback |

**No production code.** No `execution_projection` changes. No PR-11 work.

## Dispositions applied

| Topic | Disposition |
|-------|-------------|
| `execution_projection._file_satisfied` / fabrications | **Out of PR-10.** INC-023 open in ledger. Structural multi-file fix needs its own gate cycle. |
| Null-digest fail-open in catalog | **Not realised** (0 known-expected vs null archive; no null-digest row > 100 MB). No model weight waved through today. Stated in INC-023. |
| `derivation_mode` CHECK | **Option 3:** schedule with **PR-11 v7** alongside DEC-053 provenance. No DEF-033. No PR-10-only v7. Documented here only. |
| Gate-0 handback trailing WS | Fixed in commit 1 (8 lines). |

## Contract suite — expected red

File: `tests/test_pr10_gate1_contracts.py`

| Area | Pins | Result at code tip |
|------|------|--------------------|
| **DEC-055** | Routes through `archive_hash.expected_sha256` with `catalog_sha=None`; annex-key null-orig shrinks/ready on target and source; compressed annex fails closed both sides; approved must match resolved annex digest; both-null no-annex still fails closed; revert pin on column-only path | **5 red**, 4 green (compressed + both-null already match DEC-051 fail-closed) |
| **DEC-052** | Content-hash identity not gated on `source_sqlite_sha256`; `recompute` RO connect; measure RO/copy-first; config fixture path + typed skip | **4 red** |
| **v6 probe** | CHECK-specific IntegrityError (not any IntegrityError) | **1 red** |

**Totals:** 10 failed, 7 passed (including prior DEC-051 matrix + v6 migration green checks co-run in verification).

Call-shape locked for Gate 2: resolve stored digest from archive-row fields (`orig_sha256`, `compressed`, `annex_key`) via `expected_sha256(..., catalog_sha=None)`. `proposal_files` remains approved file-list authority (RFC-002). Do not reopen catalog file-list fallback.

## INC-023 (recorded, not remediated)

Ledger entry open. Blast radius: 429 multi-file executable requirements; 47 partially present; 170 files at risk of silent shrink. Mitigating: null-digest fail-open not realised on acceptance catalog. Sequencing vs PR-11 provenance left as operator decision in `planned_remediation`.

## Verification

| Check | Result |
|-------|--------|
| `pytest tests/test_pr10_gate1_contracts.py` | **10 failed, 4 passed** (expected red) |
| Co-run prior green pins (DEC-051 matrix + catalog v6) | still green |
| `git diff --check a2c3707..<tip>` | clean after WS fix |
| Production files changed | none |
| Fixture bytes | intact at `bac9bea8…1522f`; untracked |
| Operator dirt (4 files) | untouched |

## Scope for Gate 2 (when authorized)

1. Implement DEC-055 on fill source + target (shared accessor).
2. Implement DEC-052 (identity, RO/copy-first, config path).
3. Tighten v6 short-hash probe to CHECK-specific failure.
4. Turn Gate-1 reds green without widening scope.

**Not Gate 2:** INC-023 / projection loop, DEC-053/054, hash-repair run, restore, derivation_mode CHECK (PR-11).

## Stop

One tip for human Gate-1 review. **No pushes after this handback** until dispositioned. Do not begin Gate 2.
