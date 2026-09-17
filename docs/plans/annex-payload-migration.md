# Annex-managed upstream payload: ingestion fix and attended legacy migration

Date: 2026-09-14 (architecture amendment; original plan 2026-09-13)
Status: Revised architecture ACCEPTED by Grok follow-up round 1 / cumulative pass 5. Stage 0 pinned-profile qualification subsequently ACCEPTED by Grok qualification round 2; no remaining P1/P2 blockers in that qualification. Stage 1 implementation authorized and in progress, conversion disabled; no live apply. See [current implementation tracker](annex-publication-stage1-progress.md).
Policy: DEC-156 and DEC-157. Related history: INC-017; DEC-155 is the separate DGXSpark archive wave.
Operator request (latest): after implementing each stage, use local Grok CLI for up to three review/fix rounds; stop on acceptance or after round three and summarize. Track repeated root causes and propose an architectural correction when warranted. This authorizes Stage 1 implementation following Stage 0 acceptance; it does not reset earlier review counters or authorize live conversion.
Review: [Codex CLI pass 1 — NOT ACCEPT, three P1 findings](../reviews/annex-payload-plan-codex-1.md).
Re-review: [Grok CLI pass 2 — ACCEPT for qualification, one P2 clarification](../reviews/annex-payload-plan-grok-2.md).
Stage 0: [original qualification evidence](../acceptance/annex-payload-stage0-2026-09-13.md), HYP-003, followed by the accepted continuation below. Qualification complete for the initial pinned profile; later implementation/deployment gates remain.
Continuation: [2026-09-14 protocol experiments](../acceptance/annex-publication-stage0-2026-09-14.md) under subsequent operator authorization to continue Stage 0; no production implementation or live apply.
Current review: [qualification round 2 ACCEPT](../reviews/annex-publication-stage0-grok-2.md), six qualification findings closed, 69 passing check results plus a fresh 13-check representation run. Qualification cycle stopped after 2/3 rounds; next scoped work is Stage 1 with conversion disabled, not another plan-review reset.
Prior stop: [Grok CLI pass 3 — stop before Stage 1](../reviews/annex-payload-plan-grok-3.md). Original findings remain unchanged; this amendment addresses their architectural implications without claiming new test results.
Prior architecture review: [Grok CLI pass 4 — architecture accepted, Stage 1 still gated](../reviews/annex-payload-plan-grok-4.md). This follow-up clarifies phase/authority granularity, selected-vs-offline clone closure, adapter-owned transactions and map-UUID vs drive-UUID claims. The original review remains unchanged; pass 5 records closure.
Closure review: [Grok follow-up round 1 / cumulative pass 5 — ACCEPT, four P2s closed](../reviews/annex-payload-plan-grok-5.md).
The architecture-plan review cycle stopped on acceptance after 1 of 3 authorized follow-up rounds.
The later qualification cycle is separate and stopped after 2/3. Stage 1 now has implementation
authorization; further targeted qualification is appropriate if implementation exposes an unspecified transition.
The earlier planning/review run did not authorize PR, commit, push or live changes. The current
implementation work remains conversion-disabled and does not authorize service restart, live catalog
migration, reconciliation, archive conversion or automatic Fill resume. Stage 1 review counter and
current results are maintained in `annex-publication-stage1-progress.md`; integration entry points
are mapped in `annex-publication-writer-map.md`. Do not reset the stage counter for internal checkpoints.

## Plain-language outcome

Preserve upstream repository metadata (including .gitignore, .gitattributes and files in hidden directories)
as annex-managed model payload. Existing good bytes should be converted without re-downloading weights.
Git, annex, ModelArk's catalog and its drive evidence must agree afterward. A halfway conversion must be
recognizable and resumable, not mistaken for either completion or permission to discard a file.

ModelArk's own root Git-control files remain separate. Ordinary Git files can already be shared via Git;
this change is for uniform ModelArk payload identity, copy accounting and original-path Slice delivery,
not because non-annex files are inherently unshareable.

## Evidence and bounded inventory

Read-only catalog census on 2026-09-13 found 453 archived copy rows without annex keys, representing
452 logical repository/file pairs and 602,821 recorded stored bytes:

| Drive | Candidate copies | Repositories represented | Recorded stored bytes |
|---|---:|---:|---:|
| drive-00 | 210 | 149 | 234,686 |
| drive-01 | 80 | 76 | 120,923 |
| drive-07 | 163 | 160 | 247,212 |

385 rows name .gitattributes; 68 are other files, including hidden evaluation YAML.
All are recorded raw. Provenance: 174 ingestion_computed, 278 legacy_unknown, 1 archive-head-blob.
These are candidate records, not a physical audit, proof of absence, or a complete inventory of every
ordinary Git blob copied into every map/drive clone.

Five .gitignore files belonging to selected repositories are catalogued as other and have no archive
record. That is a separate manifest/acquisition gap, not a file-conversion operation.

Drive 01's paused session is 5fee0a92-88d9-433e-a30e-19607ea2e26e, proposal
9ad72ad1-9a91-4fa5-933c-f86af4acb6f7, epoch 1, dirty generation 3, token 2.
The latest clean anchor is generation 2. Fill stopped at 2026-09-13 04:00:18 UTC with
DRIVE_RECONCILIATION_REQUIRED for tencent/HY-MT1.5-1.8B-GGUF/.gitattributes, key None.
The 1,701-byte worktree file and HEAD Git blob match the recorded SHA-256
ceeaa8551656c32fff3fe8d8da02a0d6eec44c279d713026c916ecd66242e041.
This is not a serial repair or evidence of disk failure.

