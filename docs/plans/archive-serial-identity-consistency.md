# Archive serial identity consistency and legacy evidence transition

Status: implementation and one new PR approved by the operator on 2026-09-09 after
local Grok ACCEPT. Work proceeds in the slices below. Deployment and another live
transition remain separate operator approval gates.

## 1. Outcome and non-goals

Make registration, live archive observation, reconciliation and Slice source reads agree about
the physical disk serial. Correct the existing null-serial fingerprints without moving archive
bytes, rewriting historical anchors, pretending a paused Fill completed, or weakening source checks.

This is not a Slice destination ownership change. A folder remains the owned output unit. Existing
backing-device checks (serial or WWN, filesystem identity, protected-role/archive exclusion) remain.
No formatting, remount, archive cleanup/rehash/ingestion, catalog relabeling, automatic Fill resume,
device replacement adoption, general RAID support, new filesystem profile or generic migration framework.

## 2. Evidence and present live state

The mounted source is a partition. `register.probe_serial` takes findmnt's source and runs
`lsblk -dno SERIAL` directly on that partition. Registration's preparation code instead resolves
PKNAME to the parent disk. The Slice hardware observer also resolves the physical disk.

For the attended source, registration's saved serial is correct. The partition probe returned
None, so both the old clean anchor and PR 71's new clean anchor bind serial:null. Slice recomputes
from the saved disk serial and correctly rejects the different fingerprint. The fingerprint hash
function is shared already; the producers disagree about its inputs.

PR 71 recovery succeeded and is not to be undone: 2769 claims present, zero missing; old owner and
session/history preserved. Current source is epoch 1/generation 2, now anchored; planner revision
2948. Consistent pre-recovery backup and full table comparison are retained under
`/tmp/modelark-live-acceptance-R3KfnE`. Only one anchor and planner revision changed. Portal remains
stopped, Fill paused, existing USB files untouched, and no executable USB transaction exists.

Do not rerun the current reconciliation hoping this mismatch will disappear. Do not correct the
catalog serial: it is not the faulty datum. Do not simply change the probe and ship it: corrected
observations would disagree with existing fingerprints, and fingerprints also name physical locks.

## 3. Separate the three contracts

1. **Hardware observation:** find the unambiguous physical backing disk of the mounted filesystem.
   A serial is optional supporting identity evidence, not a synonym for a filesystem or folder.
   Absence must be distinguished from a failed/ambiguous observation. A known saved serial that
   cannot be confirmed must not silently turn into null.
2. **Archive identity/admission:** the existing v1 fingerprint binds filesystem UUID, annex UUID,
   optional serial and filesystem capacity. Keep that algorithm unchanged. A mounted candidate
   must match the current canonical identity and clean generation where the workflow requires it.
3. **Mutual exclusion:** all supported operations on the same archive must still contend even
   when one catalog/old attempt knows the legacy null-serial fingerprint and another knows the
   corrected fingerprint. Lock compatibility does NOT make a stale fingerprint executable evidence.

## 4. Proposed implementation design

### A. One physical ancestry observation contract

Introduce a workflow-neutral host/block-identity seam for mounted-source -> physical-disk ancestry.
Prefer extracting/reusing the already tested ancestry logic behind Slice's inventory rather than
inventing another first-parent parser. Keep compatibility imports if needed; do not make register
or Fill import the Slice workflow. Implementation must inventory actual consumers before extraction.

- Resolve the covering mount's major:minor through the block inventory to its physical disk;
  do not assume findmnt SOURCE is a literal device path (UUID/PARTUUID/mapper spellings exist).
  A mounted partition walks to its parent; a whole-disk filesystem stays on that disk.
- Support only already qualified unambiguous single-parent part/crypt/LVM chains. Detect cycles,
  duplicate/ambiguous identities, missing nodes, multiple parents and unsupported mappings.
- Obtain the serial from the physical disk, never the partition or an arbitrary first parent.
- Preserve serial spelling/case used by the established fingerprint contract; trim only established
  command-output whitespace. WWN may support topology/backing checks but is NOT substituted into
  the serial field. No UUID/serial fabrication or arbitrary fallback.
