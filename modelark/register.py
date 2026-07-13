"""Drive & library registration — the git-annex archive topology.

Topology (DEC-006): a central git-annex **map** repo tracks *what* is archived
and *where* (symlinks + git-annex location log; bytes never touch git). Each
registered drive is a **clone of the map** that physically holds content, wired
as a fleet remote so `git annex whereis / numcopies / fsck / move` work across
the whole fleet, and so a shelved drive is self-describing when re-plugged. The
NAS joins later as a `directory` special remote. The SQLite `drives` table is the
queryable offline mirror of this.

`drive register` qualifies a drive (SMART baseline → health verdict), optionally
reformats it, clones the map onto it, wires it as a remote, and records it. The
fetch pipeline then adds content directly on the drive (no scratch transit) and
`git annex sync` propagates location back to the map.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from modelark.core import db
from modelark.core import platform as osplat

DEFAULT_LIBRARY = Path.home() / "modelark-library"
ARCHIVE_SUBDIR = "modelark"          # content lives under <mount>/modelark


# ---- subprocess helpers -----------------------------------------------------

def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(list(args), capture_output=True, text=True)
    except FileNotFoundError:
        if check:                       # a genuinely-needed tool (git/git-annex) is missing
            raise
        # a Linux-only probe (lsblk/blkid/findmnt) on another OS — degrade to empty
        return subprocess.CompletedProcess(args, 127, "", f"{args[0]}: not found")
    if check and p.returncode != 0:     # surface the tool's OWN stderr, not a bare exit code
        raise RuntimeError(f"`{' '.join(args)}` failed (exit {p.returncode}):\n"
                           f"{(p.stderr or p.stdout).strip()[:1000]}")
    return p


def _sudo(*args: str) -> list[str]:
    """Elevate a single privileged command. The register process itself stays the
    invoking user — so the git-annex map/clone and the SQLite catalog stay
    user-owned (no root-owned objects, no git 'dubious ownership' refusals) — and
    only the hardware operations (SMART read, mkfs, mount) run as root."""
    return ["sudo", *args] if not osplat.is_root() else list(args)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return _run("git", "-C", str(repo), *args, check=check).stdout.strip()


def _is_annex(repo: Path) -> bool:
    return (repo / ".git").exists() and _run(
        "git", "-C", str(repo), "annex", "version", check=False).returncode == 0


# ---- the central "map" repo -------------------------------------------------

def library_root() -> Path:
    config = db.CATALOG_DIR / "library.json"
    if config.exists():
        return Path(json.loads(config.read_text())["library_root"]).expanduser()
    return DEFAULT_LIBRARY


def _save_library_root(path: Path) -> None:
    db.CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    (db.CATALOG_DIR / "library.json").write_text(json.dumps({"library_root": str(path)}, indent=2) + "\n")


def ensure_library(path: Path | None = None) -> Path:
    """Create the central git-annex map repo if absent. Idempotent."""
    path = (path or library_root()).expanduser()
    if _is_annex(path):
        _save_library_root(path)
        return path
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        _git(path, "init", "-q")
    _git(path, "annex", "init", "map")
    _git(path, "annex", "numcopies", "1")     # fleet default; irreplaceables bumped selectively
    # Seed an initial commit so drive clones check out a branch cleanly (cloning an
    # empty repo leaves the clone with no HEAD → sync/checkout breaks).
    (path / ".gitattributes").write_text("* annex.largefiles=anything\n")
    (path / "README.md").write_text(
        "# ModelArk library map\n\n"
        "git-annex map: symlinks + location log. Model bytes live on registered "
        "drives / the NAS, never in git.\n")
    _git(path, "add", ".gitattributes", "README.md")
    _git(path, "commit", "-qm", "init modelark library map")
    _save_library_root(path)
    return path


# ---- SMART qualification ----------------------------------------------------

def _parent_disk(dev: str) -> str:
    """Whole-disk node for SMART (strip a trailing partition number)."""
    if re.match(r"^/dev/nvme\d+n\d+p\d+$", dev):
        return re.sub(r"p\d+$", "", dev)
    return re.sub(r"\d+$", "", dev) if re.match(r"^/dev/sd[a-z]+\d+$", dev) else dev


def _smartctl(disk: str, *args: str) -> str:
    r = _run(*_sudo("smartctl", *args, "-d", "sat", disk), check=False)
    if r.returncode & 0b11:            # bit0 = cmdline error, bit1 = device open failed
        raise RuntimeError(
            f"smartctl could not read {disk} (exit {r.returncode}). Ensure sudo works "
            f"in this shell (register elevates smartctl/mkfs/mount), and apply the UAS "
            f"quirk first if it's a USB bridge.\n{(r.stdout + r.stderr)[:400]}")
    return r.stdout


def smart_baseline(dev: str) -> dict:
    """Read a SMART baseline and derive an ok|watch|reject verdict. Raises loudly
    if SMART is unreadable — no silent 'unknown'. On non-Linux (no smartctl/UAS
    story yet, DEC-008) SMART is skipped: verdict 'unchecked', with a note to
    health-check the drive with the OS's own tool first."""
    if not osplat.SMART_SUPPORTED:
        return {"model": "", "serial": "", "smart_passed": None, "reallocated": 0,
                "pending": 0, "offline_uncorrectable": 0, "power_on_hours": 0,
                "verdict": "unchecked",
                "note": f"SMART not read on {osplat.OS_LABEL} — health-check this drive "
                        f"with your platform's own tool before trusting it with archives"}
    disk = _parent_disk(dev)
    info = _smartctl(disk, "-i", "-H")
    attrs = _smartctl(disk, "-A")
    model = serial = ""
    passed = None
    for line in info.splitlines():
        if line.startswith("Device Model:"):
            model = line.split(":", 1)[1].strip()
        elif line.startswith("Serial Number:"):
            serial = line.split(":", 1)[1].strip()
        elif "overall-health" in line:
            passed = line.strip().endswith("PASSED")
    raw: dict[str, int] = {}
    for line in attrs.splitlines():
        parts = line.split()
        if len(parts) >= 10 and parts[0].isdigit():
            try:
                raw[parts[1]] = int(parts[9])
            except ValueError:
                pass
    realloc = raw.get("Reallocated_Sector_Ct", 0)
    pending = raw.get("Current_Pending_Sector", 0)
    offline = raw.get("Offline_Uncorrectable", 0)
    hours = raw.get("Power_On_Hours", 0)
    if realloc >= 100 or pending > 0 or offline > 0:
        verdict = "reject"
    elif realloc > 0:
        verdict = "watch"
    else:
        verdict = "ok"
    return {"model": model, "serial": serial, "smart_passed": passed,
            "reallocated": realloc, "pending": pending,
            "offline_uncorrectable": offline, "power_on_hours": hours,
            "verdict": verdict}


