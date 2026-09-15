# Annex publication qualification — Grok round 1

Date: 2026-09-14. Local Grok CLI, session 01a09eaf-c75c-7d72-be98-d7fdb8df6bed.
Qualification round 1 of at most 3 for the new experimental batch, separate from the completed
architecture-plan follow-up. The first invocation stopped without a verdict after a cancelled shell
check; the same session was resumed to deliver its final verdict without further tools. Both exited 0.

Outcome: bounded report accurately describes its limits; Stage 0 readiness NOT ACCEPT.
Findings: pre-reader input admission; native location histories/independent-map coverage; complete
map/replay proof; explicit mixed-state protocol; pointer negatives/hashdir; conflicting logical versions.
Reviewed script SHA256: e117d932ba0b6a2921fdb1ac73a4db49d00dfcf2a586dcbc82125ed028f227d5.
No production changes or live mutations. Subsequent corrections remain fixture-only.

## Reviewer verdict (verbatim)

**Stage 0 is not complete enough to authorize Stage 1.** Architecture acceptance in `docs/reviews/annex-payload-plan-grok-5.md` is unchanged and is not qualification. The 42 passing checks are a thin, fixture-shaped slice of the protocol the Stage 0 stop gate still requires.

The common gap is not isolated nits: the harness admits **fixture-generated native output after the annex/Git reader has already run**, then allowlists a **one-new-key / one-line / clone-related** merge and **modeled** replay. It does not admit untrusted annex input first, and it does not qualify the record transitions a real map will emit.

## P1

**1. Fetched annex branch is merged before any tree/grammar admission.**  
`stage()` copies the map ref, fetches the source annex into `refs/remotes/sealed/git-annex`, then immediately runs `git annex sync --only-annex --no-content --no-pull --no-push --no-commit` (`scripts/qualify_annex_publication.py` 331–350, 279–284). `qualified()` only seals the staging repo’s own config (63–94). `metadata_delta()` runs only after that merge (380–391).  
The plan requires inspecting format/profile before annex use, and treating post-command detection as not prevention (`docs/plans/annex-payload-migration.md` 176–180, 206–223). The continuation report already lists “source input safety before invoking annex” as open (`docs/acceptance/annex-publication-stage0-2026-09-14.md` 100–103). Isolation from the **real map** is shown (387–389); that is not pre-admission of the **source annex tree**.

**2. Location-log grammar and transition table are not Stage 0–complete.**  
`locations()` keeps one record per UUID and refuses a second line as ambiguous (296–307). `metadata_delta()` allows only new `status==1` rows, forbids clock replacement, and treats any non-`SHA256-s…log` path as unrelated (309–329).  
The only native positive merge is: map cloned from source (353–363), then one new key with one new location (370–391, JSON `details.metadata.delta`). Repeat merge is a no-op on that same candidate (416–424). Unknown grammar and conflicting clocks are concatenated bytes, not annex output (409–415). Native drop/presence used `setpresentkey` (393–407); the JSON drop refusal is `"drop claim"` (157–159), i.e. a single `status==0` line, which is the 327 branch — I did not read the drop fixture’s log bytes.  
That does not qualify append-only logs, adding a drive UUID to an **existing** key log, absent→present on a pre-existing log, or first publication onto an **independent** map (uuid.log / remote.log / activity.log). Because the map is a clone of the source, the source UUID is already in the inherited annex identity set; a production map is not. A parser this strict can refuse real merges, or a later implementer can widen it without evidence. Plan 223 and 451–452 still require the pinned-profile merge/failure matrix before coding.

**3. Map replay does not bind the proof the plan names.**  
After receipt-write failure, replay checks HEAD, annex ref, `write-tree`, and worktree paths only (515–524). It does not reread effective annex locations. Whereis is checked once **before** the injected receipt failure (499–502). Plan 229–230 requires rereading refs, index, worktree, **and** annex locations before `MapPublicationProof`.  
The published map tree is never passed through `pointer()` (261–277). Map completion only requires a symlink and path hashes (496–497, 526–534). `pointer()` runs only on the returning clone (254, 258). Plan 111–113 / `CommittedTreeProof` require a parsed pointer bound to that key/path after the allowed tree delta.

## P2

**4. `replay_paths` is an old-or-new subset, not a mixed-state protocol.**  
551–563 accept a path if worktree **or** index equals old **or** new, including absence (`None` matches the state that lacks the path). Index-new / worktree-old on the same path would pass. Empty extra directories are invisible to `working_paths` (536–548). Intent stores OIDs, path hashes, and `delta_sha256` only (446–448); it does not bind profile, operation, allowlisted UUIDs, decoded delta, or expected final index. Partial-path and index-lock cases are modeled as disclosed, not a kill matrix.

**5. Pointer negatives and hashdir binding are missing.**  
Locked fullmatch allows any `[A-Za-z0-9]+/[A-Za-z0-9]+` hashdir (270–274); it does not bind annex `hashdirlower`. Unlocked is an exact `/annex/objects/{key}\n` string (276). No negative cases (wrong key, extra suffix, absolute link, raw payload as `100644`, bad mode). The report already lists a complete pointer negative matrix as open (100–102). I did not independently check that `Jj/03` in the JSON pointer is the correct hashdir.

**6. Ownership negatives listed in the 2026-09-13 gate are mostly present; conflicting versions are not.**  
Wrong library/operation/profile/digest, invalid key/stored/original with a matching digest, duplicate mapping, unowned namespace, divergent bytes, and unclaimed zero catalog rows are in 165–229. Plan 273’s conflicting versions for one logical file are not exercised; this schema derives `stored` from the logical SHA, so two versions cannot appear as two rows.

## Accuracy of the bounded report

**The JSON is accurately reported as a bounded probe record, not as Stage 0 closure.**

- `docs/acceptance/annex-publication-stage0-2026-09-14.json` matches `/tmp/modelark-annex-payload-stage0-u4pxp6d6/protocol-result.json` in the copies I read (same 42 `passed` names, `stage_0_complete: false`, same OIDs/delta/pointer strings).
- Disclosures match the script: modeled interruption (515–516), conservative clocks (416–418), one-new-key positive (73–78), incomplete production profile/authority (45–48, 88–89), `not_proven` list (255–260).
- Check names correspond to the functions above; refusal strings I could map (`drop claim`, `unrelated metadata change: uuid.log`, `filter origin`) match `require()` messages.

**Not independently checked** (shell was cancelled): script SHA256 `e117d932…`, result SHA256 `3ca079e3…`, ruff, `/usr/bin/git-annex` bytes vs the pinned digest, on-disk annex trees vs JSON OIDs, uuid.log contents, and whether any production file was dirty.

Do not treat 42 passes as the remaining gates. Do not treat missing Stage 1/2 production guards (authority adapters, process-kill durability, live registry/locks, helper closure as admission) as fixture bugs; those are correctly out of this batch.

## Stage 0 → Stage 1

**NOT ACCEPT.** Close P1s with new retained probes, still disposable `/tmp` only: inspect/allowlist the fetched annex tree **before** `flush_annex`; native transitions on an **independent** map (existing key + new drive UUID, append-only clocks, what uuid.log actually does); pointer parse on the published map tree; receipt replay that rereads locations. Until those exist, Stage 1 coding is not authorized.