## Existing contract conflict

- register.ensure_library seeds '* annex.largefiles=anything' but imported dotfiles may still go into Git.
- fetch._annex_add permits a successful add with no lookupkey and returns None.
- fetch_model publishes that row and records its touched path.
- fetch._reconcile_touched demands a durable, writer-recorded annex key for every annex-drive touched path.
- drive_bootstrap._inventory separately permits raw claims whose files exist.
- hash_repair has useful exact HEAD/worktree/hash validation, but repairs hashes, not storage representation.
- formats.classify_file includes .gitattributes as aux but not .gitignore.
- archived has logical rfilename plus separate stored_relpath/stored_name/annex_key fields.
- Slice and restore consume these mappings; old proposals and Slice seals must not be silently rewritten.

Fix the writer/reader contract centrally; do not merely suppress the closing refusal or mark a drive clean.

## Scope / non-goals

In scope: exact selected payload classification, future annex-ingestion postconditions, mapping for upstream
Git-control files, inspect/apply/resume maintenance, evidence-preserving legacy conversion, physical
verification, catalog/copy accounting, drive/map propagation and approval invalidation.

Not in scope: model re-download/recompression, rebuilding the whole catalog, Git history rewriting/GC,
automatic deletion, blanket annexing of a library root, changing registered drive serials, changing
copy policy or plan membership, fulfilling the 27-repository Spark wave, P2P implementation, or a general
multi-revision catalog schema. Missing metadata acquisition remains normal approved Fill work.

## Architecture: one evidence-driven archive publication boundary

The shared component is an **ArchivePublisher**, not a collection of optional post-add checks. It owns
the state machine connecting stored bytes, committed file representation, annex location metadata and
catalog/copy evidence. It does NOT pretend Git and SQLite are one atomic transaction: durable intents,
guarded transitions and independently checked receipts bridge their failure boundaries. No caller may
infer another layer's state from command exit status, lookupkey, hash equality or a bumped revision.

Proposed internal responsibilities (names describe design, not existing APIs):

| Component / evidence | Owns | Cannot authorize alone |
|---|---|---|
| QualifiedAnnexProfile | Exact client/tool identity, repo format, helper/filter/config/attribute contract | Running a client outside its qualified profile |
| LocalPayloadProof | Original/stored hash and size, physical object presence, readable mapped path | A catalog claim, map success or clean anchor |
| CommittedTreeProof | Exact index/tree/ref delta and a parsed pointer bound to that same key/path | Object presence or annex location claims |
| MapPublicationProof | Validated metadata-ref delta, final file-tree/index/worktree agreement, durable receipt | A copy on an offline drive or completion of another operation |
| PublicationRecord | Operation/generation/owner, all proof digests, catalog CAS marker, replay state | Bypassing the shared admission or clean-publication guard |

File proofs bind a file publication ID and its parent batch/operation ID, library, drive/annex UUID,
epoch/generation, policy/profile digest and sealed before-state. Batch receipts list exact child IDs and
proof digests; generation closure names the required batch receipts. Evidence cannot be mixed between
parents or reused after those bindings change.
Use typed phase-specific values internally and a versioned durable catalog record, not a caller-provided
success boolean. The receipt is a record of checks; no cryptographic trust is implied by naming it a proof.

State sequence and ownership (three levels, not six per-file actions):

1. **PREPARED:** validate profile and authority, seal inputs and allowed changes, store intent under the
   existing dirty generation plus a persistent operation obligation before any publication mutation.
2. **LOCAL_VERIFIED:** publish bounded bytes/path, read and hash the actual object, verify mapped reader
   access; no catalog evidence yet. Missing key or wrong bytes is a refusal even after a successful add.
3. **TREE_VERIFIED:** commit only the allowed delta, parse the committed symlink/unlocked pointer using
   the qualified format grammar, bind it to the key and confined object path, and verify final index/ref.
   A substring match for the key is a useful probe, not the production pointer parser.
4. **CATALOG_PUBLISHED:** the atomic archived/replica/source-fact CAS in E records the same proof digests
   and revision. Readers remain blocked by the operation/generation guard. This is not completion.
Steps 1–4 are per-file publication. Normal Fill's per-file publisher returns at CATALOG_PUBLISHED; it
does not synchronize the whole library, release the generation obligation or publish a clean anchor.
Migration's per-file retirement/verification steps in E also feed their parent batch, not drive closure.

5. **PROPAGATED (batch):** the owning batch aggregates the exact file proof digests and obtains the F
   file-tree/annex-metadata/index receipt at the existing explicit sync boundaries, when their relevant
   tree or location metadata changed. A no-change boundary requires an explicit verified no-op receipt.
   Migration includes the proven old-path retirements before obtaining that receipt. Both old and new
   ref/index states are replay states; a per-file success cannot stand in for this batch receipt.
6. **CLOSED (generation/maintenance operation):** the enclosing authorized mutation/recovery coordinator
   checks all required file/batch receipts, inventory and the selected participant set, then closes the
   captured generation through the shared clean publisher and releases its obligation atomically.
   Normal Fill retains its existing enclosing generation lifecycle; its per-file publisher never closes
   it. Maintenance uses D/F's captured terminal/sessionless ownership and explicit recovery closure.

