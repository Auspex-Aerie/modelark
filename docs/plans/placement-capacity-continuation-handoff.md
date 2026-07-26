# Placement/capacity hardening — continuation handoff

**Snapshot:** 2026-07-25 (PR-08 Gate-2 amendment for findings 38–43; stop for re-review)  
**Purpose:** preserve remaining PR cuts and the review/implementation protocol.  
**Authority:** operator instruction → DEC-049 / RFC-002 → issue contracts →
`placement-capacity-hardening.md` → `placement-capacity-implementor-handoff.md` → this file → code.

Operational handoff only — not a new product requirements source.

## Current position

| Item | State |
|---|---|
| Integration branch | `fix/placement-capacity-hardening` |
| Integration tip (PR-07 merge) | `03afbdcc6cd0143d839e7665354e5a3669c981bc` |
| **PR-08 phase branch** | `fix/placement-capacity-pr08-proposal-control` |
| **PR-08 tip (this stop)** | **`b76e992` base + findings 38–43 amendment (push after commit)** |
| Draft PR | [#53](https://github.com/Auspex-Aerie/modelark/pull/53) → hardening |
| Last human review | Gate-2 withheld at `b76e992` with findings **38–43** |
| **Status** | Amendments applied; **stop for Gate-2 re-review**. Do not merge. |
| Worktree dirt (do not touch) | modified `.gitignore`; untracked handoff/passoff docs |

Never stage, restore, stash, rewrite, or commit operator `.gitignore` unless asked.

## What has landed (integration)

| Logical cut | Merged PR(s) | Outcome |
|---|---|---|
| PR-01 … PR-07 | #41–#52 | Through lifecycle × eligibility |
| **PR-08 / #39-A** | **#53 (open draft)** | Proposal control tables, hash, CAS, writers |

## PR-08 Gate-2 findings 38–43 (this amendment)

| ID | Fix |
|---|---|
| **38 / A6** | Fence keys refuse missing fingerprints; default evidence via `admission.execution_evidence` under held fences (no silent omit). |
| **39 / A10** | Baseline certificates computed, persisted, hashed, reloaded, revalidated on approve. |
| **40** | Replica executable tasks set `source_drive` from primary/baseline or prior target. |
| **41 / A3** | `drive_mutation()` dirty-advance and clean-anchor transactions call `bump_revision`. |
| **42 / CI** | v4 suite final pins → `db._SCHEMA_VERSION`; fence owner allows `proposal.py` for `drive_fence`; RunCtx fixtures use real connections. |
| **43** | Selection mutation + revision under `data._lock`; summary uses unlocked `con` path (no nested lock). |

## Explicit stop

- **Do not merge.** Gate-2 re-review required.
- Production only within PR-08 frozen scope; no PR-09 session / PR-10 debt.
- Operator dirt and untracked docs remain local.
