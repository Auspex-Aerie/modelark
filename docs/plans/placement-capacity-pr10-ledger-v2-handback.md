# PR-10 docs handback — ledger passback v2 (four blocks)

**Date:** 2026-07-27
**From:** implementer
**To:** human reviewer / operator
**Implements:** `/home/phaze/PycharmProjects/modelark-pr10-ledger-passback-v2.md`
**Supersedes v1 passback** (not appended)

## Tips

| Item | Value |
|------|--------|
| **Parent (prior tip)** | `0ddbc8398ee7358863ee5b03eccfd8db4921b4fd` |
| **This commit** | docs-only after parent; names only the parent above (no self-SHA) |
| Gate-2 code tip (unchanged acceptance) | `82e3f901bcc0b2b5c7e2250d046b18de8552bb0e` |

## Appended (four blocks only)

| Entry | Action |
|-------|--------|
| **INC-023** | One dated UPDATE line (supersedes symptom/planned_remediation; 47 complete; formats 77/51/36/6; must not remediate before INC-024) |
| **INC-024** | New — four unfiltered reads; capacity 15.58 vs 11.19 TiB; 47 vs 382; 35 replica stalls |
| **INC-025** | New — executor `task_manifests` / `as_fetch_record` / fail-closed; 278 FETCH reach seam |
| **DEF-033** | New — verifier policy-error false-clean / false-suspect |

## Not appended (operator call)

| Entry | Status |
|-------|--------|
| **DEF-034** (`derivation_mode` CHECK → v7 artifact) | **Held** — §7 of v2 passback; not authorized without explicit operator yes |

## Verification

| Check | Result |
|-------|--------|
| `git diff --name-only 0ddbc83..<tip>` | `docs/decision_log.md` only (plus this handback if committed same tip) |
| Production / tests | unchanged |
| DEF-034 in ledger | absent |
| Fixture / operator dirt | untouched |

## Implementer spot-checks before append

- 325 primary + 104 replica = 429; all 47 partials primary; 325−47 = 278
- Replica source-ready 29 wide / 64 planned-only → 35 stalled
- `proposal.py` absent on `origin/main`
- AttributeError on SimpleNamespace `as_fetch_record` reproduced

## Stop

No production. Gate 3 unauthorized. No DEF-034. No charter work.
Re-pin Gate-2 acceptance to the new tip if docs tip moves.
Await operator call on DEF-034 and next production scope (INC-025 first).
