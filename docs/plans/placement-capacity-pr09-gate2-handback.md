# PR-09 / #39-B — Gate 2 **findings 24–27 remediation handback**

**Date:** 2026-07-26  
**Status:** Gate 2 **remediation complete — not accepted**. Gate 3 **not authorized**.  
**Withheld tip:** `bd5f7fd`  
**Remediation tip:** `34829db03d3c0e16b559b382df43596dfc22f86b`  
**Draft PR:** https://github.com/Auspex-Aerie/modelark/pull/54  
**Phase branch:** `fix/placement-capacity-pr09-execution-projection`  
**Base:** `bc33a0664d3e65e20c6843b0a9d5b1204d15502a`  
**Issue:** #39  

---

## Findings 24–27

| # | Finding | Fix |
|---|---------|-----|
| **24 B8** | Projection never executed; `_reconcile`/optimizer still called | `fill.execute` drains `session_start.projection.tasks` only; no `_reconcile` / `reconcile_plan` / `plan_capacity`; success-path hard-cut regression |
| **25 B3/B7** | Self-referential authority; non-atomic token; claim swallow | Catalog manifests/semantic/config recompute; token+INSERT in one `BEGIN IMMEDIATE`; claim fail-closed; drift + TX rollback regressions |
| **26 B9–B10** | Recovery fences unused; label keys; CAS without expiry | `inherit_drive_fence_fds` from execute with fingerprint/epoch keys; recovery re-validates expiry under locks and CAS on `expires_at` |
| **27 B12** | Synthetic empty benchmark | Full capture + pure 5+30 timings; full/pure p95; evidence export path |

## Verification (implementer)

| Check | Result |
|-------|--------|
| Full pytest | **567 passed** |
| `python tests/test_replan.py` | all passed |
| Focused Gate + hardcut | green |
| Ruff | clean |

## Explicit stop

- **Do not** mark ready / merge  
- **Do not** begin Gate 3  
- Greptile iteration follows push  

**Await Gate-2 human disposition.**
