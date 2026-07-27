# Passback to Gate-2 reviewer — findings 35 / 37 / 38 (remaining)

> **SUPERSEDED** by `docs/plans/placement-capacity-pr09-gate2-passback-cut-list.md` (after pin `1939786`). Historical body retained; live refresh/generator/DEC-051 authority is the evidence JSON and cut-list.

**Date:** 2026-07-26
**From:** implementer
**To:** human Gate-2 reviewer

---

## Authority

| Item | Value |
|------|--------|
| PR | [#54](https://github.com/Auspex-Aerie/modelark/pull/54) (draft — **not merged**) |
| Branch | `fix/placement-capacity-pr09-execution-projection` |
| **Branch HEAD (passback tip)** | `663b207cb681deb9d9c76838468ea177304788f6` |
| **Code remediation tip** | `38373473b55e156e4733dcd14fc267351f7398d7` |
| Parent provisional tip | `00ba101cd704b6d855debbdc568f53e6b19f070f` |
| Remote was | `490bf0135b14a5a08823c90daf371f46c8f8fb0c` |
| Prior accepted | 33, 34, 36; F38 fixture identity + p95 timing |

Gate 3 remains unauthorized.

---

## Remaining findings closed

### F35 — freeze + v6 shape
- `_compression_from_ctx`: any attached `ExecutionConfig` never calls wishlist (including incomplete freeze).
- `start_session`: `execution_config_hash` NULL/empty/short → `APPROVED_INPUT_CHANGED` / unbound; requires fresh preview.
- v5→v6 rebuilds `placement_proposals` with the same null-or-64 CHECK as fresh schema; repairs prior unconstrained ADD COLUMN; backup-first + short-hash probe.

### F37 — replica source identity
- `waiting_dependency` promotion and replica execution require every approved file's `orig_sha256` match on the source drive; archive row presence alone is insufficient.

### F38 — refresh measure
- `measure_executor_refresh_boundaries` runs `fill._drain_projection` with mocked transport only; counts come from production batch-boundary `_refresh_projection` dispatch (`source: fill._drain_projection`).

---

## B12 evidence (unchanged fixture identity)

| Field | Value |
|-------|--------|
| Fixture | `docs/plans/evidence/b12_390_approved_fixture.sqlite` |
| Evidence JSON | `docs/plans/evidence/b12_390_acceptance_wall_clock.json` |
| Selected / tasks | 390 / 494 |
| Source SHA | `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f` |
| Refresh instrumentation | `fill._drain_projection` (not invented event loop) |

---

## Verification

| Check | Result |
|-------|--------|
| Full pytest | **589 passed** |
| Replan | all passed |
| Ruff | clean |
| `git diff --check` | clean |

---

## Requested action

Re-review the pushed tip against remaining F35/F37/F38 only.
Do not merge or start Gate 3 from this passback.