# ---- block-device helpers ---------------------------------------------------

def _mountpoint(dev: str) -> str | None:
    mp = _run("lsblk", "-nro", "MOUNTPOINT", dev, check=False).stdout.strip()
    return mp.splitlines()[0] if mp else None


def _fs_uuid(dev: str) -> str:
    return _run("lsblk", "-dno", "UUID", dev, check=False).stdout.strip()


def _transport(dev: str) -> str:
    out = _run("lsblk", "-dno", "TRAN", dev, check=False).stdout.strip()
    return out.splitlines()[0] if out else ""


def _raid_baseline(dev: str) -> dict:
    """Baseline for a RAID-backed LUN (iSCSI/NAS): no physical SMART to read — redundancy
    lives in the array, integrity in our sha256 + ZipNN canary. Verdict 'raid' (not reject)."""
    model = _run("lsblk", "-dno", "MODEL", dev, check=False).stdout.strip().splitlines()
    return {"model": (model[0].strip() if model else "") or "RAID/iSCSI LUN", "serial": "",
            "smart_passed": None, "reallocated": 0, "pending": 0, "offline_uncorrectable": 0,
            "power_on_hours": 0, "verdict": "raid",
            "note": "RAID-backed LUN (iSCSI) — no physical SMART; integrity via the array + sha256/canary"}


