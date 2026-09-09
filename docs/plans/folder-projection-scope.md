# Folder-based projection — approved scope

Status: approved 2026-09-09 after Grok CLI critique (DEC-130). Gate A contracts and Gate B native
ext4 and FAT32 session execution are implemented. Both FAT32 image qualification runs passed;
PR review and real archive/USB acceptance remain pending. Native authority/versioning is in DEC-131.

## Current checkpoint: public FAT32 routing

- This checkpoint supersedes earlier "disabled" and "image not run" statements below; those
  describe prior implementation stages. Changes remain local, uncommitted and undeployed.
- Operator-reported first kernel-vfat image qualification passed on 2026-09-09; artifacts are in
  `/tmp/modelark-fat32-qualification-reohetp0`. No-replace/flush, payload/report, 30 interruption
  boundaries and sibling preservation passed. This was not a physical USB or archive test.
- Public exact-folder Preview/Approve/Start routing is implemented. FAT Start dispatches by the
  sealed plan; the mount hint cannot bypass actual admission or trigger fallback after refusal.
  The name codec is narrowed to the image-tested codepage437/iso8859-1/utf8/shortname=mixed profile.
- Supplemental real-driver exact/case/numbered-short-alias checks and public workflow with
  synthetic source/backing passed in the operator's expanded attended run, with artifacts at
  `/tmp/modelark-fat32-qualification-ru4sibe7`. Every earlier writer/fault/sibling check passed again.
  Source reconciliation, physical target acceptance and review remain separate gates.
- Final full Slice source regression: **1,287 passed, 5 optional-codec skips**, two upstream Torch
  deprecation warnings. The new routing-error test exposed an unclassified mount-inventory IO
  failure; the hint now returns a typed refusal and all six routing tests pass in the full run.
- Fresh wheel installed without dependency resolution into
  `/tmp/modelark-fat32-public-wheel.EtJEk4/package`: **622 passed** across FAT/native folder
  contracts, plans, observation, transactions, public routing/operator and mapper integration,
  including an installed-import-location assertion. Copied tests ran outside the checkout;
  packaged Slice sources match the checkout. Ruff and diff whitespace checks pass.
- The supplemental result, not the first image result, supplies public-flow/alias evidence.
  Neither result supplies physical USB or real archive acceptance. No new DEC was added.

## Implementation checkpoint: Gate A

- Branch `codex/folder-projection`, worktree `/tmp/modelark-folder-projection`, based on reviewed
  PR #70 head `6fd87c3`. PR #70 was still open when checked; it has not been merged by this task.
- Additive non-executable folder target/profile/capacity/review contracts and conservative overlap
  semantics. Legacy decoder rejects mixed/unknown envelopes before reconstruction. No public CLI,
  folder writer, state migration or new lease namespace is enabled at this gate.
- Validation: 160 new contract/isolation tests; 354 source tests including transaction/operator/
  authority regressions; 160 new tests against a freshly built, separately installed wheel.
  Ruff and diff whitespace checks pass. Legacy v1/v2/v3 seal hashes match `6fd87c3` goldens.
- The initial sandboxed regression run failed on prohibited Unix-socket binds; the successful
  regression run used approved socket access. Two new fixture-constructor mistakes were corrected
  before the successful runs. No live media/service/archive operations were performed.
- At that checkpoint neither filesystem writer was implemented. The following Gate B checkpoint
  supersedes that implementation status, not the approved scope or legacy golden evidence.

## Implementation checkpoint: Gate B

- Same isolated branch/worktree and reviewed base as Gate A; no merge, commit, push or live
  deployment has been performed. The main checkout and real USB/archive bytes are untouched.
- Native exact-child CLI preview/Start dispatch, closed executable plan, private schema 7,
  durable overlapping-root/legacy-device exclusion, shared capacity, internal control/receipt,
  exclusively certified creation and authenticated Stop/fresh-Start resume are implemented.
  Explicit preview `--root` retains the legacy strict path; old seals are not reinterpreted.
