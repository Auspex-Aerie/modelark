# PR-09 / #39-B — Gate 2 **findings 31–32 / B7 / B8 refresh / B12 handback**

**Date:** 2026-07-26  
**Status:** Gate 2 **remediation complete — not accepted**. Gate 3 **not authorized**.  
**Withheld tip:** `318d319eaee0bf44111a5543f0ee36f9eaca5eff`  
**Remediation tip:** set in commit message / `git rev-parse HEAD`  
**Draft PR:** https://github.com/Auspex-Aerie/modelark/pull/54  
**Phase branch:** `fix/placement-capacity-pr09-execution-projection`  

---

## Findings addressed

| # | Finding | Fix |
|---|---------|-----|
| **31** | Worker writes hit FILL_SESSION_ACTIVE; dual drive locks | `RunCtx.write` → `session_write` with token; graph_write allows `_SESSION_WRITE_DEPTH`; removed session-level `inherit_drive_fence_fds` (envelope FDs only) |
| **32** | Session not terminalized; fences held | Every execute outcome terminalizes session in `finally`; fence release in `finally`; heartbeat each batch |
| **25/B7** | Compression/numcopies unbound; baseline loss | Draft binds full config as `derivation_mode=ecfg:<hash>`; start compares frozen hash; baseline archive missing refuses |
| **B8 refresh** | Fabricated files; no project_pure refresh | Approved `proposal_files` authority; re-run `project_pure` at batch boundary |
| **27/B12** | Synthetic empty bench; self-auth; soft thresholds | Catalog/approved structure; pure without SQLite re-read; operator identity required; threshold raises Refusal; evidence export fields |

## Verification

| Check | Result |
|-------|--------|
| Full pytest | **572 passed** |
| `python tests/test_replan.py` | all passed |
| Ruff | clean |

## Explicit stop

Do **not** mark ready, merge, or begin Gate 3. Await human Gate-2 disposition.