def _unchecked_baseline(dev: str, reason: str) -> dict:
    """Baseline for a drive whose SMART can't be trusted/read — a USB bridge that won't pass SMART
    (INC-002: 'scsi error device will be ready soon'), or an explicit --skip-smart. Verdict 'unchecked'
    (NOT reject): the drive registers but is flagged health-unverified; vet it with an external tool or
    a write-surface pass (DEF-003 / task #19). Mirrors the DEC-008 non-Linux 'unchecked' path."""
    model = _run("lsblk", "-dno", "MODEL", dev, check=False).stdout.strip().splitlines()
    return {"model": (model[0].strip() if model else ""), "serial": "",
            "smart_passed": None, "reallocated": 0, "pending": 0, "offline_uncorrectable": 0,
            "power_on_hours": 0, "verdict": "unchecked", "note": reason}


def _disk_bytes(dev: str) -> int:
    out = _run("lsblk", "-bdno", "SIZE", dev, check=False).stdout.strip()
    return int(out.splitlines()[0]) if out else 0


def _mkfs(dev: str, fs: str, label: str) -> None:
    root_src = _run("findmnt", "-nro", "SOURCE", "/", check=False).stdout.strip()
    if _parent_disk(dev) == _parent_disk(root_src):
        raise RuntimeError(f"refusing to format {dev}: it is the system/root disk.")
    # Unmount anything mounted from this disk or its partitions (e.g. a desktop
    # auto-mount of the drive's previous filesystem), then wipe old signatures so
    # mkfs doesn't stop on an "existing filesystem, proceed?" prompt.
    lb = _run("lsblk", "-nro", "NAME,MOUNTPOINT", _parent_disk(dev), check=False).stdout
    for line in lb.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip():
            _run(*_sudo("umount", parts[1].strip()), check=False)
    _run(*_sudo("wipefs", "-a", dev), check=False)
    if fs == "ext4":
        # -m 0: no root-reserved blocks — this is a write-once archive volume, not a
        # system disk, so the default 5% reserve (~365 GB on an 8 TB drive) is pure waste.
        # -E nodiscard: skip the whole-device TRIM. On a network/thick LUN (iSCSI) that
        # discard is translated to the array and can hang for HOURS on a multi-TB volume
        # (observed: an overnight mkfs on the 5.3 TB NAS LUN). The fs is correct without it.
        _run(*_sudo("mkfs.ext4", "-q", "-F", "-m", "0", "-E", "nodiscard", "-L", label[:16], dev))
    elif fs == "xfs":
        _run(*_sudo("mkfs.xfs", "-q", "-f", "-K", "-L", label[:12], dev))    # -K: skip discard (see ext4 note)
    else:
        raise ValueError(f"unsupported fs: {fs}")


def _mount(dev: str, label: str) -> str:
    mp = f"/mnt/{label}"
    _run(*_sudo("mkdir", "-p", mp))
    _run(*_sudo("mount", dev, mp))
    _run(*_sudo("chown", f"{os.getuid()}:{os.getgid()}", mp))   # so the clone/adds run as us
    return mp


# ---- registration -----------------------------------------------------------

def _add_to_active_plan(con, label: str) -> str:
    """#34: fold a freshly-registered drive into the ACTIVE plan's drive set — the plan's drive set IS
    the registered fleet IS the only capacity that exists, so this is what keeps the capacity model
    honest as the fleet grows. Idempotent (stable drive-NN key re-adds to the same row, DEC-018);
    bootstraps `ark` if no plan is active yet. Lazy import: plan imports register → avoid a cycle."""
    from modelark import plan
    ap = plan.active(con) or plan.bootstrap(con)
    plan.add_drive(con, ap["plan_id"], label)
    return ap["plan_id"]