- Distinguish a genuinely absent serial on proven topology from IO/command/parse/permission failure.
  The latter cannot create a trusted null-serial identity. Known saved serial mismatch or inability
  to confirm it refuses; never overwrite it with whatever disk happens to be mounted.
- Re-observe before the transition commits; no stale topology cache or path-only identity proof.

Use this seam in registration's identity verification and the live observations used by
`drive_bootstrap` and `fetch`. Verify parity with Slice source observation. If extraction touches
Slice observers, their admission/protected-role behavior must remain pinned by existing tests.
Extract ancestry only, not LinuxObserver's USB policy; retain direct=True where it applies today.
Do not replace register._parent_disk's SMART/formatting guards as incidental cleanup. Preserve
RAID/skip-SMART workflows; empty canonical serials are not eligible for this specific repair.
Do not change diagnostic web/disk_api serial fields into identity authority.

Normal reconcile/Fill observation must classify the recognized serial-input mismatch explicitly
and refuse with a repair-required diagnostic before inventory or dirtying, not fall into clean
refresh with only the new lock and fail late at anchor publication. The early comparison is a
diagnostic, never mutation authority: authoritative observations/CAS remain under the full fences.

### B. Permanent compatibility lock alias, not just a migration-time double lock

Proposed bounded solution for this particular v1 null-serial correction: keep the capacity epoch
unchanged and acquire both the canonical fingerprint lock and its exact null-serial v1 alias at
that epoch. Derive the alias only from validated filesystem UUID, annex UUID and capacity facts.
Deduplicate identical keys (genuinely absent serial), globally sort all drive keys and retain/release
every handle together. Child transports inherit ALL held drive FDs.

Why permanent: holding old+new keys only during transition would leave a later old-catalog reader
or old binary using the old lock while an updated operation uses only the new lock. A schema bump
on one catalog or the application launch singleton alone does not protect another copied catalog
or an already surviving child. Updated operations keep the null-serial alias so those old holds
continue to exclude them after the transition.

Expand bidirectionally from facts, including unrepaired records and sealed old candidates: never
compute null-of-an-existing-hash. Nonblocking acquisition is a sorted AND of the entire unique set,
not success if either one is free. Production missing/unprovable identity must refuse, not fall back
to a label string; retain synthetic test seams only as explicitly injected nonproduction fixtures.

The proposed alias policy is deliberately conservative: cloned UUID/capacity tuples may contend
even on separate devices. It does not establish identity or allow a write that fails admission.
Unknown arbitrary fingerprint changes, changed non-null serials, changed UUIDs and capacity changes
are not classified as this repair. Keep capacity-transition semantics, but expand BOTH the old
epoch/identity and prospective new epoch/identity into their alias sets before acquiring the union.
Leaving that branch on raw keys would bypass an old null-identity hold during a subsequent resize.
This does not promise new cross-host or arbitrary historical-capacity/epoch compatibility.

Use a shared pure key-expansion helper plus workflow-owned evidence adapters, not ad hoc extra
flocks at one caller. Retain the current low-level controller/drive lock order. Do not perform
blocking lock waits while holding a SQLite write transaction. Captured facts must be rechecked
after acquisition and at each mutation's existing guarded commit.

Required consumer audit (no caller may be left on a disjoint new-only namespace):

