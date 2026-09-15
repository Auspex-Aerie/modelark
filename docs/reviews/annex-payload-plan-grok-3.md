# Annex payload Stage 0 — Grok planning/qualification pass 3

Date: 2026-09-13 America/New_York (2026-09-14 UTC).
Reviewer: local Grok CLI 1.0.30, default configured model, not independently reported.
Session: 01a09df9-fa1d-7561-8e23-b29c5cb323b7.
Result: NOT ACCEPT for advancing to Stage 1; exit 0.
Budget: pass 3 of 3 after Codex plan pass 1 and Grok plan pass 2; stop reached.
Scope: read-only local review; no tests, production edits or live archive access by reviewer.

Reviewed input SHA256:
- docs/plans/annex-payload-migration.md: af9292c9e4b3f842591fb49bcfcf390a799cafc540760f428df6fced7ef68109
- docs/acceptance/annex-payload-stage0-2026-09-13.md: 90a719a1fa27363f58f7123957b47853199e8636733a8f3001d386cf125319c8
- scripts/qualify_annex_payload.py: ee31267fe7d679cf6607edff3943b9a9a39d3aad05d42c75ce11ea06df594a59

## Disposition

Reviewer upheld the bounded probe observations, but did not approve Stage 1. Three P1 items are
declared readiness gates, not new live failures; four P2 evidence/wording clarifications remain.
No post-pass fixes or additional review were performed. Only review status/links and this verbatim
record were saved afterward. Further qualification and any support-matrix decision return to the
operator at the three-pass stop. Do not infer approval to narrow the production support matrix
from the reviewer's recommendation alone.

## Reviewer response

**NOT ACCEPT** for advancing to Stage 1.

Planning pass 3 of 3 (budget not reset). The Stage 0 report is largely honest: synthetic `/tmp` only, 13 grouped probes × 2 binaries, `-filter` falsified, newer fixtures ended format 9, `stage_0_complete: false`. Those 13 observations are not invalidated. Missing production guards are not defects here. Remaining **declared** Stage 0 gates still match the plan’s Stage 0 stop (“unresolved semantics before implementation”) and Stage 1 still includes coding map locking. That is the stop.

## What the evidence actually shows

Worktree bytes, lookupkey, and object presence were not treated as the committed representation. After lock/unlock, `qualify_annex_payload.py` 138–141 requires the HEAD blob to contain the annex key and not the payload SHA. That is the correct repair of the `-filter` miss (`annex-payload-stage0-2026-09-13.md` 69–73). Isolation of the hostile `qualification` filter is real (`hash-object --path` plus SHA256 key identity in `add()` 96–101).

Key-only copy leaves no mapped path (157–163). Map publication moves HEAD while the index/worktree stay old, then `read-tree` completes a metadata-only tree (223–232). Slice `_open_content` (`local_source.py` 108–139) and restore `_materialize` (`restore.py` 100–127) read mapped locked/unlocked bytes when the path already exists; they do not exercise `_annex_content` dedicated_local refusal (`restore.py` 51–60, 150–155). Current v8 readers refuse empty catalog v9 without mutating it (`core/db.py` 176–177, 293–298; `slice/catalog.py` 18–37). Newer-client passing probes are format **9**, not mixed-version compatibility.

## P1 — already-declared Stage 0 stops (not new experiment lies)

**1. Annex metadata-ref protocol is still unqualified.** Plan F (`annex-payload-migration.md` 278–279) requires this on both supported versions before coding it. Stage 1’s deliverable includes “map locking/compatibility foundation” (367–370). The script only CAS-tests `refs/modelark/*` and a file-tree HEAD/index split (191–199, 222–232). Report remaining gate 1 (102–106) is still open. Native `update-ref` atomicity is not annex location-log / `git-annex` branch validation.

**2. `filter=annex` is a name, not a pinned command.** Plan A 114 still requires auditing the actual filter configuration. `isolate()` 86–87 writes `filter=annex` and never reads `filter.annex.clean` / `smudge`. Report remaining gate 2 (107–109) and 73 correctly refuse adopting arbitrary live `filter.annex`. Committed-pointer success in this fixture does not pin the helper.

**3. Production client/format matrix is unpinned.** Both inits request format 8 (72–73, 115, 218); final formats are 8 vs 9 (JSON 68–73 vs 145–150). Report 17–20 and remaining gate 3 (110–111) are honest. HYP-003’s test_matrix still says both clients use format 8 (`decision_log.md` 3255); the Results paragraph corrects it. Stage 1 must not put 10.20260717 on live format-8 drives.

## P2 — inaccuracies to fix, not extra Stage 0 scope

- Returning-clone probe never applies a retirement tree to that clone. Preservation is only *before* a sync that `map_tree()` performs on a different repo (`qualify_annex_payload.py` 175–190 vs 203–232). The check name is literally true; it is not returning-clone interception.
- Tracked-dotfile add recorded `index_changed: false` (JSON 23–27). That is a no-op `annex add`, weaker than production `_annex_add` succeeding then returning no key (`fetch.py` 573–576). Markdown 32 does not say the index was unchanged.
- `annex.largefiles` isolation without `--force-large` is unproven (`add()` 96). Do not treat info/attributes `largefiles=anything` as demonstrated if the writer omits `--force-large`.
- Plan handoff 409–410 still says the next step is disposable qualification; Stage 0 has already been partially run.

**Not findings:** unimplemented admission/sync/clean guards, v7/v8→v9 migration, and ownership *authority* (transport only). Call-site audit lines checked: six `annex sync` sites (`fetch.py` 1352, 1354, 1716, 1805; `register.py` 569, 673); one clean INSERT (`drive_mutation.py` 199); `_serial_enrich_locked` stamps `SERIAL_REPAIR_CATALOG_VERSION` unconditionally (`drive_bootstrap.py` 543–555; `catalog_versions.py` 8–11). That stamp cannot run on v9 today because repair still refuses versions outside `{7,8}` (344–346). The downgrade hole opens **when Stage 1 admits v9** — correctly Stage 1 design, already in plan D 177–180 and report remaining item 4.

## Minimum next qualification (then stop)

1. On **installed 8.20210223 / repository format 8 only**, qualify allowlisted annex metadata-ref merge: expected location evidence, unrelated metadata, partial ref/index failure, durable receipt/replay. Do not use the auto-upgrading 10.x binary unless upgrade is disabled and format 8 is re-read after every annex command.
2. Record `git config --show-origin --get-regexp '^filter\.annex\.'` in the fixture; refuse unexpected commands. Keep committed-pointer asserts.
3. Pin the production matrix in the plan/HYP-003: 8.20210223 + format 8. Treat 10.x/format 9 as a separate unsupported client.
4. After that, Stage 1 may start fail-closed guards, schema **>8** with monotonic serial-repair floor, `.gitignore` classification, shared add postconditions, and **guarded mapped-path publication** (key-copy already falsified path creation).

**Common cause:** worktree bytes, committed Git representation, annex metadata refs, and repository format are different planes. The `-filter` miss and the silent 8→9 upgrade are the same mistake: one plane’s success taken as another’s.

Do not enable conversion, live catalog bump, Fill resume, or the newer annex client on existing drives. This pass exhausts the three-review budget; remaining work is further Stage 0 qualification, not another planning review cycle.