The selected participant set is frozen in the apply/receipt: closure waits for those participants and
the central map, not every registered clone. Other registered/offline clones keep separate pending layout
obligations and do not prevent Drive 01 closing its own completed batch. A selected participant that
detaches is a blocker for that batch; never silently reclassify it as an unrelated pending clone.

An interruption retains the last durable phase and obligation. Resume observes the actual bytes, full
catalog pair, refs, index, worktree and profile against their recorded old/intended-new states; it does
not blindly rerun a shell command. Unexpected states remain blocked with a typed conflict and preserved
recovery evidence. A clean anchor is the final result of closure, not a shortcut to finish a publication.

### Existing paths must use the same boundary

Future acquisition, replica copy, legacy conversion and returning-clone conversion all call this
publisher for annex-backed payload. They supply different input proofs (downloaded bytes, verified
source key, or historical Git blob) but share publication and verification. Replica creation must establish
the mapped destination path before supplying LocalPayloadProof. Hash-only repair remains a hash repair,
not an implicit representation migration; it still honors the shared obligation/authority guard.

The publisher accepts an explicit authority adapter: ordinary Fill uses its existing session token,
session_write and owned generation; attended migration uses D's maintenance/terminal-recovery ownership.
Do not force a running Fill through graph_write's no-live-session path, supersede its own approval on
each file, or invent a competing generation. Normal Fill's existing frozen execution authority is
preserved; migration invalidates affected approvals as specified in D. Both adapters must persist an
operation obligation recognized by recovery and carry the same proof/closure contract. Exact catalog
updates use the appropriate already-authorized transaction primitive, not nested transactions. The
adapter establishes or explicitly vouches for the transaction on this connection and verifies the current
token/operation/locks. con.in_transaction alone is not authority. The CAS callback never begins, commits,
rolls back or bumps the revision independently of that adapter. The adapter performs one revision bump,
updates a Fill session's bound revision when applicable, finalizes the publication's committed revision
marker and commits all of it together. Failure rolls back the entire owning transaction.

Scope includes the six existing annex sync sites and registration tree creation identified in the audit,
shared clean publisher and recovery/admission callers. Read-only Slice/restore consumes the same evidence
under existing source fences; it does not become a writer or acquire map/controller locks in reverse
order. Non-annex storage and codecs are not redesigned: a codec supplies its already-validated stored
representation to the publisher; this maintenance wave converts only the selected raw metadata files.

### Qualified execution profile — initial rollout, not a general client upgrade

Pin this migration's initial production profile to installed git-annex **8.20210223 / annex repository
format 8**, with the exact Git, annex, helper paths/build digests and effective config sealed by qualification.
The 10.20260717/format-9 run is comparative evidence only, not an approved client for this rollout. This
does not remove general project support elsewhere or authorize changes to any installation. An additional
client/format profile requires its own isolated qualification and a separately approved deployment scope.

Before any annex command, inspect repository config/format without invoking annex initialization; reject
unsupported/ambiguous versions. Invoke only the qualified tool paths, including subprocess/filter helper
resolution, and re-read format/profile after each mutating command. A changed profile/format leaves the
obligation pending and forbids subsequent commands/closure; do not downgrade the repository automatically.
Post-command detection is not prevention: no unqualified auto-upgrading client may run on a live archive.

Capture effective config values AND origins, including includes/conditional includes, command overrides,
filter.annex.clean/smudge/process/required, hooks, attribute/exclude sources and helper executable resolution.
The initial profile allows only the exact installed/qualified annex filter commands; a process filter is
allowed only if explicitly qualified, otherwise it must be absent. Reject unknown commands/origins before
publication; do not execute a suspicious filter to see whether its output happens to be correct. A
pre-existing hostile local config is a typed preflight refusal, not authorization to overwrite it silently.

The publisher owns scoped repository policy: neutral payload paths use validated **filter=annex**, not
-filter, with upstream text/encoding/ident transforms disabled. The prepared profile includes the exact
attribute delta and preserves unrelated policy. Force-large is a mandatory, scoped part of annex-add
publication, not an incidental fixture option. Qualify it with inherited conflicting rules and prove
the actual config/helper used. Probe success with --force-large does not prove the attribute alone wins.
Profile establishment is a separately previewed, journalled setup step under the same fences, before
payload changes; readers and competing writers remain blocked through the setup-to-closure interval.

### Portable ownership and metadata publication

Ownership manifests are validated against the catalog's sealed library/operation registry and expected
manifest digest, not trusted merely because a Git ref contains them. Verify path grammar, unique mappings,
original/blob/hash/size, key, profile and permitted tree delta. A returning clone must prove its old bytes
against that exact manifest before local conversion. Unclaimed materialized copies may get an operation
receipt but no invented archive/copy claim. Absent registry/digest authority or divergent old bytes refuses.

Map publication is an ArchivePublisher phase using F's protocol. It must not call generic sync and then
label a receipt complete. Build the candidate annex metadata merge in an isolated metadata-only staging
repository/index from captured source/map OIDs; do not update the real map while evaluating it. The
qualified profile defines the parser and exact allowed log formats. Compare old/candidate trees and
decoded records: only selected keys' location deltas for physically proven source/target UUIDs are allowed.
Preserve prior unrelated records; no new payload-presence claim for the central MAP's annex UUID,
drop claim, unknown UUID, clock-dominating
conflicting record, unrelated config/trust/description update or unrecognized record format is accepted.
Unexpected changes cause refusal, not silent removal from an arbitrary merge result.
The map stores location metadata ABOUT the archive drives: allowlisted drive-UUID location-log deltas
are required and permitted. A whereis query used to validate that metadata is allowed; a positive claim
that the MAP UUID itself holds newly published payload is not. Compare locations by UUID, not by the
repository in which the metadata was read, and preserve pre-existing unrelated records as above.