- Integration review corrected canonical backing-ID ordering at the observer/plan boundary and
  Ctrl+C acknowledgment before lease teardown. Real-host observation exposed PATH/KNAME mapper
  spelling and protected procfs birth-time assumptions. Narrow shared-helper corrections preserve
  ancestry conflicts and owned-object birth requirements; source qualification is unchanged.
- Real observer plus native writer smoke completed with 12 synthetic bytes in a newly created
  disposable `/tmp` directory on the host's ext4/LUKS/LVM storage. The sibling sentinel survived;
  control/receipt and verified payload stayed inside the child. This is not a real archive/USB
  trial or a power-loss/performance qualification. No live catalog or archive was opened.
- Final full Slice source suite: **982 passed, 5 optional-codec skips**, with two upstream Torch
  deprecation warnings. The earlier run's sole failure was the expected lsblk column list after
  adding KNAME; the corrected expectation passes in this full rerun. Ruff and diff checks pass.
- Fresh wheel built without isolation and installed without dependency resolution into
  `/tmp/modelark-folder-native-wheel.v593HU/package`: **378 passed**, covering native contracts,
  plans, observation, transactions, operator/CLI dispatch, legacy isolation and Linux role/mapper
  helpers. Tests were copied outside the checkout and asserted the installed import location;
  packaged Slice sources match the checkout. The first custom-runner approval timed out before
  execution; the permitted retry used the approved pytest entry point and completed successfully.
- Next: review the FAT32 write/failure table before its adapter, then qualify session-only export
  in Gate C. Gate D still requires separately authorized source clean-anchor reconciliation and an
  exact reviewed physical target. Native success does not make the user's FAT32 USB executable.

## Gate C protocol checkpoint

- Architecture consultation identified the separate-launch preview token lifetime, consumed
  attempt fencing, execution-claim versus residue lifetime, descriptor budget and media-report
  versus host-completion seams. [The FAT32 protocol review](fat32-session-protocol.md) records the
  failure table and recommends separate durable path/backing intent from fresh live Start authority.
  The operator approved that distinction and requested no new DEC. The public FAT path remains
  disabled pending real kernel-driver qualification, not another design decision.
- Additive non-executable FAT32 name/size/full-tree/staging-layout preflight is implemented.
  It is not filesystem, short-alias, permission, flush or execution qualification. Actual vfat
  mounted-image tests remain pending. Internal writer/state integration is covered below.
- Validation: 96 new layout tests; 330 source contract/layout/legacy-isolation/native-plan tests;
  331 checks against a fresh independently installed wheel (including import-location assertion).
  Ruff and diff whitespace checks pass. No native/legacy writer behavior changed in this checkpoint.

### Gate C internal runtime checkpoint

- Distinct FAT intent/plan with no saved parent identity; fresh retained `Fat32Tree` and per-object
  descriptor authority; dedicated named-staging/no-replace port; schema-8 claim consumption before
  first destination write. Every ended attempt refuses fresh Start; disjoint new sibling intents
  retain their own scope while old residue remains claimed and untouched.
- Report says export verified, not host transaction committed. Any interrupted completion gap
  remains nonresumable. Native/legacy default birth identity and resume policy remain unchanged.
- Read-only observer requires direct USB/vfat/FAT32 evidence, closed name/mount options, canonical
  parent enumeration and current-user non-group/other-writable masks; no permission rewriting.
- Actual kernel-vfat image test is blocked on the operator's sudo password. The prepared
  `scripts/qualify_fat32_slice.py` creates/formats only a new disposable image and mounts/unmounts
  it interactively. Public FAT Start unconditionally refuses qualification-required; there is no
  bypass flag. No existing USB, archive or live service has been changed.
- Integrated full Slice source run: **1,258 passed, 5 optional-codec skips**, two upstream Torch
  deprecation warnings. Subsequent review corrections separate consumer/read errors, enforce
  retained-descriptor close before host completion and restore FAT inventory KNAME; their focused
  regressions pass. The final independently installed package run covers those corrections.
