# Usable Slice transaction core

Slice 2 adds private durable approval, device ownership, source-use gates, and recoverable
publication orchestration. It remains an **internal API with no hardware adapter or CLI/portal
entry point**. Do not use the test destination as a real USB adapter. Slice 1's domain approval
still has no execution authority; a separately reviewed transaction seal binds destination
evidence, and the destination port must validate that evidence before any writes.

## Authority and lifetime

`TransferPlan(proposal, destination_binding, metadata_reserve_bytes=...)` freezes the domain
proposal, destination binding and explicit metadata capacity reservation
into a versioned seal. `Store.create(plan, domain_approval)` persists a ready transaction;
`Store.approve(id, expected_seal=...)` durably approves that exact plan but starts nothing.
`start(store, id, destination_port, source_port)` delegates execution to `DeliveryAuthority`:
reserve the approved device, acquire kernel exclusion plus a unique attempt marker, and publish
that attempt in a guarded SQLite claim before checking the destination or activating. It returns
a `Session` to the sole writer, or non-writing `Status` when the same attempt's exclusion is retained.
Completion is recognized both during reservation and inside the claim transaction; a delayed
starter returns completed status without touching the destination after completion wins.

Every execution transition and journal append checks the exact `(transaction, attempt token)`
inside its state transaction. The lifecycle transition table permits attended-wait retries but
requires a fresh claim to resume stopped execution; completion has a separate receipt-bound gate.
The neutral `modelark.execution_authority` identity/state contract is also used by existing Fill
session writes and recovery. These remain separate stores and workflow policies: Fill's existing
portal stop Event, expiry rules and SQL fencing-token CAS are unchanged. Slice delivery receipts
do not become archive evidence, and this is not a scheduler or public Start/Stop implementation.

A busy bind permits observation only after finding a current durable attempt, querying that
attempt's live kernel marker, and re-reading the same durable attempt. Historical acquisition
flags are not authority. Otherwise every retry reports `DESTINATION_BUSY`, including when an
unrelated former owner's child retains exclusion. Transitional uncertainty is also busy.
Observation proves retained attempt exclusion, not progress, responsiveness, or a live worker.

The private SQLite store is separate from every catalog, under the fixed operator-host namespace
`~/.local/state/modelark/slice`. This is not a catalog/state-directory option: local workers for
different catalogs must share this authority. Tests replace the module's `HOST_STATE_DIR` before
construction, using a single temporary namespace shared by their child processes. Parent/child
directory entries and initial database creation are flushed; SQLite transactions use FULL
synchronous durability. Symlinked or non-private state locations are refused. There is no journal
import, alternate-authority adoption, ownership takeover, expiry, or abandoned-output cleanup.
Reader operations use deferred transactions rather than requesting the writer reservation;
writers have a bounded SQLite busy wait and return typed `STATE_BUSY` on exhaustion. The private
database handle retains SQLite rollback-recovery capability even for reader operations, so a hot
journal from a dead writer is recovered rather than exposed as a read-only-database error.
Private schema version 5 transactionally upgrades development version-1/2/3/4 databases while
preserving plans, pending stops, reservations and journal heads. It adds a nullable live-attempt
token and an acknowledged-stop serial; every pending legacy request remains unacknowledged,
including on stopped rows which may contain a newer request. Legacy `process_seen` and `activation_serial`
columns are retained for compatibility but are never read as execution authority. Older owners
must acquire a new lease before publishing an attempt; migration invents no live marker.
This migration never opens or changes a catalog database.
Transient `STATE_BUSY` during Start or execution releases the process handle without turning the
durable transaction into a terminal failure. A fresh Start can retry its existing approved authority.
This differs from a terminal refusal whose subsequent status write fails: that old Session is revoked.

A Linux abstract Unix stream socket bind provides process exclusion keyed only by the destination's
canonical physical-device identity. A second abstract datagram endpoint identifies the unique
attempt; probes only connect, never send or queue messages. There is no listener, helper service,
remote connection, or replaceable lock file. Bind exclusion before the marker, publish the token
only while both are held, and close the marker before exclusion. The fence is shared across
catalog paths, transaction IDs, and consumer roots in the same host/network namespace.
Durable reservations survive loss of this process fence. Inherited descriptors retain exclusion
and identity after their parent closes or dies; an exec child capable of writing must explicitly
inherit both and follow the same release order. The engine itself spawns no writer children, and a forked child cannot
reuse the parent's `Session` methods. Container/network-namespace isolation and cross-host ownership
are not supported execution topologies.

