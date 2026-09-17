# Annex payload Stage 0 — bounded qualification and call-site audit

Date: 2026-09-13 (operator timezone); HYP-003 / DEC-156.
Historical status: PARTIAL QUALIFICATION on 2026-09-13. Do not enable conversion or deploy from this report.
Current status: the [2026-09-14 continuation](annex-publication-stage0-2026-09-14.md) closed the
pinned Stage 0 gates and received Grok round-2 acceptance. The original two-client results and
limits below remain historical evidence; production implementation/deployment is still separate.
Review status: [Grok pass 3 NOT ACCEPT for Stage 1](../reviews/annex-payload-plan-grok-3.md).
The three-pass stop was reached. Probe observations were upheld. The 2026-09-14 operator-authorized
plan amendment (DEC-157) corrects interpretation wording below; original review/JSON results remain
unchanged. One additional design review is authorized; no new qualification results are claimed.
That review is now complete: [pass 4 accepts the architecture, not Stage 1 readiness](../reviews/annex-payload-plan-grok-4.md).
The four pre-coding P2 clarifications were subsequently folded into the plan and closed by
[Grok follow-up round 1 / cumulative pass 5](../reviews/annex-payload-plan-grok-5.md), with no remaining
P1/P2 design findings. The qualification gates and historical test results below remain unchanged.
Source HEAD: 4c0c67a678e09a8d70926dbb5e7ed8285416ac93.

Continuation: [2026-09-14 protocol qualification](annex-publication-stage0-2026-09-14.md) records
new same-clone, profile, ownership and staged metadata/replay probes. The historical observations
below are preserved; use the continuation for current evidence and remaining boundaries.

## What passed, and what that means

The final experiment passed 13 grouped checks on each of two binaries (26 check results, not 26
independent scenarios). Everything used newly created synthetic repositories and catalogs in /tmp.
No archive mount, live catalog, application CLI, controller, service, Fill, weight download or GPU was used.

| Binary | Final annex repository format | Retained final run |
|---|---|---|
| System git-annex 8.20210223 | 8 | /tmp/modelark-annex-payload-stage0-77w0oppc |
| Standalone 10.20260717-g0c917920c80ab1e8cc3d8f5886537708949e1659 | 9 | /tmp/modelark-annex-payload-stage0-uggdwiio |

Both runs requested format 8 at init. The newer client subsequently upgraded its disposable repositories
to 9. The final report records observed versions, not just init arguments. This is NOT evidence that the
new binary preserves format 8, nor that these binaries can safely share a live library. The system binary
and installed ModelArk were not upgraded. Full machine-readable results: annex-payload-stage0-2026-09-13.json.

Passed probes:

- Negative control: neutral names alone still inherit upstream filters/encoding/EOL policy.
- Scoped .git/info/attributes overriding the namespace with **filter=annex**, -text, -ident,
  !working-tree-encoding, !eol and explicit SHA256/largefiles policy preserves original bytes.
- Five synthetic logical names (control files, nested hidden files, odd Unicode/control characters)
  map to neutral names; exact content hashes and stored objects survive lock/unlock. This does not
  qualify every logical filename for public Slice acceptance.
- The committed tree contains annex pointers, not raw payload blobs. Verify this after commit.
- The tracked control-file annex-add probe was a no-op (index_changed=false) and returned no key.
  This is not a full test of production _annex_add or missing-key failure after a new publication.
- Key-only copy gives the destination an object but no mapped filename.
- A dedicated ownership-manifest ref can be fetched without replacing a clone's file tree.
  This checks transport/digest only, not a production trust/ownership validator.
- The returning clone preserves old bytes locally. The retirement tree was applied to a DIFFERENT map
  fixture, not to that returning clone: interception plus full convergence on the same clone is unproven.
- Expected-old ref CAS rejects stale state; failure of a second ref update leaves the first unchanged.
- The actual Slice confined opener and restore materializer read mapped locked/unlocked source bytes.
  This is not the complete preview/approve/project workflow or its authority checks.
- Exact synthetic map tree delta is checked; a ref can advance while the index/worktree remains old,
  then read-tree can complete publication. No annex payload object was transferred into the map.
  This is an interrupted-state model, not a kill/power-loss durability test.
- The annex-add probes use --force-large. Attribute-only largefiles isolation without that option was
  not tested; the revised publisher contract requires scoped --force-large rather than assuming it.
- Current core read-only, core read/write and Slice readers refuse a synthetic catalog v9 without
  changing its bytes. This does not implement or qualify a v7/v8 migration.

## Reproduction and package provenance

Run scripts/qualify_annex_payload.py with the development Python. It accepts no archive/catalog
destination, always creates a new /tmp directory and retains it. --annex-directory selects the isolated
standalone package in subprocess PATH only. Git-routing environment variables are scrubbed to prevent
inherited GIT_DIR/index/object routing from escaping the fixture; HOME is not changed. Per-command Git
identity and disabled hooks/global attribute/exclude files avoid unrelated operator policy.

