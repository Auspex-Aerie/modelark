# Fresh install and first archive

This guide takes a new Linux installation from a clean checkout through an exact placement review
and, when deliberately selected, its first Fill. It does not cover upgrading an existing catalog;
use [Upgrading ModelArk](upgrading.md) for that path.

ModelArk is pre-1.0 and writes to real storage. Read each preview, keep an independent backup of
anything important, and never substitute a guessed device path for the one you inspected.

## 1. Install host prerequisites

ModelArk's supervised path requires Linux, Python 3.10 or newer, systemd, and git-annex:

```bash
sudo apt-get install -y git-annex smartmontools
```

`smartmontools` powers the optional Disk Health view. Install `open-iscsi` only for a NAS LUN and
`xfsprogs` only if you deliberately plan to format XFS storage.

The current ZipNN dependency can make the Python environment 4–5 GB because its upstream package
pulls Torch and may pull CUDA libraries. ModelArk itself does not require or move tensors onto a GPU.

## 2. Clone and install without starting work

```bash
git clone https://github.com/Auspex-Aerie/modelark.git
cd modelark
python3 scripts/deploy.py --dry-run
python3 scripts/deploy.py
```

The dry run prints the source, virtual environment, data, state, configuration, service-unit, and
auto-resume choices. The live install creates `.venv`, installs ModelArk, and writes an unprivileged
`systemd --user` unit. It does not enable the service, start the portal, prepare a drive, migrate a
catalog, approve a proposal, or start Fill.

For a CLI-only installation instead:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

Optional extras are explicit:

```bash
.venv/bin/pip install ".[zstd]"       # stream-off zstd fallback
.venv/bin/pip install ".[migration]"  # legacy DuckDB conversion only
```

## 3. Load the starter catalog

A fresh data directory begins empty. Import the packaged catalog locally—no token or network walk
is needed:

```bash
.venv/bin/modelark import
```

The import seeds roughly 4,100 classified repositories. To refresh or add current Hub metadata later:

```bash
.venv/bin/modelark discover --walk
.venv/bin/modelark discover --repo org/model
```

Public repositories need no Hugging Face login. For gated repositories or higher API limits:

```bash
.venv/bin/hf auth login
```

Catalog presence is metadata evidence, not proof that the artifacts were archived or that the model
can be loaded by a particular runtime.

## 4. Start the local portal

Start the installed user service without Fill auto-resume:

```bash
systemctl --user start modelark.service
.venv/bin/modelark-deploy --source . --check
```

Or run the portal directly for a foreground session:

```bash
.venv/bin/modelark serve
```

Open <http://127.0.0.1:8077>. ModelArk has no non-loopback mode because remote authentication is not
implemented.

## 5. Choose the plan and storage

In the portal:

1. Open **Plans** and explicitly select the bootstrapped `ark` plan, or create a separate plan.
2. Review its capacity mode. **Guaranteed** assumes no compression dividend; use it when the raw
   selected artifact set must fit. **Compression-aware** deliberately admits against expected stored
   size and can stop resumably if results run high.
3. Open **Drives** and review the detected device, filesystem, mount, and stable identity before any
   registration action.

For an existing empty mounted filesystem, the onboarding preview also checks that the unprivileged
service account can traverse and write the filesystem root. If it cannot, the portal renders exact
owner/mode commands for the observed mount and service identity. Use them only for storage dedicated
to ModelArk, never recursively, then refresh the preview. Do not create ModelArk's staging or final
archive directories by hand.

Device formatting is a separate destructive flow and is never inferred from registration. See
[Operating ModelArk](operations.md#register-or-format-storage) before using it.

If a migrated or previously populated drive shows unknown capacity evidence, reconcile it only after
proving that it is dedicated to ModelArk's supported writer:

```bash
.venv/bin/modelark drive reconcile drive-01 --dedicated
```

## 6. Curate and review

Use **Catalog** to select the repositories worth keeping. Marking a repository must-have currently
uses the CLI:

```bash
.venv/bin/modelark protect --repo org/model
```

Review **Library** and **Fill** after the plan is feasible. The Fill page exposes one exact placement
workflow:

1. Select **Review exact placement**.
2. Inspect the stored revision, canonical seal, totals, drives, and every requirement assignment.
3. Type the backend-issued `APPROVE <proposal-id>` phrase exactly.
4. Select **Approve exact placement**.

Approval does not move bytes and does not start Fill. Any later selection, plan, drive-evidence, or
execution-policy change makes the approval stale and requires a new review.

## 7. Start Fill separately

Only after the approved proposal is current does **Start Fill** become available. Starting Fill can
download and write many terabytes, so confirm the selected repositories, daily bandwidth cap, drive
schedule, and rollback expectations before selecting it.

Keep the portal or service logs visible for the first run:

```bash
journalctl --user -u modelark.service -f
```

ModelArk checkpoints completed files durably. An interrupted `hf_xet` shard may still restart that
one shard from zero, so large models benefit from a stable network connection.

## Next guides

- [Everyday operations, verification, and restore](operations.md)
- [Supervised deployment, service updates, and rollback](deployment.md)
- [Upgrade an existing catalog](upgrading.md)
- [Evidence and capacity semantics](capacity-evidence.md)
