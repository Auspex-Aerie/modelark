# PR-09 / #39-B — Gate 2 **remediation handback** (await human review)

**Date:** 2026-07-26  
**Status:** Gate 2 **remediation complete — not accepted**. Gate 3 **not authorized**.  
**Withheld tip:** `1a9485c`  
**Remediation tip:** `9523ca962e56095dea4cf41cc83ae70838cf1854`  
**Draft PR:** https://github.com/Auspex-Aerie/modelark/pull/54  
**Phase branch:** `fix/placement-capacity-pr09-execution-projection`  
**Base:** `bc33a0664d3e65e20c6843b0a9d5b1204d15502a`  
**Issue:** #39  

---

## Withhold findings addressed

| # | Finding | Disposition |
|---|---------|-------------|
| 1 | B8 hard cut incomplete (legacy fallback, double entry, re-home) | `fill.execute` refuses without session; no exception swallow; no re-home on capacity; portal enters `start_fill` once and passes `session_start` into execute |
| 2 | Fabricated session authority | `production_services()` uses real `drive_fence`, clock, plan config, capacity evidence; `start_session` loads catalog drives/archived/evidence, sets lease `expires_at`, supports worker claim |
| 3 | B9/B10 in-process only | OS-visible flock child fence FDs + marker locks; real datetime expiry; token CAS UPDATE; dirty generations preserved |
| 4 | B12 measurement skipped | Distinct input vs projection hashes; requirement/task counts from proposal_tasks when present; wall-clock runs 5 warmups + 30 measured pure projections |
| 5 | Cold process bypassed production | Installed `modelark session start` CLI; portal `fill_api.start` without service monkey-patch |
| 6 | register_drive guard test-shaped | Live-session guard on normal device path before SMART/physical work |
| 7 | CI script-runner red | Replan bridge installed under `python tests/test_replan.py`; capacity terminalizes without re-home |

## Explicit non-goals (honored)

- No ready/merge; Gate 3 unauthorized  
- No production multiprocessing / fork-spawn selection  
- Forward cleanup only (no history rewrite) for accidental Gate-1 handback / DEF-032  

## Verification (implementer)

| Check | Result |
|-------|--------|
| Full pytest | **562 passed** |
| `python tests/test_replan.py` | all passed |
| Gate-1 + Gate-2 focused | 48 passed |
| Ruff (touched) | clean |

**Await Gate-2 human disposition.** Do not mark ready or merge.