The newer package came from the [official standalone download page](https://git-annex.branchable.com/install/Linux_standalone/),
using the [published signature-check procedure](https://git-annex.branchable.com/install/verifying_downloads/).
Package directory: /tmp/modelark-annex-newer-CDXMacFR.
Tarball SHA256: a3c9d10ee33a7ace69efeaa8cd6282cfd7444ec797d732cebae12a64efefa18a.
Detached signature was good for downloaded distribution key fingerprint
40055C6AFD2D526B2961E78F5EE1DBA789C809CB; web-of-trust identity was not independently established.
The disposable GPG home was inside that directory, not the operator keyring.

The stock launcher tried to write ~/.ssh helpers and the sandbox refused it. Its disposable runshell
was changed only from GIT_ANNEX_PACKAGE_INSTALL= to GIT_ANNEX_PACKAGE_INSTALL=1, the wrapper's documented
package mode, to disable SSH-helper/locale-cache setup. No privilege escalation for those home writes
was attempted. Package binaries were unchanged; this qualification used that modified wrapper.

## Failed probes retained — do not erase their interpretation

Earlier byte-only tests passed with **-filter**, but the expanded map test on the newer binary exposed
raw payload committed into Git after lock/unlock. Those early passes did not qualify committed annex
representation. Failed newer run: /tmp/modelark-annex-payload-stage0-p6t5eecu. Replacing -filter with the
known annex filter and checking committed pointers resolved that probe on both versions. This is a
candidate fixture policy, not permission to adopt arbitrary existing filter.annex commands in live config.

The initial retirement probe using git rm also refused the intentionally hostile filtered fixture.
Raw worktree/HEAD equality is different from Git's filtered dirty comparison. The final synthetic test
checks both raw copies, removes only the exact index entry with update-index, then the exact temporary
fixture path. Old bytes remain in the returning clone and old Git commit. Production retirement still
needs sealed index CAS, durable intent, no-follow path binding and recovery; this script is not that tool.

## Shared call-site audit — integration targets, not implemented guards

| Boundary | Current source | Required change / proof |
|---|---|---|
| All normal clean-anchor inserts | drive_mutation.py:189, :199; wrappers :206 and drive_mutation context | Central obligation check at the actual publisher, transactional completion exception only |
| Terminal-owner and sessionless recovery | drive_bootstrap.py:746, :847 and clean publications :534/:553/:761/:884/:907/:927/:949/:975 | Refuse/delegate pending operation, never treat inventory alone as closure |
| Start/resume, draft/approve, graph writes | execution_session.py:163/:660; proposal.py:774/:129/:1573/:1629 | Check obligation with correct lock and transaction order; revoke authority explicitly |
| Download and replica writers | fetch.py:1205/:1506, run_replica and hash_repair.py:375/:512 | Shared admission/publication contracts, not migration-only accounting |
| Map/tree sync | fetch.py:1352/:1354/:1716/:1805; register.py:569/:673 | Six explicit sync sites need shared preflight and map exclusion; replace warning/ignored failures |
| Registration/initial tree creation | register.py:104/:422/:580 | Ensure preflight precedes clone/initialization and every tree-changing helper, not just sync |
| Slice source use | slice/sources.py FencedSources._open; slice/local_source.py:108/:158 | Guard under source fences/fresh catalog evidence, preserve physical confinement |
| Legacy restore | restore.py:51/:150/:158 | Source admission as well as existing retrieval policy; do not bless missing/unknown drives |
| Version readers/writers | catalog_versions.py; core/db.py:293/:879; slice/catalog.py:18; slice/operator.py:54; hash_repair.py:29 | Explicit new version migration plus closed reader maps; no blanket future-version acceptance |
| Serial repair version write | drive_bootstrap.py:543–557, especially :555 | Currently stamps 8; before new catalogs are admitted, preserve higher floor or explicitly refuse |

Search of modelark Python sources found the one shared clean INSERT and six explicit annex sync calls.
This is a source audit, not proof against indirect/out-of-tree callers or races. Existing controller →
sorted-drive → short-DB ordering must be revised consistently if the map lock is inserted. Slice reads
need not acquire a controller/map lock if they can check the obligation under their existing physical
fence and fresh catalog read; do not introduce inverse lock ordering accidentally.

## Remaining Stage 0 gates / next work

1. Qualify the exact annex metadata-ref merge/validation protocol alongside file-tree/index publication:
   allowlisted key/UUID location evidence, unrelated metadata changes, partial ref/index failure,
   durable receipts and replay. Native update-ref atomicity alone is insufficient.
2. Finalize and negatively test portable ownership validation (wrong library/op/digest/key/path,
   pre-existing namespace, divergent old bytes, unclaimed returning Git copies). Transport is proven,
   authority is not. Validate repository-local annex filter configuration instead of trusting its name.
3. Explicitly pin the production client/support matrix before rollout: the newer qualification client
   auto-upgraded its fixture format. Do not put it on existing drives under this authorization.
4. Turn this call-site audit and monotonic schema-floor requirement into the Stage 1 design/test matrix.
   New-schema migration and production maintenance guards remain Stage 1/2 implementation work.

Grok planning/qualification pass 3 reviewed this experiment and declined Stage 1 readiness. No PR,
commit or push was made. Test reruns are not reviewer passes. On 2026-09-14 the operator authorized
an architecture-plan amendment and one additional Grok review (pass 4), not a new three-pass budget.
The initial rollout is now pinned by DEC-157 to 8.20210223/format 8; this narrows the migration's
qualification target, not general project compatibility or the meaning of the recorded newer-client run.
