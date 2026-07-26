# PR-09 / #39-B — Gate 2 **production handback** (await human review)

**Date:** 2026-07-26  
**Status:** Gate 2 **implementation complete — not accepted**. Gate 3 **not authorized**.  
**Accepted Gate-1 tip (base of production):** `59d8b604cb09cce436fee0998fe6bf89cbd314c8`  
**Draft PR:** https://github.com/Auspex-Aerie/modelark/pull/54  
**Phase branch:** `fix/placement-capacity-pr09-execution-projection`  
**Integration base:** `bc33a0664d3e65e20c6843b0a9d5b1204d15502a`  
**Issue:** #39  
**Authorization:** Gate-2 production authorized from exact accepted tip `59d8b60` (operator passback).

---

## Scope delivered (B1–B13 only)

| Contract | Production surface |
|----------|-------------------|
| B1 / B13 pure projection | `modelark/execution_projection.project_pure` + `canonical_projection_hash` |
| B3–B6 session lifecycle | `modelark/execution_session` — `start_session`, claim, heartbeat, terminalize, `session_write` |
| B7 frozen config | `modelark/execution_config` — `ExecutionConfig`, `hash_config`, freeze revalidate; `fetch.get_frozen_execution_config` |
| B8 hard entry cut | `modelark/execution_service.start_fill`; CLI `start_fill_via_service`; `fill_api.start`; `server.auto_resume_fill`; `fill.execute` |
| B8 live exclusion | `graph_write` / `bump_revision` / A3 matrix writers → exact `FILL_SESSION_ACTIVE` |
| B9–B10 recovery / dirt / fences | `modelark/execution_recovery` — lock order controller→drives, dirty owner pair, child fence hold |
| B12 harness | `modelark/execution_benchmark` — recompute from SQLite, operator identity, call-count instrumentation |
| Gate-2 cold process | `tests/test_gate2_cold_process_exclusion.py` — second cold process, other state dir, exact `FILL_SESSION_ACTIVE` |

### Explicit non-goals (honored)

- No production multiprocessing; no fork/spawn selection.
- Second portal = another cold instance of the **same** installed entrypoint.
- No PR-10 façade removal or lifecycle-operation expansion.
- Gate-1 contracts frozen (one collateral spy fix for unspyable `sqlite3.Connection.execute`).
- Local operator dirt preserved (`iscsi-login.sh` untracked).

---

## Verification (implementer)

| Check | Result |
|-------|--------|
| Gate-1 suites (45) | **pass** |
| Gate-2 cold process | **pass** (2) |
| Full regression | **561 passed** |
| Ruff (touched Python) | clean |
| `git diff --check` | clean |
| Packaging / import smoke | `project_pure`, `start_session`, `start_fill`, `hash_config`; `modelark --help` |
| Installed console script | `.venv-dev/bin/modelark` present |

---

## Collateral amendments

1. `tests/test_session_recovery_transport.py` — lock-order observation without patching read-only `Connection.execute`.
2. `tests/test_replan.py` — autouse bridge so legacy reconcile/fetch drain still runs under synthetic `SessionStart` after B8 hard cut (façade retained until PR-10).
3. Schema: one-time dirty-generation owner-pair UPDATE allowed while both owner fields are NULL (append-only otherwise).

---

## Explicit stop

- **Do not** mark ready-for-review merge or merge PR #54.
- **Do not** begin Gate 3.
- **Do not** begin PR-10 scope.
- Await **Gate-2 human review** and formal acceptance tip before Gate-3 authorization.

**Await Gate-2 human disposition.**
