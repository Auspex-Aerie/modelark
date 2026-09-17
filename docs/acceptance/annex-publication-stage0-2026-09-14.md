# Annex publication Stage 0 — protocol qualification continuation

Date: 2026-09-14. HYP-003 / DEC-157. Source HEAD: 4c0c67a678e09a8d70926dbb5e7ed8285416ac93.
Status: Stage 0 ACCEPTED for the pinned initial publication profile. Round 1 recorded 42 bounded
checks; the accepted round-2 revision records 69 passing check results (including repeated pointer
checks), detailed in the continuation below. Production implementation/deployment is not performed.
Review: [round 1 NOT ACCEPT](../reviews/annex-publication-stage0-grok-1.md);
[round 2 ACCEPT, all six findings closed](../reviews/annex-publication-stage0-grok-2.md).
Stopped on acceptance after 2 of 3 permitted qualification rounds. This reviews new experimental evidence, not another
review of the already-accepted architecture or a reset of the completed P2 plan-review cycle.

Operator authorized continuing the remaining Stage 0 experiments after plan acceptance. No production
implementation, catalog/drive mutation, Fill resume, deployment, commit, push or PR was performed.
All changes are experiment scripts and documentation; all Git/annex mutations target freshly made /tmp
repositories. Fixtures are retained. No newer annex binary was invoked in this continuation.

## Reproduction and evidence

The following hashes and 42-check observations describe the **round-1 snapshot**, not the subsequently
revised script. They are retained for the review trail. Current reproduction evidence is below.

Run `scripts/qualify_annex_publication.py` with the project development Python, without `-O`.
It imports the existing fixture utility; it takes no destination arguments. It is an executable
specification and not a migration tool or production authority guard.

- Final run: `/tmp/modelark-annex-payload-stage0-u4pxp6d6/protocol-result.json`.
- Script SHA256: `e117d932ba0b6a2921fdb1ac73a4db49d00dfcf2a586dcbc82125ed028f227d5`.
- Result SHA256: `3ca079e34799d8ca71f4a29d2452d979167e052df758c4ec9b15486d8c0500b5`.
- Full result copied into `annex-publication-stage0-2026-09-14.json`.
- Installed git-annex 8.20210223 / requested repository format 8; Git 2.34.1.
- Exact Git, annex and shell executable hashes are pinned in the script and recorded in the JSON.
- Ruff: `ruff check scripts/qualify_annex_publication.py` passed.

The original two-client representation results remain in `annex-payload-stage0-2026-09-13.md` and its
JSON. This continuation does not rerun or supersede that comparative experiment.

## What the new probes establish

### Profile/configuration

The candidate annex clean/smudge commands are exactly `git-annex smudge --clean -- %f` and
`git-annex smudge -- %f`, from local `.git/config`. Process and required settings must be absent in
this narrow fixture profile. Known namespace attributes preserve `filter=annex` and disable upstream
text, ident, encoding and EOL transformations.

Negative checks reject substituted clean/smudge/process/required settings, an included setting even
with the same command, command-line override origins, a substituted PATH helper, repository format 9,
attribute drift and other effective-config drift. A sentinel command is never executed; original
bytes, HEAD and index remain unchanged. Full effective config values/scopes/origins are sealed by
digest without copying potentially sensitive global configuration into the report.

This is not complete production profile enforcement. The harness overrides hooks/global attributes/
excludes on every command and strips inherited Git routing. Configuration snapshots alone do not make
an unreviewed initial config safe, validate every transitive executable, or close a concurrent-change
window. Production profile establishment/admission remains an implementation gate.

### Returning clone and ownership

The SAME returning clone now validates a synthetic registry-bound manifest, preserves its committed
original bytes, annexes them locally, receives/applies the retirement tree, and reads the mapped bytes
through the existing Slice confined opener and restore materializer in locked and unlocked modes.
Both committed representations are checked against complete pointer strings/grammar instead of a
key substring. The fixture retains the old Git commit and a separate small-file backup.

Wrong library, operation, profile or manifest digest refuses. Invalid key/path/original grammar and
duplicate mappings refuse even with a matching recomputed digest. Pre-existing unowned namespace and
divergent old bytes refuse without adoption/overwrite. An unclaimed copy has only a synthetic
representation receipt; no catalog is opened. The receipt's zero-row field is fixture intent, not an
integration test of real catalog publication or registry authority. Public Slice workflow and complete
source admission are still not exercised by this low-level reader test.