- Final installed-package regression: **800 passed**, including all FAT plan/layout/observer/
  transaction/public-gate tests, native contracts/operator/transactions, Linux/hardware helpers,
  legacy CLI/operator/authority and import-location proof. Packaged Slice sources match the
  checkout; Ruff and diff checks pass. The attended image script's help path was exercised;
  its privileged mount/driver test has not run. Changes remain local and uncommitted.

## Outcome

Project an approved, immutable slice of already archived artifacts into a new folder chosen by
the operator. Own that folder, not its parent filesystem or drive. The full arc includes ordinary
ext4 local folders and the user's existing FAT32 USB without formatting it. This is not a promise
that every writable filesystem supports identical recovery or durability semantics.

Example: `--destination /home/operator/exports/demo` names the exact new output root. Its parent
must already exist. Preview creates no destination objects. Start creates the root exclusively;
all destination staging, control and receipt files live inside it. Host-private journals remain
in private state. Existing roots are refused except for authenticated same-transaction resume.
Do not adopt/merge existing content, rename files to make them fit, or touch unrelated siblings.

## Why this is more than a path change

Code inspected in the reviewed direct-USB worktree at `6fd87c3`:

- `hardware.py` combines attachment observation with exact mount-root, USB, direct-block, ext4
  and non-system-device policy.
- `capacity.py` and `destination.py:UsbDestination.check` both depend on exclusive-filesystem
  free-space accounting. Merely changing the observer leaves shared folders unusable.
- `destination.py` relies on O_TMPFILE, hardlink publication, xattrs and host-private inode/birth
  certificates. `linux.py` requires mount and birth identity. These cannot be assumed on FAT.
- `transaction.py`, `authority.py` and `state.py` bind plans, receipts, reservations and live
  attempts to a device-oriented identifier. Folder identity needs explicit versioned semantics.
- Control data is currently placed at the attachment root; that is no longer an acceptable
  ownership boundary when the attachment is an existing shared parent.

The recurring architectural assumption is **exclusive filesystem use standing in for exclusive
output-tree ownership**. Fix the boundary and the dependent contracts, not just filesystem checks.

## Proposed work

### 1. Folder identity, ownership and protection

Separate target identity, attachment evidence, filesystem capabilities and policy. Seal the
existing parent's identity and intended child name; durably bind the created root after exclusive
creation. Revalidate attachment and root identity on Start/resume and before mutation. A matching
pathname, UUID or marker alone must not authorize recovery.

Allow ordinary home/data folders even when their storage also backs the OS, including qualified
local encrypted/mapped storage. Protect OS-managed subtrees and ModelArk private state by path/role,
not by banning the entire system disk. Keep registered archive backing devices excluded initially.
Specify the protected subtree policy explicitly during contract review, including how catalog and
runtime paths are discovered; do not treat `/` as a blanket protected ancestor of every folder.

Retain descriptor-relative confinement and no-clobber publication. Initially reject symlink
ancestors, unsupported aliases and nested mounts inside the owned tree with precise diagnostics.
Recognized mount/alias relationships must identify the same target; ambiguous ones fail closed.
Target exclusion must detect same/overlapping roots, including roots not yet created, without
locking a whole drive. Keep the existing application-launch singleton; no new concurrency system.
Unrelated sibling changes are allowed. Mutations inside an active owned tree are not.

### 2. Shared capacity and honest completion

Replace strict free-space conservation with available-space/inode observations, explicit bounded
or estimated metadata/headroom policy, and repeated remaining-space checks. Preview is not a
reservation or guarantee against other applications consuming space. Record that distinction in
approval and receipts. Do not invalidate merely because a sibling file changed.

ENOSPC and quota exhaustion need typed outcomes rather than accidental identity invalidation.
Native folders preserve only authenticated checkpoints; fresh Start can proceed after capacity is
restored. Proposed FAT32 v1 instead requires a new root after an interrupted attempt, including a
space-related stop. Include host journal/control-disk exhaustion and interrupted outcome persistence in the
fault matrix. No automatic cleanup of uncertain objects. Preallocation, if used, is confined to
owned files and never presented as reserving all future metadata needs.

