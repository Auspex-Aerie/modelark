# PR-09 / #39-B — Gate 2 findings **33–38** handback

**Date:** 2026-07-26  
**Status:** Gate 2 **remediation complete — not accepted**. Gate 3 **not authorized**.  
**Parent (withheld tip):** `d88d1b1b312bf600c2af389763fbe4e29f217b62`  
**Remediation tip:** `52322ded7fe3595b600e0d2e74c4e32703cfb975`  
**Draft PR:** https://github.com/Auspex-Aerie/modelark/pull/54  
**Phase branch:** `fix/placement-capacity-pr09-execution-projection`  

---

## Findings addressed

| # | Finding | Disposition | Production | Regression |
|---|---------|-------------|------------|------------|
| **33** | Live-session physical envelope still `FILL_SESSION_ACTIVE` | `drive_mutation(..., session_id=, fencing_token=)` routes dirty + clean-anchor TXs through `session_write`; populates `owner_session_id`/`owner_fencing_token`; token revalidated at each TX boundary. `fetch.run` passes `RunCtx` session authority into the envelope. | `modelark/drive_mutation.py`, `modelark/fetch.py` | `tests/test_gate2_findings_33_38.py::test_finding_33_live_envelope_succeeds_with_owner_fields` |
| **34** | Process-global `_SESSION_WRITE_DEPTH` cross-catalog leak | Replaced with connection-scoped `ContextVar` authority (`session_write_authorized(con)`). Authorized write on catalog A cannot authorize catalog B. | `modelark/execution_session.py`, `modelark/proposal.py` | `tests/test_gate2_findings_33_38.py::test_finding_34_cross_catalog_authority_does_not_leak` |
| **35** | Transport rereads global compression; `derivation_mode` held config hash | Draft stores placement `derivation_mode` (`optimized` / …) and separate `execution_config_hash` (header + column, soft-add). Start compares frozen hash. `RunCtx.execution_config` + `fetch._compression_from_ctx` use freeze. | `modelark/proposal.py`, `modelark/execution_session.py`, `modelark/fetch.py`, `modelark/fill.py`, `modelark/core/schema.sql` | `test_finding_35_*`, updated `test_compression_config_drift_refuses_start` |
| **36** | Heartbeat no expiry renew; terminal leaves lease fields; tokenless fallback | `heartbeat` renews `expires_at` under token/state CAS; `terminalize` clears `heartbeat_at`/`expires_at` and refuses failed CAS (no tokenless UPDATE). | `modelark/execution_session.py`, `modelark/fill.py` | `test_finding_36_*` |
| **37** | Projection/heartbeat/refresh fail open | `_refresh_projection` raises typed `Refusal`; drain fails closed on heartbeat/refresh failure; missing file authority and null archive identity fail closed. | `modelark/fill.py` | `test_finding_37_refresh_propagates_refusal` |
| **38** | B12 no approved structure; synthetic d0; refresh count = runs | Acceptance requires approved proposal structure (no d0 fabrication). Refresh counts instrumented. 390-repo approved fixture + wall-clock evidence exported. | `modelark/execution_benchmark.py`, `modelark/fill.py` | `test_finding_38_*`; artifact paths below |

---

## Probe / acceptance results

### Physical envelope (33)
Authorized live session envelope advanced dirty generation with both owner fields populated; no `FILL_SESSION_ACTIVE` on the real envelope path.

### Cross-catalog (34)
`unauthorized_cross_catalog_rows == 0` while `session_write` on catalog A is active and catalog B has a live session.

### Frozen config (35)
Transport compression follows frozen `ExecutionConfig` under hostile global wishlist edit. Draft `derivation_mode` is not `ecfg:<hash>`.

### Lease / terminal (36)
Heartbeat renews expiry under token CAS; wrong token refuses; terminal row has `state=done` with null `heartbeat_at` and `expires_at`.

### Refresh refuse (37)
`_refresh_projection` propagates `APPROVED_INPUT_CHANGED` instead of returning `None`.

### B12 copied-catalog acceptance (38)

| Field | Value |
|-------|--------|
| Fixture | `docs/plans/evidence/b12_390_approved_fixture.sqlite` |
| Evidence | `docs/plans/evidence/b12_390_acceptance_wall_clock.json` |
| Generator | `gate2-b12-390-v1` |
| Structure | `approved_proposal` |
| Selected repos | 390 |
| Tasks | 390 |
| Files | 390 |
| Source SQLite SHA-256 | `80a9d7136e1423a0c75d681624d47980c020e5774142114029003922ef48d43e` |
| Canonical input hash | `009af8c466a1ebbc507f90d1bfc8e3c8f837984d8b632651273990111bd23535` |
| Projection hash | `e91e982e5baf6bf7d150d55c5144994686e9b817c025fa714359fbcb5c998f28` |
| Warm-ups / measured | 5 / 30 |
| Pure p95 | ~0.092 s (contract ≤ 0.5 s) |
| Full p95 | ~0.104 s (contract ≤ 2.0 s) |
| Refresh instrumentation | 35 full-capture refresh calls (5 warm + 30 measured); source `acceptance_full_capture` |
| Host / runtime | `pop-os` / Python 3.10.12 |

---

## Verification

| Check | Result |
|-------|--------|
| Focused Gate-2 (33–38 + lifecycle + config + sessions + projection contract) | **passed** |
| Full pytest | **580 passed** |
| `python tests/test_replan.py` | **all passed** |
| Ruff | **clean** |
| `git diff --check` | **clean** |

---

## Unrelated dirt (untouched)

- `docs/plans/placement-capacity-pr09-review-restart-primer.md` (untracked operator primer)
- `iscsi-login.sh` (untracked)

---

## Explicit stop

Do **not** mark ready, merge, begin Gate 3, start PR-10, rewrite history, force-push, or re-trigger Greptile. Await human Gate-2 re-review of tip `52322de`.
