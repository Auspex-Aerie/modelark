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


def _lsblk() -> list[dict]:
    try:
        r = subprocess.run(["lsblk", "-dn", "-P", "-o", "NAME,SIZE,MODEL,SERIAL,TYPE,TRAN,ROTA"],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.splitlines():
        d = dict(re.findall(r'(\w+)="([^"]*)"', line))
        name = d.get("NAME", "")
        if d.get("TYPE") != "disk" or not name:
            continue
        if any(name.startswith(s) for s in _SKIP_PREFIX) or d.get("SIZE") in ("", "0B", "0"):
            continue
        out.append(d)
    return out


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
            base.update(status="unknown", note="needs root — run `sudo modelark serve` to read SMART")
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