Use the qualified annex metadata merge behavior to construct candidate records; do not invent a generic
text concatenation or hand-written timestamp protocol. Independently validate the candidate's effective
locations with the same qualified annex reader in the staging repository and compare to the sealed
before-state plus allowed delta. Metadata logs remain location claims, not substitute physical hashes.
The exact on-disk grammar/merge behavior and failure matrix must still pass Stage 0 on the pinned profile.

Persist candidate tree/ref OIDs, expected old OIDs, decoded delta digest and intended final index/worktree
manifest before publication. Atomically CAS co-located real refs where supported, then explicitly bring
the real map index/worktree to the verified tree under the same locks. A moved ref with old/partial
index/worktree is unfinished. Replay validates per-path old/new state without clobbering unrelated files;
index locks or unexpected edits refuse. Only after rereading refs, index, worktree and effective annex
locations does it issue MapPublicationProof. The map obligation survives this gap and blocks competing
managed tree-changing use. The verified owner of that operation is permitted to perform only its sealed
remaining publication/replay steps while holding the required fences. This is not a caller-selected
skip-guard flag: validate operation ID, current authority/token and scope on each transition. On process
loss, only explicitly admitted recovery may acquire that authority after proving the old holder and
children cannot write. The owner exception does not permit unrelated sync or an early clean anchor.
Cross-repository atomicity is not claimed.

## A. Representation qualification — first implementation gate

A simple global annex.dotfiles toggle is not the design. Upstream .gitattributes/.gitignore bytes must
not become configuration governing ModelArk's archive operations. Global user Git config is untouched.

Proposed representation:
- Keep original repo_id/rfilename as logical identity.
- Store imported paths with any dot-prefixed component under an inert per-repository namespace:
  __modelark_payload_v1__/p-<SHA256 of the exact UTF-8 original rfilename>.blob.
- Use the same mapping for any other legacy Git-backed file chosen for conversion.
- Record the mapping in stored_relpath/stored_name. The bytes remain raw and unchanged.
- Before use, refuse conflicting upstream/catalog/stored paths, symlinks, traversal or a pre-existing
  namespace not proven owned by this representation. Never auto-adopt a coincidentally matching file.
- Preserve original names only in the catalog/manifest and usable projection. Do not leave an active
  upstream control file in the library worktree after a completed conversion.
- Existing already-annexed ordinary payload paths stay unchanged. Existing annexed dot/control paths
  require an explicit audit for policy interference; do not assume the null-key census covers them.
- ModelArk's own .git directory, root policy files and unrelated operator files are excluded by scope,
  not by a loose extension-based recursive walk.

Qualify the complete candidate on the pinned 8.20210223/format-8 profile in disposable repositories before
implementation is accepted. Newer-client experiments are comparative, not rollout acceptance. Test inherited upstream attributes/ignore rules, clean
filters/EOL transformations, force-large behavior, tracked-file conversion, missing lookupkey, hostile
names and locked/unlocked forms. A successful add must establish a nonempty SHA256-based annex identity,
exact stored size and hash, and real local object presence. Command success alone is not sufficient.

If inert mapping cannot meet existing restore/replica/capacity semantics without a broader schema or
layout change, stop and bring that scope to the operator; do not silently relax isolation.

Stage 0 must prove isolation during the duplicate interval too: the still-present upstream ancestor
.gitattributes must not apply filters, encoding, EOL or annex policy to the new path. Use a bounded,
repository-local policy mechanism qualified against the complete attribute stack; do not assume that
a neutral basename alone isolates it. Portable ownership is a versioned, digest-bound migration manifest
in a dedicated ModelArk metadata ref, binding library identity, operation, original path/blob/hash/size,
new path/key and policy version. Another clone must verify that manifest and actual bytes; a matching
filename or hash alone is not ownership. Conflicting versions for one logical file are a refusal.
Stage 0 evidence: disabling every filter is insufficient; the newer client can preserve working bytes
but commit raw Git payload after lock/unlock. The qualified candidate retains ModelArk-validated annex
filter behavior while suppressing upstream transformations. Verify committed tree pointers after each
commit, not just lookupkey/object bytes before commit. Audit the actual filter command configuration.
Record final annex repository format as well as binary version: the newer test client auto-upgraded
its disposable format-8 repositories to 9. That does not authorize a live client/format upgrade.

## B. Future acquisition and manifest consistency

All affected writers use ArchivePublisher and its qualified profile/phase-specific evidence, as defined
above. A thin _annex_add wrapper alone is not this boundary. Inventory fetch and replica paths; do not
create a migration-only definition of a valid payload.

Replica qualification must prove both object availability and a readable mapped worktree path: copying
an annex key alone does not establish the latter. Scope the shared helper to materialize and validate
the mapped destination before publishing replicas.present. If existing sync/checkout supplies that path,
test its exact ordering; otherwise add guarded path publication in Stage 1. Test Slice and authoritative
restore against the destination, not only lookupkey/whereis success.

Add exact .gitignore basename recognition alongside .gitattributes, including nested paths; do not broaden
to arbitrary hidden directories or archive a .git administrative tree. Review other hidden files using
existing file selection, rather than making every unknown file an automatically selected artifact.

