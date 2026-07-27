# PR-10 Gate-0 handback — helper inventory and scope confirmation

**Date:** 2026-07-27  
**Status:** Gate 0 complete — **not accepted**. Gate 1 unauthorized until human review.  
**From:** implementer  
**To:** human Gate-0 reviewer  
**Brief:** `docs/plans/placement-capacity-pr10-gate0-brief.md` (committed verbatim)  
**Branch:** `fix/placement-capacity-pr10-content-satisfaction`  
**Branched from:** `a2c3707dc129257733fabc015b688e9738d3dc51` (`origin/fix/placement-capacity-hardening` tip at branch time; re-verified before branch)

## Commits this gate (docs/ledger only)

| Order | Subject |
|-------|---------|
| 1 | Brief committed verbatim |
| 2 | Ledger patch applied: DEC-053, DEC-054, DEC-055, DEC-051 status → superseded by DEC-055 |
| 3 | This handback |

No production code, no tests. Parent of this handback is the ledger commit; base merge tip remains `a2c3707`.

## Scope confirmation (closed four items)

| # | Item | In Gate 0 / later | Out of scope |
|---|------|-------------------|--------------|
| 1 | **DEC-055** — route Fill satisfaction through `archive_hash.expected_sha256` | Gate 1–2 | — |
| 2 | **DEC-052** — acceptance-evidence content identity; RO/copy-first SQLite; config path for fixture | Gate 1–2 | Auto-gen fixture, git-annex/NAS storage, publishing excluded JSONL |
| 3 | **`derivation_mode`** — CHECK under v7 **or** DEF-033 deferral | Raise here; do not decide unilaterally (PR-11 already owns v7 for provenance) | Unconstrained VARCHAR left silent |
| 4 | **Hygiene** — v6 short-hash probe assert; 11 trailing-WS hits in PR-09 cut-list handback | Gate 1–2 | — |

**Explicit non-work:** DEC-053 provenance column, DEC-054 automatic repair, any hash-repair run, restore, main merge, force-push, PR-11 implementation, fork/spawn, touch fixture bytes.

---

## Helper inventory (mandatory)

### Shared digest accessor (the one DEC-051 missed)

| Symbol | Location | Role |
|--------|----------|------|
| `archive_hash.annex_sha256` | `modelark/archive_hash.py` | Parse original-byte digest from `SHA256` / `SHA256E` annex keys only |
| `archive_hash.expected_sha256` | same | **Canonical restore-evidence rule:** `catalog_sha or orig_sha256`, else raw-copy annex digest; compressed annex keys return `None` |

Module docstring: *"One restore-evidence rule shared by restore, verification, and legacy repair."*

### Who already uses it (siblings agree)

| Subsystem | Call site | Args |
|-----------|-----------|------|
| **restore** | `restore._expected_hash` | `catalog_sha` from `files.sha256` join, `orig_sha256` / `compressed` / `annex_key` from `archived` |
| **verification** | `verifier.reverify` deep path | Same four fields |
| **legacy repair** | `hash_repair._expected_hash` | Same shape from repair row dict |

These three subsystems already treat a raw `SHA256(E)` annex key as original-byte evidence when `orig_sha256` is null. Fill does **not**.

### Fill today (divergence — the PR-09 / DEC-051 defect class)

| Symbol | Behavior |
|--------|----------|
| `fill._archive_content_satisfies(approved_sha, archived_sha)` | Compares **column values only**. Approved present → exact non-null match; approved absent → non-null archived string; both null → fail closed (DEC-051 as shipped). |
| `fill._source_files_content_ready` | `SELECT orig_sha256` only — **no** `compressed`, **no** `annex_key`. |
| Target path in `_projection_work_units` | Same single-column SELECT and helper. |

**Accessor DEC-055 requires:** `archive_hash.expected_sha256`. None exists specialized to Fill; do not invent a parallel resolver.

**Call-shape note for Gate 1 (not decided here):** restore/verifier pass `catalog_sha=files.sha256`. Approved Fill paths treat `proposal_files` as file-list authority (RFC-002). Inventory recommendation for implementation design: resolve the *stored* digest from archive row fields (`orig_sha256`, `compressed`, `annex_key`) and set `catalog_sha=None` unless Gate 1 contracts explicitly allow a `files` join for expected-original only — without reopening catalog file-list fallback. DEC-055 impact (265 / 497 → ~0 via `SHA256E` + `compressed=0`) is explained entirely by the annex branch of `expected_sha256`.

### Related helpers (read; not Fill satisfaction)

| Symbol | Notes |
|--------|-------|
| `archive_hash` (module) | Sole shared original-byte resolution API. |
| `compress.sha256_file` / `compress.canary_ok` | Physical byte check given an expected digest — restore/verify after resolution. |
| `hash_repair._validate_candidate` | Produces `archive-head-blob` evidence tag; UPDATE currently discards provenance (DEC-053 / PR-11). |
| `execution_projection._file_satisfied` | Separate projection-side check: row presence + optional mismatch on `orig_sha256` only; **does not** use `expected_sha256`; more permissive when expected is set and archive hash is null (`return True`). **Divergence on identical evidence** vs Fill/restore. Not in the four-item closed scope — ledger/PR-11+ if it must change; do not absorb into PR-10 without Gate-0 expansion. |
| `proposal` durable identity vs archived | Uses `orig_sha256` / size for proposal construction — not execution satisfaction. |
| `capacity` source identity | Compares `orig_sha256` on placement sources — not the Fill drain path. |

