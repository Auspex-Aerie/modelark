# Slice representation architecture and compatibility plan

Date: 2026-09-10. Status: APPROVED FOR STAGED IMPLEMENTATION (DEC-139).
Engineering qualification and staged local/remote reviews remain required.
Post-review operator requirement: shared RAM-safety admission, DEC-138. This
addition is binding on the proposal and has not had a further Grok review.
Code basis: worktree HEAD `eb95945060e5b178509432c8016355f225ef56f1` (PR #73
docs-only closeout on merged application commit `16e8b3f`).
Related: DEC-021, DEC-022, DEC-081, DEC-098, DEC-101 in ../decision_log.md.
This replaces the proposed direction in slice-decompression-compatibility.md;
that document remains a historical draft, not a second active implementation plan.

## Plain-language outcome

A slice says which files you want. A delivery profile says whether you want the
original files or their existing stored representation. Transport says where and
how they move. Decoding is requested by the profile, not inherent in selecting a
slice. Today we implement original-file delivery correctly and establish the
reusable boundary. Stored-format export and P2P remain later work, with their
requirements written here so the current fix does not obstruct them.

## Evidence and problem

The writer's `compress.plan_codec` intentionally permits whole-file ZipNN when
the shard fits DEC-022's compression budget. Slice's `decoding.py` independently
applies 64 MiB to caller reads, encoded frames, decoded frames and zstd windows.
Real preflight refused 99,630,640-byte Qwen and 409,993,344-byte Trinity original
files stored as whole ZipNN. Larger streamed siblings returned a first byte.
Neither preflight nor this plan proves a complete compressed-source delivery.
This is our producer/consumer mismatch, not a foreign ZIP format or USB issue.

Current `hf-tree-v1` means original paths/bytes/hashes, not functional model
loadability. `CopyFact` has original and stored sizes, an original digest and
annex key, but no explicit codec or standalone stored SHA-256 field. Some valid
original-byte sources lack a usable SHA256 annex key. Do not invent missing
stored identity or make current original-byte sources ineligible merely to
prepare for a future compressed-export feature.

## 1. Three independent choices

1. Selection: frozen repository/file closure and original-file identity/evidence.
2. Delivery representation/profile: original-file tree now; exact stored-object
   bundle later. A stored bundle may include compressed weights and raw auxiliary
   files. It is not a promise to newly compress everything or to produce ZIP files.
3. Transport/destination: current attended local filesystem adapters; future peer
   or scratch transport. No networking, listener, peer authorization, discovery,
   resume protocol, object-store integration, or archive registration in this arc.

Use original digest + length as byte-content identity, retaining repository/path
and revision evidence separately. The exact stored representation has its own
digest + length, encoding description and evidence linking it to the original.
Two encodings of the same original are different transferable objects. Matching a
stored hash proves a stored-byte copy, not a fresh original-byte decode check.
That exact-object distinction constrains future stored delivery, not current
original delivery: `hf-tree-v1` keeps all otherwise eligible sealed raw/ZipNN/
StreamZNN/zstd alternatives for the same original hash and length. Resource
admission may make a candidate unusable today; it does not change its original
identity or authorize pruning/widening an approved closure.

Output representation and bytes-on-the-wire need not be identical in a future
peer protocol. Where decoding happens must be explicit; changing transport must
not silently alter the requested output, approval, resource budget or receipt.

## 2. Reusable byte-reading boundary (implement now)

Prefer extending the existing MIT standalone `modelark/streamznn.py` with a small
stream-oriented API over duplicating its decoder in a new ModelArk component.
Its existing `decompress_to(path, sink)` already shares decoding between canary
and restore, but opens a path internally. Extract an iterator/reader primitive
over a caller-owned binary stream, with explicit encoded/decoded/total limits,
bounded IO and optional neutral frame-decoder injection. Keep path-based APIs
as wrappers with compatible behavior; callers own and close supplied streams.
Use that primitive from Slice through its confined attachment-checked stream.
Retain the MIT header, standalone stdlib + ZipNN dependencies and container bytes.
No Slice exceptions, catalog/drive knowledge or process manager inside StreamZNN.

