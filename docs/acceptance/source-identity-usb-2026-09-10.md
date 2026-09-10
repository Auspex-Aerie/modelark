# Source identity and USB acceptance — 2026-09-10

This records separately authorized operator acceptance after PR #72 merged as
`16e8b3f3a5f5baec34c5720971bad01a8ed19533`. It does not authorize later device
operations or imply that all sources, filesystem settings or interruption modes
have been physically qualified.

## Deployed package and live repair

- The accepted wheel's SHA-256 was
  `52b420e5c4d71846640064e94d5578521ecb8c03f09458a1ac62db61c3ca7c93`.
  All 122 packaged payload files matched both the reviewed merge and installed package.
- The new candidate was installed alongside the retained old build with existing
  runtime dependency versions. Dependency consistency checks passed.
- With the service stopped, consistent backups and an isolated clone rehearsal passed.
  The live drive-07 repair retained its saved serial and identity epoch, changed only
  its fingerprint/generation fields, retained historical anchors/owners/session rows,
  and superseded exactly one affected Fill approval. Unrelated approvals and tables
  were unchanged. Integrity and foreign-key checks passed.
- The catalog explicitly advanced from reader floor 7 to 8. The installed new reader
  accepted it read-only; the old deployed reader rejected a repaired copy.
- Portal startup passed health checks, left the catalog logically unchanged and
  created no execution session. Fill remained idle with automatic resume disabled.

Logical catalog equality is not a claim that SQLite WAL/SHM sidecars never changed.
Repair inventory was presence evidence, not a complete archive-byte rehash.

## Physical FAT32 delivery — passed

The target was the operator's existing 14.3 GiB SanDisk USB, separate from the archive.
The initial preview refused the desktop's unqualified `showexec` setting without
creating output. The operator mounted it with the admitted name/permission profile
and `noexec`; the mount directory had to be recreated after unmount. No persistent
mount configuration, formatting or deletion of existing files was performed.

The installed public CLI performed Preview → Approve → Start → Status for
`facebook/wav2vec2-base-960h`, using actual drive-07 source evidence after repair.

- Profile: `fat32-folder-session.v1`; one attempt, new output folder only.
- Closure: nine files, **377,615,574 original bytes**, all from uncompressed sources.
- Every delivered file's independently recomputed size/SHA-256 matched the sealed closure.
- The output-file set contained exactly the nine artifacts plus owner and receipt files.
- The media report said `export-verified`; separately checked host status was `complete`.
  Their transaction/seal matched. The media report correctly did not certify the later
  host commit or its own subsequent persistence.
- All eight pre-existing USB files retained their sizes and hashes. The live catalog
  was logically unchanged. The portal was restored with Fill idle.

Private operator evidence is retained under candidate
`16e8b3f/usb-slice-acceptance-k7bol08o/`: `result.json`, public CLI outputs,
the baseline catalog, sealed preview, and copied delivery receipt.
Transaction: `a40ac9f588c847f682d0b2f08939318f`.
Output and receipts remain on the USB; no automatic cleanup was performed.

This is not proof of model loading, whole-archive verification, physical unplug or
power-loss survival, compressed-source USB delivery, or arbitrary FAT mount profiles.

## Compressed-source follow-up — preflight only

The smallest compressed repository by stored catalog selection expands to a single
file above FAT32's 2^32−1-byte limit. Six smaller complete sets fitting the current USB
capacity and per-file bound were checked through the installed launch guard,
`FencedSources`, attachment-bound local reader, and unchanged 64 MiB decoder bound.
No USB or archive writes were made during these checks; the catalog was unchanged.

| Candidate set | Original closure bytes | Initial compressed reads |
| --- | ---: | --- |
| Qwen3-4B Base / Instruct-2507 / standard | 8,056,510,149 / 8,060,906,225 / 8,060,915,283 | First two streamed shards decoded; the 99,630,640-byte final shard refused `SOURCE_DECODE_LIMIT` |
| Trinity-Nano Base-Pre-Anneal / Base | 12,259,248,873 / 12,259,281,420 | First six shards decoded; the 409,993,344-byte final shard refused `SOURCE_DECODE_LIMIT` |
| ChatGLM3-6B | 12,488,333,964 | All seven compressed shards produced a first decoded byte |

The smaller-set refusal is the existing whole-ZipNN frame bound, not corruption
evidence. A first decoded byte is not full-container or original-hash verification.
The 12.49 GB ChatGLM3 candidate is much larger than the small follow-up discussed;
full physical compressed-source acceptance remains pending a decision on that test.
Do not raise limits, re-encode archive objects, split files or alter the catalog merely
to turn this preflight into a passing test.

Private report: candidate `16e8b3f/compressed-source-preflight-c99jzxxx/result.json`.
These results distinguish a supported streamed source from a complete executable
repository: one unsupported required artifact blocks the full delivery.

## Closeout and deferred work

- DEC-133 through DEC-137 landed with implementation in PR #72.
- BOT-007 records operator approval continuity; `AGENTS.md` carries the working rule.
- DEF-044 defers guided destination selection/readiness and exact mount commands.
- This documentation change does not implement decoding changes, onboarding UI,
  new filesystem support, automatic Fill resume or further device operations.
