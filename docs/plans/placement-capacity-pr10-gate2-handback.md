# PR-10 Gate-2 handback — production complete

**Date:** 2026-07-27
**Status:** Gate 2 remediation complete — **not accepted**. Gate 3 unauthorized.
**From:** implementer
**To:** human Gate-2 reviewer
**PR:** https://github.com/Auspex-Aerie/modelark/pull/55 (draft — not ready, not merged)
**Branch:** `fix/placement-capacity-pr10-content-satisfaction`
**Base:** `fix/placement-capacity-hardening` @ `a2c3707dc129257733fabc015b688e9738d3dc51`

---

## Tips

| Item | Value |
|------|--------|
| **Code tip** (production + contracts; CI and Greptile ran here) | `82e3f901bcc0b2b5c7e2250d046b18de8552bb0e` |
| Parent of code tip (Gate-1 accepted) | `0c51227380324dd44e0a47853b33cf91bb881e1c` |
| Branch base | `a2c3707dc129257733fabc015b688e9738d3dc51` |
| Handback | docs-only commit(s) after the code tip; do not treat handback SHA as production tip |

Authorize review of the **code tip** `82e3f90`. Handback commits only add this document.

---

## Commits since Gate-1 accepted tip

| Order | Short | Subject |
|------:|-------|---------|
| 1 | `82e3f90` | Gate-2 production: DEC-055/052, probe hygiene, contract tightenings |
| 2+ | handback tip(s) | This handback (docs only) |

Production files changed only in `82e3f90`:

| Path | Change |
|------|--------|
| `modelark/fill.py` | DEC-055 satisfaction via `archive_hash.expected_sha256`; SELECTs include `compressed`, `annex_key` |
| `modelark/execution_benchmark.py` | DEC-052 identity, RO connect, measure copy-first, fixture path resolver + skip |
| `modelark/core/db.py` | CHECK-specific v6 short-hash probe classifier |
| `modelark/wishlist.py` | `acceptance()` config accessor |
| `modelark/default_wishlist.yaml` | `acceptance.fixture_sqlite_path: null` |
| `wishlist.yaml` | same key (packaged-default parity) |
| `tests/test_pr10_gate1_contracts.py` | Greptile/carry-forward tightenings; suite green |
| `tests/test_projection_performance_contract.py` | self-auth test keeps honest content hashes |

---

## Step 0 — automation baseline (before production)

On accepted Gate-1 tip `0c51227` only (contracts/tests; no production):

| Check | Result | Notes |
|-------|--------|-------|
| test (3.10) | **pass** | First CI on this branch |
| test (3.12) | **pass** | First 3.12 run for PR-10 |
| e2e | **pass** | First e2e on this branch |
| Greptile | **pass** | 3× P2 on contracts only |

Baseline Greptile P2s (do **not** count against the three production rounds):

1. Measure outcome pin used bare `except Exception: pass`
2. v6 probe heuristic could inspect the wrong probe
3. Fixture skip asserted resolver only, not `skipped_measurement`

All three addressed in `82e3f90`. Draft PR #55 opened into `fix/placement-capacity-hardening` (not `main`).

---

## Production Greptile (after code)

| Round | Tip | CI 3.10 / 3.12 / e2e | Greptile | Actionable |
|------:|-----|----------------------|----------|------------|
| 1 | `82e3f90` | all **pass** | **pass** | No new production-file comments |

One baseline P2 was remapped onto `82e3f90` at the resolver lines; the same test already asserts `skipped_measurement` / `skip_reason` via `run_acceptance_wall_clock` immediately below. **No second production round.**

---

## Six authorized items — what shipped

### 1. DEC-055 — Fill content satisfaction

**Rule (unchanged intent of DEC-051; resolution path only):**

- Approved hash present → resolved stored digest must equal it
- Approved hash absent → a digest must be resolvable for the stored copy
- Nothing resolvable → fails closed on **both** source and target

**Implementation:**

- `_archive_content_satisfies` calls
  `archive_hash.expected_sha256(catalog_sha=None, orig_sha256=…, compressed=…, annex_key=…)`
- Call-shape from Gate-0 inventory: **`catalog_sha=None`** (no catalog `files` re-open; `proposal_files` remains file-list authority under RFC-002)
- `_source_files_content_ready` and target evaluation in `_projection_work_units`
  `SELECT orig_sha256, compressed, annex_key FROM archived …`
- Raw `SHA256` / `SHA256E` annex keys resolve when `orig_sha256` is null; compressed annex keys do not

**Legacy:** two-arg pure matrix still works (`archived_sha` aliases `orig_sha256`).

### 2. DEC-052 — acceptance-evidence identity

| Concern | Before | After |
|---------|--------|-------|
| Binding identity | Container `source_sqlite_sha256` could gate | `prepared_canonical_input_hash`, `prepared_projection_hash`, row counts |
| Container hash | Treated as gate | Provenance only; drift **logged**, not refused |
| SQLite open | Default read-write on evidence | `_connect_readonly` (`file:…?mode=ro`) for recompute / validate / pure paths |
| Measure path | RW on operator artifact | Tempfile **copy**; original never opened write |
| Fixture location | Implicit / path-in-descriptor only | Config: `acceptance.fixture_sqlite_path` (wishlist); **not** env |
| Missing fixture | Refuse or invent | Typed skip: `skipped_measurement=True` + `skip_reason` |