`Session.step()` transfers one remaining file, or verifies and completes a fully transferred
transaction. `run()` continues until complete, a stop, or an attended wait/block. `request_stop`
is checked between streaming and verification chunks and before publication. Each Start snapshots
both requested and acknowledged stop serials. A crashed writer's unacknowledged stop is recorded
as stopped on recovery without destination mutation; only a subsequent explicit Start whose
snapshot already contains that exact acknowledgment can clear it. A delayed Start cannot gain
resume permission from another caller's later acknowledgment. Closing a live session records stopped state
and closes its process descriptor, but retains the durable reservation. A new seal cannot acquire
that unfinished device. Completion releases the reservation only after verification and receipt
publication; existing output/control records are not automatically deleted or adopted.
An observed Stop ends the current Session attempt: it returns its stopped outcome and releases
the attempt/device descriptors before returning, without requiring caller cleanup. The old object
reports `can_write=False` and cannot resume; a fresh Start can claim the still-reserved transaction.
Stop also revokes the local attempt if acknowledgment persistence fails, while leaving that error
visible and the durable request unacknowledged. An attended wait retains its attempt unless a
pending Stop wins during outcome publication. A concurrent fresh Start cannot change the stopped
result returned by the ending attempt.
Completion closes the Session's process descriptor inside the final state transaction before
releasing durable ownership, including when the caller retains the completed Session object.
If a same-transaction starter acquires exclusion during that final commit window, its claim
observes completion inside its own write transaction and returns a nonwriting completed status.
Startup refusal publication is also serialized with terminal state and device ownership, so
an adapter error based on a stale pre-activation snapshot cannot downgrade a terminal winner.
When a retained Session retries a resolved source/destination wait, it returns to transferring
before more work, so `run()` continues through receipt publication.
Terminal refusals release the process descriptor while retaining durable ownership. A failed or
invalidated transaction cannot resume through the old Session object after an adapter condition
is restored; `step()` refuses it just as a new Start does.
Revocation runs even if persisting that terminal status fails: the old Session remembers its
terminal refusal independently of the database and cannot regain its writer capability. The
persistence error remains visible; a failed status write is not reported as a durable transition.

## Journal and recovery

The append-only journal binds transaction, seal, physical destination, operation order, path,
creation token, expected digest/size, temporary identity, and actual prepared source evidence.
Hash chaining plus a transactionally updated durable head detects altered or truncated records;
the private store—not a hash or an imported filename—is the authority. Replay rejects operations
outside the approved layout, changed bindings, or unsealed prepared source records.

Every creation has a durable intent. Every directory's own metadata and parent entry are flushed
before its completion record and before children. Files are streamed into exclusively created,
owned temporaries, checked for exact original size/SHA-256, flushed, and durably prepared with the
actual source record. Publication is atomic no-replace; the parent is flushed before completion.
Receipt publication uses the same prepared/no-replace/parent-flush protocol. Temporary cleanup is
restricted to authenticated transaction-owned objects.
The execution boundary is checked after verification EOF/stream close and again immediately before
publication, so a stop arriving at the end of hashing cannot slip through the last chunk check.
The destination control record also uses restartable temporary/prepared/no-replace publication;
a crash between its exclusive creation and content flush does not strand an empty final control.

On recovery, an authenticated prepared temporary or published file is rehashed before finishing
its publication. It does not need a still-online source: its original prepared source evidence is
retained. An unprepared temporary, even with matching bytes, must be restarted from a currently
qualifying sealed source. A completed checkpoint whose output disappeared is likewise restored
or blocked. Before rewriting a missing completed file, old prepared source proof is durably
cleared so interrupted new bytes cannot inherit it. Unrelated or replaced objects—even matching
bytes—are collisions. In-flight allocation is derived only from journal-owned object certificates,
including partial temporaries, and passed to the capacity gate; external consumption is not
subtracted as transaction work.
Protocol-v3 plans require an explicit metadata reserve in addition to original artifact bytes.
The trusted preflight adapter must charge allocation rounding, directories, temporary names and
control/receipt storage for the bound filesystem. The engine checks the reserve against a serialized
control/receipt upper bound over all sealed source alternatives, using the actual private-state root
before transaction creation and Start. Insufficient total capacity fails before destination writes.
Every destination check receives the full required byte total and authenticated owned allocation,
so the adapter can preserve the remaining reservation throughout the transfer. Tests supply a
simulated charge; proving real filesystem charges remains Slice 3's adapter obligation.