Changing classification affects existing catalog rows as well as future discovery. Preview the affected
rows and intended file-set changes through supported graph mutation; no direct ad-hoc SQL or broad metadata
refresh that silently switches revisions. Five omitted .gitignore files need their exact bytes acquired
through the normal manifest/approval workflow, not fabricated archive rows.

Keep logical manifest identity separate from storage mapping. Preserve every frozen proposal file/task;
explicitly invalidate affected execution authority on maintenance/config changes and require fresh review.
Resume of old Slice transactions must follow existing sealed evidence rules, not auto-rewrite their paths.

## C. Maintenance interfaces and frozen preview

Proposed commands (names are a design, not currently available):
- modelark archive annex-migrate inspect --drive LABEL [--repo ID ...]
- modelark archive annex-migrate apply --plan PLAN_ID --seal SEAL --writers-stopped
- modelark archive annex-migrate status PLAN_ID
- modelark archive annex-migrate resume PLAN_ID --writers-stopped

Inspect opens the catalog mode=ro/query_only and performs read-only physical checks. It must not start
generations, repair hashes, change Git index/config, fetch objects, or create destination files.
If the user explicitly asks to save a plan, write it only to private host maintenance state.

A plan binds library/catalog identity, schema, code/policy version, planner revision, drive epoch and
fingerprint, filesystem/annex UUIDs, current dirty/clean generation and owner, relevant proposals, archive
HEAD/index identity, exact before rows, logical/stored paths, Git blob IDs, expected hashes/sizes, new
mapping, and target replica facts. Per-file states: convertible, already-converted-with-proof,
needs-evidence, content-conflict, missing, unsupported, offline. No missing drive becomes a zero-work success.

Freeze exact selected candidates before apply. Conflicting candidates are excluded from an explicitly
reviewed batch and remain visible as unresolved. Do not silently migrate only the easy subset and report
the drive or library complete.

## D. Shared maintenance obligation, admission and dirty recovery

A private journal is not an authority fence. Introduce a durable catalog maintenance obligation in the
next supported schema migration, with operation/library ID, sealed plan digest, drive/epoch/generation,
captured prior owner (including sessionless), state, required map receipt and closure evidence. Separate
per-clone layout obligations bind annex UUID and old/new layout versions. These records are authoritative;
private backup/journal files supply evidence, not permission to clear them. Migration is additive and
retains historical drive/session rows. Stage 0 must prove old binaries fail closed on the upgraded schema
before enabling this feature; stop deployment if a reader/writer version floor cannot be enforced.
Use a new PRAGMA user_version greater than 8 (next unallocated version), with explicit migration from
both v7 and v8. Do not reuse SERIAL_REPAIR_CATALOG_VERSION=8, which existing builds accept. Test refusal
with current MAX_SUPPORTED_CATALOG_VERSION=8 readers/writers and Slice's closed {7,8} version mapping.
Stage 0 audit addition: preserve monotonic version floors in existing repair paths. In particular,
drive_bootstrap._serial_enrich_locked currently stamps SERIAL_REPAIR_CATALOG_VERSION unconditionally;
before admitting newer catalogs it must preserve a higher floor (or refuse that operation explicitly).
Test repair on the new schema cannot downgrade it and thereby reopen access to old clients. Keep the
catalog version distinct from annex repository format and private Slice state-store versions.

Lock order for every participating writer: controller exclusion, shared library/map lock, physical drive
fences in stable label order, then short SQLite write transaction. Hold outer locks through an apply/resume
batch; never wait for filesystem locks while holding a DB transaction. This map lock is new work, not an
assumption about current code. Audit all map mutation and drive sync callers for this order. All mutating
Git/annex children inherit the applicable fence FDs; recovery checks for surviving holders after parent
death. Pause alone is not exclusion. Recheck preview bindings and drive identity under these locks.

Before any archive/index/ref mutation, atomically create the operation and selected-drive obligations,
invalidate affected approval authority, and bump planner revision through graph-write discipline. For a
clean drive allocate its maintenance dirty generation in that same transaction. For Drive 01 generation 3,
prove the old session is terminal and no writer/child survives, then bind the obligation to the exact
existing owner tuple without replacing it or allocating a generation over it. Sessionless dirty recovery
uses the same obligation protocol. Backups are taken before this transaction; record their durable bindings.

A single shared guard must be checked by execution admission (Fill, repair, replica and Slice/restore
source admission), approval creation/approval, every tree-changing sync/checkout, and every clean-anchor
publisher, including drive_mutation._publish_anchor_locked and all bootstrap/recovery routes. Read-only
status/inspect remains available. An unresolved selected-drive obligation blocks its use as a source or
writer; a pending clone layout obligation blocks tree changes until its attended migration completes.
Ordinary reconcile, including terminal-owner and sessionless recovery, returns MAINTENANCE_REQUIRED
with the operation ID; it may not clear an obligation just because inventory passes. For a maintenance
obligation only explicit migration closure may satisfy the guard after checking every recorded condition.
For a normal Fill publication obligation, only its enclosing authorized generation coordinator (or
explicitly admitted recovery) can close it after the same evidence checks. Neither is a per-file action.
The verified current owner may perform its scoped remaining publication/replay steps under the obligation;
all competing callers remain refused as specified in the architecture section.
Implement and test these shared guards before enabling any conversion command.

Existing proposals/tasks and session tokens remain immutable historical evidence. Supersession/clearing
active approval pointers is explicit, not a side effect assumed from bump_revision. No new affected
approval can be issued during maintenance. Other-drive historical copies are not silently migrated.