Where feasible, expose the standalone validated ZipNN frame primitive too, so
whole-ZipNN and StreamZNN adapters share interpretation instead of reproducing
their headers. The neutral frame-decoder hook can call a supervised worker from
ModelArk; standalone callers remain non-spawning. An API/validation extension is
allowed; rewriting ZipNN's native codec, changing the on-disk format or claiming
that arbitrary whole blobs can be streamed would require separate scope.

Introduce a small neutral module/package, provisionally `modelark/artifact_io`,
only as needed for raw/ZipNN/StreamZNN/zstd dispatch and resource orchestration;
it must reuse the standalone primitives, not become a second StreamZNN engine
or a general transport framework. It must not import `modelark.slice`, SQL,
drive registration, archive mutation, git-annex retrieval, or destination code.

It operates on caller-supplied bounded byte streams and explicit expectations:

- Container recognition and supported lossless byte-format parsing.
- Separate stored frame, original frame, consumer read and zstd-window bounds.
- Original-byte stream production, codec requirements, typed neutral errors,
  cancellation and explicit resource policy.
- A stored-byte passthrough primitive with length/digest verification may be
  internal plumbing; it is not an executable stored-export CLI/profile yet.

Keep source acquisition and physical attachment checks outside this module.
`LocalArchiveReader` retains descriptor confinement, identity checks and IO error
attribution, supplies attachment-checked bounded reads to the codec layer, and
maps neutral errors into Slice refusal codes. `FencedSources` retains current
catalog evidence, candidate gates and drive-lock lifetime.

Slice's delivery adapter requests originals for `hf-tree-v1`. Its transaction
core owns approval, owned temporaries, publication and receipts, not codec rules.
Old direct/native/FAT output semantics and hashes stay original-byte semantics.
No branch that silently sends compressed bytes after a decode refusal.

## 3. Bring existing consumers into consistency, without importing their authority

Use actual writer fixtures as the compatibility contract from the start. Preserve
writer codec preferences, chunk format, disk capacity math, raw fallback and
canary-before-drop. DEC-138 additionally requires writer and reader admission to
use the same RAM-safety mechanism: this scoped integration is required, not an
unrelated compression-policy retune. Leave StreamZNN standalone.

Stage caller adoption: first Slice; then a separately gated follow-on for
`compress.canary_ok` and `compress.decompress_file` (and therefore restore) to use
the same neutral byte decoder. This is shared decoding, not shared source access:
restore retains its current retrieval policy and atomic publication wrapper;
Slice never gains retrieval. Canary retains its known original hash and drop gate.
Standalone StreamZNN APIs remain independently usable and cross-tested.
Its own `decompress_file` / `verify_sha256` wrappers adopt the extracted primitive
together; this is immediate reuse of the existing canary/restore path, not a
second independent implementation. The separately gated adoption below covers
ModelArk's whole-ZipNN/zstd dispatch and differing caller execution policies.
This follow-on is part of the centralization roadmap, not a prerequisite for
unblocking Slice or permission to force incompatible behavior into existing
callers. Shared format interpretation, byte decoding and RAM-safety methodology
are the target; source authority and execution topology remain caller-specific.
If only parser sharing is safely possible
in that follow-on, document the remaining duplicate execution paths explicitly
and do not describe the broader decoder consolidation as complete.

Before migrating a caller, inventory its currently accepted header/mode variants,
expected-size availability, exceptions and return conventions. Do not silently
impose Slice's old allowlist or 64 MiB cap on restore/canary. Shared format support
must cover known writer-produced and supported existing representations; unsafe
or conflicting modes require an explicit compatibility decision, not accidental
acceptance or refusal. Resource profiles can differ only explicitly (for example,
a different host budget or an old sealed approval), never through independent
per-consumer RAM logic. Format interpretation and original-byte results may not
drift. Stop and rescope
if preservation requires archive migration or unrelated restore/Fill redesign.

## 4. Large-frame compatibility and resource gate

Required parity invariant (DEC-138): compression, canary, restore and Slice call
one resource-admission mechanism with common budget interpretation, headroom and
guard methodology, using qualified estimates for each operation. Do not reuse the
compression 4x factor blindly for decoding, nor retain an unrelated Slice limit
as the new policy. Under equivalent supported codec/runtime, budget and available
headroom, an accepted compression must produce a representation that the decoder
admits and fully hash-verifies under that same profile. The writer's canary uses
this mechanism, not a more permissive path that bypasses reader requirements.