### Native annex metadata and map replay

A metadata-only staging repository contains captured source/map refs and no annex payload. Its native
merge uses `git annex sync --only-annex --no-content --no-pull --no-push --no-commit`, without live
remotes. Only this disposable staging repository runs the merge. The real-map fixture's HEAD/index/
annex ref remain unchanged while the candidate is evaluated. SHA256 location-log records are parsed
as native timestamp/status/UUID records; unknown grammar and ambiguous duplicate UUID clocks refuse.

The candidate adds one physically proved source-drive location, preserves unrelated metadata, and
agrees with native `whereis --json`. Native negative variants reject a new MAP-UUID presence claim,
unknown UUID, unrelated key, drop, changed description and trust. Artificial unknown grammar and
conflicting duplicate/future clock examples also refuse. A repeat native merge is an exact metadata
no-op. Existing-record clock replacement is conservatively refused, not a qualified positive refresh
or absent-to-present update. The current positive example is one new key/location.

A failed second ref CAS leaves both file and metadata refs old. Successful atomic publication followed
by an index lock leaves refs new but index/worktree old and intent pending. Old-path edits and unrelated
extra files refuse. A modeled partial index/worktree (old path retired, mapped path not yet installed)
passes exact per-path old/new validation and converges to the sealed tree without changing unrelated
paths. A forced receipt-write failure preserves the durable intent; reopened intent and reverified
new refs/index/worktree allow writing the receipt without repeating annex merge or transferring payload
to the map. JSON intent/receipt writes flush the file and containing directory.

This is modeled interruption/replay, not a killed-process or power-loss durability matrix. No catalog
obligation, owner/fence check, child-holder recovery or production receipt authority is implemented.

## Retained failures and limits

- First profile/same-clone-only run: `/tmp/modelark-annex-payload-stage0-52ac5tre` (25 checks).
- Expanded run `/tmp/modelark-annex-payload-stage0-pm21q1_z` failed because the harness tried
  `git write-tree` while holding its injected index lock. That diagnostic may itself acquire the lock
  to update cache-tree state. The corrected probe reads unchanged raw index bytes while locked.
- Corrected intermediate run: `/tmp/modelark-annex-payload-stage0-9cuomlmn` (38 checks).
- Final expanded run adds full-config-seal drift, native no-op merge and partial-path replay checks.

Still requiring review/qualification: full supported metadata record-transition/clock cases and source
input safety before invoking annex; complete committed-pointer negative matrix; unsupported initial
config/helper closure; full interrupted-state/receipt binding coverage. Production registry/transaction/
lock/schema integration and real kill/crash tests belong to Stages 1/2, not claims established here.
The report deliberately retains `stage_0_complete: false` pending that assessment.

## Round 2 — qualification corrections and fresh results

Local Grok round 1 found six qualification gaps; its original verdict is preserved. The corrections
below are executable-specification changes within the already accepted architecture, not production
implementation or a new migration scope. No third-party reviewer authorizes deployment.

Fresh run: `/tmp/modelark-annex-payload-stage0-756s6vow/protocol-result.json` — PASS, 69 check results.
Current script SHA256: `94461ff4713a0e706879e4f526f16c45b838fdf720dbfe947d872c0aa5a81587`.
Result SHA256: `dd22360842c6c62258cb81ff25d926f7ba57498d448a75e05baf6943c7da5634`.
Copied result: `annex-publication-stage0-round2-2026-09-14.json`. Ruff and diff whitespace checks pass.
The original representation suite was also rerun on the installed client: 13 grouped checks PASS,
all final repository formats 8, `/tmp/modelark-annex-payload-stage0-gp96bntg/result.json`.

1. **Pre-reader admission:** fetch into a non-annex quarantine ref and inspect its Git blobs before
   installing an annex-recognized ref or invoking the native merge. New config/trust/description,
   unknown UUID/key, map presence and drop variants refuse in quarantine with the native merge call
   count unchanged. Existing registered UUID description lines may be an exact subset of the map
   baseline; changes require separate registration, not implicit adoption during payload publication.