### DEC-052 inventory (acceptance evidence)

| Concern | Existing state |
|---------|----------------|
| Content hashes | `recompute_fixture_identity` already computes `prepared_canonical_input_hash`, `prepared_projection_hash`, row counts, **and** file-byte `source_sqlite_sha256`. |
| Gate misuse | Acceptance still treats container hash as binding identity in descriptors / comparisons. |
| Six `sqlite3.connect(str(path))` sites in `execution_benchmark.py` | Lines ~43, ~154, ~252, ~457, ~566, ~691 — all default read-write. `measure_executor_refresh_boundaries` runs real `fill._drain_projection` on the artifact with no copy. |
| Synthetic refusal | `_reject_synthetic_org_m_fixture` — keep. |
| Fixture path config | **No** wishlist/config key today for acceptance fixture location. Must be added in config (not env). Absent → typed skip + `skipped_measurement`. |
| Rejected approaches | Auto-generate fixture; commit rebuild manifest (publication boundary); git-annex / NAS for this repo. |

### Item 3 — `derivation_mode`

| Fact | Location |
|------|----------|
| RFC-002 audit values | `optimized` / `state_truncated` / `canonical_fallback` |
| Schema | `schema.sql` ~308: unconstrained `VARCHAR` |
| Runtime | Written by proposal/placement paths; **not** execution-config authority (`execution_config_hash` is separate; `ecfg:` deleted in PR-09) |

**Gate-0 raise (no unilateral choice):** folding a CHECK into PR-11's planned v7 (with DEC-053 provenance) is likely cheaper than a PR-10-only v7 or a DEF-033 deferral. Prefer reviewer disposition:

1. DEF-033 in PR-10 (explicit revisit: with PR-11 v7), or  
2. CHECK in PR-10 v7 alone, or  
3. Schedule CHECK with PR-11 v7 and document in handback only.

### Item 4 — hygiene inventory

| Item | State at `a2c3707` / this branch |
|------|----------------------------------|
| v6 short-hash probe | `db.py` ~661–695: catches `IntegrityError` and fails closed if INSERT succeeds. **Still weak:** probe1 accepts *any* IntegrityError (PK/FK/CHECK), not specifically the short-hash CHECK. Outer `except Exception` on plan insert (`pass`) and migration wrapper remain. Brief wants assert of the **specific** integrity failure — tighten in Gate 1–2. |
| Trailing WS | `docs/plans/placement-capacity-pr09-gate2-handback-cut-list.md` — **11** lines with trailing spaces (meta lines and list tails). Fix with lists/tables; no double-space hard breaks in new handbacks. |

---

## Subsystem agreement matrix (identical evidence question)

| Question | restore | verifier | hash_repair | fill (shipped) | execution_projection |
|----------|---------|----------|-------------|----------------|----------------------|
| Original-byte digest for a stored copy | `expected_sha256` | `expected_sha256` | `expected_sha256` | raw `orig_sha256` only | raw `orig_sha256`; null archive still "satisfied" if expected set |
| Raw SHA256E, null `orig_sha256` | resolvable | resolvable | resolvable | **not satisfied** | often **satisfied** (presence) |
| Compressed, null digests | not resolvable | not resolvable | not resolvable | not satisfied | presence-based |

**Defect class for PR-10 item 1:** Fill must agree with restore/verifier/repair via the shared accessor. Projection `_file_satisfied` is a **second** divergence — out of closed scope unless reviewer expands Gate 0.

---

## Implementation routing map (for Gate 1 contracts — not implemented now)

| Rule to touch | Existing accessor to use |
|---------------|--------------------------|
| Fill source readiness + target satisfaction (DEC-055) | `archive_hash.expected_sha256` (+ expand SELECT to `orig_sha256, compressed, annex_key`) |
| Acceptance identity (DEC-052) | Content hashes from `recompute_fixture_identity`; demote `source_sqlite_sha256` to provenance; RO/`mode=ro` or tempfile copy for write paths |
| Fixture location (DEC-052) | **None exists** — new wishlist/config key required |
| `derivation_mode` CHECK | None — schema only; coordinate with PR-11 v7 |
| v6 probe | Existing probe block in `_migrate_execution_config_hash_v6` — assert CHECK-specific failure |

---

## Preservation checks

| Check | Result |
|-------|--------|
| Fixture on disk SHA-256 | `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f` |
| Fixture in git tree | absent (ignored) |
| Untracked operator files | untouched (handoff, primer, PR-10 charter, `iscsi-login.sh`) |
| Ledger patch authority | committed as decision_log append; patch file remains outside repo |

## Stop

Gate 0 ends here. **No Gate 1** (contracts/tests), **no production**, **no PR-11**, until human Gate-0 acceptance of this inventory and scope confirmation — especially the `derivation_mode` disposition raise and whether `execution_projection._file_satisfied` stays out of PR-10.