Test both phases under equivalent conditions and also genuine differences in
host/concurrency/budget that must yield an explicit resource refusal. Shared RAM
admission on the compression side is required within this arc before declaring
parity complete; it is not deferred with optional broader decoder consolidation.
No migration or assumption about historic compression resources is implied.

The three-way product separation does not cause a RAM problem. Two independent
facts must be explained separately: the delivery profile decides whether decoding
is needed; codec framing determines how much work must be decoded at once.
The compression side already combines (a) DEC-022's estimated RAM gate for whole
files, (b) StreamZNN's independent small blocks for larger files, and (c) a child
process for native crash containment. Only (a)/(b) bound the intended workload;
process isolation alone neither adds RAM nor enforces a host memory ceiling.

Lean on StreamZNN for already-streamed content. The existing troublesome files
are bare whole-ZipNN blobs, not StreamZNN containers. A small stream-reader API
does not retroactively split them into independent compressed blocks: the
installed ZipNN API still returns the entire decoded blob. The proposed extension
therefore removes duplication and enables safe confined streaming, while large
whole-frame resource admission remains a distinct compatibility requirement.
Do not relabel an archive rewrite as a small StreamZNN API change.

Keep bounded streaming for raw/StreamZNN/zstd. Permit supported whole ZipNN and
large historical stream frames through a supervised helper, once qualified.
Allow legitimate encoded framing overhead separately from the original-frame
ceiling. Never use the original file length or current compression config as
unlimited allocation permission; the compression 4x estimate is not a proven
decoder memory bound.

The first engineering slice qualifies a guard against the installed decoder:
measure import/mapping baseline, native working memory, buffers and copies, using
real writer-made roughly 100 MiB and 410 MiB whole frames and 64 MiB stream frames.
Test the actual enforced limit with adversarial headers, allocation failure and
native crashes. A helper alone isolates many crashes but does not guarantee host
or parent OOM protection. `RLIMIT_AS` limits virtual address space, not RSS; a
resident-memory controller has different deployment requirements. No claim that
`RLIMIT_RSS` provides a Linux hard bound. No automatic cgroup/sudo/service changes.

Agreement is on a qualification-gated design, not on an unmeasured numeric limit
or already-proven mechanism. Larger decoding ships only after both useful real
fixtures succeed under the chosen guard AND failure stays contained. If no guard
works without new host authority, stop and report the remaining choice; do not
relax the guard or call the compatibility defect fixed. Existing approvals and
current small-frame behavior remain valid; this gate need not block the neutral
refactor and compatibility tests.

Helper protocol: one frame at a time, parent reads retained source descriptors in
small checked pieces; helper receives only data/control pipes, not paths, archive
or destination handles, catalog or fence FDs. Use dedicated protocol FDs, not
stdout: `compress_worker.py` already documents native-library stdout pollution.
Input completion precedes decode/output; parent supervision remains responsive
through input, compute and output, with backpressure and bounded message lengths.
No whole-file parent buffer, `communicate()` accumulation or scratch file.
Drain/discard diagnostics with a bounded retained tail; never let logging fill a
pipe and stall the child. No GPU tensors or new GPU workflow.

Parent validates size/framing/EOF and final hashes, cancels on stop/unplug/consumer
failure, terminates/reaps before releasing source fences, and has tested parent-
death handling. Worker success is never destination success. ENOSPC/EIO remains a
destination error; no success receipt after failure. Canary already runs inside
its existing compression worker: it uses a non-spawning decode adapter within
that worker, never a new nested helper. Existing write-fence inheritance for
compression remains unchanged; do not copy that inheritance into the read-only
Slice helper. Preserve the compression worker's result-file protocol. If safe
full decoder adoption cannot preserve these boundaries, share parsing first and
record the remaining consolidation instead of inventing unsafe child ownership.

## 5. Admission, compatibility and early checks

