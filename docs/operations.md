# Operating ModelArk

This guide is the everyday reference for a current schema-v7 installation. For a new host, begin
with [Fresh install and first archive](getting-started.md). For any older catalog, stop and follow
[Upgrading ModelArk](upgrading.md) before starting the new binary.

## Runtime locations

Unless explicit global options override them, ModelArk uses:

| State | Default location |
|---|---|
| Catalog and runtime data | `~/.local/share/modelark` |
| Logs and execution state | `~/.local/state/modelark` |
| Configuration | `~/.config/modelark/wishlist.yaml` or the packaged default |
| Local portal | `http://127.0.0.1:8077` |
| Default git-annex map | `~/modelark-library` |

The deployer resolves and writes explicit paths into the user-service unit. Repeat the same
`--data-dir`, `--state-dir`, and `--config` arguments when checking or updating a non-default
deployment.

## Portal workflow

The portal is the recommended operator surface:

- **Plans** chooses the active storage and capacity context.
- **Catalog** curates the repositories in that context.
- **Drives** observes attached storage, guides registration, and reports identity/capacity evidence.
- **Library** shows reconciled archive state rather than inferred catalog intent.
- **Fill** authors and reviews one immutable proposal, then controls its separately started session.
- **Verify** rechecks selected physical copies and surfaces interrupted or fallback evidence.

Start a foreground portal with `.venv/bin/modelark serve`, or use the supervised service described
in [deployment.md](deployment.md).

## Common read and curation commands

```bash
.venv/bin/modelark import
.venv/bin/modelark discover --repo org/model
.venv/bin/modelark verify --all
.venv/bin/modelark ls
.venv/bin/modelark plan list
.venv/bin/modelark plan show --plan ark
.venv/bin/modelark library plan --json
.venv/bin/modelark drive list
.venv/bin/modelark export
```

Use `.venv/bin/modelark --help` and the relevant subcommand's `--help` for current arguments. Raw SQL
querying exists for expert diagnosis, but it is not a supported mutation surface.

Must-have repositories require a second copy:

```bash
.venv/bin/modelark protect --repo org/model
```

## Review, approve, and run Fill

Fill has three distinct gates:

1. The central plan must be feasible against current identity-bound capacity evidence.
2. The operator must review and approve the exact immutable proposal shown in the Fill portal.
3. The operator must start Fill separately.

The browser never supplies placement authority. Draft creation uses the active plan; approval
requires the backend-issued phrase and revalidates the stored proposal under drive/controller
fences. Approval alone performs no archive writes.

Changing selection, plan membership, capacity policy, or relevant execution configuration can make
an approval stale. Return to **Review exact placement** and approve the newly authored proposal; do
not reuse an earlier phrase or edit planner tables directly.

When an unavailable drive has been replaced by an identity-proven drive in the same plan, use
**Use a replacement drive** from the current approval docket. Choose the unavailable predecessor and
its replacement. The backend binds that request to the active plan and exact approved proposal; the
browser cannot supply a different baseline, lane size, or assignment.

This is a bounded successor replan. The replacement inherits a writable lane equal to the old
drive's policy-adjusted capacity, capped by the replacement's current admissible capacity evidence.
Existing reusable content still wins. Work outside the inherited lane keeps its approved target when
feasible, and the review table shows every changed target as `old → new`. Extra capacity on a larger
replacement is not consumed silently by this action.

Building the successor proposal does not invalidate or edit the current approval. Review the new
immutable docket and approve it separately; only that approval supersedes the old proposal and bumps
the planner revision. It still does not start Fill.

The Fill chart retains every drive identity in the active plan so loss history is not erased. A
`lost`, `retired`, or `excluded` drive remains visible with an unavailable marker and contributes no
executable capacity or target work. An ordinary empty card instead means the drive is still eligible
but the current proposal does not need it.

Start Fill only while an operator can respond to its attended drive-loading prompts. If nobody is
physically available, leave the current approval in place and Fill idle; approval alone moves no
bytes, and a service started without explicit resume does not begin the session.

Do not run two Fill controllers against one runtime. Stop a portal-owned worker before using any CLI
execution surface. Completed files are durable, but the in-flight file may need another download.

## Register or format storage

Prefer the Drives portal because it shows the observed identity, mount, access policy, planned
namespace transition, and typed refusal before mutation.

CLI registration of an already prepared device uses an explicit device and label:

```bash
.venv/bin/modelark drive register --dev /dev/sdX --label drive-07
```