The fenced session caches a validated operation set and incrementally tracks owned allocation,
including hard-link aliases. Per-chunk checks do not replay the plan/journal or rescan completed
objects. A durable-head comparison and append CAS reject unexpected journal changes while using
the cache. Resume performs a new full replay and owned-object allocation scan.
Recovered completed files are rehashed once per session, and newly published files retain their
successful verification within that session. The final full digest pass is still mandatory.
Already-complete directories are authenticated without repeating their flush/checkpoint sequence.
Before child creation, append, publication or temporary cleanup, all journaled parent directories
must retain completed ownership certificates. The hardware adapter must still prevent replacement
inside an individual port call; these pre-use checks do not replace descriptor confinement.
Layout authentication runs before Start/resume touches the destination, at each artifact step,
and during final verification. Existing consumer roots, ancestor prefixes and descendants must
be journal-owned, including zero-byte entries that capacity checks cannot detect. These metadata
scans do not rehash completed artifacts or replay the journal, and do not run for every chunk.

Final verification rehashes every artifact, verifies the control record's presence/token/digest,
and authenticates every extant descendant, including historical temporary names. The receipt records
the sealed transaction, destination, exact delivered bytes and actual per-file source evidence.
It is delivery history, never an archive replica or placement claim.
New plans use transaction protocol v3. Their destination receipts embed the entire sealed plan,
including the evidence snapshot ID, slice specification/profile, approved closure and ordered source
evidence, alongside actual delivered-file sources, direct topology, terminal delivery status and
explicit content/layout verification results. The plan seal can be reconstructed without the host
database. These fields describe the verified delivery, not continuing physical verification or archive
replica evidence. Existing protocol-v1/v2 plans retain their receipt formats and seals so an
already-prepared receipt can recover without replacement. Protocol-v1 receipts are not self-contained;
v2 receipts carry context but their plans did not seal a filesystem metadata reserve. Legacy resume
checks at least the known control/receipt byte requirement and still requires adapter capacity proof.
Creation of new protocol-v1/v2 transactions is refused; legacy support is recovery-only.

## Adapter boundary and remaining work

`FencedSources` uses the **existing archive drive mutation fence**, nonblocking, with the same
fingerprint/epoch key as Fill. It refreshes the explicit read-only catalog under that fence and
keeps it through the injected local reader's stream lifetime. The engine revalidates the exact
sealed candidate before reading. Only approved alternatives can be used. Placement exclusion alone
does not revoke reads; changed lifecycle, identity, generation, copy or digest evidence does.
Busy, offline and locally missing sources remain distinct reasons; none triggers retrieval.
Operator `declare_lost` acquires that same identity/epoch fence nonblocking and holds it through
its graph commit. An active source read yields `DRIVE_BUSY` on revocation, leaving its preview
and lifecycle unchanged for an explicit retry. Identities without a proven fingerprint cannot
qualify as slice sources and do not require this source-read exclusion.
Source waits/blocks identify the exact repository, filename and candidate drive reasons. Source
open-error translation does not encompass destination exceptions from the consuming transaction.
The yielded source-read wrapper converts missing-source and other source IO errors raised during
lazy reads into typed source failures. It does not wrap destination operations; sealed fallback
selection and source blocking work even when the source fails after partial bytes have been read.
The repository's import-policy guard permits this exact source-gate path to import the neutral
fence only. It still forbids the slice package from importing the drive mutation envelope.

The source reader and destination interfaces are trusted adapter contracts, not security checks
implemented by arbitrary callbacks. Slice 3 must supply and test:

- Real attachment and physical identity checks; source/destination/system/archive-role disjointness.
- Descriptor confinement, symlink/nested-mount rejection, local annex content resolution without
  retrieval, and original-byte decompression.
- Sealed filesystem capabilities, metadata/allocation accounting, and per-use identity/capacity gates.
- Exclusive recoverable creation with authenticated object certificates, including crashes *inside*
  adapter calls; device-bound reads/writes and atomic no-replace publication.
- Actual device disappearance/replacement handling and the operator entry point.

DEC-123 qualifies the crash-inside-create contract for directories: creation and certification are
separate operations. An uncertified directory left in that interval requires operator intervention;
the adapter must neither adopt nor delete it. Certified-object recovery remains automatic. This
exception does not authorize cleanup, takeover, formatting, or an unattended real-device trial.

DEC-124 additionally requires application-wide exclusion at launch, before CLI configuration or
portal startup. It is not a replacement for these internal transaction/device/archive fences.
Every separate CLI invocation is a launch and refuses while another instance runs, even for query
or status; controls within the running portal remain available. The operator account is trusted,
without adding protection against unrelated processes deliberately mutating delivery paths.

The test adapter uses temporary files and simulated device/ownership evidence (test-only xattrs).
It proves the orchestration's protocol ordering and recovery across port boundaries, not a real
USB filesystem's identity, ownership-certificate or durability guarantees. Real mounts, live
catalogs, archive paths, network acquisition, and Fill service actions are not part of this work.
