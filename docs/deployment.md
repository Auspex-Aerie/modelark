# Deploying ModelArk

ModelArk's supported supervised deployment is intentionally small: a normal package install in a
checkout-local virtual environment plus a `systemd --user` service. The deployer is unprivileged and
does not install operating-system packages, edit sudoers, prepare drives, attach storage, migrate a
catalog, or start archive work unless the operator explicitly asks for resume behavior.

## Prerequisites

ModelArk currently supports the supervised deployment on Linux with Python 3.10+ and systemd. Install
the host tools separately so every privileged change remains visible:

```bash
sudo apt-get install -y git-annex smartmontools
# Optional: open-iscsi for a NAS LUN; xfsprogs only when formatting XFS drives.
```

The ZipNN dependency currently makes the Python environment large—typically 4–5 GB on Linux because
upstream pulls Torch and may pull CUDA/NVIDIA packages. This is tracked by `DEF-014`; ModelArk itself
does not require a GPU.

## Install and inspect

From a clean canonical checkout:

```bash
python3 scripts/deploy.py --dry-run
python3 scripts/deploy.py
```

The second command:

- creates `.venv` when absent and runs a non-editable `pip install` from the checkout;
- creates the explicit data and state directories with mode `0700`;
- writes `~/.config/systemd/user/modelark.service` with mode `0600`;
- runs `systemctl --user daemon-reload`;
- does **not** enable or start the service.

Defaults match the application defaults:

- data: `$XDG_DATA_HOME/modelark` or `~/.local/share/modelark`;
- state/logs: `$XDG_STATE_HOME/modelark` or `~/.local/state/modelark`;
- config: `$XDG_CONFIG_HOME/modelark/wishlist.yaml` when present, otherwise the packaged default;
- portal: `http://127.0.0.1:8077`.

Use explicit paths for a migration or non-default deployment:

```bash
python3 scripts/deploy.py \
  --data-dir /path/to/modelark-data \
  --state-dir /path/to/modelark-state \
  --config /path/to/wishlist.yaml
```

The generated unit contains those resolved paths. It does not depend on a service manager inheriting
interactive-shell XDG variables.

If the checkout contains `catalog/catalog.sqlite` or `catalog/catalog.duckdb` while the selected data
directory has no migrated SQLite catalog, a dry run prints a loud warning and the full preview; live
deployment stops before creating the venv or unit. This
prevents a non-editable install from silently starting with an empty catalog beside ignored legacy
state. Point `--data-dir` at existing SQLite state or complete the attended migration first.

## Enable and start

Start only the loopback portal:

```bash
systemctl --user start modelark.service
```

Or explicitly enable and start it during deployment:

```bash
python3 scripts/deploy.py --enable --start
```

`--enable` starts the service with the user's systemd session on future logins. Starting it before
login requires systemd lingering, which is a separate host-policy choice
(`loginctl enable-linger $USER`) and is not changed by the deployer.

Automatic fill resume is deliberately off by default. Enable it only when the catalog, plan, drives,
and rollback state have already been validated:

```bash
python3 scripts/deploy.py --resume-fill --enable --start
```

That flag changes the unit to invoke `modelark serve --resume`; starting the service may therefore
continue large downloads. Rerun the deployer with the same flag when updating a resume-enabled unit.

## Acceptance check

After the service starts, run the deployer's read-only acceptance check:

```bash
.venv/bin/modelark-deploy --source "$PWD" --check
```

It verifies the installed CLI, active user service, and the loopback `/api/meta` response. It does not
start a fill, touch drives, or modify the catalog. For a non-default deployment, repeat the same
`--data-dir`, `--state-dir`, and `--config` arguments so the check also proves the unit points at the
intended runtime. Then inspect the service and persistent logs:

```bash
systemctl --user status modelark.service
journalctl --user -u modelark.service -n 200
```

For an existing archive, the complete release acceptance also includes catalog/foreign-key checks,
annex-location checks, and one verified restore. Follow the operator-attended
[`legacy-cutover.md`](legacy-cutover.md) runbook; the deploy health check is not a substitute for
archive recovery proof.

