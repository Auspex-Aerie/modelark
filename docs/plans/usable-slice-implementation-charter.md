# Usable Slice implementation charter

Status: architecture locked; Slice 1 domain implementation under review

Updated: 2026-09-07

Decision anchors: DEC-081, DEC-098, BOT-006, DEC-101, DEC-102, DEC-103, DEC-104, DEF-041, DEF-043

## Outcome

Usable Slice turns a named subset of artifacts already protected by ModelArk into a verified,
consumer-ready destination. It guides the operator through attaching known source drives, resumes
across attended swaps, verifies the final layout, and writes a durable delivery receipt.

The first usable result is deliberately small: materialize a few already-archived models directly
onto one USB destination. It does not acquire new models, alter the ark, or depend on the DGXSpark
catalog-expansion work.

## Product boundary

Usable Slice owns:

- an explicit catalog subsection and consumer/layout profile;
- a frozen artifact closure with canonical identities and digests;
- proof that every artifact has at least one qualifying ModelArk archive source;
- an immutable source, transport, and destination schedule;
- attended drive requests, resumable transfer, destination verification, and a delivery receipt.

Usable Slice does not own:

- discovery, selection, remote download, or Fill;
- ModelArk placement, desired-copy satisfaction, retention, or reclamation;
- treating a Spark cache, catalog row, scratch copy, or destination copy as archive evidence;
- silently widening the closure when an artifact is absent;
- formatting, erasing, or repurposing a destination without a separate explicit operator action.

Archive Reshape may eventually call the same sealed materialization core, but it is a separate
transaction with authority over ModelArk's desired set and placement. That authority is not part of
this charter's first implementation.

## Eligibility and source truth

Eligibility is evaluated for every file in the frozen closure, not merely for every repository.

A file is **source-ready** when ModelArk can resolve at least one archive copy with the required
artifact identity, content digest, provenance evidence, stable physical drive identity, and an
active lifecycle state. Lost, retired, or otherwise inactive drives retain historical evidence but
cannot satisfy slice recoverability. Placement eligibility is separate: an `active + excluded`
drive remains a valid read source if its other evidence qualifies; exclusion only forbids new
archive placement. The eligible drive does not need to be attached during preview.

Qualifying residency requires both the per-file archive/provenance record and a clean anchor for
the drive's exact current identity epoch and write generation, with matching identity fingerprint
and dedicated-local write authority. A clean capacity anchor alone is not proof of file contents;
original-byte digest verification is still required during delivery. Historical rows on a dirty,
unanchored, or identity-mismatched generation cannot establish source readiness, even if the drive
is mounted. If no alternative qualifies, report `SOURCE_RECONCILIATION_REQUIRED` with the exact
file and drive evidence. Reconciliation belongs to the separate archive workflow; preview never
performs it or interrupts an active Fill to obtain a clean source.

A source-ready file on an offline drive is **waiting for source**, not missing. Its source drive is
included in the approved schedule, and execution pauses with a precise request to attach that drive.

A file with no qualifying archive source is **archive-missing**. Preview is non-executable and
reports the exact repository, path, required evidence, and reason. The transaction offers no remote
fetch action. Catalog-only rows and machine-cache residency are insufficient. Historical rows on
inactive drives appear in the gap explanation but do not make it executable.

Source choice must be deterministic under the evidence snapshot. The seal may bind an ordered set
of qualifying alternatives, but execution can use only those pre-approved identities. Changed
closure, destination identity, or required digest invalidates the execution seal and requires a
new preview. Source lifecycle and residency validity are instead checked at each source use, under
the existing archive-drive mutation fence through the read. Acquire that fence without blocking
the live Fill; contention produces `SOURCE_BUSY`. Re-read the lifecycle, identity, generation,
anchor, and file evidence under the fence. An inactive, changed, dirty, or locally missing candidate
becomes unavailable; use the next qualifying sealed alternative or preserve the transaction in
`blocked_source` with a typed reason. A qualifying but offline candidate produces `waiting_source`.
Never add a newly discovered source to an approved seal. Source changes do not revoke completed
digest-verified checkpoints or retrospectively invalidate their recorded provenance; final
destination verification remains mandatory. Before the first write, unexplained capacity drift
invalidates the seal. After execution starts, remaining-capacity checks account for the
transaction's own journaled writes and invalidate only unexplained external consumption or mutation.

## Filesystem and device safety