def register_drive(dev: str, label: str, mount: str | None = None,
                   format_fs: str | None = None, location: str | None = None,
                   library: str | None = None, dry_run: bool = False,
                   role: str = "primary", raid_backed: bool = False,
                   skip_smart: bool = False) -> dict:
    """Qualify, prepare, and register a drive as a fleet member. A RAID-backed LUN
    (iSCSI — auto-detected — or forced with raid_backed=True) has no physical SMART:
    redundancy is the array's, integrity is our sha256 + canary, so SMART is skipped.
    `skip_smart` (INC-002) registers a drive whose USB bridge won't pass SMART with an
    'unchecked' verdict — an explicit operator override; verify health externally."""
    if not raid_backed and _transport(dev) == "iscsi":
        raid_backed = True
    if raid_backed:
        base = _raid_baseline(dev)
    elif skip_smart:
        base = _unchecked_baseline(dev, "SMART skipped via --skip-smart (USB bridge won't pass SMART / "
                                        "INC-002) — verify health externally before trusting archives")
    else:
        base = smart_baseline(dev)
    plan = {"dev": dev, "label": label, "smart": base,
            "format": format_fs, "mount": mount}
    if dry_run:
        return plan

    if base["verdict"] == "reject":
        raise RuntimeError(
            f"{dev} failed SMART qualification ({base['model']} {base['serial']}): "
            f"reallocated={base['reallocated']} pending={base['pending']} "
            f"offline_uncorrectable={base['offline_uncorrectable']}. Not registering.")

    if format_fs:
        if not osplat.BLOCKDEV_OPS_SUPPORTED:
            raise RuntimeError(
                f"--format isn't supported on {osplat.OS_LABEL}. Pre-format the drive "
                f"(e.g. NTFS via Disk Management), then register by its mount path with "
                f"--mount <drive path>.")
        _mkfs(dev, format_fs, label)

    if osplat.BLOCKDEV_OPS_SUPPORTED:
        mp = mount or _mountpoint(dev) or _mount(dev, label)
    else:
        mp = mount            # off-Linux: caller supplies the already-mounted path
    if not mp:
        raise RuntimeError(
            f"no mount point for {dev}. On {osplat.OS_LABEL}, mount the drive first and "
            f"pass its path with --mount (e.g. --mount E:\\).")
    archive = Path(mp) / ARCHIVE_SUBDIR

    lib = ensure_library(Path(library).expanduser() if library else None)
    if not _is_annex(archive):
        archive.parent.mkdir(parents=True, exist_ok=True)
        _run("git", "clone", str(lib), str(archive))
        _git(archive, "annex", "init", label)
    # #14: a self-describing annex description (shown in `git annex whereis`/`info`), refreshed on every
    # (re-)registration so a shelved drive announces WHAT it is when re-plugged — not just its label.
    desc = f"{label} · {base['model'] or 'drive'} · {role}" + (" · RAID" if raid_backed else "")
    _git(archive, "annex", "describe", "here", desc, check=False)

    annex_uuid = _git(archive, "config", "annex.uuid", check=False)
    remotes = _git(lib, "remote", check=False).splitlines()
    if label in remotes:
        _git(lib, "remote", "set-url", label, str(archive))
    else:
        _git(lib, "remote", "add", label, str(archive))
    _git(lib, "annex", "sync", label, check=False)      # exchange location logs

    du = shutil.disk_usage(mp)
    con = db.connect()
    try:
        db.upsert(con, "drives", {
            "drive_label": label,
            "fs_uuid": _fs_uuid(dev) or None,
            "annex_uuid": annex_uuid or None,
            "capacity_bytes": _disk_bytes(dev) or du.total,
            "free_bytes": du.free,
            "hw_model": base["model"] or None,
            "serial": base["serial"] or None,
            "physical_location": location,
            "role": role,
            "raid_backed": raid_backed,
            "health": base["verdict"],
            "last_seen": datetime.now(),
            "notes": base.get("note") or (
                f"SMART baseline: realloc={base['reallocated']} "
                f"pending={base['pending']} offline_unc={base['offline_uncorrectable']} "
                f"poh={base['power_on_hours']}h passed={base['smart_passed']}"),
        }, pk=["drive_label"])
        plan_id = _add_to_active_plan(con, label)       # #34: the drive joins the active plan's fixed set
    finally:
        con.close()

    return {"label": label, "archive": str(archive), "annex_uuid": annex_uuid,
            "health": base["verdict"], "model": base["model"], "serial": base["serial"],
            "library": str(lib), "plan": plan_id}


