# Provenance migrate — copy-only runbook

Use this in a later session to **practice catalog cutover on a disposable copy**. It is not a PR
handoff, not Fill, and not `modelark-migrate` (legacy ModelDump cutover; see
[`legacy-cutover.md`](legacy-cutover.md)).

**Never point this tool at the live / XDG catalog.** Source, dest, and work are all copies or scratch.

Frozen CLI: `modelark-provenance-migrate` (`scripts/migrate_provenance.py`). Leftovers list frozen at
`b8895d2`. Branch when this was written: `fix/placement-capacity-pr10-content-satisfaction`.

## What this tool is

Attended **schema/provenance cutover** of a catalog directory:

1. **Rehearse** — clone the source catalog into a work folder. Source is not written.
2. **Publish** — write a new `catalog.sqlite` into an empty dest folder (fd-link, no dest clobber).
3. **Leftovers** — read-only list of leftover staging files next to dest (no delete).

Fill, portal, `adopt_current`, and drive work are out of scope here.

## Three directories

| Folder | Flag | What it is |
|--------|------|------------|
| **Source copy** | `--source-data-dir` | Directory that **contains** `catalog.sqlite` (the copy you made). Rehearse only. |
| **Work** | `--work-dir` | Scratch for this run: clone, `report.json`, leftover record. Disposable. |
| **Dest** | `--dest-dir` | Empty directory that will receive the published catalog. |

Publish refuses dest that is the same folder as the rehearsal source (including via symlink).

## Preconditions

- Checkout this branch (or later) with `.venv` / `.venv-dev` as usual.
- **Copy** a catalog data dir to a path you control. Do not use the live tree as source or dest.
- Stop any process that might write the **copy** (portal/Fill against that copy). The live catalog can stay up if you never pointed at it.
- Dest must not already contain `catalog.sqlite`.
- `--confirm-stopped` on publish must be exactly `MODELARK-STOPPED` (no extra spaces).

Example layout (names are yours to choose):

```text
/tmp/modelark-copy-run/
  source-copy/          # cp -a of a catalog data dir; has catalog.sqlite
  work/                 # empty; tool fills this
  dest/                 # empty until publish
```

Copy example (adjust the from-path; do **not** use live XDG):

```bash
mkdir -p /tmp/modelark-copy-run
cp -a /path/to/YOUR/CATALOG-COPY /tmp/modelark-copy-run/source-copy
mkdir /tmp/modelark-copy-run/work /tmp/modelark-copy-run/dest
```

## Commands

Use the installed script from the project venv (or `python -m` equivalent if that is how this
checkout is invoked):

```bash
cd /home/phaze/PycharmProjects/modelark   # or this clone
.venv/bin/modelark-provenance-migrate --help
```

If the console script is not on PATH inside the venv, run:

```bash
.venv/bin/python -m scripts.migrate_provenance --help
```

### 1. Rehearse (safe)

```bash
.venv/bin/modelark-provenance-migrate rehearse \
  --source-data-dir /tmp/modelark-copy-run/source-copy \
  --work-dir /tmp/modelark-copy-run/work \
  --run-id copy-1
```

Expect JSON on stdout (`status` ok / rehearsal report). Work will contain a run dir (here `copy-1` or
`pub` depending on layout) with clone + `report.json`. Source copy must be unchanged.

Do **not** pass `--confirm-stopped` to rehearse (parser error).

### 2. Publish (writes dest only)

```bash
.venv/bin/modelark-provenance-migrate publish \
  --work-dir /tmp/modelark-copy-run/work \
  --dest-dir /tmp/modelark-copy-run/dest \
  --confirm-stopped MODELARK-STOPPED
```

Expect dest/`catalog.sqlite`. Exit `1` and `migration refused:` on stderr if dest is occupied, leftover
staging is already present, dest is the source folder, or free space is too low.

A **failed** publish can leave dest empty plus one leftover file (see below). Retry stays blocked until
that leftover is gone.

### 3. List leftovers (read-only)

```bash
.venv/bin/modelark-provenance-migrate leftovers \
  --work-dir /tmp/modelark-copy-run/work \
  --dest-dir /tmp/modelark-copy-run/dest
```

JSON has `recorded` (from work) and `live` (lstat of dest names only). There is **no** dispose
subcommand. Do not glob-delete.

`live` always names five dest paths:

- `catalog.sqlite`
- `.catalog.sqlite.publish-staging`
- `.catalog.sqlite.publish-staging-wal`
- `.catalog.sqlite.publish-staging-shm`
- `.catalog.sqlite.publish-staging-journal`

Useful fields on a present leftover:

| Field | Meaning |
|-------|---------|
| `present` / `missing` | There now vs gone |
| `dest_relation` | `same_attempt_inode` = extra **name** for dest (not extra disk). `absent` = dest catalog not there. `different_inode` = extra **copy**. |
| `allocated_bytes_estimate` | `st_blocks * 512` (estimate) |
| `st_nlink` | `2` with dest present usually means dest + leftover share one inode |
| `unbound` on recorded members | Path is not one of the five dest names; list did not follow it |

## Leftover on disk (you delete, after looking)

Reserved leftover name:

```text
DEST/.catalog.sqlite.publish-staging
```

(and optional `-wal` / `-shm` / `-journal` next to it)

**When dest is quiet** (nothing else using that dest folder):

- `dest_relation=same_attempt_inode` and dest `catalog.sqlite` exists: leftover is a second name for the
  published catalog. Removing the leftover **name** does not remove dest if dest still exists.
- dest missing and leftover present (`nlink=1`): failed publish extra copy. Removing it unblocks retry.
- dest missing and `nlink>1`: another name may be the catalog (renamed dest). **Do not delete.**

There is no safe automated unlink in this CLI. Check `leftovers` JSON, then `ls -li` dest, then remove
only the leftover **name** you intend.

## What success looks like

- Source copy still has its original `catalog.sqlite` (bytes/mtime you noted).
- Dest has `catalog.sqlite` and can be opened as SQLite on the **copy** (optional sanity: `.tables`).
- `leftovers` shows dest present; leftover either missing or `same_attempt_inode`.
- You did not set XDG, `DB_PATH`, or `db.configure` / `db.connect`.
- You did not run Fill or `adopt_current`.

## What to do if publish refuses

| Symptom | Likely cause |
|---------|----------------|
| dest already exists | Dest was not empty; use a new dest or inspect first |
| leftover staging pathname present | Previous failed publish; list leftovers, inspect, maybe remove leftover name |
| dest free … below catalog size estimate | Dest filesystem too small |
| source and destination are the same | Dest path is the source copy; pick a different dest |
| slot-state symlink / run-dir symlink | Work tree was tampered; use a fresh work dir |

Re-run **leftovers**, then **publish** again only after dest is still empty (or you accept dest occupancy)
and any blocking leftover is gone.

## Out of scope (do not do in that session unless newly authorized)

- Live / XDG catalog as source or dest
- Fill, portal, drives
- `adopt_current` / DEF-036 approve-before-Fill
- Auto-delete leftovers (DEF-037)
- `modelark-migrate` legacy cutover
- Tagging Greptile / merging PR #55
