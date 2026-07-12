"""OS platform detection and the few Unix-only primitives, isolated in one place
so Windows/macOS branches stay clean.

Drive health (SMART via smartctl + UAS quirks) and block-device prep (mkfs/mount/
lsblk/blkid) are implemented only for Linux for now (DEF-008 / DEC-008). On other
platforms the caller skips them: the drive is prepared with the OS's own tools and
registered by its mount path, and health is punted to the OS.
"""
from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
OS_LABEL = "Windows" if IS_WINDOWS else "macOS" if IS_MACOS else "Linux"

# SMART/UAS-quirk drive health = smartctl + lsblk + sysfs → Linux-only for now.
SMART_SUPPORTED = IS_LINUX
# mkfs/mount/wipefs/blkid device prep = Linux-only; elsewhere the drive is
# pre-formatted (e.g. NTFS via Disk Management) and registered by its mount path.
BLOCKDEV_OPS_SUPPORTED = IS_LINUX


def is_root() -> bool:
    """True when running with admin/root privileges. os.geteuid is Unix-only, so
    Windows uses the shell32 admin check."""
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0