| Consumer | Required behavior / risk |
| --- | --- |
| `drive_mutation` / `fetch` | Every archive writer holds both identities; all child FDs survive controller exit. Exact live identity admission remains mandatory. |
| `drive_bootstrap` | Bootstrap, refresh, dirty recovery and explicit serial transition share key expansion; transition cannot accidentally bypass PR 71 owner guards. |
| `admission.preview_by_drive` | Nonblocking observations contend on either key and retain fail-closed executable-capacity behavior. |
| `proposal._fence_keys` / approval | Approval evidence and commit are fenced by compatible identities; recapture stale pre-lock facts. |
| `execution_service` | Fill Start/Resume acquires the same expanded set, without production label-as-identity fallback for proven catalogs. |
| `execution_recovery` | Its separate inherit_drive_fence_fds acquisition must expand every drive and retain/pass every FD plus its marker. No label fallback, new marker-presence assumption or early release. |
| `drive_lifecycle` | Loss/revocation still conflicts with a Slice source reader using either identity; history preservation stays intact. |
| `slice.sources.FencedSources` | Old sealed candidates and fresh catalog bindings cannot use disjoint locks; stale source evidence still refuses. |
| `slice.domain` / `local_source` | Preview and artifact-open checks agree with the corrected canonical serial. Do not add a blanket ignore-serial or accept-either-fingerprint branch. |

Also search direct `drive_lock_path`, `drive_lock_key`, raw flock and inherited-FD consumers;
tests must inventory the importer/key-construction set to prevent a missed independent caller.

### C. Mandatory old-reader exclusion

Updated alias locking is not enough: an OLD Slice reading a repaired schema-7 catalog can hold
only the new key while an OLD Fill on an unrepaired schema-7 copy holds only the old key. Therefore
repair MUST raise the catalog reader floor atomically with the corrected fingerprint. Proposed
mechanism: catalog PRAGMA user_version 8. This is an explicit metadata/version migration in scope,
even if no new tables/columns are needed. It is distinct from private Slice schema 8.

New code supports the unchanged table layout of catalog v7 and v8; no automatic mass-upgrade on
open. Fresh catalogs can use v8. Older provenance versions retain their existing clone-first
upgrade requirements. Repair requires a consistent backup and clone rehearsal, then raises v7 to
v8 inside the same transaction as the first repaired drive. A failure must leave both the old
identity and old version intact. Repaired copies carry v8; unrepaired v7 copies remain protected
against updated operations by the permanent alias. Quiesce already-open old processes first.

Audit every normal opener: core/db read/write and version validation; Slice's explicit sqlite
reader; CLI/portal/Fill, hash-repair and deployment/migration validators. An old shipped binary
must refuse a repaired v8 catalog before reading source bytes or mutating archive state. Prove
that behavior with the actual pre-fix wheel, not only mocks of the new version check. Raw ad hoc
SQLite clients are not supported ModelArk authority and must not be used for repair or rollback.

Avoid gratuitously staling unrelated Slice seals: v8 changes the reader floor, not the v7 domain
record layout. Propose an explicit closed v7/v8 -> existing domain-snapshot-v7 adapter mapping
(document that field as the logical snapshot schema), keeping unchanged facts' hashes/seals byte
identical. Corrected source fingerprints still change their seals normally. Golden tests must
prove unaffected existing plans remain valid and affected plans refuse; do not achieve this by
ignoring fingerprints, anchor IDs or authority fields. Unknown catalog versions still refuse.
If this mapping cannot preserve truthful semantics and safety, return that compatibility impact
for approval rather than silently invalidating every unrelated in-progress Slice.

### D. Explicit, narrow existing-catalog evidence transition

Proposed operator surface: an explicit serial-identity repair mode on the existing drive
reconciliation command, preceded by a report-only inspection of affected drives and bound work.
Do not silently run it during catalog open, ordinary observation, Fill, approval or Slice preview.
Exact flag/API spelling can be settled during implementation review; approval must name the drive
and captured old identity so stale operator intent cannot repair a different state.

Eligibility requires ALL of:

- No live Fill/other active ModelArk controller; same controller/physical fences as recovery, plus
  both identity keys. Existing old-key writer/reader/child hold blocks the transition.
- Existing current generation is clean under the OLD persisted identity. Dirty state must first
  take the explicit legacy recovery bridge below; never erase ownership or skip missing claims.
- Strictly parsed current anchor proof identifies the recognized v1 null-serial case. Validate
  version/types/keys, UUID agreement, capacity and reproduction of the exact old fingerprint.
  Missing/malformed/contradictory proof is not an invitation to guess from two possible hashes.
