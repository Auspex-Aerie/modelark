# Decision Log

Append-only decision ledger for `modelark`, in the style of
[ADRLight](https://github.com/Indubitable-Industries/ADRLight). One file, one
causal history. We record *decisions, deferrals, hypotheses, discoveries, and
incidents* — not tasks (those live in the work tracker / HANDOFF notes).

## How to use this ledger

- **Append-only.** Never delete or rewrite a past entry. The only edit allowed on
  an old entry is a **status update** (e.g. `accepted` → `superseded by DEC-007`).
- **Eight independent ID spaces**, each zero-padded and monotonic: `DEC`, `DEF`,
  `DEF-CATALOG`, `HYP`, `DIS`, `INC`, `OUT`, `BOT`. The ledger allocates IDs — even an external incident
  gets a stub here. (`DEF-CATALOG` defers *what-to-archive* curation, vs `DEF` which defers *engineering* work.)
- **Link entries** to form the causal DAG: `triggered_by` (causal origin),
  `supersedes` (DEC replaces DEC), `resolves` (DEC closes a DEF), `promotes`
  (HYP → DEC when a winner is chosen), `related` (non-causal cross-ref).
- Every entry lists **`docs_updated`** — the files it touched (dead links to since-
  deleted files are valid history).
- **Avoid:** essay-length entries, status-less drift, silent backfilling, and using
  this as how-to documentation.

### Status lifecycles
| Type | Purpose | Lifecycle |
|------|---------|-----------|
| **DEC** | Architecture / policy / process decision | `accepted` → `superseded by DEC-###` |
| **DEF** | Deferred *engineering* work with explicit revisit condition | `active` → `resolved by DEC-###` |
| **DEF-CATALOG** | Deferred *catalog-curation* item — a lab/family/scope to research + keep/prune/swap later | `active` → `curated` or `resolved by DEC-###` |
| **HYP** | Systematic experiment with a test matrix | `open` → `promoted to DEC-###` |
| **DIS** | Signal reinterpretation (no action attached) | `observed` (terminal) |
| **INC** | Root cause of a defect found in investigation | `open` → remediated via DEC |
| **OUT** | Service-impact event (downtime, data loss) | `open` → `closed` at recovery |
| **BOT** | Human intuition corrected the AI(s) — "caught the bots" | `logged` (terminal) |

### Templates
```
### DEC-###: <imperative title>
- date / status / triggered_by / docs_updated
- decision:  What changed; specific enough to act on
- rationale: Why; rejected alternatives
- impact:    Code/docs affected, work spawned
- supersedes: DEC-### (optional)

### DEF-###: Defer <the work>
- date / status / triggered_by / docs_updated
- decision:     What is deferred, what proceeds in the interim
- rationale:    Why deferring is safe
- revisit_when: Explicit re-entry condition (required)

### DEF-CATALOG-###: Revisit <lab / family / scope> in the catalog
- date / status / triggered_by / docs_updated
- observation:  What was noticed about the catalog content (models, versions, gaps)
- deferral:     What curation decision is postponed; what selection stands in the interim
- revisit_when: Explicit re-entry condition (required)

### HYP-###: <falsifiable question>
- date / status / triggered_by / docs_updated
- question / observation / interventions / test matrix / results (appendable)

### DIS-###: <what the signal actually means>
- finding / implication

### INC-###: <defect summary>
- symptom / root_cause (file:line) / blast_radius / why_not_caught_earlier

### OUT-###:
- severity / summary / remediation / detail

### BOT-###: <the over-claim the human caught>
- date / status / triggered_by
- claim:      what the AI(s) asserted
- correction: the user's intervention
- verified:   what the data/analysis showed afterward
- lesson:     the failure mode to watch
```

---

### DEC-001: Catalog all geographies; diversify labs and language model types
- date: 2026-06-27 / status: accepted / triggered_by: user prompt / docs_updated: catalog_discussions.md, wishlist.yaml
- decision: Scope = **language models, all geographies.** Lead coverage with the
  Chinese frontier labs (DeepSeek, Qwen, Moonshot/Kimi, Zhipu/GLM, MiniMax, Tencent,
  StepFun, Xiaomi, Baidu, etc.) which now top open-weight leaderboards, while **also**
  fully covering Western labs (Meta/Llama, NVIDIA/Nemotron, Mistral, Google/Gemma,
  Microsoft/Phi, AI2/OLMo, IBM/Granite). Expand lab **diversity** along the Arcee line
  (independent/second-wave labs: Arcee, Cohere, Nous, AI21, Liquid, LG EXAONE, Sakana,
  BigCode, SmolLM) rather than only more models from the same big labs. Expand model
  **types** beyond generative LLMs to encoder/BERT-family (`fill-mask`), embeddings/
  retrieval (`sentence-similarity`, `feature-extraction`), and classification.
- rationale: The open frontier shifted to China since the project's mental model formed;
  a Western-only allowlist would miss the leaders. Lab diversity and encoder/embedding
  models capture high-utility repos a big-lab, LLM-only allowlist would skip.
- impact: wishlist.yaml org allowlist widened (Chinese frontier + diverse + encoder/
  embedding orgs); discovery scope widened to language pipeline tags. Catalog landscape
  preserved in catalog_discussions.md.

### DEF-001: Defer local management web UI
- date: 2026-06-27 / status: resolved by DEC-004 / triggered_by: user prompt / docs_updated: catalog_discussions.md
- decision: Do not build the local selection/management webserver yet; keep
  architectural space (catalog status lifecycle, JSONL export, query layer) so a UI can
  sit on top later.
- rationale: Catalog + fetch pipeline must exist before there is a library to manage;
  building UI now is premature.
- revisit_when: catalog is populated and the fetch/git-annex pipeline works end-to-end.

### DEF-002: Defer non-language modalities
- date: 2026-06-27 / status: partially resolved by DEC-010 + DEC-011 (audio-speech + world-model + image-gen added; video + VLM/multimodal still deferred) / triggered_by: user prompt / docs_updated: catalog_discussions.md, wishlist.yaml
- decision: Catalog language models only for now (generative + encoder + embedding +
  classification). Defer image / video / audio-speech and vision-language (VLM) models.
- rationale: Focus — "stick to lang for now." Landscape research for the other
  modalities is preserved in catalog_discussions.md for when we expand.
- revisit_when: language catalog and fetch pipeline are established.

### DEC-002: Classify catalog scope by architecture-derived category, not pipeline_tag
- date: 2026-06-28 / status: accepted / triggered_by: DEC-001 / docs_updated: modelark/formats.py, modelark/discover.py, modelark/core/schema.sql, wishlist.yaml, modelark/cli.py
- decision: HF `pipeline_tag` is unreliable — it is absent on `library: vllm` repos,
  which includes the entire current Mistral instruct lineup — so it is demoted from
  scope *gate* to a *hint*. Scope is decided by our own `category`, derived
  **architecture-first** (`config.architectures`), with pipeline_tag/tags/library as
  secondary signals. Categories (domain=text), all in scope: generative-llm, encoder,
  seq2seq, translation, embedding, reranker, classifier, qa. An orthogonal `variant`
  (base/instruct/reasoning) is also recorded. Non-text domains and `category:unknown`
  are excluded and logged with the resolved category as the reason. Org-handle hygiene:
  `OpenBMB`→`openbmb` (case-sensitive), add `internlm` (text arm; OpenGVLab is the VLM arm).
- rationale: The DEC-001 walk cataloged only 3 mistralai models and excluded ~37 real
  language models purely for a missing pipeline_tag. Architecture is authoritative;
  cheap tags are ambiguous (`mistral3` spans both the LLM and the VLM). Cost is bounded:
  `model_info` is fetched only for text/tag-less repos; clearly-non-text tagged repos
  are excluded without a fetch.
- impact: new `category`/`variant` columns; discovery is architecture-aware and
  category-scoped; exclusion reasons become category-based; catalog rebuilt via re-walk.
- supersedes: (refines DEC-001 scope mechanism)

### INC-001: Uncapped walk exhausted HF API rate limit (1000 req / 5 min)
- date: 2026-06-28 / status: remediated via 9b1fe1d / triggered_by: DEC-002 / docs_updated: modelark/discover.py
- symptom: `discover --walk --limit-per-org 0` cataloged only 16/41 orgs; orgs 17–41
  returned HTTP 429, the embedding orgs were entirely skipped, and ~421 in-flight
  model_info calls degraded to fetch-errors. Catalog left incomplete (1292 models, 16 orgs).
- root_cause: discover_orgs issued one model_info call per text/tag-less repo with no
  rate-limit handling; uncapped volume blew HF's 1000-request / 5-minute quota.
- blast_radius: incomplete catalog until resume; no data loss (catalog is rebuildable).
- why_not_caught_earlier: capped walks (≤40/org) stayed under the quota; only the uncapped
  pass crossed it.
- remediation: `_backoff()` honors Retry-After and retries; `discover_orgs` skips
  already-cataloged repos so the walk resumes without re-fetching completed orgs (9b1fe1d).
  Walk depth set to a 400/org cap (DEC-001 coverage vs. rate-limit pragmatism).

### DEF-003: Defer drive/volume health management (SMART)
- date: 2026-06-28 / status: active / triggered_by: user prompt / docs_updated: catalog_discussions.md
- decision: Build a volume-health layer for the offline drive fleet — but not yet. Scope when built:
  (a) onboarding qualification in `drive register` — smartctl baseline + long self-test (+ optional
  badblocks) before a drive is trusted, then reformat (ext4/XFS) + label + capture fs_uuid;
  (b) ongoing `drive health` — periodic smartctl snapshots into a `drive_health` time-series table
  (reallocated/pending sectors, CRC errors, power-on hours, temp, self-test result) with
  ok/watch/evacuate thresholds;
  (c) health→action: a drive crossing 'evacuate' triggers `git annex move` off it (safe because
  numcopies≥2 keeps a copy).
- rationale: 27TB of old Seagate spinners need vetting before archival trust and monitoring to
  evacuate a dying drive before data loss. The `drives` table already reserves health/last_seen;
  this rides with the drive-registration / git-annex ingest phase, not the catalog phase.
- revisit_when: starting drive registration / the git-annex ingest pipeline.

### BOT-001: "Bases are scarce" — variant parser only matched the literal word "base"
- date: 2026-06-28 / status: logged / triggered_by: score.py prep
- claim: AI reported only 161 base models (~6% of LLMs), called bases "scarce", and shaped a
  base-first bucket policy around that scarcity.
- correction: user pushed back — "they can't be that scarce... there was a whole section of them."
- verified: `parse_variant()` tagged a model `base` only if the literal string "base"/"pretrain"
  appeared in the name, so un-suffixed foundations (`meta-llama/Llama-3.1-8B`,
  `mistralai/Mistral-7B-v0.1`) fell to `untagged`, and `Qwen3-8B-Base` was even mislabeled
  `instruct`. True base-ish (anything not instruct/chat/reasoning-suffixed) ≈ 1,841 (~70%); in the
  32–70B bucket alone base-ish ≈ 9.9 TB (~2× the bucket budget). Bases are abundant, not scarce.
- lesson: never infer a population/category from one keyword's *presence*; the discriminator is
  *absence of an instruct/chat suffix* + structured signals (chat_template, base_model tags).
  Sanity-check population claims against inventory before building policy on them.

### DEC-003: Compressed-at-rest archive via ZipNN (lossless) + mandatory round-trip canary
- date: 2026-06-28 / status: accepted / triggered_by: user prompt / docs_updated: catalog_discussions.md, (future) modelark/compress.py + fetch.py
- decision: Store weights compressed-at-rest with **ZipNN** (IBM; IEEE Cloud 2025; FSE/ANS-family;
  ~33% bf16, ~17% fp32, fp8 supported). Pin the version (pre-1.0). `.znn` files live in git-annex;
  **decompress-on-use** (cold archive — most models never read). torch dependency accepted.
  Fetch is **per-shard**: download → verify sha256 vs HF canonical → ZipNN-compress → **mandatory
  round-trip canary** → drop original → `git annex add`. The canary **streams the `.znn` through
  decompress→sha256 and requires it equal HF's canonical sha256** — full-byte verification, O(1)
  memory, zero scratch (never materializes the decompressed file). Skip compressing formats with
  no gain (GGUF/already-quantized) — store raw. Integrity is two-layered: git-annex
  content-addresses the compressed blob; the canary proves it decompresses to the canonical original.
- rationale: at archive scale small % are TBs (~33% bf16 ≈ 5-6TB recovered); ZipNN is mature,
  maintained, and its safetensors plugin already does compressed-at-rest + decompress-on-load. The
  streaming-hash canary gives complete round-trip proof cheaply, so deleting the uncompressed
  original is safe.
- impact: new compress.py (ZipNN wrappers + streaming canary) + fetch.py (per-shard pipeline);
  files/replicas gain compressed_size + znn_sha256; budget planned in RAW model sizes (~27TB),
  compression-recovered space banked for a later fill run.

### DEC-004: Local selection portal — server reads the catalog, the selection IS the wishlist
- date: 2026-06-28 / status: accepted / triggered_by: DEF-001 / docs_updated: modelark/server.py, modelark/core/schema.sql, modelark/cli.py
- decision: `modelark serve` runs a stdlib (zero-dep: http.server + duckdb) localhost web app that
  reads the catalog live and persists picks to a new `selection` table — which IS the wishlist the
  fetch pipeline consumes. Desktop UI: category/variant/size-bucket filters + search + sortable
  table, per-row checkboxes, left-rail tally (per-category count + size + most-recent) and a
  TB-vs-budget bar; "hide quant copies" on by default; selection survives restarts (in the catalog)
  and exports to JSON. Replaces the rejected static single-page generator.
- rationale: a static artifact didn't scale (no live data, no write-back). A live server makes the
  selection durable and pipeline-connected with no new dependencies.
- resolves: DEF-001
- impact (expanded 2026-06-28): portal refactored into a `modelark/web/` package
  (split backend modules + static frontend files — no monolith) and made multi-view
  (Catalog · Disk Health · Library). Selection is a two-stage flow: build a cart (selection
  table) then **Finish** promotes it to status='wishlist' (the committed fetch list).
  Budget is user-configurable (defaults 27TB) for open-source generality. Spawns DEF-006.

### DEF-006: Library viewer (read git-annex — what's archived and where)
- date: 2026-06-28 / status: resolved by DEC-009 / triggered_by: DEC-004 / related: DEF-001 / docs_updated: (future) modelark/web/
- decision: A portal view that reads git-annex (`whereis`) to show what's actually downloaded,
  on which drive, with verification status — the "what do I have" companion to the "what to get"
  catalog view. Stubbed in the nav now; built once the fetch pipeline has put bytes on a drive.
- rationale: nothing is downloaded yet, so there's nothing to view; the nav reserves the slot.
- revisit_when: fetch pipeline + drive registration land and a drive holds archived models.

### DEC-005: Non-HF ingestion surface — Manual Add (deferred build)
- date: 2026-06-28 / status: accepted (build deferred) / triggered_by: user prompt / docs_updated: docs/decision_log.md
- decision: The catalog will support non-HF sources via a Manual Add path: user pastes a direct
  URL, we `wget` the file(s), the user supplies the tags/metadata, and we slot it into the catalog
  as a first-class entry (source='manual'). Build is "for another day" — logged now so the schema
  and portal leave room for non-HF-sourced models (a `source` provenance field, manual entries).
- rationale: not everything worth archiving lives on HF; the architecture shouldn't assume HF-only.
- revisit_when: after the HF fetch pipeline is working end-to-end.

### DEF-007: Defer the "suspect drive" health tier
- date: 2026-07-02 / status: resolved by DEC-007 / triggered_by: user prompt (drive #7 — genuine 5TB ST5000DM000 with 7193 reallocated + 5943 pending) / related: DEF-003 / docs_updated: docs/decision_log.md, (future) modelark/core/schema.sql, drive_register
- decision: Add a third drive-health verdict between keeper and reject — `suspect`: a genuine full-capacity drive with media damage, admitted ONLY after a destructive full-surface write-read-verify (`badblocks -wsv`) forces all pending sectors to resolve AND a SMART re-read then shows Current_Pending=0, Offline_Uncorrectable=0, and Reallocated stabilized (spare pool not exhausted, no NEW pending on a second pass). A `suspect` drive is used under hard constraints: numcopies≥2 with ≥1 copy on an independent healthier tier (the Synology) — "re-downloadable" is NOT sufficient to justify numcopies=1 on a suspect drive — plus periodic `git annex fsck` + SMART re-read (climbing reallocated/pending → auto-evacuate) and a standing replace-ASAP flag. Until built, failing drives are simply rejected.
- rationale: the payload is re-downloadable, so a genuine-but-scarred drive with a fully-mapped, non-progressing defect zone can safely hold a mirrored copy instead of being landfilled — extends usable capacity from a counterfeit-riddled, mostly-dead fleet. Deferring is safe: no suspect drive is trusted until the admission gate + numcopies enforcement exist.
- revisit_when: drive_register / DEF-003 drive-health layer is built (`drives.health` already reserved).

### DEF-008: Defer cross-platform support (Windows/macOS, drive locations, per-OS SMART)
- date: 2026-07-02 / status: partially resolved by DEC-008 (Windows-minus-SMART); macOS + Windows-SMART still deferred / triggered_by: user prompt / related: DEF-003, DEC-004 / docs_updated: docs/decision_log.md
- decision: Keep the tooling Linux-only for now. Defer (a) Windows and macOS support for the CLI/portal; (b) a portable drive-location/mount abstraction (Linux `/dev`+`/media` vs Windows drive letters vs macOS `/Volumes`); (c) per-platform SMART access (Linux `smartctl` + usb-storage UAS quirks vs the Windows/macOS SMART stacks and their own USB-bridge passthrough quirks). Substantial work, off the current path.
- rationale: the sole operator is on Linux (Pop!_OS); building cross-platform now is premature and large. Avoid hard-coding Linux-only assumptions where cheap, but write no cross-platform code yet.
- revisit_when: the project goes open-source and non-Linux contributors/users need it.

### DEF-009: Defer the stabilization/admission run on drive #7 (ST5000DM000, SN W4J1MVW2)
- date: 2026-07-02 / status: active / triggered_by: user prompt / related: DEF-007 / docs_updated: docs/decision_log.md
- decision: Do not run the ~1-day destructive write-read-verify (`badblocks -wsv`, or checkpointed dd→SMART→read) on the failing genuine 5TB drive yet — the run is too long to hold up the interactive fleet-vetting pass, and another drive is still to be tested. The drive stays rejected-pending; if it later clears the DEF-007 admission gate it reclaims ~4.6TB usable.
- rationale: fleet vetting is time-boxed right now; a multi-hour stabilization run blocks it. No data at risk (drive is empty/disposable).
- revisit_when: a ~1-day window is free AND reclaiming ~5TB matters (e.g. capacity math is tight).

### DEC-006: git-annex archive topology — a central "map" repo with drive clones; NAS via NFS
- date: 2026-07-02 / status: accepted / triggered_by: user prompt ("register a drive and go") / related: DEC-003, DEF-003, DEF-007, DEF-008 / docs_updated: modelark/register.py, modelark/cli.py, docs/decision_log.md
- decision: Adopt the canonical git-annex multi-drive pattern. A **central "map" repo** (`~/modelark-library`, separate from the code repo; holds only symlinks + the git-annex location log — bytes never touch git; backable to its own GitHub remote later). **Each registered drive is a clone of the map** and physically holds content; the fetch pipeline adds content **directly on the drive** (no scratch transit — a 1TB shard never needs 1TB of NVMe temp), then `git annex sync` propagates location back to the map, so `whereis`/`numcopies`/`fsck`/`move` work fleet-wide and a shelved drive is self-describing when re-plugged. **numcopies=1** fleet default (re-download from HF is the recovery buffer), bumped to 2 selectively for irreplaceables (per DEC-003) — NOT full drive↔NAS mirroring (user: too restrictive). **The NAS joins as a `directory` special remote over NFS** — chosen over iSCSI (monopolizes the volume to one host, fragile) and SMB (quirky; reserved for future cross-platform, DEF-008); NFS keeps the volume DSM-managed / snapshot-able / RAID5-protected and needs no git-annex on DSM. DuckDB `drives`/`archived`/`replicas` remain the queryable offline mirror.
- rationale: content-on-drive scales to giants without scratch space; self-describing drives survive long offline shelving; the map is a small, git-backable index; NFS+directory special remote satisfies the "no git-annex on DSM" constraint while preserving the NAS as a normal NAS.
- impact: new `modelark/register.py` (`ensure_library`, `smart_baseline` → ok|watch|reject verdict capturing DEF-003 baseline, `register_drive` = qualify+clone+wire+record, `list_drives`, `archive_path`) + CLI `library init` and `drive register|list` + `catalog/library.json` config. Validated on git-annex 8.x: map init → drive clone → add-on-drive → sync → fleet `whereis` + `fsck` all pass. Pending: small fetch.py tweak (resolve `--drive`→archive path + `git annex sync` to map after adds), then the hardware E2E on the Ultrastar (drive-01) once the 5TB wipe frees the bus.
- related: DEC-003 (per-shard fetch it plugs into), DEF-003 (SMART baseline is captured here).

### DEC-007: Drop the suspect-drive admission tier; keep numcopies≥2 for irreplaceables
- date: 2026-07-02 / status: accepted / triggered_by: user (physical fleet vetting — every suspect-class drive bombed out) / resolves: DEF-007 / related: DEC-006, DEF-003 / docs_updated: docs/decision_log.md
- decision: Do not build the DEF-007 "suspect" admission tier (destructive `badblocks -wsv` stabilization → readmit a genuine-but-scarred drive at numcopies≥2 with a healthier-tier mirror). A drive that fails SMART qualification (reallocated≥100 or any pending/offline-uncorrectable) is simply **rejected** — no readmission path — and `register.py` keeps only its ok|watch|reject gate (no `suspect` verdict). The genuinely useful kernel of DEF-007 — **numcopies≥2 for irreplaceable models with ≥1 copy on a healthier tier (the NAS)** — is retained, but it already lives in DEC-006 and is independent of any scarred drive.
- rationale: every suspect-class drive in the physical fleet bombed out — drive #7 was a genuine 5TB ST5000DM000 but actively dying (7193 reallocated, 5943 pending, 111 logged UNC), and the rest were counterfeits — so the admission machinery would gate on drives we do not have (YAGNI). The reliable tier (Ultrastar 8TB + Xbox 4TB + FireCuda 1TB + NAS RAID5 ≈ 17–21 TB) already fits the compressed catalog, so admitting scarred media buys nothing. Rejecting damaged drives outright is simpler and safer; re-fetch from HF remains the numcopies=1 recovery buffer.
- impact: no badblocks/stabilization code; `register.py` verdict stays ok|watch|reject; DEF-007 closed. numcopies policy unchanged (DEC-006: 1 default, 2 for irreplaceables on the NAS). Reversible: if a genuinely-scarred-but-stable drive ever appears and capacity is tight, supersede this with a new DEC that reopens the tier.
- resolves: DEF-007

### DEC-008: Partial Windows support — everything but SMART drive health
- date: 2026-07-02 / status: accepted / triggered_by: user (added a Windows contributor) / related: DEF-008, DEF-003, DEC-006 / docs_updated: modelark/core/platform.py, modelark/register.py, modelark/web/disk_api.py, modelark/web/server.py, modelark/web/static/disk.js, modelark/web/static/app.js, docs/decision_log.md
- decision: Make ModelArk usable on Windows — catalog/discover/verify/query/portal + fetch/archive + drive registration — with ONE explicit exclusion: SMART drive-health. Platform-specific code is isolated behind a new `modelark/core/platform.py` (IS_WINDOWS/IS_LINUX/OS_LABEL, SMART_SUPPORTED, BLOCKDEV_OPS_SUPPORTED, is_root()). On non-Linux: (a) `register` skips SMART (verdict `unchecked` + a note to health-check with the OS's own tool) and skips mkfs/mount — the drive is pre-formatted (e.g. NTFS via Disk Management) and registered by its mount path (`--mount`); (b) the portal Disk Health tab shows "run your platform's preferred health tracking against the drive first before use", and a nav note flags that <OS> drives aren't health-checked in-system; (c) fetch/archive uses `--dest <path>` (archive_path's UUID→device resolution is Linux-only for now). Unix-only calls that would crash on Windows (`os.geteuid`, `os.statvfs`) are replaced with portable equivalents (`platform.is_root()`, `shutil.disk_usage`); missing Linux probes (lsblk/blkid/findmnt) degrade to empty instead of raising.
- rationale: a real Windows contributor needs it, and the platform surface turned out to be tiny and isolated (only register.py + web/disk_api.py), so most of the tool is portable for free. SMART on Windows (`\\.\PhysicalDriveN` addressing, no UAS/sat, no smartctl-UAS quirk story) is high-effort + low-value for him; health is punted to Windows' own tools.
- impact: new platform module; register + portal made platform-aware; **Linux behavior verified unchanged** (disk() still enumerates drives, smart_baseline still runs smartctl). **Windows paths are UNTESTED from the Linux dev box — the contributor must validate end-to-end: git-annex-for-Windows symlink mode, drive-letter paths, and the pre-format→register flow.** Still deferred (DEF-008 stays open for these): macOS support; Windows SMART; archive_path drive-letter resolution so Windows can use `--drive` instead of `--dest`.
- resolves: DEF-008 (Windows-minus-SMART portion only)

### DEC-009: Library view reads the DuckDB mirror (resolves DEF-006)
- date: 2026-07-03 / status: accepted / triggered_by: user ("get the Library view in") / resolves: DEF-006 / related: DEC-004, DEC-006 / docs_updated: modelark/web/library_api.py, modelark/web/static/library.js, modelark/web/server.py, modelark/web/static/index.html, modelark/web/static/app.js, docs/decision_log.md
- decision: The portal Library view reads the DuckDB `archived` + `drives` tables (the offline mirror the fetch pipeline writes) rather than shelling `git annex whereis` live. MVP: a per-drive fleet strip, an archived-models table (raw→on-disk size + savings %, drive(s), copies, verified), and a totals bar. Copy counts are derived from distinct drives per file in `archived`.
- rationale: the DB is already the durable record; reading it makes the view fast and functional even when drives are unplugged (the normal state for a cold archive), and keeps git-annex out of the portal's request path. A live `git annex fsck` re-verify stays an explicit opt-in action.
- impact: new library_api.py + `/api/library` + filled library.js; reuses the disk/catalog CSS. Deferred follow-ups: prominent numcopies redundancy flags, an on-demand fsck re-verify (needs the drive mounted), and a per-drive free-space rescan (drives.free_bytes is a register-time snapshot, so it can read slightly stale).
- resolves: DEF-006

### DEC-010: Expand scope to audio-speech + world models (partially resolves DEF-002)
- date: 2026-07-03 / status: accepted / triggered_by: user prompt / related: DEC-001, DEC-002, DEF-002 / docs_updated: modelark/formats.py, modelark/discover.py, wishlist.yaml, docs/decision_log.md
- decision: Add two modalities to the catalog. **Audio-speech** with four categories — `asr` (speech→text), `tts` (text→speech), `speech-lm` (audio-understanding / speech LLMs), `audio-gen` (music/sound) — classified pipeline-tag-first (HF audio tags are reliable) with architecture/model_type backup via `formats.audio_category`. **World models** (`world-model`; NVIDIA Cosmos, Genie, …) flagged by family name, because they masquerade as video-gen/multimodal by pipeline_tag, so a name override runs before the tag logic. Discovery's scope gate widened from `domain=='text'` to `category in include` over collected domains {text, audio, world}; the cheap pre-filter now excludes only NON-collected domains. image-gen/video-gen/vision/multimodal stay deferred (DEF-002 remains open for those). Org allowlist gains openai/facebook/suno/hexgrad/coqui/fishaudio/parler-tts/kyutai/sesame/SWivid.
- rationale: the operator wants speech (ASR/TTS) as operational tools + music + world models preserved. The schema's modality/category columns already support it, and the classifier already recognized audio (it just returned category=None → excluded). Speech models are small (cheap to archive). World-model classification is deliberately heuristic (name-based) — there is no reliable HF signal — so partial coverage is accepted ("flag if we can").
- impact: formats.classify_category returns audio sub-categories + world-model; discover collects them; a targeted `discover --org <audio orgs>` walk plus `recompute` (reclassifies already-cataloged Cosmos → world-model) populate the catalog. Portal facets pick the new categories up automatically.
- resolves: DEF-002 (audio-speech + world-model portion only; image/video/VLM still deferred)

### DEC-011: Add image-generation to scope (further amends DEF-002)
- date: 2026-07-03 / status: accepted / triggered_by: user prompt / related: DEC-002, DEC-010, DEF-002 / docs_updated: modelark/formats.py, modelark/discover.py, wishlist.yaml, docs/decision_log.md
- decision: Add a single `image-gen` category (text-to-image diffusion + image-to-image + unconditional + text/image-to-3d), classified by pipeline_tag (text-to-image is reliable on HF) with a diffusion-arch backup (flux/stablediffusion/sd3/sdxl/pixart/kandinsky/kolors). `image-gen` joins collected domains {text, audio, world, image-gen}; video + VLM/multimodal stay deferred. Org allowlist gains black-forest-labs (FLUX), stabilityai (SD), playgroundai (Qwen/tencent/baidu already present for Qwen-Image/HunyuanImage/ERNIE). **Scope is intentionally light** — image-gen models are large (FLUX.1-dev ~24GB, SD3.5-Large ~16GB, vs sub-GB speech) and are multi-component diffusers pipelines (DiT/UNet + VAE + text-encoders), so we catalog the notable and archive only a flagship one or two, not a broad sweep.
- rationale: the operator wants a couple of flagship open image generators preserved. Same mechanism as DEC-010; the size + gated-license reality (FLUX-dev/SD3.5 are gated) means selective archival, not bulk. Single category (no asr/tts-style split) since image-gen is one function.
- impact: formats.classify_category returns image-gen; discover collects it; a targeted `discover --org black-forest-labs stabilityai playgroundai` walk catalogs them; one/two flagships added to the cart. Gated repos catalog as status=skip (need hf license acceptance to fetch). The Cosmos world-model name rule still wins for cosmos-named image variants (a known refinement).
- resolves: DEF-002 (image-gen portion; video + VLM/multimodal still deferred)

### DEF-010: Defer first-class gated / license-accept model handling
- date: 2026-07-03 / status: active / triggered_by: user prompt (image-gen flagships FLUX.1-dev, FLUX.1-schnell, SD3.5 are all gated) / related: DEC-011, DEC-005 / docs_updated: docs/decision_log.md
- decision: Defer proper handling of gated (license-acceptance-required) HF repos. Today: the discovery walk catalogs a gated repo as `status='skip'` (GatedRepoError → recorded, dropped from v_ui/selection), and fetch would fail without an accepted license + token. Deferred work when built: (a) surface gated repos as a distinct *selectable* state ("flagship exists, needs a license click") instead of silently skipping; (b) a pre-fetch gate that checks acceptance status via the HF API and prompts the operator to accept (open the model page) before queuing; (c) record per-repo license/token requirements. Until then, gated models are cataloged-as-skip and archived only if the operator has manually accepted the license (`hf auth`) and pins the repo explicitly.
- rationale: many flagship open-weight models are gated (FLUX.1-dev, SD3.5, most Llama/Gemma, much of Mistral) — silently skipping them loses visibility of important models; but proper handling (license-state API, accept-flow, token scoping) is real work off the current path.
- revisit_when: the operator wants to archive a gated flagship and needs the accept-flow, OR gated repos clutter the exclusions enough to matter.

### DEF-CATALOG-001: Revisit the CohereLabs lab (Aya + Command supersession)
- date: 2026-07-03 / status: active / triggered_by: user prompt (reviewing the cart) / related: DEF-010, DEC-001 / docs_updated: docs/decision_log.md
- observation: The cart holds 2024-era CohereLabs picks — aya-expanse-8b/32b, c4ai-command-r-v01 (35B), c4ai-command-r-plus (104B) — plus small 2026 experiments (tiny-aya 3B, North-Mini-Code 30B). Newer flagships exist and SUPERSEDE Command-R+: `c4ai-command-a-03-2025` (111B) and `command-a-reasoning` / `command-a-translate-08-2025` (111B). But every current Command-A flagship is GATED (license-accept, CC-BY-NC, ~0 downloads) → not fetchable under today's pipeline (DEF-010). Aya-expanse is still the multilingual line (no newer full Aya flagship in-catalog beyond the tiny-aya experiments). Aside: `cohere-transcribe-03-2026` (2B) is miscategorized generative-llm (it's ASR).
- deferral: Defer the CohereLabs keep/prune/swap decision. Interim: keep the ungated 2024 aya-expanse (multilingual value per DEC-001) + Command-R/R+; drop the tiny-aya / North-Mini experiments if trimming. Swap Command-R+ → Command-A only once gated handling exists.
- revisit_when: gated-model handling (DEF-010) is built, OR during final selection curation.

### DEF-CATALOG-002: Sweep novel/underrepresented architectures (SSM, linear-attention, diffusion-LM)
- date: 2026-07-03 / status: active / triggered_by: user prompt ("should we sweep for more interesting architectures?") + a config.architectures survey of the 4090-model catalog / related: DEC-001, DEC-002 / docs_updated: docs/decision_log.md
- observation: The org-allowlist walk already caught most architectural novelty *because* the interesting labs are allowlisted — Jamba ×71 (SSM+attn+MoE), LFM2 family ×83 (Liquid), FalconH1 ×38 (hybrid), FalconMamba ×8 + gguf:mamba ×10, RecurrentGemma ×4, BitNet ×2, diffusion-LMs (NemotronLabsDiffusion ×6, DiffusionGemma ×1). But specific non-transformer flagship LINEAGES are entirely absent (0 hits): RWKV (linear-attention RNN — the glaring hole), xLSTM (NX-AI), canonical Mamba/Mamba2 (state-spaces; we only have Falcon's + GGUF), StripedHyena/Hyena (Together), RetNet (Microsoft), Zamba/Zamba2 (Zyphra), Hymba (NVIDIA) — missing because those small research orgs aren't in the allowlist. This is an architecture-coverage gap, not an org-breadth gap: overseas frontier is already deep (Qwen ×424, DeepSeek, InternLM, MiniCPM, Hunyuan, Falcon), and expanding orgs buys expensive generative-LLM sizes, not new architectures. Aside: the `embedding` category is polluted by mis-binned audio codecs (w2v-bert, hubert, encodec, mimi), CLIP/vision encoders, and music (jukebox) that default into it — a classifier-refinement item.
- deferral: Defer building a second, architecture-seeded discovery axis alongside the org-walk — seed by config.architectures patterns + a small lineage-org allowlist (RWKV, BlinkDL, recursal, fla-hub, NX-AI, state-spaces, togethercomputer, Zyphra; Hymba via nvidia). Interim: the org-walk stands; a deduped ~61-model canonical text/code embedding shortlist is loaded in the cart (from 385 raw — sentence-transformers alone was 127 finetune dupes, so "grab all embeddings" is rejected); broad overseas-org expansion is not pursued.
- revisit_when: after the current selection/prune pass and once drives are filling, OR when the operator specifically wants the RWKV/SSM/linear-attention lineages. Fold the embedding-category classifier mis-bin fix in at the same time.

### DEC-012: Add a `compression` category; allow repo-name matching as a classifier fallback
- date: 2026-07-03 / status: accepted / triggered_by: user prompt (ingested AutoCompressor/gist/ICAE/xRAG/LLoCO via discover --repo; they landed in generative-llm/unknown) / related: DEC-002, DEC-010, DEC-011, DEF-011 / docs_updated: modelark/formats.py, wishlist.yaml, docs/decision_log.md
- decision: Add `compression` to the category taxonomy for prompt/context compressors (gisting, AutoCompressor, ICAE, xRAG, LLoCO, 500xCompressor, LLMLingua). Classified EARLY in classify_category (before the adapter/LLM/unknown fallthrough) via (a) a custom-arch set {AutoCompressorModel, GistLlama*, ICAE, XMistral/XMixtral} OR (b) a repo-name regex {auto-compressor|icae|xrag|lloco|gisting|gist-…|prompt/context-compress|llmlingua}. Added to wishlist scope.include_categories. A surgical reclassify (promote-only) moved 13 repos → compression: the 11 ingested + 2 pre-existing `microsoft/llmlingua-2-*` that had been mis-filed as `classifier`. NOT named "summary" — summarization is a seq2seq *task*, distinct from prompt compression.
- rationale: DEC-002 made classification architecture-first (over HF's unreliable pipeline_tag), but these research artifacts have bespoke archs (→ generative-llm/unknown) and no clean tags, so architecture alone under-classifies them. Repo-name matching is accepted here as an explicit, sparingly-used fallback for tag-poor niches (operator OK'd it — "HF is so dirty… it was bound to happen"). Kept as its own category (not folded into generative-llm) so the family is findable/curatable as a group. Rejected: leaving them scattered in generative-llm/unknown; calling it "summary" (conflates with the seq2seq task). This amends (does not supersede) DEC-002.
- impact: formats.classify_category gains a compression branch that runs before all others; the promote-only reclassify touched 13 rows and left the other ~4080 categories unchanged; a `compression` filter chip auto-appears in the portal (facets are data-driven). Name-matching is now a sanctioned classifier tool for tag-poor niches.

### DEF-011: Defer "good" pickle hygiene — opcode-level malware scan, quarantine, safetensors conversion
- date: 2026-07-03 / status: active / triggered_by: user prompt (after ingesting 5 pickle-format compression checkpoints — xRAG ×2, gist-llama, AutoCompressor ×2 — via discover --repo) / related: DEC-003, DEC-012 / docs_updated: docs/decision_log.md
- decision: Defer first-class pickle hygiene. Today is BASIC: classify_file flags pickle files (safety='pickle' via PICKLE_EXTS), verify.py sets format_safety='pickle-present', wishlist `exclude.pickle_only` drops pickle-ONLY repos, and the scorer prefers safetensors — but verify.py:190 hardcodes `pickle_scan='unscanned'`; no opcode-level scan actually runs. Deferred work: (a) integrate a real pickle scanner (picklescan / fickling) that inspects opcodes for REDUCE/GLOBAL/exec payloads → set pickle_scan=clean|flagged; (b) also read HF's own security scan (hf_scan_status) as a second signal; (c) a quarantine / refuse-load policy for flagged blobs; (d) optional safetensors auto-conversion for trusted pickle so the archived copy is safe-by-default.
- rationale: pickle is a load-time code-execution vector; detection + pickle-only exclusion already AVOID the worst repos, but the archive now holds flagged-but-unscanned pickle blobs (research compression models with no safetensors release). A real scanner + conversion is meaningful work (new dependency, opcode analysis, quarantine flow) off the current path; deferring is safe because stored bytes are inert on disk (risk is only at torch.load) and nothing auto-loads them.
- revisit_when: before any workflow that LOADS archived pickle (e.g. Tier-B functional verify on a pickle model), OR when pickle blobs accumulate enough to warrant batch scanning/conversion.

### DEC-013: Replication policy — NAS is the sole safe tier; numcopies≥2 for high-risk + overseas-frontier weights
- date: 2026-07-03 / status: superseded by DEC-014 / triggered_by: user prompt (choosing tier + replication policy during infra planning) / related: DEC-006, DEC-007, DEF-010, DEF-CATALOG-002 / docs_updated: docs/decision_log.md
- decision: The NAS (RAID-backed) is the ONLY `safe` tier; local single disks (Ultrastar, FireCuda, Xbox) are `standard`. A model earns numcopies≥2 — a required 2nd copy on the safe tier (NAS) — when EITHER (a) it is hard to re-acquire: gated (DEF-010), ultra-niche research (e.g. compression / novel-arch), or near-zero-download "could vanish"; OR (b) it is from a non-local (overseas) frontier lab, on the thesis that those labs both lead AND are at elevated risk of going private / access-restricted (geopolitical). Everything else numcopies=1 (freely re-fetchable). A manual "pin irreplaceable" override always forces ≥2.
- rationale: operationalizes DEC-007's deferred definition of "irreplaceable." The redundancy budget should protect what's hardest to re-acquire, and the overseas frontier open weights are simultaneously the most valuable (leading) and the most likely to disappear — a geopolitical-risk-weighted replication. NAS is the only redundant target, so it is the safe tier by default.
- caveat: "overseas frontier" is a LARGE share of the cart (Qwen/DeepSeek/etc.), so a blanket regional flag could require the NAS to hold a 2nd copy of most of the archive. The flag must be bounded to fit NAS free space (e.g. flagship/base + high-value variants, or a size ceiling), reconciled by the librarian (C). Rejected: flat numcopies=1 (under-protects the at-risk leaders); ≥2 for everything (wastes the safe tier on re-fetchable US weights).
- impact: needs (B) an org→region attribute (wishlist.yaml already groups orgs by region) + a per-model numcopies derivation {high-risk ∪ overseas-frontier ∪ manual-pin} + git-annex numcopies/required wiring; and (C) a capacity-aware librarian that places the required safe-tier copy on the NAS and bounds how much regional preference it can honor.

### DEF-012: Defer the safe-tier / numcopies replication build
- date: 2026-07-03 / status: resolved by DEC-014 / triggered_by: user prompt (skip safe/numcopies for now; build the librarian's spreading first) / related: DEC-013, DEC-007, DEC-006 / docs_updated: docs/decision_log.md
- decision: Defer BUILDING the replication policy (per-model numcopies, org→region flag, safe-tier semantics, git-annex numcopies/required). DEC-013 stands as the target policy for when it's built. Interim: the drive `tier` attribute is NOT added yet; the NAS is registered as a plain placement target (no safe-tier role); the librarian (C) spreads a SINGLE copy of each model across all targets by capacity (keep-whole + anti-fragmentation), no redundancy.
- rationale: the operator wants a working end-to-end placement + download loop before layering redundancy; single-copy spreading is the 80% and unblocks filling drives now. Deferring is safe — adding numcopies later only ADDS a required 2nd copy for flagged keys; git-annex enforces numcopies at copy-time, so existing single-copy placements stay valid and become retroactively satisfiable.
- revisit_when: after the librarian + fetch/verify loop are proven on real fills, OR when NAS free capacity is known and the operator wants the overseas-frontier redundancy from DEC-013.

### DEF-013: Defer model sharding across drives (keep-whole leftover packing)
- date: 2026-07-03 / status: active / triggered_by: user prompt (librarian leaves leftover when a model can't fit whole) / related: DEC-006, DEF-012, task #11 / docs_updated: docs/decision_log.md
- decision: Defer splitting one model's files across multiple drives. The librarian (v1) keeps each model whole (best-fit-decreasing), leaving two leftover cases: (a) a model larger than any single drive's free space → unplaceable; (b) packing leftovers — small per-drive gaps that sum to enough space but no single drive fits the next model. Sharding a model's shard files across drives (git-annex tracks multi-location natively) would reclaim both. Interim: keep-whole; the plan flags unplaceable/overflow so the operator adds capacity or splits manually.
- rationale: keep-whole is simpler and keeps a model self-contained on one drive — easier restore, verify, and self-describing manifests (task #14) — and the leftover waste is small relative to fleet size. Sharding adds real complexity (per-shard placement, reassembly on restore, manifests spanning drives) for marginal capacity; not worth it until drives are genuinely full and leftovers block a wanted model.
- revisit_when: the fleet is near-full and packing leftovers (or an unplaceable big model) actually block archiving something wanted.

### HYP-001: Does a general compressor (zstd/xz) meaningfully shrink FP8 / already-quantized weights?
- date: 2026-07-03 / status: open / triggered_by: user prompt (ZipNN gets ~0% on FP8; try zstd for a few %) / related: DEC-003 / docs_updated: docs/decision_log.md
- question: ZipNN exploits bf16/fp16 exponent-byte redundancy and gains ~0% on FP8 (e4m3/e5m2) and int-quantized (gptq/awq), so fetch stores those raw. Can a general-purpose compressor (zstd -19, maybe xz) reclaim a worthwhile fraction (target ≥ ~5%, enough to beat the decompress-on-use tax + added complexity)?
- observation: trained weights are near-high-entropy, but quantized tensors can carry structure (value clustering, zero runs, skewed histograms) a byte-level compressor might catch where ZipNN's float model cannot.
- interventions / test matrix: on the first real FP8 and gptq/awq models fetched, measure raw vs zstd-19 vs xz-6 vs ZipNN — ratio + (de)compress throughput on representative shards. Decide per-format.
- results: (pending — measure during early fills)

### INC-002: SMART qualification passed a failing drive (synthetic SMART over a USB bridge)
- date: 2026-07-03 / status: open / triggered_by: registering the Xbox 4TB (ST4000LM024) — SMART verdict 'ok', mkfs then hit Medium Errors
- symptom: `drive register --dev /dev/sdc` (ST4000LM024 "Game Drive Xbox", USB) passed SMART qualification (verdict=ok, realloc=0, passed=True), then `mkfs.ext4` failed — kernel `Medium Error` / `Peripheral device write fault` on writes near end-of-disk (sector ~7.78 B) + `Unrecovered read error` at the last sector. A drive with bad media.
- root_cause (register.py:smart_baseline): the USB bridge did not pass real SMART — power_on_hours=0 (implausible for a used drive) and reallocated=0 were synthetic/default values, so the ok|watch|reject grade was computed on garbage. SMART "PASSED" is a weak signal regardless. Identity also flip-flopped (ST4000LM024/WFF1R7QK ↔ Game Drive Xbox/00000000…), confirming unreliable passthrough. No write-surface test existed to catch bad media.
- blast_radius: none to data — mkfs failed before the catalog upsert, so the drive was never registered (nothing to unregister). Caught before any archive bytes were written (qualification's whole purpose). Drive rejected/set aside; the register's swallowed-stderr bug was fixed in the same session (register.py:_run now surfaces the tool's stderr).
- why_not_caught_earlier: qualification is SMART-only and trusts the bridge's SMART. Remediation (task #19): (a) flag synthetic SMART — poh=0 or all-zero attrs → verdict 'unreliable', require an explicit override or a write test; (b) optional write-surface check at registration (badblocks -w sample or a strided write→read→verify pass) so bad media fails registration, not the first fill.
- 2026-07-11 UPDATE — partial remediation of (a): `drive register --skip-smart` (register._unchecked_baseline) is an EXPLICIT operator override for a USB bridge that won't pass SMART — registers with verdict 'unchecked' + a loud warning, mirroring the DEC-008 non-Linux path; verify health externally. During the PIV-001 startover the HGST in a Game-Drive USB enclosure actually returned REAL SMART (health=ok) once spun up — the earlier failure was "device becoming ready" (the drive was ASLEEP), not a synthetic/absent-SMART bridge, so --skip-smart wasn't needed here. The write-surface check (b) stays deferred (DEF-003 / #19).

### DEC-014: Storage roles + 1-or-2 replication; librarian consolidates (VSBPP) and advises coverage
- date: 2026-07-03 / status: accepted / triggered_by: user refinement of the replication model / supersedes: DEC-013 / resolves: DEF-012 / related: DEC-006, DEC-007, tasks #11 #16 / docs_updated: modelark/core/schema.sql, modelark/core/db.py, modelark/librarian.py, modelark/cli.py, docs/decision_log.md
- decision:
  - **Drive roles** (`drives.role`): `primary` (librarian bin-packs the working set here) or `replica` (holds copies of *tagged* content only; reserved from the primary math). Local disks = primary; NAS + 200 GB (+ any promoted drive) = replica.
  - **Copies are 1 or 2** (`models.numcopies`), on the "can I re-acquire it?" test: re-fetchable bulk -> 1 (a primary disk — re-download if lost); a lone copy on the RAID NAS also counts as safe; important/irreplaceable (operator-picked: first-party, gated, at-risk-of-going-private) -> 2 — a 2nd, independent copy on the replica tier, because RAID is not a backup. No 3rd copy.
  - **Librarian objective = Variable-Sized Bin Packing (VSBPP) consolidate:** pack the working set onto the FEWEST primary drives (largest-drive-first, keep-model-whole, tranched headroom), freeing the rest for the replica tier — replacing the old best-fit that spread across every drive.
  - **Coverage advisories:** warn "not enough at all" (bulk > primary capacity), "not enough for must-haves" (2nd-copy set > replica surface), "must-have too big for any replica drive"; recommend which freed drives to promote to `replica`.
- rationale: supersedes DEC-013's auto-regional-flag with an explicit operator-picked list (more deliberate). 1-or-2 (not 2-or-3) because a disk copy isn't a mandatory floor. Roles model the replica-only drive the tooling lacked. git-annex enforces numcopies + preferred-content natively — we expose it.
- impact: schema (drives.role, models.numcopies via db._migrate), librarian rewrite (consolidate + reserve replica + replica-copy plan + advisories), cli (register --role, plan output, `protect`). Deferred to tasks: NAS special-remote add-as-target (drive-99), git-annex preferred-content + replica-copy execution, and a "Replication" portal tab.

### DEC-015: Must-haves live only on the replica tier (refines DEC-014's placement)
- date: 2026-07-04 / status: accepted / triggered_by: operator refinement ("must-haves → NAS + replication drive; the other drives are all primary bulk") / related: DEC-014 / docs_updated: modelark/librarian.py, docs/decision_log.md
- decision: Partition the two tiers by importance instead of keeping a primary copy of everything. A must-have (numcopies≥2) lives ONLY on the replica tier — `numcopies` copies on DISTINCT replica drives (NAS + a WD Blue) — NOT also on a bulk drive. Bulk (numcopies=1) lives only on primary. So: primary = re-fetchable bulk; replica = the protected set; no third surface. Roles + must-have tags are operator-set (`protect`, `drive register --role` / a role UPDATE); the tool does not auto-decide.
- rationale: DEC-014 kept a primary copy of everything AND a replica copy of must-haves (an over-provisioned 3rd surface; the whole 5.2 TB NAS was reserved as replica while bulk crammed the USB drives). The operator's model is cleaner and honors 1-or-2: important → the safe tier (NAS+WD), bulk → the cheap re-fetchable disks.
- impact: librarian.plan_placements now splits bulk→primary (`_consolidate`) and must-haves→replica (`_place_replica`: nc copies on distinct replica drives); advisories updated. Live layout: replica = drive-99 (NAS) + drive-04 (WD); primary = HGST + Seagates + drive-05. 125 must-haves (~0.66 TB) → NAS + WD ×2; 319 bulk (~10.9 TB) → HGST + 4 TB Seagate; drive-03/05/06 + ~5 TB NAS free.

### DEC-016: NAS = iSCSI block clone (raid-primary), superseding DEC-006's NFS special remote for the NAS
- date: 2026-07-04 / status: accepted / supersedes: DEC-006 (NAS-topology only) / triggered_by: 9-agent panel verified an NFS `directory` special remote is silently non-executable as a fetch primary (archive_path needs fs_uuid → NULL → fetch.run no-ops) / docs_updated: modelark/register.py (iscsi auto-detect → raid_backed)
- decision: Attach the NAS over iSCSI as a block LUN, format ext4, register it as a normal git-annex CLONE via register_drive (auto-detected iscsi → raid_backed → health='raid', but fs_uuid IS captured, so archive_path resolves and fetch.run writes copy#1 directly). role=PRIMARY (the safe home for must-have copy#1), NOT a replica target. Retire the old NFS `directory` special remote (drive-99). Single-host trade-off accepted (only the workstation mounts the LUN; no clustering).
- rationale: only a real fs_uuid-bearing clone can be a fetch primary; the RAID's redundancy + 5 TB are wasted as a mere 2nd-copy target; iSCSI gives the block-level POSIX semantics git-annex needs (NFS working clones are fragile — the reason DEC-006 chose a special remote; iSCSI sidesteps it).

### DEC-017: Tiered layout — RAID-primary for must-haves, smallest-independent replication, two-librarian flow (supersedes DEC-014/DEC-015 role semantics)
- date: 2026-07-04 / status: accepted / supersedes: DEC-014, DEC-015 (role semantics) / triggered_by: operator correction (NAS = primary, not replica) + panel + 12-step review
- decision:
  - Sort drives DESCENDING by size, with the RAID PULLED OUT of the sort (an 8 TB local can exceed the 5 TB RAID — the redundant tier is a distinguished domain, not the biggest disk).
  - Must-have COPY#1 → the RAID (if a redundant domain exists), else the largest single disk (public product: no RAID). Bulk → primaries via the existing _consolidate VSBPP.
  - Must-have COPY#2 → the SMALLEST sufficient INDEPENDENT drive (ascending); feasibility keys on an independent drive distinct from the RAID and from copy#1 with usable ≥ the aggregate must-have set M — NOT total free space (kills the huge-NAS false-comfort trap). Register only as many replication drives as needed.
  - 2nd copy keeps each MODEL whole (no sharding — DEF-013 deferred), but the SET may span replication drives via whole-model FFD when one won't fit: prefer smallest single sufficient → a larger drive → span 2+ drives.
  - Redundancy is FLEET-DERIVED (a drive `raid_backed`/redundant attribute), not hardcoded "RAID=safe"; with no redundant domain, force numcopies≥2 across distinct disks + flag loudly.
  - FLOW: DISCOVERY (read-only `drive scan`) → LAYOUT librarian (pure `drive plan` — roles/numbers/feasibility) → REGISTRATION (`drive register --from-plan`, the only committing stage) → PLACEMENT (existing plan_placements) → FILL. Two distinct librarians; layout reuses _consolidate/headroom/est_stored as a pure feasibility oracle. Role/label move OUT of manual `drive register --role` into the layout output.
- impact: replace _place_replica (largest-first) with smallest-sufficient-independent + whole-model FFD; deprecate register_nas for the NAS (use the iSCSI block clone); add the layout librarian + `drive scan`/`drive plan`/`drive register --from-plan`; per-format ZipNN sizing (fp8/gptq/awq→1.0 per HYP-001, float→0.67, + margin); deterministic tie-break (usable, health, SMR, poh, label) + ORDER BY in drives().

### DEC-018: Drive identity & naming — stable drive-NN key + annex_uuid anchor + role-as-display (reject role-in-key)
- date: 2026-07-04 / status: accepted / triggered_by: operator lean toward role-prefixed IDs + panel's rename-churn finding
- decision: The stable KEY is `drive-NN`, minted once and NEVER renamed (drive-00 reserved for the single network RAID — its role is near-immutable). `annex_uuid` (already captured) is the deep identity/reconciliation anchor (what git-annex location logs + fetch._remote_name_for_uuid already key on). `role` stays a MUTABLE column (+ a persisted `raid_backed`). The readable role/type/size-rank (NRAID · R · P) renders in the DISPLAY layer only — a portal badge + `git annex describe` — never in the key, git-remote name, or fs-label. Re-tier = one `UPDATE drives SET role WHERE annex_uuid=?` (metadata; no re-plug, no rename cascade). v1 = one network drive.
- rationale: role/size-rank are the most mutable attributes; drive_label is the most load-bearing key (PK of drives + composite PK of replicas/archived + git remote name + fs-label). Encoding a mutable attribute in an immutable key makes every re-tier a multi-system migration; display-layer role delivers the operator's readability without the churn.

### DEC-019: Safety invariant — no must-have silently ends below numcopies
- date: 2026-07-04 / status: accepted / triggered_by: panel found two silent under-replication paths
- decision: enforce via a resume-granularity fix + three gates:
  - RESUME/completion tracked per-(repo, drive) off the `archived` rows, NOT models.status — a must-have is done only when count(DISTINCT drive_label) ≥ numcopies. (Fixes: fetch_model flips status='archived' after copy#1, so an interrupt between copies made plan_placements drop the model and never schedule copy#2.)
  - GATE-A (pre-apply executability): every direct-fetch target's archive_path must resolve, else BLOCK (replaces fetch.run's silent no-op on a special-remote/unmounted target).
  - GATE-B (--apply must-have gate): extend the apply refusal (today bulk-capacity only, cli.py:207) to the must-have tier — refuse if any numcopies≥2 model would finish on < numcopies distinct drives.
  - GATE-C (post-apply post-condition): re-derive count(DISTINCT drive_label) per repo; assert every must-have reached numcopies, else exit nonzero + red advisory.
- impact: fetch.py (resume off archived rows; status → UI hint only), librarian.py:136-137 (per-placement pool subtraction), cli.py (GATE-B + GATE-C).

### DEC-020: Bulk fills the RAID first — the NAS is the best primary, not a must-have-only vault (refines DEC-017)
- date: 2026-07-07 / status: accepted / triggered_by: operator ("plenty of NAS space, higher tier — why aren't we filling it?") / related: DEC-017 / docs_updated: modelark/librarian.py
- decision: Must-haves keep FIRST claim on the RAID (copy#1 reserved before bulk), but bulk then fills the RAID's REMAINING space BEFORE the externals — the RAID is the best primary drive (redundant, fast iSCSI, abundant), not a vault reserved for the small must-have set. `_consolidate` orders `raid_backed` drives first, then largest. On this fleet: the NAS fills (must-haves + bulk), the HGST takes the overflow (~75%), the flaky SMR Seagate is FREED. A future must-have preempts bulk on the RAID on the next plan (must-haves are placed first each run). An info advisory fires when bulk lands on the RAID (re-fetchable data on the redundant tier).
- rationale: DEC-017 walled the RAID off from bulk, leaving ~4.8 TB of premium storage empty while cramming SMR USB drives to ~98%. Using the RAID's excess for bulk is far better storage utilization; must-haves' first-claim + re-plan preemption keeps the safe tier available for them as the set grows.
- trade-off: fills the NAS toward 100%-of-usable, so a new must-have forces bulk eviction (a re-plan move, git-annex handles it). A must-have growth buffer on the RAID is a possible future refinement.

### INC-003: Whole-shard ZipNN compress OOM-killed the portal on the first ≥10 GB shard
- date: 2026-07-08 / status: remediated via DEC-021 / triggered_by: live fill died mid-run; filesystem forensics (nothing on :8077, no modelark process, `sdc` /proc/diskstats frozen, swap 4.0/4.0 GiB full)
- root cause: `compress.compress_file` did `ZipNN().compress(src.read_bytes())` — the whole shard in RAM plus ZipNN's internal array + output buffer ≈ ~26 GB peak for a 9.8 GB shard (BAAI/bge-reranker-v2.5-gemma2-lightweight, shard 1/4). With ~21 GB already used by other processes and swap exhausted, the kernel OOM-killed `modelark serve` during compress. Earlier shards (≤2.5 GB) fit, so it surfaced only on the first large one; the old docstring's "memory bounded by one shard" assumed shards were small.
- impact: portal down ~25 min; NO data loss (shard was downloaded, not yet recorded in `archived`, original not dropped → resumable per DEC-019). Also exposed: the Fill-tab status poll silently retried on connection failure, freezing the UI on the last frame instead of showing "portal lost" — a small UI fix still pending.

### DEC-021: Stream compression at O(chunk) memory via StreamZNN (refines DEC-003 implementation)
- date: 2026-07-08 / status: accepted / triggered_by: INC-003 / docs_updated: modelark/streamznn.py (new), modelark/compress.py, tests/test_streamznn.py
- decision: Replace the whole-file ZipNN compress/canary/decompress with a standalone `modelark/streamznn.py` (StreamZNN, MIT) that slices a file into fixed chunks (default 64 MiB), compresses each as an independent self-describing ZipNN blob, and frames them as `MAGIC + [uint32 len][blob]…`. Peak memory is O(chunk) regardless of shard size (measured 783 MB peak for compress+canary on a real 1 GB slice, vs ~26 GB before). The canary (`verify_sha256`) and restore (`decompress_file`) share ONE streaming decompress path (incremental sha256, no scratch file); outputs are written atomically (temp + `os.replace`). `compress.py` keeps the ModelArk glue + a magic-routed fallback that still reads pre-StreamZNN whole-blob `.znn`. Chunk size is tunable and ratio-insensitive (256 KB vs 64 MB differ 0.01%), so peak can be lowered freely.
- rationale: verified two ZipNN properties that make this safe — (1) blobs are FULLY SELF-DESCRIBING (decompress reconstructs dtype/byte-reorder/length from the blob header, so a wrong dtype hint cannot corrupt a restore: compress bf16 → decompress "as" fp32 is byte-identical); (2) the canary is a complete restorability proof because it shares the restore's decompress path. Byte-identity proven by 9 tests + a real 1 GB round-trip.
- supersedes: the whole-file compress implementation inside DEC-003 (DEC-003's canary-before-drop invariant is unchanged and preserved).

### DEF-014: Extract StreamZNN to its own MIT repository + local folder
- date: 2026-07-08 / status: active / triggered_by: DEC-021 (module written standalone — zipnn + stdlib only, no modelark imports) / docs_updated: modelark/streamznn.py
- decision: `modelark/streamznn.py` is embedded for now but written self-contained + MIT (declared in its header and referenced from compress.py). Defer splitting it into its own git repository + a dedicated local folder, and any public release as "StreamZNN".
- rationale: proving it in-tree against the real fill first is lower-risk; the lift-out is free later because it has no project-specific dependencies.
- revisit_when: after the external code review lands + the module has a second real consumer, or before any public release (verify the "StreamZNN" name / prior art first — ZipNN's own `is_streaming` chunks only in-memory, not file-level O(chunk)).

### DIS-001: Crash-resume is clean, and ZipNN.compress mutates its input buffer
- date: 2026-07-08 / status: observed / triggered_by: INC-003 investigation + StreamZNN test authoring / related: DEC-021, DEC-019
- crash-resume: a mid-fill portal death leaves the in-flight shard downloaded but NOT in `archived`, original not dropped. On restart the pipeline re-plans, `fetch_model`'s per-(repo,file) `have` set skips already-recorded files, and hf_hub_download reuses the on-disk cache — so resume re-verifies/re-compresses only the interrupted shard with NO HF re-download and no corruption (per-file transactional). Forensic signature for future readers: nothing on :8077, no `modelark` process, `sdc` diskstats frozen, swap 100% full, the interrupted repo dir holding the raw shard but no matching `.znn`.
- ZipNN footgun: `ZipNN.compress()` REORDERS ITS INPUT BUFFER IN PLACE (native core writes through even an immutable `bytes`; the input's sha256 changes after the call). StreamZNN is safe only because it feeds ZipNN single-use chunks read fresh from the file; `test_source_file_untouched` locks that the source shard is byte-identical after compression. Never hand `ZipNN.compress` a buffer you still need — pass a copy.

### DEC-022: Config-gated compression codec — whole-file / StreamZNN / zstd / raw by RAM budget (refines DEC-021)
- date: 2026-07-08 / status: accepted / triggered_by: operator ("gate it — don't flex streaming unless controlled; log it well") / related: DEC-003, DEC-021, INC-003 / docs_updated: wishlist.yaml, modelark/compress.py, modelark/wishlist.py, modelark/fetch.py, tests/test_streamznn.py
- decision: Choose the codec PER SHARD from `wishlist.yaml` `compression:` (max_compress_ram_gb=4.0, stream_compress=true, threads=4), not always-stream. `compress.plan_codec(shard_bytes, cfg)`: whole-file in-memory ZipNN when ~4× shard ≤ budget (fastest, best ratio); over budget + stream_compress → StreamZNN (DEC-021, O(chunk)); over budget + stream off + `zstandard` importable → streaming zstd; else store raw (still sha-verified/annexed). Restore/canary route by the stored file's MAGIC (SZNN / zstd frame / "ZN" whole-or-legacy), so every codec + pre-StreamZNN `.znn` restores. Compression runs at ZipNN threads=4 (shared-memory internal threads — measured no peak-RSS penalty, no pickling). Every shard logs its codec + orig→stored + ratio + canary verdict.
- rationale: whole-shard-in-RAM compress OOM-killed the portal (INC-003); the ~4× peak factor is measured (~3.67× + fixed baseline). The gate keeps the fast in-RAM path for shards that fit and streams only the big ones, with an explicit toggle + a boring zstd/raw fallback so the newer StreamZNN path isn't "flexed" unless the operator opts in — with loud per-shard logging.
- impact: fetch_model picks the codec + logs it; compress.py owns the codecs + magic routing; wishlist.compression() supplies config. Applies on the next portal restart; an in-flight fill is unaffected (imports at startup).

### DIS-002: Every production StreamZNN archive independently restores byte-identical to HF canonical
- date: 2026-07-08 / status: observed / triggered_by: operator paranoia check after the INC-003 fix went live / related: DEC-003, DEC-019, DEC-021, DEC-022, DIS-001
- observation: With the fill live, all 11 `.znn` files written by the streaming path (SZNN magic) — including all four ~8 GB shards of the exact model that OOM-killed the portal (BAAI/bge-reranker-v2.5-gemma2-lightweight) — were re-decompressed in a FRESH process and their sha256 compared to HF's canonical hash (git-committed catalog/export/files.jsonl). 11/11 PASS, 0 FAIL. Independent of the write-time canary (DEC-003/DEC-019): different process, fresh decompress, checked against HF's immutable truth rather than the just-written buffer. Confirms StreamZNN (DEC-021) under the codec gate (DEC-022) produces restorable archives in production, not only in unit tests.
- note: decompress+verify of an 8 GB shard took ~3.5 min over contended iSCSI (~40 MB/s). A full-fleet re-decompression is therefore expensive — the Library Audit (#23) should sample + treat the write-canary's recorded sha256 as the standing guarantee, with re-decompression as a spot-check.

### INC-004: A stalled HF download wedged the fill for ~7 hours
- date: 2026-07-08 / status: remediated in code (pending live confirmation) / triggered_by: operator ("stuck in the AM, downloading LiquidAI") / related: DEC-021, DIS-001
- symptom: portal alive (~12% CPU) but zero progress for ~7 h. `LiquidAI/LFM2.5-1.2B-Instruct/model.safetensors` frozen as a 2.21 GB `.incomplete` since 04:31, network RX ≈ 0, disk idle. The single fill worker was blocked inside `hf_hub_download` on a dead/half-open socket; the UI Stop can't interrupt a mid-file hang, so only a portal restart freed it (resume then continued from the partial).
- root cause: hf's built-in 10 s download read timeout did NOT fire (it hung/retried internally and never returned control), so one stalled connection blocked the single-threaded fill indefinitely — and there was no per-repo isolation to skip past a bad download.
- fix (modelark/fetch.py): (1) a HARD socket-level timeout (`socket.setdefaulttimeout`, 120 s) scoped around each `hf_hub_download`, so a silent socket raises instead of hanging; (2) bounded per-shard retries (4×, growing backoff) that resume the on-disk `.incomplete` — no re-download of what already landed (the "bump + restart the shard"); (2b) a circuit-breaker — >1 stall in a 20-min window escalates the backoff to a 2-min cooldown so a flaky network gets time to recover instead of being hammered; (3) a per-repo `except Exception` in `run()` that logs + moves on so one repo can't wedge the whole fill; a mid-retry stop is honored via `_StopRequested`. Retry / gated / 429 / stop / stall-clustering paths unit-tested.
- residual: a hard socket timeout is not guaranteed against every half-open/trickle stall. If one recurs, the guaranteed fix is running each download in a killable subprocess with a no-progress (`.incomplete` growth) watchdog — deferred until shown necessary.
- 2026-07-09 UPDATE — residual materialized + fixed: the hang recurred exactly as warned. A `tiiuae/Falcon-H1-34B-Instruct` download stalled at 4.29 GB and the fill sat blocked in `poll()` for ~7 h; the socket timeout never fired (likely hf_xet native I/O, which Python's socket default can't reach), and systemd saw a live process so it couldn't help — a HANG, not a crash (→ OUT-002). Fixed (DEC-023 stage 2, #27): downloads now run in a killable child (`download_worker.py`), and `fetch._run_monitored` KILLS any download/compress child whose on-disk output (`.incomplete` / `.znn` temp) stops growing for a window (`_DL_STALL_SECS`=180 / `_COMPRESS_STALL_SECS`=300), then retries (download, hf resumes the partial) or stores raw (compress). Terminal hf errors are reconstructed from the child's result via `_HttpResp` so run()'s gated/429/not-found classification is unchanged. Bonus: Stop now interrupts mid-download/compress, not just at boundaries. Validated: `_run_monitored` kills a hung child in tests; the isolated download fetches a real file E2E.

### INC-005: ZipNN threaded compress double-freed → portal core-dumped mid-fill
- date: 2026-07-08 / status: RESOLVED — compress isolated to a child process + raw fallback (validated on the real shard) / triggered_by: operator — `double free or corruption (!prev)` / `Aborted (core dumped)` right after DeepSeek-R1-Distill-Qwen-32B shard 3/8 downloaded / related: DEC-022, INC-004, INC-006, DEC-023
- symptom: native glibc `double free or corruption (!prev)` → SIGABRT/core dump, firing as an 8.78 GB shard finished downloading and handed off to compress. INTERMITTENT — ~30 shards (ERNIE ×9, DeepSeek-Coder ×4, R1 ×2, plus earlier models) compressed fine first. The fill worker runs in-process, so the abort took the whole portal down; the fill sat dead ~5.5 h until noticed (the frozen UI hid it — see the DIS-less UI fix + INC-004).
- root cause (leading): ZipNN's internal multi-threaded compressor (`threads=4`, added in DEC-022). A race in its native threaded `free()` double-frees occasionally; the intermittency + native-abort signature fit a threading heap bug, not our Python. (Editing files on disk did NOT cause it — a running process holds its already-loaded code; the threaded feature in the running portal did.)
- mitigation: default compression `threads: 1` (single-threaded — no race). Slower compress, but the pipeline is download-bound anyway and stability wins.
- residual: if the double-free recurs at threads=1 it's a different ZipNN bug → isolate compress (and download) in a killable subprocess so a native crash kills the subprocess, not the portal. Broader: supervise the portal (auto-restart + resume-on-boot + heartbeat) so ANY crash self-heals unattended (planned).
- 2026-07-08 UPDATE — root cause corrected + resolved: threads=1 did NOT hold. The double-free recurred on the SAME shard under systemd (journald: `double free or corruption` / `status=6/ABRT`), and `--resume` re-attempting it produced an infinite ZERO-progress crash-loop. Every attempt died at the identical output offset — `5,859,139,224` bytes — so it's DETERMINISTIC and data-dependent, NOT a threading race; the leading hypothesis above was wrong. Supervision itself behaved exactly as designed (systemd restarted in ~11 s; the crash line was captured in journald — DEC-023 stage 1 validated live). FIX (DEC-023 stage 3): compression now runs in a short-lived CHILD process (`modelark/compress_worker.py` + `fetch._compress_isolated`); on a signal-death the parent stores that shard RAW and the fill continues — a crashing shard can no longer core-dump the portal or loop. VALIDATED against the real shard 3: the child SIGABRT'd (signal 6, `double free or corruption`), `_compress_isolated` returned `status=crash`, the PARENT SURVIVED (exit 0), and the crash-path cleanup swept ~41 GB of orphaned `.sznn.tmp` the loop had left. threads stays 1 (no benefit shown; keeps the variable out). ZipNN's underlying bug is upstream/unfixed — isolation routes around it. Cost: a raw-fallback shard is uncompressed (~+33% for that one shard) — rare + acceptable.

### OUT-001: Fill portal down ~5.5 h (no data loss)
- date: 2026-07-08 / status: closed at recovery (manual restart) / triggered_by: INC-005 core dump / related: INC-006, DEC-023
- impact: `modelark serve` (which hosts the single fill worker) core-dumped ~16:27 and stayed down until noticed ~22:00 — ~5.5 h of no fill progress. NO data loss, NO corruption: the pipeline is per-shard transactional + resumable, so a restart continues at the next shard (DeepSeek-R1-Distill-Qwen-32B shard 3). Only cost = lost fill hours. "No real impact so far" — solo/dev fill, nothing served off it.
- recovery: manual `modelark serve` + Start; to be automated (DEC-023).

### INC-006: Unsupervised in-process fill turns any crash into prolonged downtime
- date: 2026-07-08 / status: remediation adopted (DEC-023) / triggered_by: OUT-001 / related: INC-003, INC-004, INC-005
- root cause: the fill worker runs INSIDE `modelark serve` and nothing restarts the process, so ANY death — OOM (INC-003), a rough Ctrl-C, a native double-free (INC-005) — takes the whole fill down AND leaves it down until a human notices (the frozen UI hid it). Three deaths this session; each survivable alone, but with no supervision each became hours of lost progress.
- remediation: DEC-023.

### DEC-023: Hard persistence — supervise the fill under systemd + persistent logging
- date: 2026-07-08 / status: accepted / triggered_by: INC-006, OUT-001 / related: INC-003, INC-004, INC-005, DEC-022
- decision: Run `modelark serve` as a `systemd --user` service — `Restart=always`, `RestartSec`, `StartLimit*` backoff, ordered after the iSCSI mount (also closes the boot-persistence gap). Add opt-in **resume-on-boot** (`fill.auto_resume`): on startup the portal auto-starts the fill worker if finalized work remains, so a restart self-resumes at the next shard. Add a **heartbeat + no-progress watchdog** for HANGS (systemd catches dead processes, not stuck ones). **Log to a persistent rotating file** (not just the terminal) so a crash's output survives — the INC-005 double-free was only visible ephemerally in the operator's terminal.
- rationale: the pipeline is fully resumable, so the one missing piece for unattended running is "bring it back + keep going + record why it fell over." systemd over a container: the workload is host-bound (iSCSI LUN, drive fleet, DuckDB, git-annex) — a container re-plumbs all of that for no gain.
- impact: new systemd unit (operator-installed), `serve()` resume-on-boot, a worker heartbeat, file logging. Staged: (1) unit + resume-on-boot + file log; (2) heartbeat/watchdog; (3) subprocess-isolate compress/download if double-frees recur at threads=1 (INC-005 residual).
- 2026-07-09 UPDATE — logging (stage 1) + watchdog (stage 2) built & tested:
  • Logging (#26): the earlier "satisfied by journald" was WRONG — an alive-but-hung process never flushed its block-buffered `print()`s, so journald was EMPTY during the OUT-002 hang. Ported Bayence-Certus's stdlib logger into `modelark/core/telemetry.py` (`TaggedLogger` + `get_logger`, `msg | k="v"` context) and added a `RotatingFileHandler` + a stdout sink (a logging handler flushes per record → the file AND journald stay current *while alive*). Config in `wishlist.yaml` `logging:` (no env vars, per rule). The fill worker tees every progress event into the log (`fill_api._log_event`), logging the START of each download/compress so a hang's last line says WHAT it's stuck on → `logs/modelark.log`, rotating.
  • Watchdog (#27): killable download/compress children + a no-progress monitor — see the INC-004 2026-07-09 update. Stage 3 (compress isolation) already shipped for INC-005.

### OUT-002: Fill hung ~7 h overnight on a stalled download (no data loss)
- date: 2026-07-09 / status: closed at recovery (manual restart) / triggered_by: INC-004 residual (Falcon-H1 download stall) / related: INC-004, DEC-023
- impact: after archiving ~131 GB / 477 objects across ~64 models overnight (the INC-005 fix held — DeepSeek-R1-32B shards 3–7 all stored raw via the crash fallback), the fill hung at ~02:03 on a `tiiuae/Falcon-H1-34B-Instruct` download and sat idle ~7 h until noticed. Alive process (systemd blind), empty journald (buffered stdout) → diagnosed via /proc. NO data loss; the partial `.incomplete` resumes.
- fix: DEC-023 stage 2 (no-progress watchdog) + stage 1 file logging so the next one is both prevented and visible.

### DEF-015: iSCSI boot persistence declined — don't couple boot to a network mount
- date: 2026-07-09 / status: declined (revisit only on a strong, specific need) / related: DEC-023, task #29 (dropped)
- decision: do NOT auto-attach + mount drive-00's NAS iSCSI LUN at boot (no `_netdev` / `node.startup=automatic` on the boot path). Coupling machine startup to a network resource risks hanging or failing the WHOLE boot when the NAS is slow/unreachable — a classic ops footgun (operator's call, and a sound one).
- rationale: not needed anyway — the guided fill AWAITS drive-00 at runtime (`fetch._await_drive`: "⏳ insert drive… continues when it mounts"), so an unattached LUN just delays the first shard; it never blocks boot. The operator attaches/mounts the LUN when running a fill (manually or via a non-boot-critical unit). The systemd portal unit (DEC-023) already only *orders after* the mount, best-effort, and never requires it.

### DEC-024: Catalog on SQLite (WAL), replacing DuckDB — concurrent read/write
- date: 2026-07-09 / status: code ready + validated on real data; cutover pending / triggered_by: operator "we need a lock-free or multi-user-lock db solution" / related: OUT-001, OUT-002, INC-006, #30, #31, #32
- problem: DuckDB is single-writer — one process holds it read-write and NO other process (even read-only) can open it. Every "inspect the catalog while the portal fills" was blocked (stop the portal to read `archived`/plan/drives). Recurring friction (the drive-01 diagnosis, the archived-durability check).
- decision: move the catalog to SQLite in WAL mode — many readers + one writer across processes, no exclusive lock. Fits the scale (thousands of rows; the plan's ~444 small lookups are point-queries SQLite handles at least as well as DuckDB's scan engine). `db.py`: sqlite3, `isolation_level=None` (autocommit, matches DuckDB), `check_same_thread=False` (portal shares one conn under `data._lock`), `PRAGMA journal_mode=WAL` + `busy_timeout=15000` + `synchronous=NORMAL`; `read_only` → `query_only=ON`. Same connect/upsert/replace_files API (`?` params + `ON CONFLICT … DO UPDATE … excluded.` all port).
- schema ports: `tags VARCHAR[]` → `TEXT` (JSON, encoded in Python); `DEFAULT now()` → `CURRENT_TIMESTAMP`; `now() - INTERVAL '24 hours'` → `datetime('now','-1 day')`; views — `GROUP BY ALL` → `GROUP BY` the PK, `list_distinct(list(x))` → `group_concat(DISTINCT x)`, `CREATE OR REPLACE VIEW/TABLE` → `DROP … IF EXISTS` + `CREATE` (SQLite has neither). Only `v_ui` is code-read (the `data.py`/`server.py` cache).
- validation: `scripts/migrate_duckdb_to_sqlite.py` run on a COPY of the live catalog (no portal stop) — all 8 tables row-count-exact (4118 models / 101652 files / 1612 archived / 444 selection / 816 events); `v_ui`, `tags` JSON, the archived-by-drive + 24h-date queries, `v_model_summary` group_concat, and the portal `build_cache` all correct on the migrated data; a concurrent second reader works while the portal connection is open. Tests: `test_db_sqlite` 4/4 + full suite green.
- cutover (PENDING — needs the portal fully stopped): stop portal → migrate live `catalog.duckdb` → `catalog.sqlite` → start portal (opens sqlite). `db.connect()` guards against starting on an empty sqlite while `catalog.duckdb` still exists. `catalog.duckdb` kept as backup; `duckdb` stays in requirements only for the migration script.
- UPDATE: cutover DONE (exact row parity, WAL, backup retained). Portal ran on SQLite; concurrent live reads confirmed (see DEC-025, diagnosed live).
- 2026-07-11 UPDATE: the cutover missed the non-`v_ui` code SQL — surfaced when the operator opened the **Library tab** on the fresh fill ("near 'ALL': syntax error"). `library_api` used DuckDB `GROUP BY ALL` / `list_distinct(list())` / `any_value()`, and CLI `ls` (`GROUP BY ALL`), `query` (`con.sql().show()`), `export` (`COPY … TO (FORMAT json)`) were all DuckDB-only. Ported to SQLite: `GROUP BY` the PK / positional, `group_concat(DISTINCT x)` (+ split), `max()`, and `con.execute` + `json.dumps` streaming for the JSONL export. Verified live (Library loads, `ls`/`query` work).

### DEC-025: Placement tracks reality — live disk free + observed compression ratio (not fixed guesses)
- date: 2026-07-09 / status: done, validated live / triggered_by: operator "every restart we need more of the last drive… it's gotta get accounted for as we run" / related: #31, INC-005, DEC-017, DEC-020, DEC-024
- symptom: each re-plan crept onto more of the tail drive. TWO fixed guesses drift from reality as the fill runs: (a) `est_stored_bytes` assumed float→0.67, but float shards that CRASH/HANG fall back to raw (stored at 1.0) — 5 shards / 44 GB so far (~14 GB overshoot), growing with each fallback; (b) `drives()` computed `remaining` from the STALE registration `free_bytes − archived`, which can't see cruft or real compression.
- fix (supply side, closes #31): `drives()` now takes a MOUNTED drive's `remaining` from LIVE `shutil.disk_usage(mount).free − headroom` — reality that already nets out archived bytes, real compression, AND cruft (orphan partials, raw working files); an unmounted drive falls back to the snapshot. Enabled by DEC-024 (reading the live disk/catalog while the portal fills).
- fix (demand side): `est_stored_bytes` estimates the unplaced pool from `plan_float_ratio` = the OBSERVED float stored/orig ratio so far (archived⋈files, blending real compression + raw-fallbacks), FLOORED at 0.67 (never more optimistic) + the 1.08 margin. Self-corrects as fallbacks accumulate.
- honest note: this snapshot showed the float ratio ACTUALLY accurate (observed 0.6687 ≈ the 0.67 floor) and `free_bytes` benignly consistent, so today's drift is SMALL (~14 GB). These fixes make the plan reality-tracking so the drift can't accumulate unbounded — not a claim that a large bug was found. Validated by running `plan_placements` live (read-only, portal filling): drive-00 remaining 4.82 TB (live) vs 4.84 (stale), plan consistent, 0 unplaceable. Watching across restarts to confirm the creep stops.

### DEF-016: Predictive, compression-aware placement ("level 2") — deferred behind a level-1 capacity failsafe
- date: 2026-07-09 / status: deferred — build the level-1 failsafe first / related: DEC-025, DEC-017, DEC-020
- want (level 2): the librarian PLANS AROUND compression uncertainty — predict each model's ACTUAL compressed footprint (per dtype/arch, the observed ratio, fp8/gguf specifics, the raw-fallback rate + confidence) so bulk packs each drive to its TRUE capacity: no compression savings left as dead slack, and no surprise "flip-over" onto the next drive when reality exceeds the estimate. Updating as it runs, it is never surprised by a drive's end.
- why deferred: real per-model prediction + confidence is a project. The level-1 failsafe is simpler and ALWAYS correct, and REMAINS the installed failsafe even after level 2 ships — if the prediction is off, level 1 still stops cleanly before overflow.
- level-1 (build first; its own DEC once designed): keep the estimate-based packing (DEC-025) as the plan; ADD an uncompressed-size FORWARD CHECK at each MODEL boundary against the current drive's live free → STOP + "insert the next drive in the archive set" before any overflow; operator adds a drive → re-plan → continue. Plus two always-on budget bars (compressed vs fully-uncompressed footprint against fleet capacity) + a warning when the uncompressed budget exceeds registered disk. Per-shard number/warning updates; per-model stop decision. RESOLVED (operator, 2026-07-09): NOT swap — the plan targets ALL registered drives IN THE PLAN (fixed set, registered up front). Planning basis DEFAULTS to fully-uncompressed (over-provision → smooth, never runs out; compression just finishes early with drives to spare); the operator can OVERRIDE to bet on compression, at which point the recompute-per-model failsafe carries the risk. The failsafe = a Library `update()` that recomputes full-raw + full-compressed after every MODEL (numbers/warnings per shard); RAW drives the model-boundary "do we still fit" check on the plan's drives. It runs CONSTANTLY in both modes, so an unexpected inflation (orphans/bug making actual > expected) is caught regardless. Foundation to build first = a first-class "Plan" (see below).
- 2026-07-11 UPDATE: **level-1 BUILT** — DEC-030 (the first-class Plan entity + copy-aware totals) + DEC-031 (the per-model capacity failsafe, provisioning-aware fill, `plan-capacity-stop`). **Level-2** (predictive per-file compression-aware packing) remains deferred; level-1 is the always-correct net beneath it.

### DEF-017: Grow the fleet mid-run — register + fold in a drive while a plan is filling
- date: 2026-07-09 / status: deferred / related: first-class Plan, DEC-025
- want: register a drive INTO an active plan while it downloads and re-plan the remaining pool onto it, no full stop/restart. For now a plan's drive set is FIXED at plan creation; adding capacity = stop → register into the plan → re-run.

### DEF-018: Rebalance — redistribute already-archived bytes across a plan's drives
- date: 2026-07-09 / status: deferred / related: first-class Plan, DEF-017
- want: `git annex move` archived content between drives to consolidate, free/retire a drive, or even out fill (e.g. after DEF-017 adds capacity). Active data movement with its own integrity checks — distinct from today's read-mostly planning.

### DEF-019: In-flight queue ops in the UI while a fill runs — append / recompute / re-download / re-verify
- date: 2026-07-09 / status: deferred / related: first-class Plan, DEC-024
- want: change the running fill's work from the UI without stopping it — append models to the queue, re-run ZipNN on a shard, re-verify sha256, re-download a corrupt file. Room for advanced scheduling later (GPU-aware compress, priority). Enabled by the SQLite concurrent-access move (DEC-024).

### DEF-020: Multiple git-annex repos per plan
- date: 2026-07-09 / status: deferred / related: first-class Plan
- want: a plan spanning >1 annex (today: exactly 1 annex per plan) — segment a library by tier/location or exceed one annex's practical limits.

### INC-007: drive-01 dropped off the USB bus mid-fill
- date: 2026-07-10 / status: recovered (re-seat + fsck clean) / triggered_by: USB topology churn (drive moved to a new port; hubs + a dock now in play) / related: INC-008, DEC-026, #19
- root cause: drive-01's USB enclosure dropped mid-fill — the block device reported `limit=0` (zero sectors) on every access and EXT4 EIO'd reading the root inode (#2); `df` still showed 7.3T (stale cache). NOT media failure: a re-seat re-enumerated it (READ CAPACITY(16) → full 8 TB), `fsck` clean, remounted r/w. The machine's USB field is currently wild (a second device, sde, also EIO'd its journal superblock around the same time).
- impact: the fill was just past `DeepSeek-V3.1-Base` (which had actually completed, 173/173); drive-01 was its next primary target. The drop exposed two software gaps (→ INC-008); no data loss (skipped models re-plan).
- remediation: DEC-026 (write-probe → a dead drive is awaited/bailed, never trusted). Ops: prefer stable ports; the standing #19 (SMART/write-surface hardening at registration) still applies.

### INC-008: the fill silently skipped a dead drive's assignment AND marched off a half-empty NAS
- date: 2026-07-10 / status: remediation adopted (DEC-026) / triggered_by: INC-007 / related: DEC-025, DEF-016, DEC-026
- root cause: two gaps, both because the plan was computed ONCE at fill start and the drive check was mount-only:
  (a) SILENT SKIP — `fill._await_drive` accepted drive-01 because it was *mounted* (`archive_path` resolved), never probing writability; `fetch.run` then no-op'd on the EIO path and the loop advanced, dropping drive-01's ~270 assigned models with no error.
  (b) NAS UNDER-USE — the static plan over-estimated drive-00 (0.67 ratio floor + 8% margin; fp8 giants store raw), "filled" it on paper and spilled the rest to drive-01; the models landed smaller, freeing ~2.7 TB on the RAID that the static plan couldn't reclaim, so the fill marched off a half-empty NAS. Exactly the case DEF-016's level-1 (boundary re-plan) exists to prevent — not yet built.
- remediation: DEC-026.

### DEC-026: Re-plan the primary tier per drive-batch + write-probe every drive (targeted level-1 of #37)
- date: 2026-07-10 / status: accepted / triggered_by: INC-008 / related: DEC-025, DEF-016, INC-007, #19
- decision: `fill.execute` RE-PLANS (`plan_placements` — live disk free + observed ratio per DEC-025) before each primary drive-batch instead of marching a single start-time plan: fetch the highest-priority drive's copy#1 work, re-plan, repeat — so estimate-vs-actual slack is reclaimed (the NAS keeps filling) and drives fill in live-priority order. Loop-guarded: a repo that fails to place `_MAX_REPO_ATTEMPTS` passes is blocked; the 24h cap returns a clean resumable stop. Every drive is WRITE-PROBED (write+read+delete a hidden file) at the await boundary (`fill._writable`) AND mid-batch after a repo error (`fetch._dest_writable`) — a mounted-but-unwritable drive is awaited/bailed, never silently skipped.
- rationale: the targeted, always-correct slice of the Plan epic's #37 — cheap (~1s re-plan vs multi-minute downloads) and closes both INC-008 gaps without waiting on the first-class Plan entity (#33). DEF-016's level-2 (predictive per-file packing) stays deferred with this as the net under it.
- impact: `fill.execute` (drive loop → re-plan loop), `_writable`/`_dest_writable` probes, a `paused`/`blocked` terminal classification (see DEC-028), `tests/test_replan.py` (7 tests). Takes effect on portal restart.

### DEC-027: 24h download cap is config-driven (wishlist download.max_24h_gb)
- date: 2026-07-10 / status: accepted / triggered_by: operator (cap looked like it might be an "overall", and was blocking DLs) / related: DEC-026
- decision: the fill's 24h download throttle moves from a hardcoded 2000 GB in `fill_api.start` to `wishlist.yaml` `download.max_24h_gb` (code default 4000; **0 = unlimited**). Set to 0 now to unblock.
- rationale: verified the mechanism is a correct ROLLING window (`fetch._bytes_last_24h` sums `orig_bytes` over `now-1day`; 956 GB of older archives correctly excluded, no cross-drive double-count, a real steady ~110 GB/h × 19 h = 2.05 TB today) — the throttle was legitimate, not a bug, so the fix is configurability, not a rewrite.

### DEC-028: Fill queue is one row per model with real completion state (nothing vanishes)
- date: 2026-07-10 / status: accepted / triggered_by: operator confusion — a finished model appeared to "vanish"; the per-copy queue conflated "behind the download pointer" with "done" / related: DEC-026
- decision: the Fill queue renders ONE row per finalized model (was per-copy, scoped to the active drive), state derived from live placed-vs-numcopies: **done** (all copies → struck, STAYS visible), **partial** (some → orange + "N copies left", re-sorted to its remaining-copy drive), **upcoming**, **current**. A done row settles at its copy#1 home (operator chose placement **'b'** over "stay at last copy"). Backed by read-only `queue_view` (whole selection incl. done: size/numcopies/copy-drives) + cheap `queue_state` (`{repo: copies_placed}`, polled at model boundaries). Also: drive cards now show TRUE fill = archived (grey base) + NEW-planned — not planned-only, which read a full RAID as half-empty (it re-counted already-archived must-have copy#1); and a throttle reads "paused", not "fill complete".
- rationale: the queue must show what's DONE, not only what's left, or finished work looks lost and capacity looks wrong. General N-copy re-sort ("Nth of N drives") deferred until numcopies>2 exists — a rider on DEF-016.

### INC-009: NAS powered off mid-fill → a re-plan guard bug advanced to GATE-C → hard-stop
- date: 2026-07-11 / status: resolved (software fixes + PIV-001 startover) / triggered_by: NAS power-off / related: INC-007, DEC-026, DEF-022, DEF-023, DEF-024, DEF-021
- root cause: the NAS (drive-00, iSCSI LUN `/dev/sdc`) powered off overnight; ext4 hit I/O errors and `errors=remount-ro`-flipped drive-00 to read-only, contents EIO'd. The fill was still MID-PRIMARY — only 128/444 models had copy#1, ~316 unfetched, and nearly all copy#1 to date sat on drive-00 (drive-01: 12 files, drive-02: 0, drive-04: 6). Two failures stacked: (1) a BUG in the re-plan loop's no-progress guard (DEC-026, → DEF-024) — a no-progress pass blocks EVERY repo in the pass, not just the failed drive's, so when drive-00 (+ flaky drive-01) stalled it blocked its way to an empty primary work-list and WRONGLY advanced to the replica tier; (2) copy#2 is `git annex copy` FROM the now-dead drive-00, so every copy failed → GATE-C saw 124 must-haves at 1/2 → 'error'.
- impact: NO data loss (the copy#1 that landed is RAID-protected; the fault was iSCSI/power, not the platters). But the fill did NOT gracefully await the dead NAS — a loop bug churned it to a hard GATE-C error with copy#1 barely a third done, and the stop sat silent until queried by hand. The re-plan's *reclaim + probe* halves are sound; the *no-progress guard* is not.
- fix: re-establish the NAS (power/iSCSI/remount rw) → resume. Then: guard over-block DEF-024, soft-fail on an offline copy#2 source DEF-022, loud surfacing DEF-023, suspect re-verify DEF-021.
- 2026-07-11 UPDATE — RESOLVED. All four software root causes fixed: DEF-024 (guard, commit 3e75fa4), DEF-022 (fail-soft replica, DEC-031), DEF-023 (loud oopsies, DEC-032), DEF-021 (Verifier, DEC-033 — it independently re-flagged the interrupted `arcee-ai/Trinity-Large-Thinking` copy). The physical RO-at-full trap is being removed by the PIV-001 startover: the maxed LUN was deleted and is being recreated at ~85% of the volume (DEC-029), so a power event on the copy#1 home can no longer strand the volume with no repair room.

### DEF-024: Re-plan no-progress guard over-blocks — scope it to the attempted drive; await dead drives
- date: 2026-07-11 / status: resolved (await-first + per-batch block; commit 3e75fa4) / triggered_by: INC-009 / related: DEC-026
- bug: `fill.execute`'s primary re-plan loop blocks EVERY repo in a no-progress pass (`for _, its in work: … block`), but a pass only attempts `work[0]` (one drive). So a stalled/dead drive wrongly blocks the OTHER drives' un-attempted repos too; after `_MAX_REPO_ATTEMPTS` passes the primary pool empties and the loop advances to replica/GATE-C with copy#1 far from complete (INC-009: 128/444). A DEAD drive must be AWAITED (the write-probe's job), never "blocked" — blocking is only for a persistently-failing REPO.
- want: count/block only `work[0]`'s repos; distinguish a dead-drive stall (→ await via the probe, no block) from a repo-level failure (→ block after N). The guard must never empty the pool while placeable work remains on a healthy drive.

### DEC-029: Provision the copy#1 LUN with headroom — never max the volume
- date: 2026-07-11 / status: accepted / triggered_by: INC-009 (twice-bitten) / related: PIV-001, DEC-017
- decision: the NAS iSCSI LUN (copy#1 home) is recreated at ~85–90% of its volume, NOT ~99%. Synology volumes (Btrfs on the DS918+) flip read-only near-full, and a power event on a near-full volume can't self-recover (no space to check/repair with) — INC-009, and the same trap years ago (lost the LUN, forced a switch to NFS). Leave the volume real breathing room; the plan's capacity model treats the LUN's usable capacity conservatively.
- rationale: the recurring failure mode is "thick LUN maxed to the volume → full → read-only → unrecoverable in place." Headroom is the cheap structural fix; it also gives Btrfs room for metadata/CoW.

### PIV-001: Pivot — refactor + harden, then a clean startover on a first-class Plan
- date: 2026-07-11 / status: in progress — refactor+harden COMPLETE; startover underway / triggered_by: INC-009 + "we bolted a TON on" under fire / related: INC-007, INC-008, INC-009, DEC-026, DEC-029, DEF-021, DEF-022, DEF-023, DEC-030, DEC-031, DEC-032, DEC-033, roadmap.md
- (PIV = a strategic **piv**ot / product-initiative — a direction change bigger than one DEC.)
- decision: STOP extending the firefought state. The archived 3.32 TB is re-fetchable model weights (we're testing ModelArk E2E, not holding user data), so DO NOT rescue it — **0 out the annex and start clean**. Before re-running: (1) **REFACTOR + HARDEN** plan/librarian/fill — build the first-class **Plan entity (#33, roadmap.md)** as the real foundation and fold the session's bolted-on re-plan loop / no-progress guard / write-probes / gates into it cleanly + tested; (2) recreate the NAS LUN with **headroom** (DEC-029) + **nicer annex metadata** (the deferred #14); (3) then a fresh fill. This is the productization pass — spend time here.
- rationale: the code accreted fast (SQLite cutover, resilience, re-plan, probes, queue, cap) while firefighting three storage incidents; consolidating onto the intended foundation (the Plan epic that WAS the original agenda) beats carrying forward fragility + a maxed, opaque LUN. The lessons survive in the ledger even though the bytes are wiped.
- 2026-07-11 UPDATE — **item 2 (refactor + harden) COMPLETE**: the full Plan epic (#33–#38) + all three deferred DEFs (021/022/023) + DEC-029 shipped on `feature/plan-epic` (PR #2), landed as DEC-030 (Plan entity), DEC-031 (per-model failsafe + fail-soft replica), DEC-032 (portal surfaces + oopsies), DEC-033 (Verifier); verified with unit tests + the Playwright harness. **Startover underway**: stale iSCSI session + dead mounts cleared; the maxed RO LUN + target deleted; recreating the LUN at ~85% of the volume (DEC-029) → then `scripts/reset_physical.py`, re-register the fleet (folds into `ark`), fresh fill. Also building #14 (self-describing annex metadata) into the fresh registration.

### DEF-021: Verifier — on-demand re-verify + surface disruption-boundary "suspects"
- date: 2026-07-11 / status: resolved by DEC-033 / triggered_by: INC-009 / related: verify.py, INC-004, INC-005, INC-007
- want: a Verifier surface that (a) re-runs `verify.py` (checksum / structural / shards-complete / load) against the ARCHIVED copy of OPERATOR-CHOSEN models on demand, and (b) auto-surfaces SUSPECT models — those whose archiving overlapped a disruption boundary (a restart, a worker crash, a drive failover / RO flip, a compressor raw-fallback) — as re-verify candidates. Needs the pipeline to record disruption events with timestamps so "what was in-flight when X happened" is queryable (`fetch_events` is a start).
- why: a hiccup can leave a shard half-written or a drive's state ambiguous. The write-canary proves each shard at store time, but a mid-write power loss / RO flip (INC-009) is exactly the case to spot-check — cheaper and more trustworthy than blind full re-verification.

### DEF-022: Fail SOFT when a copy#2 source/target is offline — probe + await, don't hard-error
- date: 2026-07-11 / status: resolved by DEC-031 / triggered_by: INC-009 / related: DEC-026, DEF-023
- want: (a) `run_replica` PROBES its source (and target) before/during copy#2 and, on a dead drive, emits awaiting-drive + bails — the same treatment `_await_drive` / `_dest_writable` give the primary tier (DEC-026) — instead of churning every failed copy. (b) GATE-C distinguishes "copy#1 all safe, copy#2 merely deferred by an offline drive" (→ PAUSED, resumable) from genuine under-replication (→ error). Copy#1 is the irreplaceable data; a transient offline replica source must not turn a safe run red.
- why: INC-009 — a dead NAS (copy#2 source) hard-errored the fill even though copy#1 of everything had landed safely.

### DEF-023: First-class "oopsies" — surface a stopped/errored fill LOUDLY, on portal open
- date: 2026-07-11 / status: resolved by DEC-032 / triggered_by: operator ("this should have been a giant pop-up when I saw the portal") / related: INC-009, DEC-023
- want: when the fill ends in a non-DONE terminal state (error / blocked / paused / awaiting-drive), the portal shows an UNMISSABLE surface on open — a modal / big banner with the reason, the affected models/drive, when it happened, and the next action ("re-seat drive-00, then Start"). Not a quiet status line. Persist the last terminal event so it shows even after a page reload / restart, until acknowledged.
- why: INC-009 sat silently overnight; the operator only found the GATE-C reason by querying the API by hand. A fill that fell over must announce itself.

### DEC-030: First-class Plan entity + copy-aware capacity model (level-1 failsafe foundation, #33)
- date: 2026-07-11 / status: accepted / triggered_by: PIV-001 (productize pass) + DEF-016 (level-1 needs a first-class Plan) / related: DEF-016, DEC-025, DEC-029, roadmap #33 / docs_updated: modelark/core/schema.sql, modelark/plan.py, modelark/cli.py, modelark/web/server.py, tests/test_plan.py
- decision: A first-class **Plan** (`plans` + `plan_drives`; new `modelark/plan.py`) is the foundation the fill / librarian / failsafe read. A Plan = {identity, provisioning mode, a FIXED drive set (`plan_drives`), the GLOBAL selection/archived it fills}. `plan.totals()` computes three numbers LIVE (never stored snapshots):
  - **uncompressed** = Σ raw footprint of the finalized selection, COPY-AWARE (× numcopies) — the boundary currency (DEF-016), what "are we out" keys on.
  - **compressed** = bytes actually archived so far + an observed-ratio estimate (DEC-025 `plan_float_ratio`, floored 0.67) for the copies still to write, copy-aware — the best guess; the compression dividend = uncompressed − compressed. (Over-counts a single in-flight partial copy at most — transient, safe direction.)
  - **capacity** = Σ (drive capacity − headroom) over `plan_drives`, with a DEC-029 conservative headroom FLOOR (≥3%) for a `raid_backed` LUN.
  Bootstrap of plan **`ark`** is idempotent (owns every registered drive; activated if none is). Exactly one plan is `is_active` (the backend/portal context) — a boolean kept DISTINCT from `status` (lifecycle) so the #35 UI gate can force an explicit pick without touching lifecycle. Provisioning DEFAULTS to 'uncompressed' (over-provision → never runs out); 'compressed' bets on ZipNN, with the per-model failsafe (#37, next) carrying the risk. `selection`/`archived` stay GLOBAL (one plan `ark`); a plan_id column on them is the multi-plan future (a DEF).
- rationale: the session bolted the re-plan loop / guards / probes / gates onto `fill.execute` with no entity underneath (PIV-001); the first-class Plan is the intended foundation (DEF-016) that makes the capacity numbers first-class + testable. Copy-aware footprint (not one-copy) is what's comparable to fleet capacity — a must-have's 2nd copy is real bytes on a real plan drive. Computing LIVE (not snapshots) carries the DEC-025 reality-tracking lesson forward. `is_active`-as-boolean (vs overloading `status`) keeps the #35 gate clean.
- impact: schema (plans/plan_drives); `plan.py` (create/list/get/active/set_active/set_provisioning/add_drive/totals/bootstrap); `modelark plan` CLI (list/show/create/select/provisioning); portal bootstraps `ark` at serve() start. Verified live: `ark` owns 7 drives; the 444-model selection = 16.41 TB uncompressed / 12.91 TB compressed vs 22.03 TB capacity (74% / 59%). Next: DEC-031 wires the per-model boundary failsafe (#37) onto this; #34 folds registration into `plan_drives`; #35/#36/#38 UI reads `totals()`.

### DEC-031: Per-model capacity failsafe + provisioning-aware fill on the Plan (#37; DEF-016 level-1)
- date: 2026-07-11 / status: accepted / triggered_by: PIV-001 + DEF-016 (level-1) + DEF-022 / resolves: DEF-022 / related: DEC-030, DEC-025, DEC-026, DEC-019, INC-009, roadmap #37 / docs_updated: modelark/fill.py, modelark/fetch.py, modelark/librarian.py, modelark/cli.py, modelark/web/fill_api.py, modelark/web/library_api.py, tests/test_replan.py
- decision: fold the session's re-plan loop / write-probes / gates onto the first-class Plan (DEC-030) and add the DEF-016 level-1 failsafe:
  - **fill.execute is Plan-driven**: resolves the active plan, scopes placement to its `plan_drives`, packs in the plan's PROVISIONING currency. `'uncompressed'` (DEFAULT) packs bulk + must-have copy#1 against RAW sizes — over-provision, "guarantee fit without betting on compression"; `'compressed'` packs against the ZipNN estimate (the bet). BOTH totals always reported (DEC-030 / the #36 bars) so inflation shows in either mode.
  - **Per-model forward check**: `fetch.run` takes a `fits(repo)` hook; before each model it checks the model still fits the target drive's LIVE remaining (same `drives()` remaining + est size the librarian packs with). On a non-fit it breaks the batch → the next re-plan re-homes the overflow onto another plan drive, or (nothing fits) STOPS as `plan-capacity-stop` (resumable "add a drive to the plan"). Prevents an ENOSPC mid-shard; catches actual > estimate. Distinct from GATE-B `blocked` (up-front: the selection exceeds the fleet) by a `fetched_any` flag (mid-fill drive-full vs never-started).
  - **Replica tier always sized COMPRESSED**: a copy#2 is a `git annex copy` of the already-compressed copy#1 blob (not a fetch — no compression uncertainty), so raw-over-provisioning it would falsely short the small replica tier and GATE-B a fill that fits. `plan_placements` sizes copy#2 at the compressed estimate regardless of provisioning.
  - **DEF-022 fail-soft replica**: `run_replica` PROBES its source (INC-009: the RAID can go read-only + EIO) and each target with a write-probe; an offline source/target is DEFERRED (emit awaiting-drive, no per-repo churn) and reported back. GATE-C then distinguishes "copy#1 all safe, copy#2 deferred by an offline drive" → PAUSED (resumable) from a genuinely missing copy#1 → error.
- rationale: DEC-026's re-plan reclaim + probes were sound but lived as a start-time march with no per-model net and no Plan underneath; #37 is the always-correct level-1 failsafe on the DEC-030 foundation. Uncompressed-default = never runs out (compression finishes early with drives to spare); the operator bets only by choosing compressed mode. INC-009's hard GATE-C error on a safe-copy#1 run is exactly what DEF-022 softens.
- impact: `fill.execute(*, plan_id, ...)` (no positional plan); `fetch.run(fits=)` + `run_replica` returns a deferral report; `librarian.{drives,est_stored_bytes,plan_placements,plan_view,queue_view}` take `plan_id`+`provisioning`; callers (fill_api, cli, library_api) pass the active plan. New terminal states `plan-capacity-stop` + softened `paused`. Verified live: the 444-model plan places all copy#1 + 124 must-have copy#2 (0.71 TB compressed) under uncompressed provisioning, 0 unplaceable. tests/test_replan.py +4 (14 checks).
- resolves: DEF-022

### DEC-032: Plan-epic portal surfaces — Plans tab + gate, capacity bars, catalog gate, loud oopsies (#35/#36/#38, DEF-023)
- date: 2026-07-11 / status: accepted / triggered_by: PIV-001 (productize) + roadmap #35/#36/#38 + DEF-023 / resolves: DEF-023 / related: DEC-030, DEC-031 / docs_updated: modelark/plan.py, modelark/web/{plan_api.py, fill_api.py, server.py}, modelark/web/static/{index.html, app.js, app.css, catalog.js, fill.js, plans.js}, tests/test_oopsie.py
- decision:
  - **#35 Plans tab + gate**: a LEFTMOST Plans tab (create + recall). Every other tab is greyed until the operator EXPLICITLY selects a plan per session (no auto-select); selecting sets it active server-side + reloads. Guard: bootstrapped `ark` is always listed + selectable, so the gate can NEVER lock you out. (client sessionStorage flag over the server-side active plan.)
  - **#36 two capacity bars**: always-on fully-UNCOMPRESSED (boundary currency) + fully-COMPRESSED (on-disk estimate) footprints, both vs the plan capacity line, on the Fill page — refreshed at each model boundary. The gap = the compression dividend, in drive terms.
  - **#38 graduated catalog gate**: the CART's compressed footprint vs plan capacity drives four tiers (ok → soft → warn → prevent); `prevent` (compressed ≥ capacity) blocks ADDING more (un-checking always allowed) while you build the set. `plan.cart_totals` + `plan.gate_tier`.
  - **DEF-023 loud oopsies**: a non-DONE terminal fill (error / blocked / plan-capacity-stop / paused) is persisted to `catalog/last_fill.json` and shown as an UNMISSABLE modal on portal open — surviving reload/restart until Acknowledged. A clean done / user-stop clears it.
  - New `plan_api` (overview/totals/cart/select/create/provisioning) + `/api/fill/last-terminal` + `/api/fill/ack-terminal`.
- rationale: the Plan (DEC-030) is only a safety net if the operator can SEE it and is forced to choose one. The bars make the compression bet legible; the gate stops over-committing at selection time; the oopsie ends the INC-009 "sat silent overnight" failure. All verified in the Playwright harness (gate forces Plans + disables tabs; select unlocks; bars/gate/modal render correct live numbers).
- resolves: DEF-023

### DEC-033: Verifier — re-verify archived copies + auto-surface disruption suspects (DEF-021)
- date: 2026-07-11 / status: accepted / triggered_by: DEF-021 / resolves: DEF-021 / related: DEC-003, DEC-019, DIS-002, INC-005, INC-009 / docs_updated: modelark/verifier.py, modelark/web/{verify_api.py, server.py}, modelark/fetch.py, modelark/web/static/{index.html, app.js, app.css, verify.js}, tests/test_verifier.py
- decision: a new `verifier` module + Verify tab, DISTINCT from `verify.py` (Tier-A SOURCE checks against HF). `suspects(con)` auto-surfaces re-verify candidates — a compressor RAW-FALLBACK (a float safetensors stored uncompressed, INC-005), a PARTIAL copy (a drive holds fewer than the planned files — interrupted), or an archive that landed within ±15 min of a recorded DISRUPTION event. `reverify(con, repo)` checks RECORD consistency offline (all planned files archived; each stored orig_sha256 matches the catalog) + runs a decompress-CANARY spot-check per stored blob when its drive is mounted (skipped, not failed, when shelved). fetch now PERSISTS `compress-fallback` + `awaiting-drive` to `fetch_events`, so disruption boundaries are timestamped + queryable. `verify_api` uses its OWN read-only connection (WAL) so a slow canary never freezes the portal.
- rationale: the write-time canary (DEC-003/019) proves each shard at store time, but a mid-write RO flip / crash / raw-fallback is exactly the case to spot-check on demand (INC-009) — cheaper + more trustworthy than blind full re-verification. Verified live against the real catalog: it surfaced the DeepSeek-R1-Distill-Qwen-32B raw-fallback (record OK) + the arcee-ai/Trinity-Large-Thinking PARTIAL copy (FAIL: 15/32 planned files — the actual INC-009 interruption).
- resolves: DEF-021

### DEC-034: Giants-first fetch order — biggest downloads up front, then must-haves, then the rest
- date: 2026-07-11 / status: accepted / triggered_by: operator ("get the giants first, then must-haves, then the rest; anything over 250 GB up front") / related: DEC-031, DEC-026 / docs_updated: modelark/fill.py, modelark/librarian.py, tests/test_replan.py
- decision: the primary tier fetches copy#1 in a GLOBAL priority order — (1) **giants** (raw download > 250 GB), (2) **must-haves** (numcopies≥2), (3) the rest — largest-first within each tier, regardless of which drive each model lands on. `fill._primary_order` builds it from `librarian.raw_sizes` (download size — mode-independent, so a giant is a giant in either provisioning currency) + the must-have set. RECONCILED with the hot-swap workflow (DEC-023 / `_await_drive` — the operator cannot keep every USB drive mounted at once, so the guided fill asks for drives in sequence): the primary loop DRAINS one drive per pass — the giant-heaviest drive first, giants-first WITHIN it — before asking for the next, so giants land early WITHOUT swap-thrashing across drives. Each model lands on the drive the librarian assigned it; the plan spans ALL registered `plan_drives` whether or not they're currently mounted.
- rationale: a giant (e.g. a 400 GB model) is the long pole and the highest-risk download — if it fails late in a multi-day run it wastes the most, and leaving giants to the end risks never finishing them; front-loading de-risks the run. Must-haves next (the protected set). The order sets the DRIVE sequence + the within-drive order; the #37 fits-hook still catches a mid-batch capacity overflow. Draining per-drive (NOT per-model) keeps the hot-swap to ONE insert prompt per drive — essential when the fleet can't all be online at once (the operator's reality: ~6 USB drives, limited ports).

### DEF-025: Automate host setup — the smartctl sudoers drop-in + the systemd portal unit
- date: 2026-07-11 / status: active / triggered_by: operator (had to hand-write the `smartctl` sudoers rule for Disk Health; "we'll automate it later, log a DEF") / related: DEC-008, DEF-003, DEC-023 / docs_updated: README.md, docs/decision_log.md
- decision: defer automating the privileged, host-specific setup a fresh install needs; document it MANUALLY for now (README → Setup): (a) a passwordless `smartctl` sudoers drop-in (`/etc/sudoers.d/modelark-smartctl`) so the `systemd --user` portal reads SMART for Disk Health WITHOUT running as root (keeping the catalog + annex clones user-owned); and (b) the DEC-023 `systemd --user` unit (`Restart=always`, ordered after the mount, opt-in resume-on-boot). A future `modelark setup` (or install script) should write both IDEMPOTENTLY — detect the `smartctl` path, `$USER`, the repo/venv paths — with `--dry-run`, chmod the sudoers file 440, and verify `sudo -n smartctl` afterward.
- rationale: these are one-time, root-touching, easy-to-get-subtly-wrong steps (path, 0440 perms, unit ordering). Documenting them unblocks now; automating is a nicety, off the critical path to the fill.
- revisit_when: onboarding a second machine / contributor, OR before any public release (a fresh clone should come up with one documented command).

### INC-010: hf_xet re-downloads a killed shard from 0 instead of resuming — costly on giant models
- date: 2026-07-11 / status: mitigated (orphan sweep); xet-transport decision pending (DEF-026) / triggered_by: operator ("stalled on the 5th tensor") during the first giant fetch — arcee-ai/Trinity-Large-Thinking (398 B, 797 GB, 29 GB shards) / related: INC-004, DEC-023, DEF-026 / docs_updated: modelark/fetch.py, tests/test_replan.py
- symptom: shard 5/31 (29.36 GB real) downloaded to 22.9 GB, stalled ~3 min; the DEC-023 watchdog CORRECTLY killed + retried — but the retry opened a NEW `.incomplete` (from 0) instead of RESUMING the 22.9 GB partial, leaving 31.6 GB of orphaned `.incomplete` on drive-00 and re-spending the bandwidth. NOT hung (the retry was progressing at ~47 MB/s); NOT an inflation bug (the shard is genuinely 29 GB).
- root_cause: downloads use **hf_xet** — HF's content-addressed WIRE transport (auto-enabled whenever the `hf_xet` package is installed + the repo is xet-backed), which reconstructs the file from CAS chunks into the `.incomplete`. A killed xet reconstruction does NOT resume the on-disk partial the way classic HTTP byte-range resume does — hf restarts it. The INC-004 fix's "hf resumes the .incomplete" holds for CLASSIC transfer, not xet. Amplified by 29 GB shards: each stall = a full-shard re-download. (xet is HF's network transport, NOT ModelArk's at-rest format — that's ZipNN `.znn`, unaffected.)
- blast_radius: none to integrity (per-file transactional; stored shards safe) and no capacity wall (DEC-025 live-free tracking nets out cruft). Cost = wasted bandwidth/time on a stall + orphaned `.incomplete` cruft.
- mitigation: `fetch._sweep_incomplete` clears orphaned `.incomplete` after each successful store (safe — sequential shards, so no active partial exists then; idle-age guarded). Deeper levers under discussion: disable hf_xet → classic resumable transfer (near-term); DEF-026 (sub-shard/tensor checkpointing).

### DEF-026: Sub-shard / tensor-level download checkpointing — resume a partial giant shard
- date: 2026-07-11 / status: active — DISCUSS ONLY, not building yet / triggered_by: operator ("I'd like to be able to checkpoint tensors") after INC-010 / related: INC-010, INC-004, DEC-019, DEC-023 / docs_updated: docs/decision_log.md
- want: resume a partially-downloaded SHARD across a stall/kill/restart, so a 797 GB giant never re-downloads a whole 29 GB shard because it stalled at 90%. Today resume is per-FILE (DEC-019) — a killed shard restarts from 0 unless the transport resumes the `.incomplete` (classic hf does; hf_xet does not — INC-010). Options to weigh (NOT decided): (a) simplest — just use a resumable transport (classic hf byte-range → the `.incomplete` resumes for free); (b) checkpoint at the safetensors TENSOR boundary — the shard header carries per-tensor byte-offsets, so a partial could be validated up to the last complete tensor and resumed from there; (c) our own ranged sub-downloads with checkpoints + reassembly. (b)/(c) are real work (validating a partial safetensors, range requests, reassembly) and interact with the sha256 canary (a partial isn't hashable until whole).
- rationale for deferring: option (a) likely covers ~95% of the pain for near-zero effort, so the elaborate tensor-checkpointing (b)/(c) may prove unnecessary. Settle the transport first (INC-010); only build (b)/(c) if whole-shard re-downloads still bite after that.
- revisit_when: after the INC-010 xet-transport decision is settled and the first full giant fills show whether whole-shard re-downloads remain a real cost.

### INC-011: compress watchdog false-killed the round-trip CANARY on giant shards → whole giant stored raw
- date: 2026-07-11 / status: fixed / triggered_by: operator ("compressor failures on the ARCEE model … storing this whole thing raw at a snail's pace") during the first giant fill — arcee-ai/Trinity-Large-Thinking (398 B, 797 GB, 29.36 GB bf16 shards) / related: INC-005, DEC-022, DEC-023, DIS-002 / docs_updated: modelark/fetch.py, tests/test_compress_isolation.py
- symptom: shards 1–4 (small) compressed fine (ratio ~0.66), but every 29 GB shard (5, 6, …) logged `compressor crashed → stored raw` after a ~13.6 min gap (17:06:45 "compressing" → 17:20:21 stored raw), so the whole giant was landing UNCOMPRESSED and slowly. Looked like a native crash; the operator asked whether it was a special format / too few workers.
- root_cause: NOT a crash — journald showed `[raw-fallback] … compressor HUNG (300s no progress), stored uncompressed`. The DEC-023 monitored compress child does two phases: **compress** (writes the `.znn` temp → the watchdog's progress signal, `glob(dst.name + ".*.tmp")` size, grows) then the mandatory **round-trip canary** (decompress + hash the WHOLE shard to certify restore, DEC-003) which only READS the `.znn` — so the temp stops growing and the watchdog goes blind. `_COMPRESS_STALL_SECS` was a FIXED 300 s; a 29 GB canary takes ~13 min over iSCSI (DIS-002 measured 8 GB ≈ 3.5 min ≈ 26 s/GB → 29 GB ≈ 770 s), which blew past a flat 300 s → the watchdog killed a WORKING canary → `_compress_isolated` returned "stalled" → raw-fallback. Small shards' canaries finish under 300 s, which is exactly why shards 1–4 survived and 5+ didn't. Format (plain bf16) and threads/workers were red herrings — the compress succeeded every time; the certify step got guillotined. (Distinct from INC-005, which was a genuine native SIGABRT; this is a false-positive hang.)
- blast_radius: none to integrity or capacity — raw-fallback is safe by design (DEC-023: the shard IS stored, just uncompressed; DEC-025 live-free tracking absorbs it). Cost = the ~34% ZipNN dividend forfeited on the whole giant (~260 GB not reclaimed on 797 GB) PLUS ~13.6 min wasted per big shard trying-then-failing (~6 h across Trinity's 27 giant shards) — the "snail's pace." Would have recurred on EVERY giant.
- fix: scale the compress watchdog window to the shard size so it covers the big-shard canary — `stall = max(_COMPRESS_STALL_SECS, int(shard_bytes/1e9 × _COMPRESS_STALL_PER_GB))`, `_COMPRESS_STALL_PER_GB = 60` (≈2.3× the measured ~26 s/GB → the canary fits with margin). The compress phase grows the temp every few seconds so it never approaches even the 300 s floor; only the canary needed the headroom, so enlarging the window costs only a longer time-to-detect on a genuinely-wedged compress (rare; the isolation still protects the portal). Applies on the next portal restart. tests/test_compress_isolation.py +2 (window scales for a 30 GB sparse shard; floors at 300 s for a small one). A phase-aware liveness heartbeat covering compress AND canary is the more robust long-term shape but heavier; size-scaling is the pragmatic correct-enough fix.
- follow_up: Trinity's already-stored giant shards are RAW; after the restart they'd compress correctly on a re-fill, but re-fetching a 797 GB giant is expensive — operator to decide accept-raw (it fits) vs re-fill compressed.

### INC-012: Portal E2E replaced the default catalog instead of injecting test state
- date: 2026-07-12 / status: fixed / triggered_by: public-release audit / related: DEC-035 / docs_updated: tests/test_e2e_portal.py, contributing/contributions.md
- root_cause: the browser harness temporarily moved `catalog.sqlite`, seeded a replacement at the application's implicit checkout-relative path, then restored the original in `finally`. It did not account for SQLite's `-wal`/`-shm` sidecars or a concurrently running portal. The documented test command could therefore split or damage a live catalog.
- fix: DEC-035 makes data/state locations explicit and injectable. The harness now creates a `TemporaryDirectory`, seeds only that catalog, passes the same `--data-dir`/`--state-dir` to the portal subprocess, and never reads or mutates the operator's default catalog.

### INC-013: Re-verification could report success without finding the recorded physical bytes
- date: 2026-07-12 / status: fixed / triggered_by: public-release audit / related: DEC-033, DEC-036 / docs_updated: modelark/verifier.py, modelark/fetch.py, tests/test_verifier.py
- root_cause: a recorded blob absent from a mounted drive was silently skipped, so database consistency alone could produce `ok=true`. Fetch also persisted only `stored.name`; nested Hugging Face paths were discarded and could not be reconstructed reliably during verification.
- fix: DEC-036 persists the archive-relative path and makes mounted missing/dangling bytes a hard failure. Shelved drives produce an explicit `unknown`, never a green result, and required copy counts are evaluated per planned file.

### DEC-035: Runtime data is explicit and writable; installed assets are package resources
- date: 2026-07-12 / status: accepted / triggered_by: INC-012 + public-release wheel audit / related: DEC-024 / docs_updated: modelark/core/db.py, modelark/wishlist.py, modelark/cli.py, modelark/discover.py, modelark/register.py, modelark/web/server.py, modelark/default_wishlist.yaml, pyproject.toml, tests/test_runtime_paths.py, tests/test_e2e_portal.py, README.md, contributing/contributions.md
- decision: installed code is never its writable runtime root. Catalog/export state defaults to the platform user-data location (XDG data on Linux), logs to the user-state location, and the CLI exposes global `--data-dir`, `--state-dir`, and `--config` overrides. Static web files, the schema, and a default wishlist are packaged resources. A checkout-root wishlist remains an editable-install compatibility source, but an old checkout-local catalog is never silently moved or replaced: startup fails with an explicit migration/override instruction.
- rationale: a wheel may be installed read-only in `site-packages`; configuration can be absent from the checkout; tests must select their own state rather than manipulating operator data. Explicit paths also make service deployments and clean-room wheel tests reproducible.

### DEC-036: Archive records preserve relative paths; physical verification is tri-state
- date: 2026-07-12 / status: accepted / triggered_by: INC-013 / related: DEC-003, DEC-019, DEC-033 / docs_updated: modelark/core/schema.sql, modelark/core/db.py, modelark/fetch.py, modelark/verifier.py, modelark/web/static/verify.js, tests/test_db_sqlite.py, tests/test_verifier.py
- decision: every archived row stores `stored_relpath`, the normalized path below `<archive>/<repo_id>`; new writes derive it lexically from the actual destination and reject absolute/escaping paths. Existing basename-only rows are migrated to `parent(rfilename)/stored_name`. Re-verification checks every recorded mounted copy, treats missing or broken annex content as failure, enforces the model's required copy count per planned file, and reports exactly `verified`, `failed`, or `unknown`. Offline/shelved copies may make the result unknown but can never make `ok=true`.
- rationale: basename is not an identity for nested repositories and permits collisions. Database consistency is useful evidence, but it cannot prove physical bytes; a green verification result is reserved for the required number of mounted copies whose bytes were actually checked.

### DEC-037: Restore is a verified, staged product workflow
- date: 2026-07-13 / status: accepted / triggered_by: public-release audit finding #8 / related: DEC-003, DEC-019, DEC-036 / docs_updated: modelark/restore.py, modelark/cli.py, tests/test_restore.py, README.md
- decision: `modelark restore --repo ID --dest ROOT` restores to `<ROOT>/<org>/<model>` from the catalog's recorded copies. For each planned file it selects a mounted copy, asks git-annex to retrieve dropped content (path first, key fallback), routes compressed blobs through the existing magic-aware decompressor, and requires the catalog/annex canonical sha256. A corrupt/unavailable copy falls through to another replica. The complete Hugging Face layout is built in a hidden sibling tree and atomically published only after every file verifies; existing destinations and unsafe paths are refused.
- rationale: codec primitives and write-time canaries proved that individual blobs could round-trip, but recovery is the archive product. Retrieval, layout reconstruction, replica selection, decompression, final hashes, and failure atomicity must be one operator-facing command rather than a manual sequence.

### DEC-038: Treat the localhost portal as a hostile-web trust boundary
- date: 2026-07-13 / status: accepted / triggered_by: public-release audit finding #3 / related: DEC-004, DEC-032 / docs_updated: modelark/web/server.py, modelark/web/static/{app.js,plans.js,catalog.js,disk.js,library.js,fill.js,verify.js}, tests/test_web_http_security.py, tests/test_web_xss.py, SECURITY.md, README.md
- decision: the unauthenticated portal remains loopback-only and refuses non-loopback binds. Every request requires a literal loopback/localhost Host with the actual listening port. Every POST additionally requires an exact same-origin HTTP Origin, a per-process CSRF capability injected into the non-cacheable index and sent back in `X-ModelArk-CSRF`, `application/json` containing an object, an explicit Content-Length, no Transfer-Encoding, and at most 64 KiB. Responses add a restrictive CSP plus frame, MIME-sniffing, referrer, resource, and permissions controls. API/operator values entering structured HTML pass through the centralized `MA.esc`; plain messages use `textContent` where practical.
- rationale: a hostile webpage can issue no-CORS requests to localhost, and stored plan/catalog/hardware text can cross an `innerHTML` boundary. Loopback binding alone prevents neither class of attack. Host+Origin+capability checks stop cross-site mutation and DNS rebinding; bounded JSON narrows the parser surface; contextual output encoding and CSP prevent stored script execution.

### DEC-039: Tier A records format-specific remote-header evidence, never loadability
- date: 2026-07-13 / status: accepted / triggered_by: public-release audit finding #6 / related: DEC-003, DEC-036 / docs_updated: modelark/verify.py, modelark/core/db.py, modelark/core/schema.sql, tests/test_verify_tier_a.py, tests/test_db_sqlite.py, README.md
- decision: Tier A is evidence-specific. For safetensors it requires a duplicate-free header, known dtypes, shapes whose byte lengths match their ranges, a contiguous non-overlapping layout covering the declared data region, and exact tensor-to-shard agreement when an index exists. Without an index, only a single file or a complete standard split filename sequence proves shard completeness. For GGUF, Tier A means only fixed-header sanity plus a complete standard split filename sequence; tensor metadata, offsets, shapes, and data bytes remain unchecked. Passing requires both the applicable structural check and proven shard completeness, and advances a discovered model to `inspected`, not `verified`; existing `verified` rows from the old Tier A behavior are migrated to `inspected`. Tier B remains unimplemented.
- rationale: a range-read header can provide useful evidence for models too large to download, but it cannot hash physical tensor bytes, prove a known architecture, or demonstrate that a runtime can load the checkpoint. The database, CLI, and README must preserve that boundary instead of converting partial evidence into a green loadability claim.

### DEC-040: Archive planning is the authoritative pickle-only acquisition gate
- date: 2026-07-13 / status: accepted / triggered_by: public-release audit finding #7 / related: DEF-011, DEC-037 / docs_updated: modelark/fetch.py, modelark/plan.py, modelark/restore.py, modelark/verifier.py, modelark/wishlist.py, modelark/discover.py, modelark/formats.py, modelark/cli.py, wishlist.yaml, modelark/default_wishlist.yaml, tests/test_archive_policy.py, README.md
- decision: `exclude.pickle_only` is enforced where the file plan is built. By default, a repository whose only supported weights are pickle is refused with an operator-visible policy error; unsupported weight formats are also refused instead of producing an auxiliary-files-only archive. An explicit opt-in stores pickle weights unchanged as inert raw bytes and never imports or executes them. Capacity planning mirrors the same gate. Restore bypasses the current acquisition choice only for already-archived pickle bytes, so tightening policy later cannot strand recoverable content. Inactive score, threshold, pattern, domain, pin, and trusted-quantizer keys are removed from shipped configuration rather than presented as working policy.
- rationale: classification alone is not a security gate, and a silent auxiliary-only plan creates a false archive. The boundary that actually decides which bytes enter storage must fail closed and be reflected in capacity estimates. Recovery is a separate trust decision: copying and hashing inert archived bytes is safe and must remain possible regardless of future acquisition settings. Opcode scanning and Hugging Face scan integration remain explicitly unimplemented.

### DEC-041: Public packaging is portable; catalog relationships are enforced at the database boundary
- date: 2026-07-13 / status: accepted / triggered_by: public-release audit release-engineering findings / related: DEC-024, DEC-035, DEC-036, DEC-039 / docs_updated: pyproject.toml, requirements.txt, README.md, catalog/export/README.md, modelark/core/schema.sql, modelark/core/db.py, scripts/migrate_duckdb_to_sqlite.py, tests/test_db_sqlite.py
- decision: make `pyproject.toml` the dependency and public-package authority. The normal runtime installs only direct application requirements; DuckDB moves to the `migration` extra, `zstandard` to the `zstd` extra, and build/test/browser tools remain in `dev`. `requirements.txt` is a portable compatibility install of the local project, not a workstation freeze. Package metadata declares the README, Apache-2.0 SPDX license, Auspex-Aerie author/contact, project URLs, alpha status, Python versions, and the currently supported Linux operator platform. The embedded StreamZNN module retains MIT; Auspex-Aerie's copyrightable contributions to the sanitized catalog export use Apache-2.0 while upstream identifiers/metadata and trademarks retain their source rights.
- decision: enable SQLite foreign-key enforcement on every connection and make durable relationships explicit. Files belong to models; selections and plan membership cascade with their parent; archived bytes restrict deletion of their file/model/drive provenance; replica and plan-drive rows cannot name unknown drives. Verification and fetch-event rows intentionally remain standalone because their CLIs may inspect or record a failed attempt for an uncatalogued repository. Local enums, copy counts, booleans, and non-negative sizes have CHECK/NOT NULL constraints, and at most one plan may be active. Because SQLite cannot add those clauses with ALTER TABLE, a pre-constraint catalog first receives a consistent, non-overwriting `pre-integrity-v1` backup, then is rebuilt beside the old tables in one transaction, checked for orphans before commit, and rolled back unchanged on any invalid row. Rediscovery preserves file rows already referenced by archive/replica records.
- rationale: a public install must not pull CUDA/Torch or a one-time database engine, and package consumers need machine-readable ownership, licensing, support, and project links. Application-side validation alone cannot prevent raw SQL, migrations, or future code from creating orphaned archive state. Failing a legacy migration with precise diagnostics is safer than silently deleting or inventing operator data; retaining archived file provenance is more important than making rediscovery an exact mirror of a mutable upstream repository.
- update (2026-07-13): A transitive-dependency audit corrected the no-Torch conclusion: upstream `zipnn==0.5.4` itself requires Torch/safetensors, and PyPI's Linux Torch resolution can include several gigabytes of CUDA/NVIDIA and Triton packages. DuckDB and the workstation freeze remain removed as decided. The temporary ZipNN footprint is accepted and documented because compression stays a hard product capability; `DEF-014` owns the future byte-only StreamZNN package split.

### DEC-042: Gate the legacy working-copy cutover on an operator-attended, rollback-ready migration
- date: 2026-07-13 / status: accepted / triggered_by: public-release closeout + operator requirement / related: DEC-024, DEC-035, DEC-041 / docs_updated: docs/decision_log.md, docs/roadmap.md
- decision: Build migration tooling, dry runs, fixtures, and the canonical release test entirely in `Auspex-Aerie/modelark` without reading, stopping, modifying, or reconfiguring the currently running legacy checkout. The live cutover is a separate operator-attended gate and begins only after the operator explicitly stops its fill. At that gate, identify the actual deployed database/version first; stop and verify all writers are gone; take non-overwriting, hash-manifested backups of the database and its sidecars plus configuration/service/library-map state; migrate a copy; install and smoke-test canonical ModelArk; then repoint the personal checkout's git origin to `Auspex-Aerie/modelark` and fast-forward/pull `main` only after its runtime state is protected. Retire the old remote only after functional, catalog, archive-location, and rollback checks pass.
- rationale: The legacy checkout is performing a real multi-terabyte fill, so a generic release task does not authorize downtime or mutation. SQLite WAL sidecars, possible DuckDB ancestry, namespace/layout changes, git-annex location state, and a remote replacement make an unattended “pull the new repo over it” unsafe. Separating tool construction from the attended cutover lets normal CI prove deterministic transformations while keeping the operator in control of the only steps that affect live state.
- impact: The closeout roadmap must distinguish canonical code/fixture validation from the human-gated final installation and migration. Migration commands default to inspection/dry-run, refuse active writers and in-place database conversion, emit a backup manifest, and provide explicit rollback instructions. No agent or automation performs the final stop, backup, migration, re-origin, pull, service start, or old-remote retirement without the operator present and approving that stage.
- update (2026-07-13): `scripts/migrate_legacy_runtime.py` and `tests/test_legacy_runtime_migration.py` implement the fixture-proven half of this decision for SQLite and optional DuckDB input. `docs/legacy-cutover.md` owns the human gates, canonical clean-install validation, unrelated-history directory-swap fallback, real restore acceptance test, service restart, and rollback. The live execution remains pending and operator-attended.

### DIS-003: Roadmap #30 was two resume guarantees collapsed into one label
- date: 2026-07-13 / status: observed / triggered_by: public-release closeout / related: DEC-019, INC-010, DEF-026 / docs_updated: README.md, docs/roadmap.md, modelark/fetch.py, tests/test_fetch_resume.py
- finding: No independent reproduction or specification survives for roadmap label #30. The code and incident record show two distinct guarantees: (1) completed files are durably keyed by `(repo_id, rfilename, drive_label)` and skipped after restart (DEC-019); (2) the interrupted file depends on transport-level partial resume. The first is implemented and now regression-tested. The second is known to restart from zero under `hf_xet` (INC-010) and remains explicitly deferred as DEF-026.
- implication: Close #30 as a stale duplicate label, remove absolute “no re-download” claims, and keep INC-010/DEF-026 visible. File-level transactional integrity remains a shipped guarantee; partial-file bandwidth preservation does not.

### DEC-043: Keep compression mandatory while deferring the lightweight StreamZNN package
- date: 2026-07-13 / status: accepted / triggered_by: public dependency-footprint review / related: DEC-021, DEC-041, DEF-014 / docs_updated: README.md, docs/decision_log.md
- decision: ZipNN compression remains a hard default ModelArk capability for the initial public release even though upstream `zipnn==0.5.4` currently brings Torch/safetensors and, on Linux PyPI, potentially several gigabytes of CUDA/NVIDIA/Triton packages. Do not make the product's compression/canary selling point an optional extra merely to hide that footprint. `DEF-014` remains active and owns a future normal `streamznn` wheel containing only the compatible byte-mode/native path ModelArk needs; the installer will consume that release normally rather than clone or arrange sibling repositories.
- compatibility_gate: Preserve restore support for `SZNN\x01` and legacy whole-blob `.znn`, pin and attribute upstream sources/notices, and require compatibility tests plus supported-Python Linux wheels before switching the dependency. The `streamznn` PyPI/GitHub names appeared unused when checked but remain unreserved until publication.
- revisit_when: after the initial public release when dependency-footprint work is prioritized, or sooner if the current 4–5 GB environment materially blocks adoption.

### DEC-044: Deployment is an unprivileged, explicit-path user service
- date: 2026-07-13 / status: accepted / triggered_by: final public-release/readme pass + operator request for the final-test deploy surface / related: DEC-023, DEC-035, DEC-042, DEF-025 / docs_updated: scripts/deploy.py, scripts/modelark.service, scripts/setup.sh, pyproject.toml, tests/test_deploy.py, README.md, docs/deployment.md, docs/legacy-cutover.md, docs/roadmap.md, contributing/contributions.md
- decision: Ship one minimal deployer from the canonical checkout. It creates or updates a checkout-local `.venv` with a normal non-editable package install, creates explicit private data/state directories, renders an idempotent `systemd --user` unit, and can run a read-only post-start CLI/service/loopback health check. Dry-run changes nothing. Enabling, starting, and `serve --resume` are independent explicit flags; the default unit cannot silently restart a multi-terabyte fill. The generated service contains resolved data/state/config paths rather than relying on the user manager to inherit shell XDG variables.
- boundary: The deployer never runs as root and does not install OS packages, edit sudoers, attach/mount/format storage, initialize drives, migrate data, enable lingering, or touch the legacy checkout. Those remain visible operator steps. `DEF-025` is partially resolved: user-service installation is automated; the privileged SMART sudoers helper remains deferred.
- rationale: The old `scripts/setup.sh` performed an editable install plus opportunistic apt mutation, while the committed unit was a root-installed system-service template with unconditional auto-resume—contradicting DEC-023 and making a public first run harder to audit. A small user-service surface is reproducible enough for the final canonical/cutover acceptance test without hiding privileged or archive-mutating behavior inside setup.

### INC-014: Completed protected first copies were reserved again, causing a false capacity stop
- date: 2026-07-14 / status: diagnosed; open (#9) / triggered_by: stopped fill appeared idle, then reported a capacity stop after refresh / related: DEC-019, DEC-032, DEC-034 / docs_updated: docs/decision_log.md
- root_cause: `plan_placements()` keeps a protected model in the incomplete pool while copy #2 is pending, but then reserves both copy #1 and copy #2. Live free space already reflects the completed first copy, so that copy is counted twice. Separately, the Fill page's live terminal map omits `plan-capacity-stop`; it falls back to the generic Start prompt until a refresh loads the persisted alert.
- impact: a safe but misleading stop—no archive data was lost, but the fill can refuse remaining work that fits and fail to explain the stop live.
- required_fix: reserve only missing copy work and classify `plan-capacity-stop` immediately in the live Fill surface.

### DEC-045: Derive missing archive work from durable facts; retain whole-plan Gate B admission for this release
- date: 2026-07-14 / status: accepted; Phase 1 shadow implementation complete, review pending / triggered_by: INC-014 + three-pass external architecture review / related: DEC-014, DEC-017, DEC-019, DEC-020, DEC-025, DEC-030, DEC-031, DEC-034, DEC-042 / docs_updated: docs/plans/reconciled-work-graph-capacity-ledger.md, docs/decision_log.md
- decision: Replace count-based placement inference with canonical manifests, exact per-drive copy facts, derived unassigned work intents, deterministic behavior-preserving `tiered_v1` placement, one capacity/workspace ledger, typed failures, and an idempotent executor. The work graph is never persisted as completion truth; restart reconciliation recreates it from `files`, `archived`, plan policy, and drive facts. Phases 1–2 run shadow-only before the executor changes.
- gate_b_contract: Gate B admission remains whole-plan for this release. A structurally infeasible committed set—including a replica tier too small for protected copy #2—blocks the entire fill before it starts, even when bulk copy #1 fits primary capacity. This does not govern runtime disruption: an otherwise valid source/target going offline follows DEF-022 and pauses resumably, while unexpected mid-run capacity exhaustion preserves completed bytes and stops resumably for re-plan. Partial continuation is deferred to a separately reviewed explicit mode with its own subset, consent, resume, and UI semantics.
- rationale: INC-014 came from treating an incomplete repository count as permission to reserve every copy again. Requirements, observed facts, placement, capacity, and execution need separate typed boundaries so satisfied work reserves nothing and crashes self-heal from durable evidence. Keeping pre-start admission conservative limits this incident fix to correcting feasibility rather than introducing an underspecified best-effort execution policy.
- rollout_boundary: Approval authorizes Phase 1 implementation in canonical ModelArk only. It does not authorize the operator-attended ModelDump migration/cutover, legacy catalog writes, repository visibility changes, or bypass of later phase review gates.
- implementation_update_2026_07_14: Phase 1 now provides the canonical manifest, exact copy facts and requirements, deterministic unassigned work intents, typed policy diagnostics, a safety-tested legacy comparison normalizer, and read-only `library plan --explain`. All 160 tests pass; the executor remains entirely legacy. Copied-catalog replay, release-host latency/lock evidence, and implementation review remain open gates before Phase 2.
- phase_1_review_2026_07_14: Phase 1 merged as PR #10 after CI passed on Python 3.10, Python 3.12, and the end-to-end job. Greptile scored the merged revision 4/5; its two non-architectural P2 findings were carried onto the Phase 2 branch with regression coverage rather than lost after the merge race.
- phase_2_update_2026_07_14: The shadow-only `tiered_v1` placement and capacity ledger, internal canonical modes, candidate/file budgets, typed failures, exact/estimated replica sizing, file-preflight API, and pre-write codec output caps are locally implemented. Read-only CLI and API diagnostics expose the result; the fill executor remains legacy. A disposable git-annex trace confirmed temp-directory rename publication for the current directory remote, while the conservative replica workspace bound remains pending review. Real-bf16 high-water evidence and copied-catalog/release-host replay remain hard gates before Phase 3.
- phase_2_review_2026_07_14: Phase 2 merged as PR #11 after its CI matrix passed and Greptile's three implementation P2s were fixed with regressions. Phase 3 gate preparation subsequently tightened diagnostic catalog connections from post-open `query_only` to filesystem-enforced SQLite URI `mode=ro` and added a sanitized, self-cleaning evidence collector. The real operator-approved shard and copied-catalog runs remain required before executor conversion.
- phase_3_gate_update_2026_07_14: The real-bf16 gate passed on an operator-approved 29.36 GB archived shard whose restored SHA-256 matched the canonical catalog hash. StreamZNN filesystem high-water was 19.47 GB against the 29.36 GB raw-plus-framing cap, with a complete round-trip hash pass. The copied-catalog release-host replay remains the sole empirical entry gate before executor adoption.
- phase_3_replay_update_2026_07_14: The consistent copied-catalog replay passed the production performance/locking gate: 271.724 ms p95 and 329.878 ms maximum over 20 graph-plus-ledger runs, versus the 500 ms budget, with concurrent reading proven on a disposable writer-held clone. The shadow-only legacy comparison was measured separately at 552.885 ms. Replay exposed no byte-capacity failures but did expose 54 root manifest-policy blockers (50 pickle-only under the safe default and four unsupported artifact repositories); Phase 3 executor adoption is paused for an explicit operator policy decision rather than silently skipping selected data.