Formatting is destructive and deliberately requires two commands. Review the first command's real
topology evidence before repeating the exact device path as confirmation:

```bash
.venv/bin/modelark drive register --dev /dev/sdX --label drive-07 --format ext4 --dry-run
.venv/bin/modelark drive register --dev /dev/sdX --label drive-07 --format ext4 \
  --confirm-format /dev/sdX
```

The command refuses regular files, root/system backing devices, mounted descendants, active swap,
encrypted/LVM/RAID stacks, and failed topology probes. It never unmounts a filesystem for you.

For an existing mounted filesystem, successful mounting is not write authority. The portal may ask
you to prepare the root ownership and mode for the effective service identity. Use only the exact
commands it renders, only on a dedicated filesystem, and never recursively. ModelArk itself does not
invoke `sudo` or create the archive namespace until the separate registration confirmation succeeds.

## Reconcile a dedicated drive

Use reconciliation to publish fresh identity-bound capacity evidence for storage exclusively owned
by ModelArk's supported writer:

```bash
.venv/bin/modelark drive reconcile drive-07 --dedicated
```

Reconciliation proves catalogued annex claims against the exact annex UUID, scans the archive
worktree without descending into Git metadata, reports bounded progress, and publishes only after a
fresh final identity/capacity observation. A missing claim or failed annex query refuses the clean
anchor.

Its `present / missing / debris / extra` counts are evidence, not a cleanup request. Debris and extras
remain on disk, are not adopted into catalogued residency, and are never deleted automatically. Do
not use `--dedicated` to promote shared or otherwise unfenceable storage.

## Verify archive copies

Remote-header verification and physical archive verification are different operations. Tier A
checks declared safetensors layout or basic GGUF header structure with range reads; it does not hash
all tensor bytes or prove a runtime can load the model.

The **Verify** portal view rechecks recorded mounted copies. Ingestion hashes and compression
canaries prove what ModelArk wrote; later physical verification proves what the medium can still
read. See [capacity-evidence.md](capacity-evidence.md) for the complete evidence vocabulary.

## Restore a usable model tree

Restore is the first-class read path:

```bash
.venv/bin/modelark restore --repo org/model --dest ./recovered
```

ModelArk selects a readable annex copy, retrieves dropped content when possible, reconstructs the
recorded Hugging Face paths in hidden staging, decompresses as required, and verifies every recorded
original-byte digest. It publishes `./recovered/org/model` only after the complete tree passes and
refuses to overwrite an existing model directory.

Catalog or location evidence alone is not a restore result. The final verified publication is the
handoff to local inference, training, packaging, or a later delivery system.

## Audit legacy restore-hash gaps

Older archives can lack original-byte digest evidence for small Git-tracked files. Audit a repository
read-only first:

```bash
.venv/bin/modelark repair-hashes --repo org/model
```

Apply mode is a separate operator decision. Stop Fill writers, retain the automatic catalog backup,
and review every candidate before using `--apply`; the command refuses contradictory evidence and
does not overwrite an existing backup.

## Configuration

`wishlist.yaml` controls discovery scope, archive policy, compression, and the rolling download cap.
An explicit global `--config` path wins, followed by the user config and packaged default. The
default cap is 1 TB per rolling 24 hours; raise it deliberately rather than using ModelArk as an
indiscriminate Hub mirror.

Eligible floating-point safetensors use whole-file ZipNN or StreamZNN according to the configured
memory gate. If streaming is disabled, oversized shards use optional zstd or verified raw fallback.
Quantized safetensors, GGUF, auxiliary files, and explicitly allowed pickle artifacts remain inert
raw bytes. ModelArk never imports archived pickle content.

## Service checks and logs

```bash
systemctl --user status modelark.service
.venv/bin/modelark-deploy --source . --check
journalctl --user -u modelark.service -n 200
```

Automatic Fill resume is disabled unless the installed unit was explicitly rendered with
`--resume-fill`. Treat enabling it as a separate operational decision after plan, drives, proposal,
and rollback state are reviewed.

## Things ModelArk intentionally does not do

- silently migrate an older catalog in place;
- listen beyond loopback without authentication;
- infer that a catalogued model is loadable;
- treat scratch, cache, or transport bytes as an archive replica;
- rewrite a registered drive identity from a USB-enclosure serial;
- adopt or delete unclaimed drive content during reconciliation;
- approve a proposal or start Fill on the operator's behalf.

For failure recovery and service replacement, use [deployment.md](deployment.md). For a schema
transition, use [upgrading.md](upgrading.md) before opening the old catalog with new code.