For new original-delivery approvals, seal a versioned decode/resource policy into
the appropriate direct/native/FAT admission envelope. No mutable wishlist lookup
may widen approved behavior at Start. Old plans without new fields retain legacy
limits; fresh preview/approval is needed to use larger ones. Version readers so
older binaries cannot reinterpret new approved semantics. Do not migrate the live
catalog or reinterpret old receipts. Define private-state compatibility explicitly.
Put the policy in each binding's canonical hash payload, not an unbound side
record. Retain closed-world key/version validation so an older binary refuses
new semantics; test the actual direct, native and FAT readers rather than
assuming one envelope's change covers the others.

Before first destination mutation or consuming FAT's one attempt, inspect attached
sealed source candidates under the existing source fences/confinement. Respect
alternatives and absent-drive/wait semantics; do not require all drives online or
label absent candidates checked. Check all relevant StreamZNN frame headers with
bounded reads/checked seeks, not only the first. Offline preview remains offline.
Recheck on actual use. Preflight detects known header/resource refusals, not later
corruption, resource contention or full-file correctness. Preserve native resume
and FAT one-attempt/no-resume behavior. No new source labels or widening a seal.

## 6. Stored representation delivery (design now; implement later)

Add a distinct versioned profile only when explicitly scoped for implementation.
It must seal exact output paths, encoded digest/size, codec/version or honest
unknown-encoding status, original identity and linkage evidence, approved source
alternatives, capacity calculation, and verification claims.

- Copy stored bytes exactly; no decoder allocation and no implicit recompression.
- Require trustworthy expected stored digest/size before claiming verified stored
  delivery. SHA256 annex keys can supply that evidence when valid. Missing evidence
  is a gap requiring separately authorized observation/enrichment, not a fabricated
  value or a reason to break existing original-file delivery.
  A compressed object's annex hash identifies its stored bytes, not its decoded
  original. For a raw object the same hash identifies both, because those bytes
  are identical. Always retain the original-to-stored linkage evidence separately.
- Different encodings sharing an original digest are not interchangeable after
  approval of exact stored bytes. Equivalent sources must supply identical sealed
  output bytes, or a newly approved proposal is needed.
- Use a dedicated bundle layout/manifest mapping logical paths to stored objects;
  do not copy annex object-store structure or append `.znn` blindly. Resolve raw/
  compressed filename collisions and FAT case/short-name collisions at planning.
- Count stored bytes plus manifest/receipt/filesystem overhead, and apply FAT's
  per-file ceiling to actual output sizes. Do not reuse original-size totals.
- Receipt says stored representation verified and separately identifies historical
  original-byte evidence. It cannot claim fresh decode verification, model
  loadability, archive registration or desired-copy satisfaction.
- Wire-to-output transcoding, peer trust and negotiation are future protocol work.
  No placeholder public switch that accepts unsupported stored/P2P modes now.

Revisit stored-profile implementation when the operator scopes compressed/local
export or peer serving; require the evidence/layout/admission specification above
before exposing a switch. Revisit P2P transport when peer distribution is scoped,
after representation identity and verification claims are stable. These are
proposed deferrals to be recorded in the ledger on approval, not shipped features.

## Implementation sequence after operator approval

| Slice | Work | Exit evidence |
|---|---|---|
| A | Characterize shared codec and RAM-admission contract; qualify compression and decode guards | Real writer/canary/restore/decoder fixtures under the same profile; measured successful and failure limits; explicit go/no-go |
| B | Small standalone StreamZNN stream API, existing wrappers, thin neutral dispatcher and Slice original-output adapter | Shared existing decode loop; MIT/standalone APIs/container preserved; identity, confinement, format, hash and transaction tests pass; no behavior widening |
| C | Qualified large-frame helper, sealed policy and early admission | Real failing-size fixtures now decode; old seals safe; stop/crash/unplug/pipe/FAT tests pass |
| D | Separately gated canary/restore decoder adoption, without policy changes | Existing format/publication/retrieval/capacity/fallback contracts preserved; no nested helper; any parser-only adoption explicitly reported as partial |
| E | Full public-CLI synthetic delivery and documentation | Complete decoded hashes/receipts, failure matrix, truthful acceptance summary; physical test separately scoped |