Every source and output path must pass lexical confinement before it enters a sealed closure or
transfer plan. Reject absolute paths, parent traversal, platform-separator ambiguity, and NULs.
For offline sources, seal the safe relative catalog path and proven archive identity without
resolving an unavailable filesystem. On attachment, under the source fence and before each read,
prove device identity and descriptor-relative confinement, including annex link targets; reject
escaping symlinks and mount substitutions without a check/open race. Attached source observations
at preview do not replace this execution check. Destination paths additionally require resolved
confinement of their existing ancestors at preview/Start and bound descriptor access during writes;
reject any ancestor or symlink that escapes the approved root. Source reads use the
archive's local annex evidence but require a retrieval-disabled reader: it may resolve and read
locally present annex content but must never run `git annex get`, contact a configured remote, or
mutate the source archive. Missing local content makes that candidate unavailable; execution uses
another sealed qualifying source or stops with an attended source gap.

Destination and scratch identities are writable roles and must not match any registered ModelArk
archive identity, alias, or stable physical-device evidence. Source, destination, and scratch roles
must remain disjoint at preview and Start; a changed or ambiguous identity blocks execution. A
writable role must also be backed by a dedicated non-system device: reject any filesystem, partition,
or parent device that backs the host root, boot, or another system-managed path.

Preview binds the destination's stable device and filesystem identity, writable mount evidence,
available capacity, per-file size limit, path/name limits, and other capabilities required by the
chosen layout. Start revalidates them before writing. A read-only mount, unsupported filesystem, or
layout whose files or paths exceed those limits is non-executable even when aggregate free space is
sufficient. Scratch adapters must provide the equivalent capability evidence for their backend.

Execution holds descriptor-relative access rooted in the verified destination filesystem; later
writes never reopen an absolute mountpoint path. Bind device, filesystem, and mount identity for
the execution, revalidate before each file publication and resumed phase, and reject symlink or
nested-mount substitutions. Descriptor confinement must close the check/open race rather than
relying on a path check followed by an unrestricted open. Detachment stops in `waiting_destination`;
a replacement identity blocks writes. The same device may resume only after identity, writable
capabilities, ownership, journal, and capacity checks pass again. A vanished USB must never redirect
output into the host filesystem below its uncovered mountpoint.

The first implementation has no merge or overwrite mode. The approved consumer root is dedicated
to the slice, every planned output path must be absent, and unexpected existing content or a path
collision blocks preview or Start. Resume may recognize only exact paths and checkpoints written by
the same sealed transaction, including recoverable in-flight intents defined below; everything else
remains a collision. Recognizing transaction control files is a narrowly defined exception, not a
general existing-content allowance.

## Execution ownership and durable publication

The first implementation is single-host and direct-USB. Start claims an approved transaction with
an atomic compare-and-swap and takes a nonblocking exclusive destination-device lock shared by all
local slice workers, independent of catalog path, transaction ID, mount alias, and consumer root.
Both claims must succeed before any output mutation; a partial claim is recoverable without
starting a writer. Repeated Start for the same active seal returns that execution idempotently;
another owner receives `DESTINATION_BUSY`. The conservative whole-device lock also prevents
overlapping roots and competing capacity consumption by local slices.

A durable ownership record binds destination identity, root, transaction, and seal. It reserves
unfinished output across stops or process death; another transaction cannot reclaim it by elapsed
time or by replacing a lock file. Resume reacquires the process lock, proves prior workers and
children are gone, and reconciles the durable record before writing. Any child capable of writing
must retain the execution fence until it exits. Graceful stop quiesces writers before releasing the
process lock. Completion releases ownership only after final verification and durable receipt
publication; completed output still triggers the collision rule. Cross-host concurrent execution,
ownership takeover, and automatic abandoned-output cleanup are outside this first slice.

The journal and ownership metadata live in a private durable slice state store outside the catalog
and archive, with a transaction-bound control record on the destination. Before creating that
record, persist the host-side reservation and creation intent; exclusive creation under the device
lock makes interrupted initialization recoverable. An unrelated existing control record blocks
Start. Journal records bind the seal, destination identity, relative path, operation sequence,
expected digest/size, and transaction-owned temporary name. Names or matching hashes alone never
establish ownership of unrelated existing content.

Each file's prepared/completed record also binds the actual sealed source candidate read: archive
drive identity, identity epoch, write generation, clean-anchor reference, annex/artifact identity,
and per-file provenance/digest evidence observed under the source fence. A fallback records the
chosen alternative, not merely the candidate list. Completion references the durable prepared
record, and the final receipt carries these actual per-file sources alongside the approved source
set. Later lifecycle changes cannot rewrite that history. Incomplete data with no authenticated
source record cannot become completion merely by matching a digest; restart that file from a
currently qualifying sealed source or block.

