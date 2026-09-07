# Usable Slice transaction core

Slice 2 adds private durable approval, device ownership, source-use gates, and recoverable
publication orchestration. It remains an **internal API with no hardware adapter or CLI/portal
entry point**. Do not use the test destination as a real USB adapter. Slice 1's domain approval
still has no execution authority; a separately reviewed transaction seal binds destination
evidence, and the destination port must validate that evidence before any writes.

## Authority and lifetime

`TransferPlan(proposal, destination_binding)` freezes the domain proposal and destination binding
into a versioned seal. `Store.create(plan, domain_approval)` persists a ready transaction;
`Store.approve(id, expected_seal=...)` durably approves that exact plan but starts nothing.
`start(store, id, destination_port, source_port)` acquires device exclusion and atomically claims
the approved transaction and durable reservation. The initializing reservation is committed before
process exclusion becomes visible, so overlapping first Starts can identify the same owner. Final
activation preserves any stop request arriving during initialization. It returns a `Session` to the sole writer, or a
non-writing `Status` for a repeated Start while that same transaction holds the device.

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
Private schema version 2 transactionally upgrades development version-1 databases, adding the
stop serial when absent while preserving plans, pending stops, reservations and journal heads.
This migration never opens or changes a catalog database.

A Linux abstract Unix socket bind provides process exclusion keyed only by the destination's
canonical physical-device identity. No listener, remote connection, or replaceable lock file is
used. The fence is shared across catalog paths, transaction IDs, and consumer roots in the same
host/network namespace. Durable reservations survive loss of this process fence. An inherited
descriptor retains exclusion after its parent closes or dies; an exec child capable of writing
must explicitly inherit it. The engine itself spawns no writer children, and a forked child cannot
reuse the parent's `Session` methods. Container/network-namespace isolation and cross-host ownership
are not supported execution topologies.

`Session.step()` transfers one remaining file, or verifies and completes a fully transferred
transaction. `run()` continues until complete, a stop, or an attended wait/block. `request_stop`
is checked between streaming and verification chunks and before publication. Closing a live session records stopped state
and closes its process descriptor, but retains the durable reservation. A new seal cannot acquire
that unfinished device. Completion releases the reservation only after verification and receipt
publication; existing output/control records are not automatically deleted or adopted.
Terminal refusals release the process descriptor while retaining durable ownership. A failed or
invalidated transaction cannot resume through the old Session object after an adapter condition
is restored; `step()` refuses it just as a new Start does.

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

Final verification rehashes every artifact, verifies the control record's presence/token/digest,
and authenticates every extant descendant, including historical temporary names. The receipt records
the sealed transaction, destination, exact delivered bytes and actual per-file source evidence.
It is delivery history, never an archive replica or placement claim.

## Adapter boundary and remaining work

`FencedSources` uses the **existing archive drive mutation fence**, nonblocking, with the same
fingerprint/epoch key as Fill. It refreshes the explicit read-only catalog under that fence and
keeps it through the injected local reader's stream lifetime. The engine revalidates the exact
sealed candidate before reading. Only approved alternatives can be used. Placement exclusion alone
does not revoke reads; changed lifecycle, identity, generation, copy or digest evidence does.
Busy, offline and locally missing sources remain distinct reasons; none triggers retrieval.
Source waits/blocks identify the exact repository, filename and candidate drive reasons. Source
open-error translation does not encompass destination exceptions from the consuming transaction.
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

The test adapter uses temporary files and simulated device/ownership evidence (test-only xattrs).
It proves the orchestration's protocol ordering and recovery across port boundaries, not a real
USB filesystem's identity, ownership-certificate or durability guarantees. Real mounts, live
catalogs, archive paths, network acquisition, and Fill service actions are not part of this work.