- Live stable UUIDs and capacity are unchanged; physical-disk serial is proven and exactly matches
  the nonempty canonical saved serial. Recompute the intended canonical v1 fingerprint. A changed
  real serial, absent canonical serial, disk replacement or capacity change is a separate refusal.
- Full report-only inventory passes, followed by fresh identity/free-space observation while all
  fences remain held. Extras/debris remain untouched; this is not full-byte verification.

**Dirty legacy recovery bridge, available in the corrected binary:** report-only classification
may use a validated prior anchor proof in the same epoch with the exact current persisted old
fingerprint/capacity when the dirty current generation has no anchor. No matching historical proof
means explicit unproven/refusal, not guessing from a hash. For an operator-selected dirty owned or
sessionless generation, acquire both identities plus controller exclusion, prove live UUID/capacity
and canonical serial, apply PR 71's exact terminal-owner/child/CAS rules, inventory and take fresh
observation. Atomically publish ONLY that existing generation's OLD-identity clean anchor and
revision, leaving its owner/history unchanged. The proof retains the legacy null-serial identity
encoding; separately report the newly proven actual serial, not a claim that it was unavailable.
This bridge is allowed only inside explicit serial-repair workflow, never ordinary writers/readers.
Commit and report this recovery milestone before the separate enrichment step. If enrichment then
fails, the old-identity clean state is safe/retryable; do not roll back successful recovery by SQL.
Incomplete claims or unproven owner leave dirty and refuse. Never publish the new fingerprint on
the dirty generation. Test this with the real-style paused/no-expiry generation and prior anchor.

**Clean enrichment transaction:** within one short BEGIN IMMEDIATE, recheck no live session and exact captured old
epoch/generation/fingerprint/capacity/authority/UUID/serial, current clean anchor and relevant owner
evidence. In this precise order: verify old cleanliness; insert dirty generation G+1 with a distinct
operation code and null owner; atomically update the drive's generation and fingerprint; publish
the new clean anchor for G+1 using live serial-bearing proof. Preserve all old rows/anchors. Do not
update fingerprint before checking old cleanliness or publish before the new drive facts exist.
Private guarded primitives can be reused only if they implement this order in the same transaction;
their standalone/public wrappers are not a substitute for the composite transition.
Keep the epoch unchanged because neither stable identity nor capacity changed; the new generation
records this evidence correction. Supersede affected approved Fill proposals and clear the active
approval pointer IF it refers to an affected proposal (target/source/satisfying drive bindings all
count), using existing proposal machinery; preserve unrelated approvals and all immutable proposal
task rows. Raise the catalog reader floor and bump revision in this same commit. A revision bump
alone is insufficient because executable task identity checks can be epoch-only. Any failed check
or commit leaves this step's prior state intact. A rerun after success reports already-correct.

No table-layout changes are currently proposed: existing generation/anchor/proof records hold the
correction. The catalog reader-version migration above IS mandatory. Any additional DDL, generic
epoch-alias registry or new serialized authority format requires a separately explained scope delta.

### E. Existing work and rollback

- Archive files, paths, digests/provenance, residency, copy/task results, drive labels, plan membership
  and canonical descriptive serial remain unchanged. No re-download or re-ingestion.
- Paused/terminal session row, token, terminal reason and old generation owner stay unchanged.
  Affected Fill approvals are superseded: operator uses new Preview -> Approve -> Start (new session).
  Resume of the old session/proposal must refuse with stable superseded/approval-missing guidance,
  including systemd auto-resume. No implicit successor-resume onto a different proposal is added.
  Fresh planning must recognize already archived work; no task/archive reset or forced redownload.
- Preserve pending/approved/completed Slice journals, seals and any existing output. Old approvals
  remain tied to old identity; refuse stale execution with a clear re-preview action. Never reseal
  or transparently change a transaction's source. Completed receipts remain historical evidence.
- Destination folder target IDs, reservation overlap rules, filesystem profiles, private-state
  schema, hardware/protected-role admission and root certificates are not to change.
