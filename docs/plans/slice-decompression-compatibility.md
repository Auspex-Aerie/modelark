# Slice decompression compatibility — proposed scope

Historical draft: superseded as the active proposal by
[Slice representation architecture](slice-representation-architecture.md), jointly
reviewed with Grok CLI on 2026-09-10. Retained for review history; do not implement
this helper-only scope independently of the newer plan.

Status: PROPOSED, not approved or implemented. 2026-09-10.
Related: decision_log.md INC-003, DEC-021, DEC-022; PR #73 acceptance evidence.

## Problem and objective

Slice currently uses the same 64 MiB number for caller read size, stored frame
size, decoded frame size, and zstd window size. The archive writer still selects
whole-file ZipNN when a shard fits its configured compression RAM budget.
Consequently valid existing archives can restore through the general restore
path but refuse through Slice. A 99,630,640-byte Qwen shard and 409,993,344-byte
Trinity shard refused in read-only preflight; their larger streamed siblings
returned a first decoded byte. This is not complete model verification.

Support existing archive encodings with explicit resource admission. Preserve
original-byte hashes, attachment-bound source reads, read-only archive access,
and destination publication/receipt guarantees. Do not rewrite existing archives.

## Proposed design

1. Keep this first change in Slice: separate validated byte-format header parsing,
   stored and decoded frame limits, streaming chunk/window limits, and decode
   resource requirements. Original artifact size is an integrity bound, not
   permission to allocate that much RAM. Keep caller-facing IO chunks small and
   independent. Use actual writer output as the compatibility test contract;
   do not change writer/canary/restore production behavior in this PR.
   Compressed expansion/header overhead must not accidentally make a legitimate
   writer-produced 64 MiB original chunk unreadable.

2. Separate compatibility from resource admission. Whole-file ZipNN is supported
   but requires a budget appropriate to that frame; large StreamZNN frames from
   previously configurable chunk sizes need the same consideration. Never derive
   today's permission solely from current compression settings or file headers.
   A machine may safely refuse a valid archive it cannot decode with its approved
   resources, with an actionable explanation rather than "unsupported archive".

3. Decode large frames in a fresh, one-at-a-time helper process with an enforced
   memory/allocation ceiling. The main process alone reads the retained, confined
   source descriptor in bounded chunks, checking the source attachment before and
   after each stored-byte read. It feeds the helper through bounded pipes; the
   helper receives no catalog, archive/destination path or inherited archive fence
   descriptors. It returns bounded output messages; the parent never buffers the
   entire decoded frame. No whole-file scratch, network, retrieval, or new GPU work.
   Existing small-frame streaming remains available; do not claim that its 64 MiB
   frame cap alone is a hard process-memory limit.

4. First implementation gate: qualify the resource-control mechanism against the
   installed ZipNN stack and Linux deployment. Investigate a fresh-worker address-
   space limit versus an OS-enforced resident-memory budget; these are not the
   same. An address-space cap needs interpreter/import/mapping headroom, while a
   resident-memory controller may require unavailable host delegation. Do not
   silently add sudo/service setup, treat a subprocess alone as OOM protection,
   or claim the compression-side 4x estimate bounds decoding. Measure real peak
   usage, imports and copies; set explicit conservative defaults and hard ceilings
   only after qualification. If a suitable guard cannot be installed, large-frame
   decoding refuses; no unbounded fallback. Bring back any host-setup requirement.

5. Keep lifecycle and integrity in the parent: source fence remains held until
   helper termination/reaping; stop, attachment loss, decode failure and consumer
   failure cancel the worker and close pipes. Bounded communication must support
   cancellation/backpressure without deadlock. Use a one-frame request protocol:
   child consumes the declared input and its EOF before decoding/output, while
   the parent closes input after sending the frame. Parent supervision must stay
   interruptible during pipe writes, native decode and output reads; do not use a
   blocking full-buffer communicate() or accumulate a whole response. Close
   unrelated inherited descriptors; establish parent-death termination with a
   race-safe handshake as well as ordinary cleanup. Enforce both input and output
   lengths, header allowlists, framing/EOF, and final original SHA-256. Worker
   success alone is never delivery success. Partial output follows the existing
   destination rules; no success receipt on resource failure or native crash.

6. Establish consistency through cross-path tests in this PR: generate fixtures
   with the real writer, require its canary and general restore to pass, then
   require Slice to emit the same original bytes under a qualified budget. Preserve
   DEC-022 codec selection, capacity math, canary-before-drop, restore publication,
   and standalone StreamZNN. No new writer decode-eligibility gate or codec-column
   schema migration. A shared production codec module can be separately proposed
   after the safe Slice read path works; that broader refactor is not required
   to read existing archives. Do not promise every archive fits every machine.