Use a new implementation PR, not unrelated docs-only PR #73. Land/review slices
incrementally in that PR; do not merge an unsafe intermediate behavior widening.
Follow the operator's Grok CLI and Greptile/Codex review workflow, with at most
three review/fix cycles before summarizing unresolved findings. Operator merges.
No changes to the live service, USB, archive or catalog in this implementation
stage. The approved workflow permits scoped commits, PRs and review requests,
not automatic merging or deployment. No performance guarantee or full physical
acceptance is claimed by unit tests.

## Must-test regressions and scope boundaries

- Whole/stream/raw/optional-zstd real fixtures, all supported byte dtypes, exact
  cap boundaries, incompressible overhead, large final/tail frames and historical
  configurable chunks; round trips match original hashes across consumers.
- Oversized/malformed/truncated/trailing content; worker crash/allocation failure;
  noisy stdout/stderr; blocked pipes; stop during native compute; parent death;
  no orphan or leaked fence; attachment changes on every stored/read-output leg.
- Exact file and aggregate lengths, original verification after EOF, source versus
  destination errors, no false success receipt, old seal decoding and reader fences.
- Known attached-source refusals before FAT attempt consumption; safe source
  alternatives; drive swaps; native partial recovery unchanged.
- Slice changes no archive bytes, catalog identity, generation or schema. No
  archive recompression, new replica, automatic host configuration or broad Fill
  refactor. Shared code must not introduce retrieval into Slice.
- Future stored-export contract tests/specification must not masquerade as a
  shipped selectable mode or as a completed P2P capability.
- StreamZNN stream API handles short reads, non-seekable input, bounded requests,
  callback failures, early cancellation and caller-owned descriptor lifetime;
  existing path APIs produce identical output and retain exception/publication
  behavior. No accidental new dependency or on-disk format change.

## Review and decision record

Local Grok CLI session: `01a08c73-d885-7930-bcc6-bb6dd26a9e56`.
No web Grok or delegated reviewer was used. The previous helper-only review does
not count as architecture approval; this direction received its own review:

1. Revised architecture: ACCEPT WITH REQUIRED CHANGES. Incorporated preservation
   of original-identity alternatives, stored/original hash distinction, all
   admission hashes, no nested canary helper, candidate-aware preflight and
   explicit stored/P2P deferrals. Grok agreed that the memory guard is mandatory
   before large-frame rollout, not optional crash isolation.
2. Amended written architecture: ACCEPT. Codex agrees with the staged scope,
   boundaries and qualification gates. Broader caller adoption remains separately
   gated, with partial centralization reported honestly.
3. Operator-requested StreamZNN reuse amendment: ACCEPT. Both reviewers prefer
   the small caller-owned stream API in the existing standalone MIT module, its
   path wrappers sharing that loop, and a thin ModelArk dispatcher rather than a
   duplicate StreamZNN decoder. Both distinguish this reuse from the unresolved
   engineering requirement to safely decode existing whole-ZipNN frames.

No unresolved architecture disagreement remains. The concrete memory-guard
mechanism and numerical limits are deliberately qualification results, not facts
asserted by the plan. Agreement means agreement on staged scope and gates, not
proof that the compatibility defect is fixed or implementation is complete.
Operator approval is now recorded in DEC-139, with DEF-045/DEF-046 for stored
export and P2P and HYP-002 for the resource qualification. DEC-138 remains the
mandatory shared-RAM invariant. IDs were allocated in the authoritative ledger.

## Implementation progress

Stage A merged in PR #74 (`1995a4b`): shared policy, disposable real-codec
qualification and tests; both final-head reviews and CI passed. Production
resource-policy adoption is still false. See ../codec-resource-qualification.md.

Stage B is implemented on `codex/streamznn-reader-adapter`: the standalone
StreamZNN stream API, neutral dispatcher and Slice compatibility adapter. Local
Grok CLI round 1 accepted `c272891`; local qualification passed. Remote review
and merge remain pending; no live deployment or admission widening is claimed.
See [Stage B evidence and boundaries](../streamznn-reader-adapter.md). Expanded
zstd coverage found the legacy native-window units mismatch (INC-064); qualify
and correct it in Stage C's explicit policy gate, preserving old seal semantics.