Before first mutation, capture a consistent SQLite backup and restorable Git recovery evidence for the
exact drive and central map, plus the original small-file bytes and inventory bindings. Do not copy a
live SQLite main file alone or assume a Git commit is a backup of annex objects.
Keep unrelated dirty index/worktree changes untouched; refuse ambiguous or overlapping changes.

## E. Journalled conversion and atomic catalog cutover

Git/filesystem and SQLite are not one transaction. Use a private, versioned durable maintenance journal,
reusing existing store patterns where appropriate, keyed by operation ID and exact per-file identity.
Journal durability includes file/directory flush or SQLite durable transaction, not merely an in-memory list.
The catalog operation also contains per-file intent and commit markers so a lost/trailing private journal
cannot erase a pending obligation. Each intent binds the complete before and intended after state of
archived, replicas (including explicit row absence), relevant source file facts and operation/generation.

Per file:
1. Persist INTENT with before row, original HEAD blob and verified SHA-256/size, proposed mapping and backup binding.
2. Re-read a stable no-follow descriptor and prove bytes match the exact committed regular Git blob and
   all existing non-null hash/size evidence. If missing provenance can be independently established,
   retain prior evidence in the journal and use the established archive-head proof rules. Never replace
   a conflicting hash to make conversion pass.
3. Persist PUBLISH_INTENT, then create annex-managed bytes at the neutral stored path using no-clobber
   publication; check the actual annex object, lookupkey, size and original hash.
4. Commit only the intended payload index changes and record the resulting commit/object identity durably.
   Do not bulk-stage other files. Initially preserve the old logical file for rollback until the new object
   is durably established; consumers remain excluded while duplicate representation exists.
5. In ONE short adapter-owned authorized write transaction under the maintenance locks, CAS the full captured archived
   and replicas state (including expected absence), source file facts, planner revision and active
   operation/epoch/generation. Update archived mapping/key/size AND the selected drive's replica
   presence/key evidence and write the complete per-file DB_PUBLISHED marker. The adapter opens BEGIN
   IMMEDIATE only when it owns entry to a new transaction; if its authorized transaction is already open,
   this callback joins it without BEGIN/COMMIT or a second revision bump. Reject an unrelated existing
   transaction instead of adopting it. The adapter bumps planner revision exactly once and finalizes
   the marker/session revision in that same transaction. Any mismatch rolls back the entire transaction.
   Journal the resulting revision as this operation's next expected revision; do not reuse the original
   preview revision for subsequent files. Keep repo_id/rfilename/original bytes/hash stable; provenance
   may be strengthened only with independent captured evidence. Preserve ingestion timestamps; new
   verification evidence belongs to this maintenance operation, not a fictitious new download.
6. Publish replica evidence only for this physically proven drive. Freeze the intended treatment of an
   absent replica row in the preview: create it only when existing copy-accounting rules require that
   assertion and proofs are complete; otherwise preserve absence. Never manufacture other-drive copies.
   SQLite readers see either the entire old pair or entire new pair, never split accounting. Consumers
   remain excluded by the maintenance obligation even after this per-file commit.
7. Journal DB_PUBLISHED, then retire only the exact old live Git path after the alternate bytes and mapping
   are durable. Use scoped Git index changes; keep old commits and backup bytes. Record OLD_PATH_RETIRED.
8. Verify the final stored file/object and original-byte identity; record FILE_VERIFIED.

Database commit and journal marker ordering must be recoverable in both directions. Resume compares the
WHOLE intended post-state (archive, replica or its absence, source facts, committed marker and revision
lineage), then verifies the physical object before repairing a trailing external marker. A new archived
row alone is insufficient. If objects/commits exist but the complete DB state is old, verify intent and
continue the guarded CAS. A mixed state or unrelated revision advance is a typed refusal requiring a new
reviewed preview, not an automatic partial repair. A resumed batch retains its central obligation.
Missing, divergent or externally changed evidence means typed refusal, not adoption or overwrite.
Do not mark the drive clean or resume Fill after a partial file operation.

## F. Drive closure, central map, offline clones and approval

### Map publication protocol

The sealed preview names the central map and exact selected apply participants with their identities,
affected paths, expected file-tree HEAD/ref OIDs and index tree, participating git-annex metadata refs, and exact allowed
tree delta: add the owned neutral paths, retire the selected old paths, and publish the ownership manifest
in its dedicated ref. No unrelated tree/index changes may ride along. Pending layout records cover all
registered clones BEFORE map publication, not only drives with archived rows. Other/offline clones are
listed by known identity as separately pending, not assigned invented current HEAD/index observations or
made closure prerequisites. Their actual refs/bytes are captured in a new attended preview on return.
Unknown/unregistered clones
must be inventoried and admitted before ModelArk permits their first tree-changing operation.

Under the library/map lock, require the captured map/index and source ref states, build and inspect a
candidate tree in an isolated index, and publish only the exact reviewed delta. Use expected-old-OID ref
updates; do not substitute unrestricted git-annex sync/merge. The annex metadata publication allowlist
contains only the captured participating annex refs and validated location changes for proven keys/UUIDs;
unexpected tree or metadata changes are conflicts. Stage 0 must qualify the precise Git/annex ref protocol
on the pinned rollout profile before coding it. Map publication permits the validated drive-UUID location
claims in its metadata but no new payload-presence claim for the MAP UUID itself. It does not transfer
annex payloads to the map or rewrite old Git history containing the original metadata.