Final completion still requires full original-byte hashes, expected layout and successful receipt
persistence under the declared profile. Distinguish current verification from stronger claims about
surviving sudden power loss. Do not claim a portable profile inherits ext4's guarantees.

### 3. Two concrete capability profiles

**Native Linux folder profile:** qualify ext4 operations needed for confinement, ownership,
no-replace publication, flush and recovery. Reuse existing certificates where applicable, without
the raw-superblock/external-xattr-allocation capacity model. Handle effective permissions and ACL
inheritance explicitly; do not change ownership, modes or ACLs to force admission. Ordinary folder
use must not require privileged raw-device reads. Filesystem type alone is not capability proof.

**FAT32 session profile:** a separate qualification task, not a flag on the native adapter.
First specify a failure-state table for root creation, named staging,
publication, control writes and receipt persistence without assuming xattrs, O_TMPFILE, hardlinks
or persistent inode birth identity. Host-private records and destination markers corroborate
ownership; a random marker or matching hash/path does not independently prove it.

Recommended v1 scope: do not resume a stopped or interrupted FAT32 destination. Leave residue
untouched and require operator recovery or a fresh output root, including after graceful Stop.
This is a conservative release boundary, not a proof that future safe portable resume is impossible.
Disclose those limits before approval. Do not silently
adopt/delete a residue tree or weaken the native profile to accommodate portable media. The
failure-state table still matters even without resume: it proves no unrelated data is touched and
no interrupted run becomes a false completion. Native graceful Stop/fresh Start remains resumable.

Use closed versioned profiles, not a combinatorial capability/plugin framework. XFS and exFAT
qualification are proposed follow-on work, not part of this first delivery. FAT32 support does not
implicitly qualify exFAT. Ordinary mapped/encrypted ext4 home storage is still an acceptance case
when mounted backing-role checks are unambiguous; no new raw-device forensic subsystem is needed.

Portable publication must prove no-replace behavior on the supported driver; ordinary overwriting
rename is not a fallback. Required flush failures block completion, never become "best-effort"
success. Explicitly specify the ordering of intent, file publication, receipt and host completion
records, including failure to persist any one of them. Completion evidence must be profile-specific
without calling any delivery copy newly verified archive evidence.

Validate the entire closure, including staging/control filenames, against file-size, component,
path, case-collision and name rules. Refuse unsupported layouts before the first output write;
no splitting or mangling to bypass FAT limits. Do not format the existing USB. Remote shares,
FUSE/cloud mounts and cross-platform execution are outside this first arc.

### 4. Integration and compatibility

Reuse domain/catalog closure, original-byte verification, source fences/readers, decoding,
transaction Stop/Start machinery, journal concepts and shared IO error classification. Adapt the
destination port and its budget/identity contracts where necessary rather than duplicating the
whole transaction engine.

Migrate the existing direct-USB public path to the same folder-facing contract for NEW plans.
Keep old approved plans and in-flight state under their original versioned strict policy; do not
reinterpret seals or certificates. Test compatibility dispatch across plans, admission data,
receipts, reservations and certificates. Removing legacy execution support is a later explicit
decision, not an incidental cleanup.

Fill and archive write authority stay unchanged. Inspect existing restore for genuinely shared
path/capability utilities, but do not force it through Slice planning, ownership or state merely
for consistency. No generic backend/plugin framework; extract only contracts used here.

## Reviewable delivery gates

1. **Contract and identity:** agree semantics, protected paths, capability/receipt claims and state
   versions; implement folder targeting/exclusion and legacy dispatch with focused safety tests.
2. **Native folder execution:** replace both capacity assumptions, internalize destination control
   files, qualify native IO; tests on system-backed/mapped storage, populated parents, unrelated
   sibling writes, mount/root replacement, aliases/overlap, permissions, ENOSPC/quota, Stop/resume,
   original hashes and installed-package regressions.
3. **FAT32 session profile:** review the write/failure protocol BEFORE adapter implementation.
   Capability tests on FAT32, then interruption-at-every-write-stage, name/case/file-limit,
   no-clobber and truthful receipt tests. If safe recovery demands a broader protocol, return for
   scope approval rather than silently expanding or weakening guarantees. Start with tiny fixture
   data; preserve the original-byte closure rules for the later real archive trial.
