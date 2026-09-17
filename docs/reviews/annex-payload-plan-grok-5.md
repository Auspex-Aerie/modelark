# ArchivePublisher P2 follow-up — Grok round 1 / cumulative pass 5

Date: 2026-09-14.
Reviewer: local Grok CLI 1.0.30; default configured model (not independently reported).
Session: 01a09e90-6397-7d82-9539-9f1314ae7d93, resumed with a fresh follow-up review request.
Exit: 0.
Authorization: operator explicitly requested folding the four P2s and up to three review/fix rounds.
This is follow-up round 1 of maximum 3, cumulative pass 5. Stop on acceptance: rounds 2 and 3 not used.

## Outcome

ACCEPT the revised architectural plan. All four pass-4 P2s CLOSED; no remaining P1/P2 design findings.
Stage 1 readiness remains NOT ACCEPT because Stage 0 qualification is unfinished.
No tests, implementation, live catalog/archive changes, Fill resume, PR, commit or push.

Reviewed plan SHA256:
44158d9d60f5fb4757f463c07ae98972f99468db9efd8dea92d21f581328f84d

After the verdict only status/links and this record were updated. The reviewed design body is unchanged.
Prior pass-4 review is preserved verbatim; this record closes its findings rather than rewriting it.

## Reviewer response (verbatim text; Markdown hard-break whitespace normalized)

**ACCEPT** the revised architectural plan (follow-up round 1 / cumulative pass 5). Prior pass-4 ACCEPT does not cover this text; this verdict does.

**Stage 1 readiness remains NOT ACCEPT.** Unfinished Stage 0 qualification is unchanged. This fold is plan-only; the historical probes are not new results.

## Prior P2s

**1. Phase grain vs adapters — CLOSED.**
102–126 split per-file (1–4), batch `PROPAGATED` (5), and generation `CLOSED` (6). Fill’s publisher stops at `CATALOG_PUBLISHED` and does not sync the library, drop the generation, or publish a clean anchor (113–114, 125–126, 497–498). Hierarchical receipts bind file → batch → generation (95–98, 117–121). The verified owner may finish only sealed remaining steps; that is not a skip-guard (227–232, 363–368, 536–537). F-protocol stays at the existing sync sites when tree/location metadata changed; a no-change path needs a verified no-op receipt (117–119, 202).

**2. Selected vs independently pending — CLOSED.**
The apply/receipt freezes participants; closure waits on that set plus the central map (128–131, 434–442, 489–497). Other registered/offline clones keep layout obligations and are not Drive 01 closure gates. A selected participant that detaches still blocks that batch and must not be rewritten as “just pending” (131, 538–539). Missing selected members stay blockers (319, 131).

**3. Nested write — CLOSED.**
The adapter owns BEGIN/COMMIT/revision/session-marker; `con.in_transaction` is not authority; the CAS callback does not begin, commit, roll back, or bump on its own (152–157, 400–407). An unrelated open transaction is refused. One bump and one commit for the owning transaction; failure rolls back facts, marker, and session revision (540–541).

**4. Map UUID vs drive UUID — CLOSED.**
Allowlisted drive-UUID location-log deltas in the map are required; a new positive payload-presence claim for the **map UUID** is refused. Compare by UUID, not by the repo that held the log (207–214, 449–451, 542–543).

## Remaining design findings

**P1: none. P2: none.** Do not treat unwritten Stage 1/2 code as a plan defect.

Declared Stage 0 work is still open and is not a design hole: pinned 8.20210223/format-8 filter commands, committed-pointer grammar, staged metadata merge/failure matrix, portable ownership, same-clone replay (255–276, 220, 448–449, 560).

No conversion, catalog bump, Fill resume, or newer annex client is authorized. Next execution step after operator direction remains those targeted probes, not Stage 1 coding.
