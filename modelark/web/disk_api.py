"""Disk Health API — reads SMART via smartctl for attached drives.

Best-effort and privilege-aware: smartctl needs root, so we try a plain call,
then `sudo -n` (passwordless), and if neither works we return the drives with a
clear 'needs privilege' note instead of failing. First step toward DEF-003
(the full onboarding-qualification + evacuate automation stays deferred).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from modelark import register as drive_register
from modelark.core import platform as osplat


def _smartctl_bin():
    """smartctl installs to /usr/sbin (often not on a user's PATH)."""
    return (shutil.which("smartctl")
            or next((p for p in ("/usr/sbin/smartctl", "/usr/bin/smartctl") if os.path.exists(p)), None))

# SMART attribute ids that matter for spinning disks
_REALLOC, _PENDING, _OFFLINE, _CRC, _POH = 5, 197, 198, 199, 9


# Virtual / pseudo block devices that aren't real drives.
_SKIP_PREFIX = ("nbd", "zram", "loop", "ram", "sr", "fd", "dm-", "md")


def _usb_id(name: str):
    """Resolve a block device's USB VID:PID from sysfs (for the UAS quirk hint)."""
    p = os.path.realpath(f"/sys/block/{name}")
    while p and p != "/":
        vid, pid = os.path.join(p, "idVendor"), os.path.join(p, "idProduct")
        if os.path.exists(vid) and os.path.exists(pid):
            try:
                return open(vid).read().strip() + ":" + open(pid).read().strip()
            except OSError:
                return None
        p = os.path.dirname(p)
    return None


def _lsblk_result() -> tuple[bool, list[dict]]:
    try:
        r = subprocess.run(["lsblk", "-dn", "-P", "-o", "NAME,SIZE,MODEL,SERIAL,TYPE,TRAN,ROTA"],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, []
    if r.returncode != 0:
        return False, []
    out = []
    for line in r.stdout.splitlines():
        d = dict(re.findall(r'(\w+)="([^"]*)"', line))
        name = d.get("NAME", "")
        if d.get("TYPE") != "disk" or not name:
            continue
        if any(name.startswith(s) for s in _SKIP_PREFIX) or d.get("SIZE") in ("", "0B", "0"):
            continue
        out.append(d)
    return True, out


def _lsblk() -> list[dict]:
    """Compatibility helper for the existing SMART endpoint."""
    return _lsblk_result()[1]


def attached_inventory() -> dict:
    """Return passive block-device inventory without running SMART or mutating hardware."""
    available, disks = _lsblk_result()
    return {
        "available": available,
        "devices": [
            {
                "dev": "/dev/" + item["NAME"],
                "size": item.get("SIZE"),
                "model": item.get("MODEL") or None,
                "serial": item.get("SERIAL") or None,
                "bus": item.get("TRAN") or None,
                "spinning": item.get("ROTA") == "1",
            }
            for item in disks
        ],
    }


def _registration_device_path(dev: str) -> bool:
    """Accept a concrete /dev path as one argv token; never interpret it through a shell."""
    if not isinstance(dev, str) or not dev.startswith("/dev/") or len(dev) > 256:
        return False
    if any(char in dev for char in ("\x00", "\n", "\r")):
        return False
    return ".." not in PurePosixPath(dev).parts


def _flatten_lsblk(nodes: list[dict]) -> list[dict]:
    flattened: list[dict] = []
    for node in nodes:
        flattened.append(node)
        flattened.extend(_flatten_lsblk(node.get("children") or []))
    return flattened


def registration_topology(dev: str) -> dict:
    """Read a prospective registration target's filesystem topology without SMART or writes."""
    if not _registration_device_path(dev):
        return {
            "available": False,
            "requested_dev": dev,
            "system_backing": False,
            "nodes": [],
            "error": "invalid_device_path",
        }
    try:
        result = subprocess.run(
            [
                "lsblk", "--json", "-b", "-p", "-o",
                "PATH,TYPE,SIZE,FSTYPE,UUID,MOUNTPOINTS,MODEL,SERIAL,TRAN,ROTA",
                dev,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {
            "available": False,
            "requested_dev": dev,
            "system_backing": False,
            "nodes": [],
            "error": "lsblk_unavailable",
        }
    if result.returncode != 0:
        return {
            "available": False,
            "requested_dev": dev,
            "system_backing": False,
            "nodes": [],
            "error": "topology_probe_failed",
        }
    try:
        roots = json.loads(result.stdout).get("blockdevices") or []
    except (json.JSONDecodeError, AttributeError):
        roots = []
    raw_nodes = _flatten_lsblk(roots)
    nodes = []
    for node in raw_nodes:
        mountpoints = node.get("mountpoints") or []
        if isinstance(mountpoints, str):
            mountpoints = [mountpoints]
        mountpoints = [str(item) for item in mountpoints if item]
        archive_path = None
        archive_state = "unmounted"
        annex_uuid = None
        registration_receipt = None
        if len(mountpoints) == 1:
            archive = Path(mountpoints[0]) / "modelark"
            archive_path = str(archive)
            if archive.is_symlink():
                archive_state = "unsafe_path"
            elif not archive.exists():
                archive_state = "absent"
            elif archive.is_dir() and (archive / ".git").exists():
                try:
                    annex = subprocess.run(
                        ["git", "-C", str(archive), "config", "--local", "--get", "annex.uuid"],
                        capture_output=True,
                        text=True,
                    )
                except FileNotFoundError:
                    annex = None
                if annex is not None and annex.returncode == 0 and annex.stdout.strip():
                    annex_uuid = annex.stdout.strip().splitlines()[0]
                    registration_receipt = drive_register.registration_receipt(archive)
                    archive_state = (
                        "prepared_registration" if registration_receipt else "annex"
                    )
                else:
                    archive_state = "unrecognized"
            else:
                archive_state = "unrecognized"
        nodes.append({
            "dev": node.get("path"),
            "type": node.get("type"),
            "size_bytes": int(node.get("size") or 0),
            "fstype": node.get("fstype") or None,
            "fs_uuid": node.get("uuid") or None,
            "mountpoints": mountpoints,
            "archive_path": archive_path,
            "archive_state": archive_state,
            "annex_uuid": annex_uuid,
            "registration_receipt": registration_receipt,
        })

    root_source = None
    try:
        root = subprocess.run(
            ["findmnt", "-nro", "SOURCE", "/"],
            capture_output=True,
            text=True,
        )
        if root.returncode == 0 and root.stdout.strip():
            root_source = root.stdout.strip().splitlines()[0].split("[", 1)[0]
    except FileNotFoundError:
        pass
    node_paths = {
        os.path.realpath(str(node["dev"]))
        for node in nodes
        if node.get("dev")
    }
    system_backing = any("/" in node["mountpoints"] for node in nodes)
    if root_source:
        system_backing = system_backing or os.path.realpath(root_source) in node_paths
    return {
        "available": bool(nodes),
        "requested_dev": dev,
        "system_backing": system_backing,
        "nodes": nodes,
        "error": None if nodes else "device_not_found",
    }


# -d drivers to try, in order — covers most USB-SATA/USB-NVMe bridges.
_D_TYPES = ["auto", "sat", "sat,12", "usbjmicron", "usbprolific", "usbsunplus", "usbcypress", "nvme"]
_HEALTH_KEYS = ("smart_status", "ata_smart_attributes", "nvme_smart_health_information_log")


def _smart(dev: str):
    """Return (json, needs_priv). Auto-discovers the working -d driver; uses sudo -n if not root."""
    binp = _smartctl_bin() or "smartctl"
    runner = [binp] if osplat.is_root() else ["sudo", "-n", binp]
    needs_priv = False
    for d in _D_TYPES:
        cmd = runner + ["--json", "-H", "-A", "-i", "-d", d, dev]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        try:
            j = json.loads(r.stdout)
        except (json.JSONDecodeError, ValueError):
            continue
        msgs = " ".join(m.get("string", "") for m in j.get("smartctl", {}).get("messages", [])).lower()
        if "permission" in msgs or "requires" in msgs or "must be run as" in msgs:
            return None, True  # privilege issue is the same across all -d types
        if any(k in j for k in _HEALTH_KEYS):
            j["_dtype"] = d
            return j, False
    return None, needs_priv


def _drive(d: dict) -> dict:
    dev = "/dev/" + d["NAME"]
    base = {
        "dev": dev, "size": d.get("SIZE"), "model": d.get("MODEL") or "—",
        "serial": d.get("SERIAL") or "—", "bus": d.get("TRAN") or "—",
        "spinning": d.get("ROTA") == "1",
    }
    j, needs_priv = _smart(dev)
    if j is None:
        if not osplat.is_root():
            base.update(status="unknown", note="SMART needs root — grant smartctl passwordless sudo (README > Setup); don't run the portal as root")
        elif d.get("TRAN") == "usb":
            usb = _usb_id(d["NAME"])
            base.update(status="unknown",
                        note="USB bridge blocks SMART — the Seagate 'SAT-over-UAS' issue. "
                             "Force usb-storage for this device, then reopen this page:")
            if usb:
                base["quirk_cmd"] = (f'echo "options usb-storage quirks={usb}:u" | '
                                     f'sudo tee /etc/modprobe.d/modelark-uas.conf '
                                     f'&& sudo update-initramfs -u   # then reboot (or replug)')
        else:
            base.update(status="unknown", note="SMART unavailable (unsupported device)")
        return base
    passed = j.get("smart_status", {}).get("passed")
    attrs = {row["id"]: row.get("raw", {}).get("value")
             for row in j.get("ata_smart_attributes", {}).get("table", [])}
    nvme = j.get("nvme_smart_health_information_log", {})
    poh = (j.get("power_on_time", {}) or {}).get("hours") or attrs.get(_POH) or nvme.get("power_on_hours")
    temp = (j.get("temperature", {}) or {}).get("current") or nvme.get("temperature")
    realloc = attrs.get(_REALLOC)
    pending = attrs.get(_PENDING)
    offline = attrs.get(_OFFLINE)
    crc = attrs.get(_CRC)
    media_err = nvme.get("media_errors")
    crit = nvme.get("critical_warning")
    pct_used = nvme.get("percentage_used")          # NVMe endurance consumed (%)
    spare = nvme.get("available_spare")             # NVMe spare blocks remaining (%)
    spare_thr = nvme.get("available_spare_threshold")
    unsafe = nvme.get("unsafe_shutdowns")
    spare_low = spare is not None and spare_thr is not None and spare < spare_thr

    # Reallocated >=100 = widespread platter degradation (failure is "when, not if").
    if (passed is False or (offline or 0) > 0 or (pending or 0) > 0 or (crit or 0) != 0
            or spare_low or (pct_used or 0) >= 100 or (realloc or 0) >= 100):
        status = "evacuate"
    elif ((realloc or 0) > 0 or (media_err or 0) > 0 or (crc or 0) > 0 or (pct_used or 0) >= 85):
        status = "watch"
    else:
        status = "ok"
    base.update(status=status, smart_passed=passed, power_on_hours=poh, temp_c=temp,
                reallocated=realloc, pending=pending, offline_uncorrectable=offline,
                crc_errors=crc, media_errors=media_err, dtype=j.get("_dtype"),
                percentage_used=pct_used, available_spare=spare, unsafe_shutdowns=unsafe)
    return base


def disk() -> dict:
    if not osplat.SMART_SUPPORTED:
        return {"drives": [], "tool_missing": False, "needs_privilege": False,
                "platform_unsupported": True, "os": osplat.OS_LABEL,
                "message": f"Drive health isn't checked in-system on {osplat.OS_LABEL} yet — "
                           f"run your platform's preferred health tracking against the drive "
                           f"first before use."}
    if _smartctl_bin() is None:
        return {"drives": [], "needs_privilege": False, "tool_missing": True}
    disks = _lsblk()
    drives = [_drive(d) for d in disks]
    needs_priv = not osplat.is_root() and any(dr.get("status") == "unknown" for dr in drives)
    return {"drives": drives, "needs_privilege": needs_priv, "tool_missing": False}