Persist MAP_INTENT before ref updates and a durable receipt of expected/resulting tree, manifest and annex
ref OIDs afterward. Ref groups may be published atomically where supported; cross-repository publication
is not atomic. On interruption inspect every recorded ref: expected old permits retry, intended new permits
verified acknowledgement, anything else refuses. If either source/map ref or index advanced unexpectedly,
do not merge on retry: retain the obligation and require a freshly reviewed propagation plan. Sync errors
are hard incomplete states, never warning-only success. A drive cannot close until its required map receipt
is complete; other offline drives remain individually pending rather than preventing this drive's closure.

### Returning-clone interception

Every ModelArk tree-changing path (fetch, replica, restore if it mutates, registration, maintenance and
manual CLI sync wrappers) must enter one shared compatibility guard BEFORE sync, checkout or merge. A
returning old-layout clone is stopped for attended inspect/apply: enumerate committed/materialized affected
Git paths even without archived rows, preserve exact old bytes/HEAD and backups, verify the portable
manifest and convert locally before incorporating the retirement tree. Unclaimed materialized copies get
representation/ownership receipts, not invented archive or replica rows. Missing/divergent content or
unknown ownership blocks convergence; never remove the old path simply because the map already removed it.
After local publication and catalog cutover (where claims exist), validate the resulting tree/objects and
receipt before marking that clone compatible. Do not infer success from equality of annex keys.

This protects ModelArk-managed operations, not arbitrary external git commands. During rollout the
operator must not run unmanaged sync/checkout, and old installations must be stopped/version-fenced.
If complete interception of ModelArk paths cannot be demonstrated, map retirement is disabled: stop at
the qualification gate rather than leave a documented but unenforced warning. Offline/unregistered clone
work remains visible independently of the selected drive's success.

### Closure and evidence strength

Hash every converted file/object against its original-byte proof. For unchanged catalog claims, reconcile
local existence/object presence and accounting under the current drive identity; location logs alone
are insufficient. This is not a new whole-weight physical hash verification. Classify every extra within
the affected scope, bind pre-existing unrelated extras in the inventory, and refuse unexplained new extras;
ordinary inventory's acceptance of extras cannot satisfy migration closure. Re-measure capacity including
temporary/retained live objects. Record changed-file hashing, unchanged presence, map propagation and
pending clones separately so the receipt cannot overstate what was physically verified.

One final adapter-owned guarded DB transaction CASes operation/owner/generation, all file completion markers,
required map receipt, compatibility of the exact selected participants, inventory bindings and revision; it publishes the clean anchor,
marks the selected obligation complete, and bumps revision atomically. The shared clean publisher accepts
this closure only with those explicit transactional proofs. A trailing private receipt is recoverable from
this catalog state. Approval authority was revoked at admission and stays revoked; frozen proposals and
old tokens are never revived. Fresh approval and an explicit Fill start are separate steps, not automatic
migration side effects. Any unmet closure condition keeps the drive blocked. Independently pending
offline clones remain visible and blocked from incompatible tree updates, but are not prerequisites for
this selected drive's closure. This F closure describes attended migration; normal Fill retains its own
enclosing mutation coordinator and current session authority, with the shared evidence prerequisites.

No history rewrite, Git prune, annex drop or backup cleanup in the initial migration. Reclamation, if ever
wanted, is separately scoped after successful delivery proof.

## G. Tests and acceptance gates

Hermetic fixtures must cover:
- Root/nested .gitignore and .gitattributes, hidden evaluation directories, ordinary raw legacy files,
  already-annexed files, library control files, and namespace collisions.
- Divergent/missing worktree vs HEAD vs catalog; legacy_unknown provenance; stale preview and stale row CAS.
- Successful annex add returning no key, wrong key/bytes, absent annex content and phantom whereis entries.
- Original-path restore/Slice including metadata; replica copy and reconcile with remapped stored paths.
- Interrupted/crashed process before and after every intent, physical publication, Git commit, database
  commit, journal marker, old-path retirement, map sync and clean-anchor boundary.
- Concurrent Fill/repair, stale terminal owner, PID reuse/child writer, drive detach/reattach, wrong disk,
  second drive offline and copy-policy preservation.
- Disk full, SQLite commit failure, unavailable map and partial sync. Preserve recovery evidence.
- No phantom downloaded/verified state, no out-of-scope mutations, no whole-cache/global-config changes.
- Existing read paths continue to support historical ordinary Git-backed copies until migrated.
- New required metadata invalidates old manifest approval rather than silently changing an approved Fill.
- Every ordinary clean/recovery path refuses a pending operation, including terminal-owned and sessionless
  generations, after crashes at each DB/physical/ref boundary; all writer and source admission paths agree.
- Atomic archive/replica visibility and resume with absent replica rows, trailing journals, mixed or stale
  post-state, failed revision/approval transactions, and a process killed immediately after DB commit.
- Returning offline clones with unclaimed Git blobs, divergent bytes, old clients, and attempted preflight
  bypass; partial map/annex-ref publication, source or map advancement, unknown clones and exact-delta refusal.
- Attribute isolation throughout duplicate representation, portable ownership on a second clone, and
  actual mapped replica path readability before copy evidence is published.
- One fixture must preserve and convert a returning clone AND apply the retirement tree to that SAME
  clone, then read its mapped bytes through existing consumers; the prior map-only replay is insufficient.
- Exercise the real _annex_add caller's missing-key behavior, distinguishing a no-op tracked-file add
  from a successful publication that supplies no key. No-op output must not be labelled an ingestion test.