2. **Actual log histories:** parse per-UUID clock history, not one line per UUID. Native absent→present
   merge retains old absence plus new presence; the previous parser wrongly called that ambiguous.
   The candidate must preserve the map's old history and may add only physically authorized positive
   records; equal-clock contradictions, nonadvancing/conflicting changes, new drops and future clocks
   refuse. Native `whereis` independently agrees with existing-key unknown→present and absent→present.
   Native present→present is an exact no-op in this client, not a fabricated clock refresh. An independent
   map file-history fixture now has no common Git ancestor with the source. Its known source UUID and
   baseline metadata are registered in controlled fixture setup before sealing; unknown UUID registration
   remains outside this publication transaction. No generic text merger builds annex records.
3. **Full replay checks:** intent now binds operation, profile/config seal, source refs, exact decoded
   delta/UUID allowlist/key, old/new path manifests, both ref pairs and expected final index. After a
   failed receipt write, replay rereads profile, source refs, final refs/index/worktree, parses the map
   pointer, and makes a fresh native `whereis` query before the receipt. Map content stays absent.
4. **Explicit interruption states:** old index + partially old/new worktree is an admitted modeled
   read-tree interruption. Mixed index and new index + incomplete worktree refuse. Unexpected empty
   directories also refuse. This replaces round 1's too-broad per-field old-or-new rule. Actual SIGKILL,
   file-system power loss, live-owner/fence/DB integration remain Stage 1/2 tests, not new claims here.
5. **Pointer binding:** use pinned native `examinekey --format=${objectpath}` to get the canonical object
   path and compare the entire relative symlink or unlocked pointer. The installed object's directory
   is mixed-case (`Jj/03` in this case), not `hashdirlower`. Validate map pointers as well as physical
   returning copies. Reject wrong keys, suffixes, absolute links, wrong hashdir, traversal, raw Git
   payload blobs and executable modes. Object bytes are required on physical copies, not on the map.
6. **Conflicting versions:** a second manifest entry for the same logical path with different bytes/key
   refuses, even with a recomputed matching registry digest. The versionless storage mapping is not
   silently expanded into multi-version storage.

Additional retained observations:

- `/tmp/modelark-annex-payload-stage0-qu8v8dsc/existing-location-result.json`: the initial parser
  accepted adding another UUID to an existing key with no prior record for that UUID.
- `/tmp/modelark-annex-payload-stage0-diy6a6g4/existing-location-result.json`: native absent→present
  returned two valid locations, while the initial parser refused multiple records for one UUID. This
  independently confirmed the round-1 grammar gap before the correction.
- `/tmp/modelark-annex-payload-stage0-fs20_5zd/protocol-result.json`: the expanded parser passed
  unknown→present and absent→present, then the harness incorrectly expected a new timestamp from
  native present→present. Corrected to assert the observed exact no-op, not force a invented clock.
- `/tmp/modelark-annex-payload-stage0-90fc5aum`: passing intermediate before the explicit native
  config-change quarantine case was added.

The journal/registry/profile values are still synthetic. These probes qualify candidate primitives and
refusal rules; they do not implement catalog authority, production admission/locks, monotonic migration,
child-process recovery or deployment helper closure. The initial migration remains pinned to installed
8.20210223/format 8. The run deliberately kept `stage_0_complete: false` pending review. That original
JSON and its hash are preserved: subsequent Stage 0 acceptance is the separate round-2 review record,
not a rewritten test result. Grok accepted the qualification on round 2 with no remaining P1/P2;
the unused third round is not needed. This does not imply production or deployment acceptance.

## Handoff / next scope

Stage 0 is complete for the pinned initial profile. Implement Stage 1 of the existing plan next:
shared ArchivePublisher evidence/state model, authority adapters, ingestion/classification integration,
maintenance obligations/guards and map locking/compatibility. Conversion remains disabled. The next
implementation scope needs operator authorization; a reviewer's ACCEPT is not that authorization.
Do not expand this into live migration, a newer annex client, catalog changes or automatic Fill resume.

No common architectural redesign is needed beyond the approved boundary. The common qualification
failure was checking native output too late and modeling logs as current rows. The accepted executable
specification now separates Git-only input admission, native merge, independently validated evidence
and replay acknowledgement. Later implementation must preserve those boundaries.

Final local verification: copied round-2 JSON is byte-identical to the retained run, all 34 final
fixture repository formats are 8, both qualification scripts pass Ruff, and `git diff --check` passes.
These verifications were performed locally by Codex; Grok explicitly did not run shell checks.