def archive_path(con, label: str) -> Path | None:
    """Resolve a registered drive label to its on-disk archive dir (if mounted)."""
    row = con.execute("SELECT fs_uuid FROM drives WHERE drive_label = ?", [label]).fetchone()
    if not row or not row[0]:
        return None
    by_uuid = Path(f"/dev/disk/by-uuid/{row[0]}")          # resolves without root
    dev = str(by_uuid.resolve()) if by_uuid.exists() else _run("blkid", "-U", row[0], check=False).stdout.strip()
    mp = _mountpoint(dev) if dev else None
    return Path(mp) / ARCHIVE_SUBDIR if mp else None


def list_drives(con) -> list[dict]:
    cols = ["drive_label", "hw_model", "serial", "health", "capacity_bytes",
            "free_bytes", "annex_uuid", "physical_location", "last_seen"]
    rows = con.execute(f"SELECT {', '.join(cols)} FROM drives ORDER BY drive_label").fetchall()
    return [dict(zip(cols, r)) for r in rows]


def register_nas(remote: str = "nas", label: str = "drive-99", role: str = "replica") -> dict:
    """Record an existing git-annex `directory` special remote (e.g. the NAS over NFS) as a
    librarian target. No SMART/mkfs — the special remote already receives content via
    `git annex copy --to <remote>`. Reads its uuid + directory from the map repo config, and
    the free/total from the mount the directory lives on (DEC-006, DEC-014)."""
    lib = library_root()
    uuid = _git(lib, "config", f"remote.{remote}.annex-uuid", check=False)
    directory = _git(lib, "config", f"remote.{remote}.annex-directory", check=False)
    if not (uuid and directory):
        raise RuntimeError(
            f"no git-annex directory special remote '{remote}' in {lib} "
            f"(uuid={uuid or '?'}, directory={directory or '?'}). Create it first, e.g. "
            f"`git -C {lib} annex initremote {remote} type=directory directory=<nfs-path>/annex encryption=none`.")
    mount = str(Path(directory).parent)                 # <mount>/annex -> <mount>
    du = shutil.disk_usage(mount)
    con = db.connect()
    try:
        db.upsert(con, "drives", {
            "drive_label": label,
            "annex_uuid": uuid,
            "capacity_bytes": du.total,
            "free_bytes": du.free,
            "hw_model": "special-remote (directory/NFS)",
            "physical_location": f"NAS {remote} ({directory})",
            "role": role,
            "health": "raid",
            "last_seen": datetime.now(),
            "notes": f"git-annex directory special remote '{remote}'; content via `annex copy --to {remote}`",
        }, pk=["drive_label"])
        plan_id = _add_to_active_plan(con, label)       # #34: join the active plan's fixed set
    finally:
        con.close()
    return {"label": label, "uuid": uuid, "mount": mount, "directory": directory,
            "role": role, "free": du.free, "total": du.total, "remote": remote, "plan": plan_id}
