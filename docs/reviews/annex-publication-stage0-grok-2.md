# Annex publication qualification — Grok round 2

Date: 2026-09-14. Local Grok CLI; session 01a09eaf-c75c-7d72-be98-d7fdb8df6bed.
Qualification round 2 of at most 3 for this experimental batch. Exit 0; file-only review.
Outcome: ACCEPT Stage 0 for the pinned publication-protocol batch; all six round-1 findings closed,
no remaining P1/P2 blockers. Review cycle stops on acceptance after 2/3; round 3 unused.
Reviewed script SHA256: 94461ff4713a0e706879e4f526f16c45b838fdf720dbfe947d872c0aa5a81587.
Reviewed result: docs/acceptance/annex-publication-stage0-round2-2026-09-14.json (69 check results).

Reviewer acceptance establishes readiness to propose Stage 1, not operator authorization to implement
or deploy it. No production implementation, live mutation, commit, push, PR or Fill action occurred.
The test JSON's stage_0_complete:false is retained as its pre-review state; this separate review record
records the subsequent acceptance. Do not rewrite test evidence or claim later production gates passed.

## Reviewer verdict (verbatim export)

**ACCEPT Stage 0 for this publication-protocol batch.** Round-1’s six findings are closed in `scripts/qualify_annex_publication.py`. Architecture in `docs/plans/annex-payload-migration.md` is unchanged and is not this verdict. `stage_0_complete: false` is a review flag, not a remaining probe gap.

Round-1’s common cause was **post-hoc allowlisting of annex merge output**. Round 2 splits **Git-only quarantine → native merge → independent proof**. That structure now matches the accepted plan’s staging protocol.

## Prior findings

**1. Pre-reader admission — CLOSED.**  
`stage()` fetches into `refs/modelark/quarantine-annex`, runs `admit_input()` on Git blobs, then installs `refs/remotes/sealed/git-annex` and `flush_annex()` (407–428, 386–405). Native config/trust/description/map-presence/unknown-UUID/drop cases refuse in quarantine; `native_merges` is unchanged (487–497). uuid.log may be a line-subset of the map baseline only; extra identity metadata is not adopted at publication.

**2. Location grammar / independent map — CLOSED.**  
`locations()` keeps per-UUID clock history (333–346). `record_delta()` / `metadata_delta()` preserve old clocks and allow only advancing `status==1` additions (348–384). Native existing-key logs in the JSON: unknown→present appends a second UUID; absent→present keeps `0` and adds `1`; present→present is an exact no-op (333–359 in the round-2 JSON). Independent map: `init` + fetch + `commit-tree` with failed `merge-base` (436–447). Source UUID is registered in that setup **before** `old_annex`; unknown UUID registration is not a publication step.

**3. Map replay proof — CLOSED.**  
Intent binds operation, profile/config seal, source refs, decoded delta, UUID allowlist, key, path manifests, ref pairs, expected final index (591–598). After receipt failure: profile, source refs, map `pointer(..., require_content=False)`, fresh `whereis`, annex ref, `write-tree` (677–695). No payload objects; no second merge.

**4. Mixed-state protocol — CLOSED.**  
Index must be exactly old or exactly new (748). New index requires complete new worktree (749–750). Old index may have partial old/new worktree (751–754). Mixed index, extras, and empty dirs refuse (633–651, 725–747). Modeled interruption, not SIGKILL.

**5. Pointer negatives / hashdir — CLOSED.**  
Canonical path is native `examinekey --format=${objectpath}` compared in full (280–289, 268–278). Seven refusals: key, suffix, absolute, hashdir, traversal, raw Git payload, executable (296–313). Map pointers parsed; object bytes required only on physical copies.

**6. Conflicting logical versions — CLOSED.**  
Second same-`original` row with different bytes/key refuses `mapping identity/uniqueness` (222–227; JSON 103–106). Versionless mapping is not widened.

**P1: none. P2: none** that still block Stage 0. Quarantine `unselected-key` refuses as `new MAP UUID presence claim` (JSON 211–214) because that native input used `setpresentkey` on the map UUID; post-merge still refuses unselected keys (374). Existing-key histories use clone maps (543–546); the independent-map path is the new-key publication case. Neither reopens the round-1 holes.

## Not qualification defects

Synthetic registry/journal, harness `-c` isolation, production authority/locks/schema, helper-closure as an admission service, process-kill/power-loss, live migration, USB/archive tests. These are Stage 1/2 implementation of this spec (`not_proven` 407–412). Do not recode them as fixture bugs.

## Bounded accuracy

Copied `docs/acceptance/annex-publication-stage0-round2-2026-09-14.json` has 69 check `passed` results (repeated pointer checks included) and the same `details.metadata` OIDs/`not_proven` tail as `/tmp/modelark-annex-payload-stage0-756s6vow/protocol-result.json`. Check names and refusal strings match the script. Round-2 narrative (acceptance md 111–172) matches the revised code; lines 35–109 are the labeled round-1 snapshot. **Hashes, ruff, and the 13-check representation rerun were not independently verified** (no shell; hashes not required for this logic review).

## Stage 0 readiness

**ACCEPT authorizing Stage 1** of the existing plan (publisher/adapters/guards; conversion still disabled). Passing 69 is not itself acceptance; closure of the six Stage 0 gates is. Pin remains installed 8.20210223 / format 8. Next work is implementing this executable spec, not another probe expansion unless Stage 1 design hits an unspecified native transition.