4. **Attended acceptance:** after separately authorized source reconciliation, preview a small
   archived model against the actual FAT32 USB. Preserve sibling sentinels, verify output hashes,
   layout and receipt, and exercise native resume versus FAT32 Stop/new-root restart. Deliberate unplug/power-loss
   tests require separately approved disposable-media testing.

These are bounded review gates, not a promise of exactly four PRs. The native gate alone does not
complete the user's requested arc: the current FAT32 USB is an explicit acceptance target.
Installed-package regression belongs on every write-path change, not only the native gate.

## Exclusions and independent blockers

No downloads, Fill changes, archive reshape, adoption/merge, projection garbage collection,
cross-host resume, scheduler or portal redesign. No formatting, sudo/ACL changes or archive repair
is authorized by this proposal. The observed source clean-anchor reconciliation requirement is
independent of destination support and must not be bypassed to make the test pass.

## Approved scope (2026-09-09)

- Native ext4 folders plus the FAT32 session profile, staged as above; XFS/exFAT qualification
  remains outside this first delivery.
- FAT32 Stop/interruption requires a new output folder in v1; native ext4 retains authenticated
  resume. Portable resume is not part of this implementation.
- Observed/headroom capacity admission, not guaranteed exclusive capacity.

Recommended default: native ext4 plus FAT32 only; keep safety and honest evidence invariant;
accept explicit FAT32 restart-in-new-folder limits rather than fabricate ownership proof.

## Grok review status

Completed through the installed Grok CLI after the user explicitly approved sending the prepared
summary. Tools and web search were disabled. The first connected run reached its one-turn limit
after an acknowledgment; a four-turn retry returned the complete critique with exit code 0.
Grok explicitly reviewed supplied facts only, not repository code. No independent filesystem
experiment was performed. Reviewer model ID was not verified; attribution is to Grok CLI.

Prepared review prompt: `/tmp/modelark-folder-projection-grok-scope.txt`. This is the INITIAL broader
proposal sent for critique, not the revised recommendation above. It contains summarized contracts,
not source-file uploads, credentials, catalog contents or user file contents.

### Incorporated recommendations

- Narrow first delivery to native ext4 plus FAT32 session export; propose XFS/exFAT separately.
- Split capacity observation from target identity. An advisory snapshot may remain in the sealed
  review record, but a later free-byte difference is not itself destination substitution.
- Keep device-wide leases only for legacy strict transactions; folder transactions use target
  scope and never acquire drive-wide destination authority.
- Make parent-to-created-root transition, live attachment versus durable identity, exact owned-tree
  layout, and host/destination persistence ordering explicit.
- Use closed versioned profiles, preserve old seals, and regress installed packages per write PR.
- Make FAT32 no-resume a visible operator choice rather than smuggling in a recovery project.

### Qualifications and disagreements

- Grok called the proposal a "second product." The portable ownership work is substantial, but
  the existing closure/source/verification engine remains reusable; duplicating it is not justified.
- Its categorical claim that safe FAT recovery is infeasible was not proven by a summary review.
  No-resume is the recommended v1 boundary, not an impossibility result.
- Its "best-effort" fsync suggestion and shorthand staging-plus-rename do not establish our
  completion/no-clobber contract. Keep explicit required flush and no-replace qualification.
- Do not drop ordinary encrypted/mapped ext4 home support merely to avoid a device-mapper project;
  inspect mounted backing roles conservatively, while allowing normal system-backed user folders.
- Its suggested PR-D Stop/fresh-Start acceptance needs profile distinction: native resumes;
  proposed FAT32 v1 starts into a new root. The gates above correct that inconsistency.

Grok's review was advisory. The operator subsequently approved the narrowed scope, including
FAT32 restart in a new folder, on 2026-09-09 (DEC-130). Filesystem qualification and source
reconciliation remain separate work; consultation alone did not establish them.