7. Add an execution-readiness check before creating/writing a destination, when
   sources are attached: inspect currently attached, sealed source candidates under
   the same source confinement/fences and identify resource refusals early. Respect
   per-artifact alternate candidates; do not require every sealed drive to be
   attached simultaneously or mark absent candidates as checked. Preserve existing
   attended-swap/wait semantics. For StreamZNN, readiness must walk frame headers
   with checked bounded reads or confined seeks, not certify the whole container
   from just its first frame; zstd uses its actual header length. Offline catalog
   preview remains offline and cannot certify unseen compressed headers. Recheck
   on actual open/use; preflight is not durable authorization or a full decode.
   Integrate resource settings into the existing Slice configuration/admission
   model explicitly across legacy direct, native-folder and FAT32 envelopes. Seal
   both frame ceilings and the resource policy/version into approval; do not read
   mutable wishlist compression settings at Start. Old approvals without those
   fields retain old limits, and using new limits needs fresh preview/approval.
   Run readiness before the FAT attempt is consumed or any control/root/temp write.
   A header/resource pass is not proof against later corruption or runtime OOM.
   Source-candidate
   selection must not silently widen the sealed proposal.

## Scope and delivery slices (one separate code PR after approval)

1. Resource-control qualification and cross-path tests using real writer-generated
   whole/streamed/raw/zstd fixtures. Document measured limits. This is a go/no-go
   gate: roughly 100 MiB and 410 MiB whole frames must successfully decode with
   the chosen guard on the target stack before calling the proposal viable.
2. Confined large-frame worker and Slice integration, cancellation/error tests,
   early readiness and explicit resource settings.
3. Complete writer/canary/restore compatibility regression coverage with no changes
   to their production behavior; retain codec selection, disk capacity accounting
   and canary-before-drop unchanged.
4. End-to-end synthetic public CLI qualification, then separately scoped physical
   compressed-source acceptance. Append approved decisions to the authoritative
   ledger and synchronize user docs. Review Grok locally and Greptile/Codex in PR
   according to the user's review workflow when implementation is authorized.

## Required tests and review hazards

- Real writer round trips above 64 MiB, including roughly 100 MiB and 410 MiB
  whole frames, short tail frames, configurable stream chunks, incompressible
  expansion, all supported byte dtypes, optional zstd absence and raw fallback.
- Do not monkeypatch away the native decoder for resource qualification. Test the
  actual enforced ceiling, native allocation failure/crash, controlled low budget,
  malformed oversized headers, truncated/trailing frames and wrong hashes.
- Worker cleanup on stop, source unplug/remount, destination ENOSPC/EIO, blocked
  pipe and parent failure; no inherited fence lifetime, orphan worker or success
  certificate. Preserve source-vs-destination error attribution.
- A failed readiness check must create no destination. Runtime rechecks still
  catch changes after preflight. Existing FAT one-attempt/no-resume stays intact.
- Prove archive bytes and catalog identity/generation are untouched by Slice;
  no serial repair, schema migration, annex-key change, re-compression or retrieval.
- Writer and canary must not produce/drop the original of an output they cannot
  round-trip; restore must preserve current supported formats and publication
  behavior. Capacity upper-bound/fallback tests must continue to hold.

## Alternatives rejected for the proposal

- Just raise/remove 64 MiB: combines unrelated limits and offers no native-memory
  protection or common producer/consumer contract.
- Stream all future archives only: leaves existing valid whole-file archives
  unreadable by Slice and changes DEC-022 without solving backward compatibility.
- Rewrite the archive or change its catalog: unnecessary mutation and migration
  risk for what should be a read-path compatibility fix.

## Review

Local Grok CLI review completed 2026-09-10, session
`01a08c73-d885-7930-bcc6-bb6dd26a9e56`; verdict on the initial draft:
ACCEPT WITH CHANGES. Its initial inspection reached the turn cap; the same session
was resumed to deliver its conclusion, not a separate review pass.

Incorporated: narrow first PR to Slice and cross-path tests; preserve DEC-022 and
restore/canary behavior; preflight attached sealed candidates without breaking
drive swaps; bind budgets to approval; specify pipe lifecycle and descriptor
ownership. Keep StreamZNN standalone and zstd out of the ZipNN helper.

Not accepted: Grok suggested that a child that can OOM is sufficient initial
protection and that hard memory enforcement should be optional. Process isolation
contains many native crashes but does not itself guarantee protection of the
parent or host under memory pressure. Resource enforcement and successful real
fixture qualification remain required. This revised plan has not received a
second Grok verdict; do not describe it as unconditionally approved.

No implementation or live test authorized by this proposal itself. The unrelated
docs-only PR #73 must remain unchanged remotely. No accepted DEC has been appended:
this remains a proposal pending operator approval, with its resource mechanism
explicitly subject to qualification rather than assumed proven.