- Negative profile tests for substituted filter commands/process filters, includes/overrides, helper-path
  substitution, format drift and an unqualified newer client; prove refusal before suspicious execution.
- Per-file committed pointer grammar/key/path validation, not merely key substring or working hash.
- Staged annex metadata merges: selected/unknown keys and UUIDs, unrelated trust/config changes,
  conflicting clocks/records, ref-CAS failure, old/mixed index/worktree, receipt failure and replay.
- Per-file completion cannot close a generation; batch receipts cannot substitute another batch's proof.
  The valid owner can finish publication under its obligation while competitors and stale owners refuse.
- Selected participant loss blocks its batch; an unrelated offline clone stays pending without blocking
  Drive 01 closure. Neither can be silently moved between those sets during replay.
- Adapter-owned existing/new transactions each commit atomically with one revision bump; an arbitrary
  open transaction is refused. Failure rolls back archive/replica facts, marker and session revision.
- Positive drive-UUID location metadata is allowed in the map; a new positive MAP-UUID payload claim
  is refused. The repository holding metadata must not be mistaken for the payload's location.

Acceptance sequence:
1. Disposable repository representation experiment on the installed git-annex version.
2. Unit/integration crash-recovery suite on cloned catalog + disposable archive/map copies.
3. Dry-run of all candidates, with exact before/after manifest and space report.
4. Operator-reviewed Drive 01 batch after merge/deployment and backup; verify an original-byte Slice
   containing the converted control files and prove the next scoped Fill closes a clean generation.
5. Drive 00 and 07 attended batches when available. Keep unresolved files and offline work visible.

The data volume is tiny; identity, index and durability checks dominate. No performance promise is made
from the 603 KB census, and no model-sized re-hash is required merely to convert small metadata.

## Staged implementation / review plan

| Stage | Deliverable | Stop gate |
|---|---|---|
| 0 | Finish pinned profile/filter/committed-pointer qualification, returning-clone ownership and exact staged metadata/ref/index replay; retain existing audit/reader-floor evidence | Actual recorded results close remaining gates; plan acceptance alone is insufficient |
| 1 | ArchivePublisher evidence/state model, authority adapters, shared ingestion, metadata classification, maintenance guards and map locking/compatibility | Existing acquisition/replica/recovery paths share the boundary; conversion remains disabled |
| 2 | Inspect/apply/resume with atomic catalog cutover, returning-clone migration and guarded drive/map closure | Crash suite and cloned-catalog rehearsal pass |
| 3 | Reviewed release and attended Drive 01 qualification | Fresh approval/start and exact restore evidence, no automatic resume |
| 4 | Drive 00/07 rollout and missing-dotfile acquisition | Per-drive completion, unresolved inventory and ordinary Fill gates |

After pass 4, the operator explicitly authorized folding its four P2s and up to THREE local Grok
review/fix rounds for this follow-up. Number them follow-up rounds 1–3 (cumulative passes 5–7), stop on
acceptance or after round 3, and retain findings. This does not authorize tests, implementation, deployment
or live migration in this turn. Report design acceptance separately
from Stage 0 readiness; unfinished qualification remains unfinished regardless of the design verdict.
Later code stages use the operator's established bounded review policy and currently available reviewers.

## Source map

- modelark/formats.py: classify_file / AUX_EXTS
- modelark/archive_manifest.py: canonical file selection
- modelark/fetch.py: _annex_add, fetch_model, _reconcile_touched, replica path
- modelark/hash_repair.py: exact HEAD/worktree evidence and consistent backups
- modelark/drive_bootstrap.py: inventory and explicit recovery/anchor rules
- modelark/drive_mutation.py: fences, dirty ownership and clean publication
- modelark/proposal.py and execution_session.py: graph revisions, approval/session authority
- modelark/slice/catalog.py, domain.py and restore.py: original-path/content evidence consumers
- git-annex documented Git-to-annex conversion:
  https://git-annex.branchable.com/tips/largefiles/

## Review handoff — 2026-09-13

Codex CLI pass 1 returned NOT ACCEPT. Its original review remains unchanged. Grok CLI pass 2 found the
following three amendments closed at design level; they are proposed contracts, NOT implemented guarantees:

1. Durable migration obligations visible to all clean-anchor publication and execution-admission paths,
   including ordinary recovery of both terminal-session-owned and sessionless dirty generations.
2. Explicit map locking/ref/index protocol and returning-clone interception before tree-changing sync,
   including unclaimed Git files and retry behavior after partial propagation.
3. One per-file catalog CAS transaction for archived + replicas + planner revision, with complete
   intended post-state and explicit approval supersession/restart rules.

Sections D, F and E respectively define these contracts, with crash/admission coverage in G. Representation
isolation/portable ownership, materialized replica paths, schema version fencing and precise ref publication
remain mandatory Stage 0 qualification gates. The common issue is that a migration-private journal cannot,
by itself, control shared Git trees, catalog readers, and generation authority. Grok accepted beginning
Stage 0 qualification, with a P2 request to make the greater-than-v8 schema floor explicit. That wording
and a non-blocking function-name correction were applied after review. Pass 3 then reviewed partial
Stage 0 results and declined Stage 1 readiness. Pass 4 accepted the architecture with four P2 clarifications.
The latest operator authorization covers folding those clarifications and up to three follow-up rounds.
Next execution step, after the bounded reviews and operator direction, is to finish the
remaining targeted qualification, not restart completed probes or begin conversion implementation.
This does not authorize weaker reconciliation, a live layout switch or an automatic review-budget reset.
