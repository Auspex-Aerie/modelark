# Passback to Gate-2 reviewer — findings 33–38

**Date:** 2026-07-26
**From:** implementer
**To:** human Gate-2 reviewer

---

## Authority

| Item | Value |
|------|--------|
| PR | [#54](https://github.com/Auspex-Aerie/modelark/pull/54) (draft, open — **not merged**) |
| Branch | `fix/placement-capacity-pr09-execution-projection` |
| **Code tip to re-review** | `e3530667f96d2fcb2a7fef14e4d2455210f84bfd` |
| Remediation commit | `52322ded7fe3595b600e0d2e74c4e32703cfb975` |
| Prior withheld head | `d88d1b1b312bf600c2af389763fbe4e29f217b62` |
| Implementer handback | `docs/plans/placement-capacity-pr09-gate2-handback-33-38.md` |
| This passback | `docs/plans/placement-capacity-pr09-gate2-passback-33-38.md` |

Gate 3 remains unauthorized. Implementer stop: no ready mark, no merge.

The code tip above includes remediation (`52322de`) plus the implementer handback commit. Later commits on the branch may add only this passback document.

---

## What was remediating

Human Gate-2 withheld at `d88d1b1` for findings **33–38**. One remediation cycle produced code tip `e353066` (code `52322de` + handback).

---

## Per-finding disposition

| # | Finding | Closure |
|---|---------|---------|
| **33** | Live envelope still `FILL_SESSION_ACTIVE` | `drive_mutation` takes `session_id`/`fencing_token`; dirty + anchor via `session_write`; owner fields set; `fetch.run` passes `RunCtx` session authority |
| **34** | Global `_SESSION_WRITE_DEPTH` cross-catalog leak | Connection-scoped `ContextVar` authority (`session_write_authorized(con)`) |
| **35** | Global compression reread; `derivation_mode` held config hash | Placement `derivation_mode` preserved; `execution_config_hash` separate; frozen config on `RunCtx` / transport |
| **36** | Heartbeat no expiry; terminal leaves lease fields; tokenless fallback | Heartbeat renews `expires_at` under CAS; terminalize clears heartbeat/expiry; failed CAS refuses (no tokenless UPDATE) |
| **37** | Refresh/heartbeat fail open | Typed refusals propagate; drain fails closed; missing/null file authority fails closed |
| **38** | B12 no approved structure / fabricated d0 / fake refresh counts | Acceptance requires approved proposal; refresh instrumented; 390-repo artifact exported |

Primary regressions: `tests/test_gate2_findings_33_38.py`
Production: `drive_mutation.py`, `execution_session.py`, `proposal.py`, `fetch.py`, `fill.py`, `execution_benchmark.py`, `schema.sql`

---

## B12 acceptance artifact

| Field | Value |
|-------|--------|
| Fixture | `docs/plans/evidence/b12_390_approved_fixture.sqlite` |
| Evidence | `docs/plans/evidence/b12_390_acceptance_wall_clock.json` |
| Structure | `approved_proposal` · 390 repos / 390 tasks / 390 files |
| Source SHA | `80a9d7136e1423a0c75d681624d47980c020e5774142114029003922ef48d43e` |
| Pure / full p95 | ~0.092 s / ~0.104 s (within contract) |
| Refresh count | 35 instrumented full-capture calls (not measured-run arithmetic) |

---

## Verification on code tip

| Check | Result |
|-------|--------|
| Full pytest (local) | **580 passed** |
| `python tests/test_replan.py` | all passed |
| Ruff / `git diff --check` | clean |
| GitHub CI (3.10, 3.12, e2e) on `e353066` | **SUCCESS** |
| Greptile (round 1/3) on `e353066` | **SUCCESS** — 39 files, **0 comments** |

---

## Unrelated dirt (still untracked, untouched)

- `docs/plans/placement-capacity-pr09-review-restart-primer.md`
- `iscsi-login.sh`

---

## Requested reviewer action

Re-review code tip **`e353066`** against findings **33–38** and RFC-002 / DEC-049.
Automation is green; human Gate-2 acceptance is still required.
**Do not merge or start Gate 3** from this passback.
