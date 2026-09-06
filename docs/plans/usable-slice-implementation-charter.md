# Usable Slice implementation charter

Status: architecture locked; implementation has not started  
Updated: 2026-09-06  
Decision anchors: DEC-081, DEC-098, BOT-006, DEC-101, DEF-041, DEF-043

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
active lifecycle state. Lost, excluded, retired, or otherwise inactive drives retain historical
evidence but cannot satisfy slice recoverability. The eligible drive does not need to be attached
during preview.

A source-ready file on an offline drive is **waiting for source**, not missing. Its source drive is
included in the approved schedule, and execution pauses with a precise request to attach that drive.

A file with no qualifying archive source is **archive-missing**. Preview is non-executable and
reports the exact repository, path, required evidence, and reason. The transaction offers no remote
fetch action. Catalog-only rows and machine-cache residency are insufficient. Historical rows on
inactive drives appear in the gap explanation but do not make it executable.

Source choice must be deterministic under the evidence snapshot. The seal may bind an ordered set
of qualifying alternatives, but execution can use only those pre-approved identities. Any closure,
destination identity, lifecycle, or digest drift after approval invalidates the execution seal and
requires a new preview. Before the first write, unexplained capacity drift does the same. After
execution starts, remaining-capacity checks account for the transaction's own journaled writes and
invalidate only unexplained external consumption or mutation.

## Filesystem and device safety

Every source and output path must pass lexical and resolved confinement before it enters a sealed
closure or transfer plan. Reject absolute paths, parent traversal, platform-separator ambiguity,
NULs, and any destination ancestor or symlink that escapes the approved root. Source reads use the
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

The first implementation has no merge or overwrite mode. The approved consumer root is dedicated
to the slice, every planned output path must be absent, and unexpected existing content or a path
collision blocks preview or Start. Resume may recognize only exact paths and checkpoints written by
the same sealed transaction; everything else remains a collision.

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
6. **Execute** — copy checkpointed content; request only the source, scratch, or destination device
   required for the next phase; preserve completed evidence across stops.
7. **Verify and publish** — validate destination content and layout before publishing a receipt.

Public transaction states should distinguish at least `draft`, `blocked_gaps`, `ready`, `approved`,
`waiting_source`, `waiting_scratch`, `waiting_destination`, `transferring`, `verifying`, `complete`,
`stopped`, `invalidated`, and `failed`. A wait is resumable and names the missing physical resource;
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
- **SliceJournal** — append-only phase and file progress suitable for safe resume.
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
- Offline qualifying sources produce attended drive requests and resume without replanning completed
  work.
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