Each directory creation, file publication, and receipt publication follows a recoverable protocol:

1. Durably append an intent before creating a transaction-owned path. Create directories and
   temporary files exclusively through the bound destination descriptor; record parent/child
   ownership, including creation of the consumer root. For every new directory, flush its metadata
   and its entry in the parent directory, then durably append directory completion before creating
   children. Apply this to the private state/control directories as well as the consumer layout;
   the pre-existing destination root is the durability boundary. Unexpected content is a collision.
2. Stream into the owned temporary file, verify the required original-byte digest and size, flush
   the file, and durably record its prepared state. Journal in-flight allocation as well as completed
   files so crash recovery can reconcile the transaction's own capacity consumption.
3. Publish with an atomic **no-replace** operation on the same filesystem, flush the containing
   directory, then durably append completion. A filesystem without the required durability and
   no-replace semantics is rejected at preflight. The receipt uses the same publication protocol.
4. On recovery, hold exclusive ownership and validate the seal, destination, and intent/prepared
   records before inspecting exact owned paths. Reconcile an interrupted directory creation or
   rename against those records; rehash a published file before completing a missing checkpoint.
   Resume or recreate incomplete temporary data only within authenticated ownership. A checkpoint
   without its expected output is not completion: restore that file from a still-valid sealed
   source, or block. Contradictory records, digest mismatch, or unrelated content stop with a typed
   recovery/collision error and never authorize overwrite or deletion of unknown bytes.

Authentication here means provenance from the private state store plus matching destination
ownership and sealed operation records, not a self-asserted filename or an imported journal. The
first slice does not support checkpoint adoption across successor transactions. A source block
retains its original seal and progress; changing the approved source set requires a separately
approved transaction on an empty destination root, leaving earlier output intact. Other seal
invalidations likewise preserve evidence without promising automatic successor resume.

## Transaction and state model

The workflow is one resumable transaction with an immutable approved core:

1. **Draft scope** — select repositories/artifacts and a consumer/layout profile.
2. **Freeze closure** — resolve every required file and digest under one catalog/evidence snapshot.
   The immutable file manifest is authoritative; a repository commit SHA is included only when it
   was captured as trustworthy evidence and is never inferred by querying the current remote head.
3. **Resolve sources** — choose qualifying archive copies and build an attended drive schedule.
4. **Preview** — show exact bytes, lifecycle-active source drives, destination writes, scratch use,
   device/filesystem capabilities, capacity, gaps, and verification policy. Any archive-missing file
   makes the preview non-executable.
5. **Approve** — bind the closure, evidence snapshot, source choices, transport, destination identity,
   capacity evidence, and layout to one seal. Approval does not begin transfer.
6. **Execute** — claim exclusive ownership, recover the journal, and copy checkpointed content;
   request only the source, scratch, or destination device required for the next phase; preserve
   completed evidence across stops.
7. **Verify and publish** — validate destination content and layout before publishing a receipt.

Public transaction states should distinguish at least `draft`, `blocked_gaps`, `ready`, `approved`,
`waiting_source`, `blocked_source`, `waiting_scratch`, `waiting_destination`, `transferring`,
`verifying`, `complete`, `stopped`, `invalidated`, and `failed`. A wait is resumable and names the missing physical resource;
an invalidation requires a successor preview.

## Core records

- **SliceSpec** — requested subsection, consumer profile, and destination intent.
- **ArtifactClosure** — repository identity plus the exact immutable file manifest, sizes, and
  digests. Repository revision is optional evidence, not a value reconstructed from a mutable remote.
- **SourceEvidence** — lifecycle-active qualifying archive copies, ordered sealed alternatives, and
  the evidence snapshot that supports them.
- **TransferPlan** — deterministic source order, topology, capacity and filesystem-capability
  charges, checkpoints, and final layout operations.
- **SliceApproval** — operator approval bound to the exact preview seal.
- **SliceJournal** — durable ownership, creation intents, prepared publications, and append-only
  phase/file progress bound to the transaction, suitable for crash reconciliation and safe resume.
- **SliceReceipt** — catalog snapshot, slice definition and consumer profile, closure, source
  evidence, topology, destination identity, verification results, and terminal status. It is
  delivery evidence, never ModelArk replica evidence.

These records should be transport-neutral. USB direct, local scratch, and R2 scratch adapters must
not reinterpret closure, evidence, approval, or receipt semantics.

