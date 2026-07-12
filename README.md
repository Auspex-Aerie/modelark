# ModelArk

> 🚧 **Building in public.** An in-progress project, developed openly (with Claude). It works
> end-to-end and restorability is production-verified — but it isn't 1.0. The honest
> [Status](#status) + gaps are at the bottom; feedback and contributions are welcome.

An ark for open model weights: catalog every open model worth
knowing about, archive the ones worth keeping across an offline drive library —
**compressed and integrity-proven** — and verify each is intact and loadable, even
the giants you can't run locally.

## Why

Hugging Face has north of a million repos — really, have you checked lately? Most you'll
never keep. ModelArk catalogs the metadata broadly (cheap — a few MB), then downloads only
the curated, full-precision weights worth archiving, tracked by **git-annex** across a fleet
of external HDDs + a NAS that mostly live offline. The git repo is the *map* of the library;
the bytes never touch git.

## Why else?

You could just use the Hugging Face tooling, keep it all local, and move it around afterward.
ModelArk is for when you'd rather **store now, use later**: version your library over time, and
let it spread downloads across whatever drives you have — efficiently, instead of by hand.

## Is that it?

Not even close. Weights are stored **ZipNN-compressed** (lossless, float-aware) for real
savings, and it **streams** — a giant shard compresses and restores in O(chunk) memory
(hundreds of MB), never fully materialized in RAM. On top of that:

- **Integrity you can trust** — every compression passes a round-trip *canary* (decompress →
  hash → match HF's sha256) *before* the original is dropped, so a model can't be archived in an
  unrestorable state.
- **Drive-health monitoring** — SMART vetting before a volume ever holds archives.
- **Plans** — multiple archive sets, each with its own drive fleet and budget.
- **Smart resume** — a crash re-processes only the interrupted shard; no re-download.
- **Tiered storage** — mark keepers *must-have* (kept redundantly) vs bulk that's cheap to re-fetch.
- **N-copies redundancy** — copy counts enforced from a durable record, not guessed.

…and there's plenty more in the [decision log](docs/decision_log.md). The product is **early**,
put out deliberately in a build-in-public stance — [contributions welcome](CONTRIBUTING.md).

## Architecture

```
GitHub: Auspex-Aerie/modelark          ← code + catalog export + git-annex index
   the map: what exists, where it lives, is it intact — small, versioned, backed up
Tiered drive fleet (git-annex remotes)      ← the actual TBs of weights
   NAS RAID (iSCSI, copy #1) · big USB primaries (bulk) · a small replica drive (copy #2)
```

- **Catalog** — SQLite (`catalog/catalog.sqlite`, rebuildable). Source of truth for
  *what exists* and *what we want*. Diffable JSONL under `catalog/export/` is versioned.
- **git-annex** — authority for *where bytes physically live*; tracks drives even
  unplugged, enforces N-copies redundancy, `fsck`s integrity.
- **modelark.core** — reusable catalog/db primitives.
- **modelark** — discovery, placement (the "librarian"), fetch, compression, verify.
- **modelark.streamznn** — standalone MIT streaming-compression module (see below).

## The pipeline

`discover → curate → plan → fill → verify → replicate`, all resumable:

1. **Discover** metadata for the wishlist orgs (architecture-first classification, not HF's
   unreliable pipeline tags).
2. **Curate** in the portal — build a set within a size budget; mark keepers *must-have*.
3. **Plan** — the librarian bin-packs the set across the tiered fleet (must-have copy #1 on
   the RAID, bulk consolidated onto the big primaries, must-have copy #2 on a replica drive).
4. **Fill** — per shard: HF download → sha256 vs HF's canonical hash → compress → **canary**
   → drop the original → git-annex add → record. Throttled by an optional rolling 24 h cap (uncapped by default), resumable at
   the file level (a crash re-processes only the interrupted shard, no re-download).
5. **Replicate** — must-have copy #2 is a *local* clone→clone transfer from copy #1 (no second
   HF download).

## Compression & integrity

Weights are stored **ZipNN**-compressed (`.znn`, lossless, float-aware) and decompressed on
use. Every compression is gated by a mandatory **round-trip canary**: decompress the `.znn`,
hash the result, and require it equal HF's canonical sha256 **before** the uncompressed
original is ever deleted. A model can never be archived in an unrestorable state.

**StreamZNN** (`modelark/streamznn.py`, MIT, standalone) wraps ZipNN so a shard of *any* size
compresses and restores in **O(chunk) memory** — a 10 GB shard peaks ~800 MB instead of the
~26 GB that whole-file compression needs (which OOM-killed the portal once; see `INC-003`).
It frames independent, self-describing ZipNN blobs; writes are atomic; corruption fails loud;
and the canary shares the exact restore decompress path, so "canary passed" *means* "restore
is byte-identical." Proven in production: every archived file independently re-decompresses to
HF's canonical hash (`DIS-002`), including the four ~8 GB shards of the model that OOM'd.

**Codec gate** (`DEC-022`) — the codec is chosen *per shard* from config, so the new streaming
path isn't used unless it's warranted:

| condition | codec | why |
|---|---|---|
| `~4× shard ≤ max_compress_ram` | whole-file ZipNN | fastest, best ratio, in-RAM |
| over budget, `stream_compress: true` | **StreamZNN** | O(chunk), float-aware |
| over budget, stream off, `zstandard` installed | zstd-stream | boring/proven fallback |
| over budget, stream off, no zstd | raw | never compress what we can't do safely |

Restore/canary route by the stored file's magic, so every codec (and legacy `.znn`) restores.

## The portal

`modelark serve` → http://127.0.0.1:8077

- **Plans** — create or recall an archive set (its own drive fleet, budget, and provisioning mode);
  you pick an active plan per session before the other tabs unlock.
- **Catalog** — browse/curate the set within a size budget; filters, bulk select, finalize.
- **Disk Health** — SMART for attached drives (vet a volume before it holds archives).
- **Library** — what's actually archived and where, from the durable record (works offline).
- **Fill** — the librarian's placement plan as a live run surface: **Start/Stop**, a "now
  fetching" panel (per-shard phase, throughput, ZipNN ratio, 24 h-cap gauge), a queue, and
  per-drive fill bars. The fill runs in a single safe background worker inside the portal
  (one at a time, clean stop at a file boundary, dies with the process, per-file transactional).
- **Verify** — re-check archived copies on demand (record consistency + a decompress canary when the
  drive is mounted), and auto-surface anything that looks disrupted: a raw-fallback, a partial copy,
  or an archive written near a recorded interruption.

![The Fill run surface](docs/fill-dashboard.png)

## Configuration — `wishlist.yaml`

Curation (what to collect) plus operational knobs:

```yaml
scope:            # architecture-derived categories in scope (language models for now)
always_include:   # orgs to always walk
exclude:          # repos/patterns to skip
score_weights:    # ranking inputs
threshold: 6.0    # keep score cutoff

compression:            # DEC-022 codec gate
  max_compress_ram_gb: 4.0   # whole-file peak ≈ 4× shard; over this, don't use whole-file
  stream_compress: true      # over budget → StreamZNN; false → zstd-stream if installed, else raw
  threads: 1                 # ZipNN internal threads (was 4; its native threaded path can double-free — INC-005)
```

Must-have status (a replicated 2nd copy) is set with `modelark protect --repo <id>` (there is
no cart UI for it yet — see gaps below).

## Setup

**Python (3.10+):**
```bash
python3 -m venv .venv
.venv/bin/pip install -e .            # or: .venv/bin/pip install -r requirements.txt
```
`zipnn` is a hard dependency (compression + the canary). `zstandard` is *optional* — only the
stream-off zstd fallback needs it; without it that fallback stores raw.

**System packages (apt):**
```bash
sudo apt-get install -y git-annex smartmontools open-iscsi   # open-iscsi only if using a NAS LUN
```
- **git-annex** — tracks model bytes across the offline fleet.
- **smartmontools** — `smartctl`, for the Disk Health page.
- **open-iscsi** — attach a NAS RAID LUN as copy-#1 storage (optional).

**Disk Health SMART access — grant `smartctl` passwordless sudo (do NOT run the portal as root):**
The portal runs as *your* user (the shipped systemd unit sets `User=` to you, not root) so the catalog and the
git-annex clones on each drive stay user-owned. `smartctl` needs root, so grant it a single
passwordless-sudo rule rather than escalating the whole service:
```bash
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/smartctl" | sudo tee /etc/sudoers.d/modelark-smartctl
sudo chmod 440 /etc/sudoers.d/modelark-smartctl
```
`disk_api` already calls `sudo -n smartctl`, so the Disk Health tab populates immediately — no portal
restart. **Don't run the portal as root:** root-owned catalog/annex files trigger git "dubious
ownership" refusals and it's a needless privilege escalation for a network-listening service. Only the
hardware ops (SMART read, `mkfs`, `mount`) are elevated, and only via `sudo`. *(Automating this
drop-in + the systemd unit is `DEF-025`.)*

**Hugging Face auth** (optional — gated repos, higher rate limits): `.venv/bin/hf auth login`.

## Usage

```bash
modelark discover --walk                     # catalog the wishlist orgs
modelark verify --all                        # Tier A structural check (no download)
modelark protect --repo org/model            # mark must-have (numcopies=2 → a 2nd copy)
modelark serve                               # portal → :8077 (curate, then Fill)
modelark library plan                        # review the placement plan
modelark library plan --apply                # run the fill from the CLI (portal must be stopped)
modelark export                              # dump JSONL for git
```
The fill runs either from the **Fill tab's Start button** (worker inside the running portal) or
from `library plan --apply` (CLI — needs the portal stopped, since it holds the DB write lock).
Disk Health needs SMART access — grant passwordless sudo for `smartctl` (see **Setup**); don't run
the portal as root.

## Verification tiers

| Tier | Proves | Cost |
|------|--------|------|
| **A — Structural** | valid, complete, known-architecture checkpoint | range-reads headers only — **works on a 700B without downloading it** |
| **B — Functional** | actually generates coherent tokens | needs the model to fit (~≤32B Q4 on 24 GB) |

Integrity = sha256 vs HF's canonical hash + git-annex `fsck`. Security gate = format-safety
(prefer safetensors/GGUF; flag pickles) + HF scan.

## Safety invariants (the gates)

- **Canary before drop** (`DEC-003`) — the uncompressed original is deleted only after the
  `.znn` is proven to decompress back to HF's exact bytes.
- **No silent under-replication** (`DEC-019`) — a must-have never ends below its copy count:
  GATE-A (every fetch target must be a live mount), GATE-B (refuse an unplaceable plan),
  GATE-C (post-fill assertion that every must-have holds ≥ numcopies complete copies).
- **Codec gate** (`DEC-022`) — streaming/zstd/raw chosen by an explicit RAM budget, logged per shard.
- **Crash-resume** — per-file transactional writes; a portal death loses at most the in-flight
  shard, which resume re-processes from the on-disk cache (no HF re-download).

Every architecture/policy decision, deferral, and incident is in
[`docs/decision_log.md`](docs/decision_log.md) (ADRLight-style ledger).

## Status

**Working + proven end-to-end:** catalog (~4.1k models) + Tier A verification +
architecture-first classification; the librarian placement plan; the full fetch pipeline
(download → verify → ZipNN + canary → git-annex → record); StreamZNN streaming (no OOM on
10 GB shards); the codec gate; must-have 2-copy replication; crash-resume; first-class Plans
(per-set drive fleet + a capacity failsafe) + on-demand re-verification; and the portal's six
views including the live Fill run surface. Restorability is production-verified (`DIS-002`).

**In flight:** the first full-scale fill — running, not yet completed end-to-end.

**Missing / roadmap (honest gaps):**
- **Must-have has no UI surface** — you can only mark a model must-have via `modelark protect`
  on the CLI; the cart should let you toggle it (planned, #21).
- **Scheduled Library Audit** — the Verify tab does on-demand re-verification + auto-surfaces
  disruption suspects; a *scheduled* fleet-wide sample audit isn't automated yet (#23).
- **Download Status view** — `fetch_events` are recorded (and surfaced in Verify), but there's no
  dedicated history view yet (#18).
- **iSCSI boot persistence** — a NAS LUN doesn't auto-reattach after a reboot yet (manual re-login).
- **zstd fallback** — implemented but dormant/untested until `zstandard` is installed.
- **Run-page polish** — ambient progress animation is planned, not built.
