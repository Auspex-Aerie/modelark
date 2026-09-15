# ArchivePublisher architecture — Grok CLI pass 4 (explicit exception)

Date: 2026-09-14.
Reviewer: installed Grok CLI 1.0.30, default configured model (not independently reported).
Session: 01a09e90-6397-7d82-9539-9f1314ae7d93.
Scope: operator-authorized architecture-plan amendment and ONE additional local review after pass 3.
No review-budget reset, implementation, tests, live catalog/archive access, PR or push.

## Outcome

- Architecture plan: ACCEPT as the basis to finish Stage 0 qualification.
- Stage 1 readiness: NOT ACCEPT; missing qualification results remain missing.
- No P1 design defects; four P2 clarifications to amend before Stage 1 coding.
- The four P2 items remain OPEN. After review only status/links and this record were changed.
  No silent correction or fifth review was run.

Reviewed input SHA256:
- docs/plans/annex-payload-migration.md: c66170e64e146316d57389d1c1eb4d11e20cc406924d92fca921ebaea77659d7
- docs/acceptance/annex-payload-stage0-2026-09-13.md: 0a83d6af88a6aef4128801f5d3b135aa361e51dbf7e8cde9e17fb482ba4e9e4a

The first headless invocation exited 0 after reading/hashing the files but returned no verdict.
The SAME session was resumed solely to emit its assessment from already-read evidence, with no further
tools or file reads requested. The resumed invocation exited 0 with the verdict below. This was completion
of pass 4, not a separate fifth review. File/line references below are the reviewer's original references;
some combine plan and acceptance-report ranges and should be resolved against the recorded input.

## Reviewer response (verbatim)

**A. ACCEPT** the architectural plan as a basis to finish remaining Stage 0 qualification.

**B. NOT ACCEPT** Stage 1 readiness. Required Stage 0 results are still missing. DEC-157 and plan pinning are not those results. Historical 13×2 probes remain historical.

This is pass 4 of 4 (one extra authorized pass after the three-pass stop), not a new budget. No files were edited.

## What the amendment actually closed

Pass 3’s three P1s were **qualification/policy holes**, not missing publisher invariants. The 2026-09-14 text now states the contracts those holes needed:

1. **Metadata-ref protocol** (`docs/plans/annex-payload-migration.md` 178–200, 392–415): isolated staging from captured OIDs, allowlisted location deltas, independent semantic check, persist old/new OIDs, CAS refs, then explicit index/worktree; a moved ref with a stale index is unfinished.
2. **Filter name ≠ helper** (150–167): origins, `filter.annex.clean/smudge/process/required`, helper resolution; unknown commands refused **before** execution; `filter=annex` retained, `-filter` rejected; `--force-large` is scoped and mandatory.
3. **Client/format pin** (142–154; DEC-157): initial rollout is installed **8.20210223 / format 8**. 10.20260717/format 9 is comparative only. Format is re-read after mutating commands; auto-upgrade is forbidden on a live archive.

Pass 3 P2 wording is corrected in the plan and in `docs/acceptance/annex-payload-stage0-2026-09-13.md` 36–50, 41–42, 480–483, 548–551: same-clone retirement, no-op add ≠ missing-key publication, `--force-large` not treated as attribute-only proof, handoff is remaining qualification not a fresh Stage 0.

HYP-003 stays **open**. Its test_matrix still describes requested format 8; Results plus DEC-157 record observed 8 vs 9. That is honest ledger, not mixed-version approval.

## Architecture (requested surfaces)

Phase-specific proofs (`LocalPayloadProof`, `CommittedTreeProof`, `MapPublicationProof`, `PublicationRecord`, 87–97) cannot authorize one another. Resume inspects bytes, catalog pair, refs, index, worktree, and profile against sealed old/new state; it does not rerun a shell (115–118). A receipt is a check record, not cryptographic trust (97–98).

Durable obligation is catalog-visible, created before mutation, bound to the captured owner/generation, and cleared only by explicit closure (101–102, 293–331). Ordinary recovery, including terminal-owner and sessionless paths, cannot inventory-clean past it. Current `_recover_owned_generation` / `_publish_anchor_locked` holes remain **implementation targets**.

Authority adapters (128–134) preserve Fill’s session token, `session_write`, and owned generation; they forbid per-file approval supersession, a competing generation, and forcing Fill through `graph_write`’s idle-session path. Migration uses D’s maintenance/terminal ownership and revokes affected approvals at admission (317–336, 449–450). `archived` is already per `(repo, rfilename, drive_label)` (`slice/catalog.py` 63–67), so Drive 01 CAS does not rewrite other drives’ mappings.

Slice/restore stay readers under existing fences; they do not take map/controller locks in reverse order (136–139, 107–108 of the acceptance audit). Shared sync/clean/add sites stay the integration boundary, not a parallel migration writer.

Inspected source only to confirm those existing shapes (`catalog_versions.py` 8–11; `fetch._annex_add` 573–576; `drive_bootstrap._serial_enrich_locked` 543–555; `restore._annex_content` 51–60). Unwritten Stage 1/2 code is not a design defect.

## P1 design defects

**None.** Remaining Stage 0 items are declared experiments (plan 405–406, 222–226, 503–505; acceptance 110–121): exact format-8 metadata merge/failure matrix, recorded `filter.annex.*` commands, portable-ownership negatives, same-clone replay. Do not treat those as missing architecture.

## P2 — amend before Stage 1 coding; does not block the remaining probes

1. **Phase grain vs adapters** (99–118 vs 128–134 vs E 351–379 vs F 445–447). The six-phase list mixes per-file publication, map publication, and drive closure. **Amendment:** Fill per-file work ends at `CATALOG_PUBLISHED` (F-protocol only at existing sync sites when location metadata changes). Fill does not publish a clean anchor or release the generation. `CLOSED` is generation/maintenance closure. The in-progress publisher/session is the sole performer of remaining ref/index/worktree steps (199–200 must not block its holder).

2. **`selected-clone` vs offline pending** (112–113 vs 414–415). **Amendment:** closure waits on clones named in this apply/receipt. Registered-offline clones stay independently pending and must not gate Drive 01’s clean.

3. **Nested write** (134 vs E 363–367). **Amendment:** the archived+replicas CAS runs inside the adapter’s already-open authorized transaction; `BEGIN IMMEDIATE` only if the adapter is not already in one.

4. **Map location wording** (183–185 vs 406–407). **Amendment:** forbid a whereis for the **map UUID**; allowlisted drive-UUID location-log deltas remain required.

## Common cause

Pass 3’s cause still holds as an empirical fact: worktree bytes, committed Git representation, annex metadata refs, and repository format are different planes. The publisher is the design response (typed proofs + pinned profile). What remains is measuring annex merge/filter/format behavior on **8.20210223/format 8**, not another missing shared invariant.

Do not enable conversion, live catalog bump, Fill resume, or the newer annex client. Next execution step after operator direction: finish those targeted Stage 0 probes. Stage 1 coding stays gated on recorded results, not this ACCEPT.