- Hash-repair state also binds fingerprint at (drive, epoch). Preserve it as old evidence, do not
  retag complete/halted results onto the new identity. Calls with the old fingerprint must halt/refuse
  under its existing CAS; any later hash repair must explicitly bind the new fingerprint. No hash
  repair or provenance rewrite is performed by this transition. Regression-test this separate consumer.
- Back up each catalog before its attended repair; rehearse on a consistent clone first. Offline
  drives remain unmodified and explicitly marked as requiring inspection, not mass-upgraded.
- A backup is not an automatic rollback: restoring an old catalog after new archive writes would
  discard subsequent state. Before any later writes, failed repair rolls back transactionally;
  after successful repair, old code must be rejected by the catalog version gate; updated-vs-old
  copied-catalog operations still contend through aliases. Do not promise that rolling back only
  the binary restores service. Do not auto-start the old portal. Deploy/rollout remains attended.

## 5. Test and review gates

1. Reproduce the actual partition/disk discrepancy below the helper boundary (findmnt names a
   partition, lsblk partition serial empty, parent serial present). Registration, archive observer
   and Slice observer must agree. Include whole disk, qualified single-parent mappings, cycles,
   ambiguity, missing nodes, probe failure, genuine serial absence and WWN-without-serial.
2. Seed a real-schema catalog with correct descriptive serial and authentic old null-serial proof;
   reproduce current Slice refusal, then exercise public inspection/repair/Preview/Approve/Start
   against disposable source bytes. Do not stop at isolated helper success or mocked final evidence.
3. Prove exact repair eligibility, no-op retry, unchanged history/content tables, old-anchor retention,
   same epoch/new generation, coherent canonical fingerprint and only intended revision changes.
4. Refuse wrong serial/UUID/capacity, unknown proof version, duplicate JSON keys, malformed proof,
   dirty owned/sessionless states, live sessions, held child/reader fences and stale operator binding.
   Define the dirty-state recovery route explicitly; test old paused catalog, not just our now-clean one.
5. Inject races before/after acquisition, during inventory and before BEGIN; rollback after each write
   point. Owner/session, current anchor and drive fact changes cannot publish partial repaired state.
6. Real cross-process lock matrix: old null-only holder vs updated reader/writer/approval/repair;
   updated holder vs legacy null-only process; corrected-key legacy holder vs updated process;
   copied catalog, different state dirs, stale candidate, released controller with surviving child,
   reverse label order and duplicate aliases. All must contend without deadlock or early release.
   Add the two-OLD-binary cell (old Slice/repaired catalog versus old Fill/unrepaired copy): old
   Slice must fail the version gate. Assert both epoch sides' aliases in capacity transitions and
   all FDs from execution_recovery's independent child acquisition. Distinguish truly absent serial
   from failed observation; a second acquisition of the same alias cannot deadlock its own process.
7. Test Fill re-preview/approval/restart behavior and preservation of completed copy/task results.
   Test old Slice seals refuse or remain readable as appropriate without changing output/receipts.
8. Existing archive mutation/admission/proposal/lifecycle/recovery/source/observer suites, golden
   plan seals, all-source lint, installed wheel and full CI must pass. Imported helpers must not
   broaden the reviewed mutation-envelope owners or introduce register/Fill/Slice dependency cycles.
   Test catalog v7/v8 accepted by updated code, v8 refused by old wheel, older provenance upgrade
   rules unchanged, reader floor rolled back on every failed enrichment, unchanged-domain v7/v8
   mapping preserves unaffected seals, and no automatic version/fingerprint upgrade on normal open.
9. Run copied-catalog rehearsal and read-only real-host identity parity before requesting separate
   live repair. Preserve the 2409-test baseline and add regressions; never edit fixtures solely to
   hide the old mismatch or turn an unproven observation into trusted evidence.

## 6. Delivery stages and approval gates

- **Approved implementation:** one draft PR, with independently tested commits in the order below.
  Do not deploy intermediate commits or the probe correction alone. If audit reveals additional authority
  consumers/format migration, report the delta before expanding implementation.