**F38 evidence basis (restated):** Gate-2 acceptance identity is **content hashes** (and counts), not the SQLite container byte hash. Stronger than pinning layout; a re-copy of the same logical catalog remains valid. `source_sqlite_sha256` is retained for provenance logging only.

Rejected approaches not used: auto-generate fixture; commit rebuild manifest; git-annex/NAS for this repo.

### 3. v6 short-hash probe hygiene

- Added `db._is_execution_config_hash_check_error`
- Both probe inserts require a CHECK-class IntegrityError
- Unrelated UNIQUE/FK IntegrityError **raises** (no longer false-pass)

### 4. Gate-1 expected-red suite → green

`pytest tests/test_pr10_gate1_contracts.py`: **16 passed, 0 failed**

(Previously 9 failed / 7 passed at Gate-1 re-review tip.)

### 5. Measure outcome pin (carry-forward)

`test_dec052_measure_refresh_leaves_evidence_bytes_unchanged` no longer swallows all exceptions. Success must return instrumentation keys; failures must be a typed set (`Refusal`, `RuntimeError`, `sqlite3.Error`, `OSError`, `ValueError`). Artifact SHA + no `-wal`/`-shm` still asserted.

### 6. Fixture skip typing (carry-forward)

- `resolve_acceptance_fixture_path(config=…)` → `(None, reason)` when absent/missing
- `run_acceptance_wall_clock` with no path + empty config returns `skipped_measurement` / `skip_reason` without synthesizing data

---

## DEC-055 impact (independent recount on acceptance fixture)

Join shape matches production: `archived` on `(repo_id, rfilename, drive_label)`; `repo_id` / drive from `proposal_tasks`; approved proposal only. Resolution: `expected_sha256(catalog_sha=None, …)`.

**Unhashed approved files with an archive row present:**

| Side | Drive | DEC-051 fail-closed | DEC-055 fail-closed | Rescued (annex) |
|------|-------|--------------------:|--------------------:|----------------:|
| Target | `drive-00` | 265 | **0** | 265 |
| Target | `drive-01` | 0 | 0 | 0 |
| Replica source | `drive-00` | 497 | **0** | 497 |

**Totals:** 265 + 497 fail-closed under DEC-051 → **0** under DEC-055. Matches the ledger expectation (~zero).

---

## Which contracts need the on-disk B12 fixture

| Requires `docs/plans/evidence/b12_390_approved_fixture.sqlite` on disk | Does not (unit-scale / synthetic) |
|----------------------------------------------------------------------|-----------------------------------|
| `test_dec052_measure_refresh_leaves_evidence_bytes_unchanged` (skips with typed reason if absent) | Annex/source/target matrix, recompute RO, identity gate, config skip, v6 probe, writer demos |

| Fixture fact | Value |
|--------------|--------|
| Path | `docs/plans/evidence/b12_390_approved_fixture.sqlite` (untracked, ignored) |
| SHA-256 | `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f` |
| In git tree | **absent** (DEF-032) |
| Touched this gate | **no** |

---

## `derivation_mode` disposition

**Option 3 (locked at Gate 0/1):** schedule the CHECK with **PR-11 v7** alongside DEC-053 provenance. No DEF-033. No PR-10-only schema version.

---

## Explicitly out of scope (not done)

| Item | Status |
|------|--------|
| INC-023 / `execution_projection` multi-file fabrications | Open in ledger; **not remediated** |
| DEC-053 provenance column | PR-11 |
| DEC-054 automatic repair | PR-11 |
| Hash-repair run / restore | not authorized |
| Ready / merge / Gate 3 / main | unauthorized |
| Force-push / history rewrite | not done |

---

## Verification (local at code tip `82e3f90`)

| Check | Result |
|-------|--------|
| `pytest tests/test_pr10_gate1_contracts.py` | **16 passed** |
| Focused: gate2 findings / catalog v6 / projection performance contracts | pass |
| Full pytest | **618 passed** |
| `python tests/test_replan.py` | all passed |
| Ruff on touched production/test files | clean |
| `git diff --check a2c3707..82e3f90` | clean |
| Exact-tip CI on `82e3f90` | test 3.10 ✅ · test 3.12 ✅ · e2e ✅ |
| Greptile on `82e3f90` | pass (production round 1 of ≤3) |

---

## Untracked operator files (untouched)

- `docs/plans/placement-capacity-reviewer-handoff.md`
- `docs/plans/placement-capacity-pr09-review-restart-primer.md`
- `docs/plans/placement-capacity-pr10-charter.md`
- `iscsi-login.sh`

---

## Stop

- Do **not** mark PR #55 ready
- Do **not** merge (Gate 3 unauthorized)
- Do **not** begin PR-11 work from this handback
- Pin re-engages on handback: **no further implementer pushes** until human Gate-2 disposition

Awaiting human Gate-2 re-review of code tip **`82e3f90`**.
