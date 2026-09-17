# RFC-003: Local verified-export provider (FrostByte / AEON)

- **Status:** OPEN — design contract; FrostByte reviewing the shared envelope
- **Date:** 2026-09-16
- **Owners:** Auspex-Aerie + operator
- **Related:** DEC-081, DEC-084, DEC-155, DEF-041, FrostByte #20
- **Partner draft:** [FrostByte `docs/MODELARK-ARCHIVE-PROVIDER.md`](https://github.com/Blackfrost-AI/FrostByte/blob/modelark/local-archive-provider/docs/MODELARK-ARCHIVE-PROVIDER.md) on branch `modelark/local-archive-provider`

## Summary

ModelArk already archives, verifies, and restores. FrostByte and AEON Orb already transfer and share. This RFC is the local seam: when those apps are installed next to ModelArk, they may ask for an identified artifact and receive **verified original files** plus labeled evidence (vendor hashes, git-annex custody). They seed or pin with their own engines.

This is the same **materialization engine** Usable Slice needs, with a different **claim**:

| Purpose | Receipt |
|---|---|
| `verified-export` (this RFC, hop 1) | These original bytes matched; FrostByte/Orb may share them |
| `usable-slice` (DEC-081, later) | Layout plus bytes match; usable for a named consumer |

A seeded torrent is not proof a model is loadable. Annex location is not a live replica count.

No adapter, portal route, archive mutation, or P2P transport is authorized by opening this RFC. Implementation waits on FrostByte review of the envelope and a later DEC.

## Product split

- **ModelArk** owns retrieval, drives, verification, visit order, and the evidence envelope.
- **FrostByte** owns BitTorrent, IPFS, and Sharing.
- **AEON Orb** owns IPFS library/deploy. Same envelope, REST encoding later.
- Three codebases stay three codebases. FrostByte source is not copied into Apache-2.0 ModelArk.

Files handed over are **plain regular files** (`nlink === 1`). No git-annex symlinks, no hardlinks onto annex objects, no `.git/annex/objects` paths. FrostByte's `seed()` and IPFS add refuse linked files.

## Why this is not “Usable Slice with a magnet”

DEC-081 is operator-guided DR onto a destination disk with a consumer/layout profile and a usable receipt. DEF-041 still defers that workflow.

DEC-084 already named the shared core: artifact identity, manifests, placement, copy evidence, resumable movement, verified materialization. Slice and export are two purposes on that core.

Export does not pick a ScintiLab layout, does not format a destination disk, and must not say “usable.” Slice later should reuse this job (visit plan, journal, cancel, dual ETAs), not a second restore path.

`restore_repo` is not the executor. It walks files and tries copies in catalog order, which can ping-pong drives. Fill already pins one drive until that drive's ready work is exhausted. Both purposes use **that** scheduler.

## FrostByte flow this has to match

Sharing today expects bytes already on disk:

1. HF download (or a local folder) produces regular files.
2. `seed()` reviews them, **reads every byte once** to build torrent metadata, then serves pieces until Sharing is off.
3. Explicit library shares ignore ratio/time goals. IPFS sharing comes back **off** after restart until the user turns it on.

Cold annex media does not fit inside `seed()`. Drive swaps and decompression belong in a **prepare** job. When prepare is `done`, FrostByte uses the existing Sharing switch — the same shape as “HF finished → Sharing on.”

Do not call `seed()` while ModelArk is waiting on a disk. Incomplete trees are not shareable.

Hop 1 does **not** add rows to FrostByte `catalog.js` (that catalog still requires a magnet/infohash). After prepare, existing `library:seed` / `ipfs.add` create a normal inventory item.

## Hold — no ModelArk timer

Prepared files stay available until the caller **releases** them (Sharing off, pin removed, or explicit release). There is no ModelArk expiry.

`holdUntil` is honored only if the caller sends it. Default is unlimited.

The constraint is **staging space and attached media**, not time. Refuse a prepare that needs more staging than is free, or more drives than the operator can have attached for that job. Do not start a visit plan that cannot finish.

A later **direct-read** path can avoid the extra copy (FrostByte already serves from held file descriptors). That is not hop 1. Direct read still needs original bytes (decompress on the way if stored compressed) and still needs the relevant drive mounted for the initial hash and for as long as Sharing is on. It does not remove the operator; it removes the second tree.

## Shared job

One `MaterializationJob`:

- frozen file set from `archive_manifest`
- visit plan (drive order, bytes per visit, currently attached)
- durable journal (completed files survive crash)
- cooperative cancel at a **file** boundary
- hidden staging until the whole selection verifies; then publish the hold root
- snapshot: `progress` (0..1), `bytesDone` / `bytesTotal`, `etaTotalSec`, `etaNextSwapSec`, `awaitingDrive`

`purpose` is `verified-export` | `usable-slice`. Hop 1 implements export only.

Pin a drive until this job's files on it are done. Do not ask for a finished drive again unless the caller cancelled, changed the file set, or verification failed. Changing the file set after start is cancel + new job. Queued jobs may be replaced.

A hold is not Sharing-on and not Frost-Net publication.

## Envelope (draft, same as the FrostByte review copy)

Returned when prepare completes. `hold.root` is the folder FrostByte may seed or pin.

```json
{
  "schema": 1,
  "purpose": "verified-export",
  "artifact": {
    "id": "org/model",
    "revision": "abc…",
    "manifest_digest": "sha256:…"
  },
  "files": [
    {
      "path": "model.safetensors",
      "size": 123,
      "original_sha256": "…",
      "authority": {
        "kind": "hf-revision",
        "repo": "org/model",
        "revision": "abc",
        "vendor_sha256": "…",
        "provenance": "hub_confirmed"
      },
      "stored": {
        "representation": "raw",
        "annex_key": "SHA256E-s123--…"
      },
      "copies": [
        {
          "drive": "drive-07",
          "fs_uuid": "…",
          "observed_at": "2026-09-12T00:00:00Z",
          "class": "attached",
          "presence": "physical-verify"
        }
      ]
    }
  ],
  "hold": {
    "id": "hold_…",
    "granted_at": "2026-09-16T00:00:00Z",
    "root": "/path/to/regular-files",
    "holdUntil": null
  }
}
```

- `original_sha256` is the bytes at `hold.root` (what they seed).
- `authority` is the vendor claim (HF LFS / revision). It may equal `original_sha256`; it stays labeled separately. Provenance uses existing archive values (`hub_confirmed`, `ingestion_computed`, `annex_key`, …).
- `copies[]` is custody (drive label, filesystem UUID, when observed, how). Not a mount path, not `replicas.serving`.
- `holdUntil` is null unless the caller set it.

## Prepare snapshot

```json
{
  "id": "job_…",
  "status": "preparing",
  "progress": 0.4,
  "bytesDone": 40,
  "bytesTotal": 100,
  "etaTotalSec": 1200,
  "etaNextSwapSec": 300,
  "awaitingDrive": null,
  "visitPlan": [
    { "drive": "drive-07", "bytes": 80, "state": "current" },
    { "drive": "drive-01", "bytes": 20, "state": "pending" }
  ]
}
```

`status`: `queued` | `preparing` | `needs-media` | `paused` | `done` | `error` | `cancelled`

Unknown ETAs are null. Unknown is valid.

## Encodings (not hop 1)

Same operations, two encodings when implemented:

- FrostByte: JSON-line over a local socket, `{action, …}` → `{ok, data|error}`, matching their Spark companion envelope. Not a Spark device.
- Orb: HTTPS + bearer, fire-and-forget prepare, poll snapshot, then existing `POST /api/ipfs/add`.

Discovery is optional, off by default, loopback/socket only. Existing portal `/api/*` is operator UI, not this service.

## Hop 1 vs later

**Hop 1 (after FrostByte agrees the envelope):** ModelArk prepare of one artifact from **currently attached** media (or a stated staging budget); regular-file hold root; this envelope. FrostByte: small client + fixture on `modelark/local-archive-provider`.

**Later:** direct read (no extra copy); range-read into their reviewed store; Frost-Net catalog consumption; AEON source adapters (HF / Ollama / Civitai / IPFS / local import); `usable-slice` purpose; datasets as generic artifacts plus kind-specific metadata.

## Non-goals

- Merging repositories or vendoring FrostByte
- Serving annex checkouts
- Inventing infohashes so rows fit FrostByte's current catalog
- Treating transport, gossip, or `whereis` as usability or live replica proof
- Building the Slice destination-disk wizard or R2 in order to ship export
- Wrapping `restore_repo` as the provider
- Messages or adapters beyond the already-open FrostByte issue/branch and this RFC

## Open

FrostByte is reviewing the partner copy of this envelope. Amend both documents together if that review changes fields. Do not implement the provider until a follow-up DEC closes this RFC or explicitly authorizes hop-1 code.
