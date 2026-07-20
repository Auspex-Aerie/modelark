# Placement & capacity hardening

Working plan for the fix effort opened after the 2026-07-20 placement/capacity audit. This document
is for local review; it will evolve as PRs land. It records **what** we are addressing and the
intended **approach/sequence** — the authoritative problem statements live in the GitHub issues.

## Origin

An operator mid-fill add of `zai-org/GLM-5.2` (BF16, 1.51 TB) exposed a cluster of drift-between-model-
and-reality problems in the capacity/placement subsystem. The immediate incident was resolved
operationally (un-pinning GLM-5.2 by deleting 9 aborted-start stub files; restoring `free_bytes` to
its empty-free contract). This effort addresses the underlying classes so they stop recurring.

## Issues (problem statements in GitHub)

| # | Title | Severity |
|---|---|---|
| [#35](https://github.com/Auspex-Aerie/modelark/issues/35) | Free-space is a mutable field and the recompute is never persisted | P1 |
| [#36](https://github.com/Auspex-Aerie/modelark/issues/36) | Durable-partial pinning is disproportionate (trivial/aborted partials wedge placement) | P1 |
| [#37](https://github.com/Auspex-Aerie/modelark/issues/37) | No retire / un-archive / drive-loss recovery to release pins and re-home | P1 |
| [#38](https://github.com/Auspex-Aerie/modelark/issues/38) | Placement consolidation strands small drives and can wedge large blocks | P1 |
| [#39](https://github.com/Auspex-Aerie/modelark/issues/39) | Mid-fill add has no pre-commit placement/feasibility preview | P1 |

Already deferred on the roadmap, not re-filed: cross-drive shard spanning; multi-RAID copy-#1 home.

## Intended approach (high level — to be detailed per PR)

- **#35 — derive free-space, remove the footgun.** Treat current free as a pure derivation from
  durable facts (`capacity − Σ stored_bytes`, or live `df`), with registration values immutable and
  no writable current-free field to clobber. No persisted current-free means nothing to reconcile.
- **#36 — value-proportional pinning.** Only meaningful committed bytes (e.g. weight content past a
  threshold) pin a model to a drive; sub-threshold/aborted partials are freely re-homable and are
  auto-cleaned on stop/replan. Preserves crash-resume for genuine in-progress transfers.
- **#37 — retire / re-home path.** A supported operation to declare a drive removed/dead, release its
  pins, and re-home its unfinished models; complements DEF-029 from the fill side. Subsumes a safe
  "un-archive a model" primitive (also removes the manual annex/DB surgery we did by hand).
- **#38 — placement that uses the whole fleet.** Best-fit / two-pass (big models to big drives, then
  small models onto small drives) so small drives are used and large blocks are not wedged; make the
  legacy path's drive tiebreak explicit.
- **#39 — mid-fill-add preview.** Show the placement diff + GATE-B re-admission for a proposed add
  before any bytes are written, so an add never silently pins a model to the wrong drive.

Sequencing (proposed, revisit as we learn): **#35 → #36 → #38 → #37 → #39.** #35 makes the capacity
picture trustworthy; #36 removes the acute wedge; #38 improves distribution; #37 and #39 add the
recovery and preview surfaces on top.

## Workflow

- One long-lived fix branch: `fix/placement-capacity-hardening` (off `main`).
- **One PR per issue, targeting the fix branch** (not `main`).
- **Merge commits — no squash. Do not delete merged PR branches.** Keep full history for review.
- Local review at our own pace; the fix branch merges to `main` only when the set is complete and
  reviewed.
- A `DEC-###` decision-log entry will be recorded once the #35/#36 approach is settled.

## Out of scope (tracked separately)

Operational continuation — restarting the fill so GLM-5.2 lands on drive-01, then removing the
mistakenly-downloaded `zai-org/GLM-5.2-FP8` after the BF16 is archived — is independent of this code
effort and proceeds on the operator's cadence.