## Transport modes

### Direct USB

The source archive drive and destination are attached together. Verified bytes stream directly to
the destination with bounded temporary state. This is the first implementation target.

### Local USB scratch

Verified bytes stage onto an explicitly identified scratch volume so source reads and destination
writes can happen in separate attended phases. Scratch has its own capacity and identity gates and
may be discarded only after destination verification and receipt publication.

### Cloudflare R2 scratch

R2 provides the same staged topology across machines or time. The adapter must bind bucket/object
identity, encryption and credential policy, lifecycle/cleanup, cost preview, multipart resume, and
digest verification. R2 objects remain transport state, not archive copies.

Scratch adapters follow the direct-mode core; they are not prerequisites for proving the first
vertical slice.

## Operator workflow

The operator should be able to:

1. choose a named subset from the current catalog and a target layout;
2. see whether the entire closure is recoverable from the existing ark;
3. inspect exact gaps, bytes, source drives, swaps, scratch use, and destination impact;
4. approve the exact stored preview without starting it;
5. start when physically present, attach each requested device, and resume after safe stops;
6. receive a verified destination and a receipt that states precisely what was delivered.

The UI must never turn `archive-missing` into an invitation that implicitly downloads content. It
may link to the separate catalog/Fill workflow as explanatory next work, but that workflow requires
its own planning and approval.

## First vertical slice

The first implementation milestone is intentionally narrow:

- choose two or three small, already-archived model repositories;
- support one minimal consumer/layout profile and one explicitly identified USB destination;
- derive and seal the exact closure and source-drive schedule;
- fail preview with a structured gap when one test artifact lacks archive evidence;
- support direct copy, attended offline-source requests, stop/resume, final digest/layout
  verification, and a receipt;
- read only locally present archive content through a retrieval-disabled source adapter;
- prove that preview and execution perform no network acquisition and do not mutate catalog,
  selection, plan, proposal, Fill, placement, or archive-copy evidence.

Local USB scratch and R2 scratch follow only after the journal, verifier, and receipt are stable.
Archive Reshape follows as a separate transaction after the materialization core is proven.

## Acceptance gates

- Every executable preview has a qualifying archived source for every closure file.
- Catalog-only and cache-only fixtures produce exact blocking gaps and zero remote fetch attempts.
- A fixture whose only historical archive row belongs to a lost or inactive drive produces a
  blocking gap rather than an impossible drive request.
- An otherwise qualifying `active + excluded` drive remains an executable read source, mounted or
  offline. Toggling placement eligibility alone neither invalidates the slice nor blocks reads.
- Dirty, missing-anchor, old-epoch, old-generation, and mismatched-fingerprint sources cannot
  establish readiness from retained archive rows; a clean alternate may qualify. A capacity anchor
  without qualifying per-file provenance is insufficient. No source check repairs the archive.
- Lifecycle or residency changes before a source read preserve the seal and verified checkpoints;
  only a still-qualifying sealed alternative may be used. Otherwise return a typed source block,
  with no new source approval, implicit fetch, or successor adoption. Source-fence contention waits
  without interrupting Fill. Source digest mismatch never produces a completed checkpoint.
- Offline qualifying sources produce attended drive requests and resume without replanning completed
  work. Preview never resolves paths on an offline source; attachment performs confined descriptor
  resolution before reads and rejects escaping annex links or replaced mounts.
- Closure identity is the immutable per-file manifest and digests; absent historical commit SHAs are
  never synthesized from a current remote lookup.
- Approval and Start are separate; drift cannot silently substitute a source or destination.
- Destination and scratch identities cannot resolve to ModelArk archive drives, and their writable
  roles remain disjoint from every source identity.
- Destination and scratch backing devices cannot be the host root, boot, or another system device.
- Preview and Start reject read-only or incompatible filesystems and layouts exceeding per-file,
  path, or name limits, even when aggregate capacity is adequate.
- Unsafe or escaping paths, existing destination content, and unjournaled path collisions block
  preview or Start; the first implementation never merges or overwrites.
- Capacity revalidation subtracts sealed journaled writes, while unexplained external consumption
  invalidates the transaction.
- Interrupted transfers resume from durable checkpoints and never publish an unverified receipt.
- Fault injection covers intent persistence, directory/root creation, partial temporary writes,
  file flush, prepared record, no-replace publication, directory flush, completion append, and
  receipt publication. Recovery recognizes only owned intents, revalidates bytes, accounts for
  temporary allocations, and rejects unknown content even when its digest matches. Inject failure
  before/after each new directory's parent flush and completion append, including consumer-root
  and state/control-directory creation; no durable receipt may depend on uncommitted directory entries.
