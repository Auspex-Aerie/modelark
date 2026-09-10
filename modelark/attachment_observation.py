"""Short-lived, read-only Linux directory attachment binding.

This is neither a mount lease nor a claim against changes after return. Workflow
identity, physical ancestry, authority and evidence formats remain caller policy.
"""
import os
from pathlib import Path
import stat
import sys

from modelark.block_identity import BlockObservationError


def _open_directory(path):
    """Resolve every directory component without following symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _fd_identity(fd):
    """Kernel mount instance plus device/inode; never a pathname-derived ID."""
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise BlockObservationError("archive attachment descriptor is not a directory")
    text = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii")
    values = [line.split(":", 1)[1].strip() for line in text.splitlines()
              if line.startswith("mnt_id:")]
    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal() or int(values[0]) <= 0:
        raise BlockObservationError("kernel descriptor mount identity is unavailable")
    return int(values[0]), info.st_dev, info.st_ino


class BoundDirectory:
    """Retain one directory and check that its explicit pathname still selects it."""

    def __init__(self, path):
        self.fd = -1
        if sys.platform != "linux":
            raise BlockObservationError("Linux descriptor attachment observation required")
        self.path = Path(path)
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise BlockObservationError("absolute archive attachment path required")
        try:
            self.fd = _open_directory(self.path)
            self.identity = _fd_identity(self.fd)
            # Parent PID is deliberate: subprocesses do not inherit this CLOEXEC
            # fd, but can read the retained directory through the parent's procfs.
            self.pinned_path = Path(f"/proc/{os.getpid()}/fd/{self.fd}")
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.fd < 0 or _fd_identity(self.fd) != self.identity:
            raise BlockObservationError("archive attachment descriptor changed")
        current = _open_directory(self.path)
        try:
            if _fd_identity(current) != self.identity:
                raise BlockObservationError("archive directory attachment changed")
        finally:
            os.close(current)

    def read(self, probe, *args):
        self.check()
        value = probe(*args)
        self.check()
        return value

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