## Updating or removing the service

Before updating, check [`upgrading.md`](upgrading.md). A fresh install or already-current catalog can
be redeployed normally. An existing pre-v7 catalog—including ModelArk 0.2.0's schema-v2 catalog—must
complete the stopped, side-by-side provenance migration before the new service starts; the new binary
will refuse rather than auto-migrate it.

After pulling a reviewed release with no pending catalog migration, rerun `python3 scripts/deploy.py`
with the same explicit paths and resume choice, review the generated unit, then add `--start` to
restart onto the new package.

To remove supervision without deleting data:

```bash
systemctl --user disable --now modelark.service
rm ~/.config/systemd/user/modelark.service
systemctl --user daemon-reload
```

The virtual environment, data, state, configuration, archive drives, and git-annex map are
intentionally left untouched. Delete or migrate them only as separate, explicit operations.

## Dedicated drive permissions

The supervised service runs as the ordinary user who deployed it. Before portal registration can
initialize an existing mounted filesystem, the read-only onboarding preview proves that this service
identity can traverse and write the filesystem root. A freshly created ext4 filesystem is commonly
mounted with a root-owned top-level directory; being mounted and empty does not by itself make it
registration-ready.

When access is blocked, the portal shows **You do this now** with commands resolved to the observed
absolute mount and effective service user/group. The rendered sequence has this shape:

```bash
sudo chown -- <service-user>:<service-group> /absolute/dedicated/mount
sudo chmod -- 0750 /absolute/dedicated/mount
stat -c '%U:%G %a %n' -- /absolute/dedicated/mount
```

Copy the portal's concrete commands rather than substituting a guessed account or path. This policy
is only for a filesystem dedicated to ModelArk. Never apply either command recursively, and do not
change the root owner of a shared filesystem without a separate host-level design. ModelArk does not
invoke `sudo`, change permissions, or use successful mounting as authority to write.

After running the commands, refresh the onboarding preview. Do not retry a stale confirmation modal
and do not create these directories manually:

```text
/absolute/dedicated/mount/
├── .modelark.registering-drive-NN/  temporary; ModelArk creates this
└── modelark/                        final; atomically promoted from the temporary directory
```

The hidden directory is the recoverable preparation area. The final archive namespace appears only
through ModelArk's atomic promotion during the separately confirmed registration action.

## Drive identity and reconciliation

The passive Drives view enriches an attached disk with read-only mounted-volume topology. An exact,
unique filesystem UUID + annex UUID pair maps it to the registered identity even when an iSCSI layer
has no stored hardware serial or a USB bridge reports its own serial. ModelArk displays that
discrepancy and leaves the registered serial untouched. If a complete observed pair conflicts with
the registered pair, serial cannot override it and no identity is rebound.

After migration or new registration, establish capacity evidence explicitly for storage that is
actually dedicated to the supported ModelArk writer:

```bash
modelark drive reconcile drive-NN --dedicated
```

The command now prints bounded inventory milestones. It queries the git-annex location log once for
all keys recorded on the exact target annex UUID, treats a command failure or absent key as missing,
and scans only the archive worktree—not `.git/annex/objects`—for extras/debris. A successful command
returns that exact classified inventory and prints bounded `present / missing / debris / extra`
counts. It never turns debris or extras into catalogued residency and never deletes them
automatically. Reconciliation remains under the controller and drive fences and publishes only from
a fresh final identity/capacity observation. Do not use `--dedicated` for shared or otherwise
unfenceable storage.

## SMART access

The deployer never edits sudoers. If the Disk Health view should read SMART data, grant only the
documented `smartctl` command to the service user and verify it independently:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/smartctl" | sudo tee /etc/sudoers.d/modelark-smartctl
sudo chmod 440 /etc/sudoers.d/modelark-smartctl
sudo -n /usr/sbin/smartctl --version
```

Confirm the `smartctl` path with `command -v smartctl` on distributions that install it elsewhere.
Never run the portal itself as root.