- Review each slice locally and with Greptile/Codex in the same PR before proceeding to the next.
  Keep review requests tied to exact heads and record findings/fixes. The established three-round
  iteration limit remains a stop-and-summarize gate if findings persist; do not loop indefinitely.
  Merge is the operator's action, not an automatic step.
- **After separate rollout approval:** rehearse backup -> quiescence -> exact-drive repair -> new
  source preview; verify before/after catalog invariants and only then resume the original tiny USB
  acceptance. Qualified FAT32 remount still requires an attended sudo password. No cleanup is implied.

### Implementation slices (mutable tracker)

| Slice | Scope | State |
| --- | --- | --- |
| 1. Shared observation | Extract neutral block ancestry; add tested mounted-path physical serial observer. Preserve Slice policies and leave the archive probe unchanged until safety integrations land. | Implemented; 336 targeted tests passed; PR review pending |
| 2. Compatible exclusion | Pure canonical/null-serial key expansion; every reader/writer/approval/recovery/lifecycle caller; both resize epochs; deduplication and child FD inheritance. | Pending |
| 3. Reader compatibility | Catalog 7/8 readers, no implicit upgrade, old-reader rejection, logical Slice schema mapping and unchanged-seal tests. | Pending |
| 4. Explicit repair | Enable corrected observation with early refusal; bound inspection, dirty legacy bridge, atomic clean enrichment and affected-approval invalidation. | Pending |
| 5. Qualification and handoff | Public workflows, fault/race and old-wheel matrix, full suite/installed wheel, clone rehearsal, operator docs and final PR review. No live migration. | Pending |

Slice-1 validation: 39 new command-boundary/ancestry cases plus existing observer,
source and direct/folder/FAT32 public integration tests: 336 passed, two upstream
Torch deprecation warnings. Repository-wide Ruff and diff whitespace checks passed.
The first sandbox run could not create four ACL fixtures; the host rerun passed
using disposable fixtures, without changing/skipping those tests.

Slice-2 audit note: independent execution recovery also manufactures `d0` after
proposal-load failure and can leak the just-opened handle if its raw flock fails.
Remove that unproven production fallback and make partial acquisition cleanup
exception-safe as part of the already approved all-caller/child-FD integration.

## 7. Questions for Grok

Is same-epoch/new-generation plus permanent null-serial lock alias the smallest safe repair, or is
there a concrete caller/old-client path it misses? Does the explicit repair preserve paused work
without silently validating an old proposal? Is a schema/seal change actually required? Identify
implementation surface we missed, unsafe topology assumptions, migration/rollback holes, and tests
that would still pass while the real workflow fails. Give ACCEPT or REQUEST_CHANGES with must-fix
versus optional points; do not implement or touch any live service/catalog/device.

## Local review record

Grok CLI session `01a0878a-6089-7821-8e86-bbe090fdcc8d`, review pass 1: REQUEST_CHANGES.
The bounded inspection reached its tool-turn limit; the same session then produced its verdict
without further investigation. Must-fix points: reader-version floor/two-old-client split; shared
bidirectional alias expansion including resize; all child FDs/no label fallback; early explicit
ordinary-path refusal; exact composite publication order; dirty legacy bridge; affected approval
invalidation; neutral ancestry extraction without destination-policy changes. All are now explicit
above. Added separate hash-repair CAS and unaffected Slice-seal compatibility checks.

Reviewer point 5 said advance and publish helpers cannot be reused. The actual issue is ordering:
advance under the old clean facts, update identity, then publish under the new facts can compose
inside one guarded transaction. The plan pins the order and prohibits convenience wrappers; it
does not require duplicating a correct private primitive merely to satisfy that wording.

Revised-plan confirmation in the same local CLI session: ACCEPT. Grok confirmed all eight
must-fix requirements are covered and accepted the guarded private-primitive composition.
It also confirmed the separate hash-repair CAS and two-old-binary test requirements.
This accepted the written plan only. The operator subsequently approved implementation and
PR publication; live migration remains a separate gate.