- Sealed-alternative fallback followed by stop/resume and a source lifecycle change retains the
  actual per-file source identity, epoch/generation, anchor, and provenance in checkpoints and receipt.
- Concurrent/retried Start, different roots or aliases on one device, a crashed owner, and a
  surviving child prove at most one writer. Stopped ownership cannot be stolen by another seal;
  failed partial acquisition is recoverable and a duplicate Start is idempotent.
- Unplug, mount replacement, symlink/nested-mount substitution, and resume between write boundaries
  produce no host-filesystem writes and no wrong-device receipt; the original device resumes only
  after identity and ownership are reproved.
- A locally absent annex object never invokes `git annex get` or any remote; execution uses only a
  sealed locally present alternative or stops.
- Destination verification covers both content and the chosen consumer layout.
- A completed receipt cannot satisfy ModelArk replica policy or mutate archive placement.
- Existing Fill behavior and a running approved Fill remain untouched.

## Independent Spark catalog lane

DGXSpark recipes and caches are discovery inputs. The separate lane freezes a named Spark profile,
discovers missing repositories, reviews selection, and archives approved artifacts through normal
ModelArk planning and Fill. Only after those artifacts carry sufficient archive evidence may a
Usable Slice include them.

This separation allows catalog work to follow fast-changing weights and recipes without changing
the delivery contract or making the first slice depend on content ModelArk does not yet protect.

## Explicitly deferred

- hydrate-then-materialize composition (DEF-043);
- local USB and R2 scratch adapters beyond their stable interfaces;
- Archive Reshape authority and physical reclamation;
- P2P transport and trust policy;
- multiple consumer profiles and automatic destination preparation;
- treating any delivery or scratch copy as archive durability evidence.

## Safe stopping point

This charter and its ledger decisions are the stopping point. No implementation, live deployment,
service restart, Fill action, catalog expansion, or archive/destination byte movement is part of
this change. The next implementation session starts with domain contracts and expected-red tests
for eligibility, exact gap reporting, and the sealed direct-USB transaction.

## Development entry and review slices

Slice 1's internal API and limits are documented in [Usable Slice domain](../usable-slice-domain.md).
Its domain approval binds intent and archive evidence; execution readiness stays false until the
later hardware preflight and durable ownership contracts are implemented.

Finish PR #67's renewed bounded review (up to three iterations) at its exact pushed head and let the
operator merge it. Then branch from that merged charter for implementation. The first test commit
defines expected-red domain contracts; failures must identify missing behavior rather than broken
fixtures. Preserve separate test and implementation commits so the contracts remain reviewable.

| Slice | Deliverable | Required evidence before advancing |
|------|-------------|------------------------------------|
| 1 — Domain | Immutable closure/source facts, exact gaps, deterministic alternatives, canonical preview seal and explicit approval; pure core with a read-only catalog adapter. | Synthetic clean/dirty/offline/inactive matrices, shuffled-order seal stability, tampered/stale approval refusal, no catalog writes or remote access. |
| 2 — Transaction | Private state store, idempotent Start, device ownership, source-use gates, journal and recoverable publication. | Disposable filesystem and multi-process fixtures for every crash/concurrency boundary above; no real mounts, live state, or archive paths. |
| 3 — Direct USB | Retrieval-disabled source reader, device-bound destination adapter, stop/resume, final verifier and receipt, minimal operator entry point. | Two or three tiny archived fixtures, exact gap and drive-wait demonstrations, isolated end-to-end and installed-wheel checks; full existing regression and static checks. |

Candidate code seams are a new `modelark/slice/` package and `tests/test_slice_*.py`. Inspect
`modelark/restore.py` for confined paths and original-byte verification, `modelark/drive_fence.py`
for mutation exclusion, and catalog generation/anchor readers for evidence. Reuse only helpers
whose contracts fit: the materializer must not inherit restore's optional annex retrieval or
turn capacity evidence into file verification. Keep durable slice state out of catalog schema and
keep production Fill wiring unchanged. Decide any API/CLI integration details against the proven
domain core in slice 3, rather than coupling the first tests to a UI.

An attended trial with real archived models and a real USB destination is a later operator gate,
after these disposable-fixture checks. Return with the exact source schedule, destination identity,
byte/capacity preview, and stop/recovery evidence before any real destination write. Formatting,
erasure, archive mutation, deployment, and live Fill changes require their own explicit direction.
