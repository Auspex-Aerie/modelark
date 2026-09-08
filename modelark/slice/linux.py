"""Linux descriptor confinement primitives for direct delivery.

These primitives prove path confinement, not device eligibility or exclusive namespace
ownership. Callers must separately prove those preconditions. No fallback to path-based IO.
"""
from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import platform

from .transaction import TransferRefusal


class _Timestamp(ctypes.Structure):
    _fields_ = [("sec", ctypes.c_int64), ("nsec", ctypes.c_uint32),
                ("reserved", ctypes.c_int32)]


class _Statx(ctypes.Structure):
    # Linux UAPI statx layout, including reserved tail (256 bytes).
    _fields_ = [("mask", ctypes.c_uint32), ("blksize", ctypes.c_uint32),
                ("attributes", ctypes.c_uint64), ("nlink", ctypes.c_uint32),
                ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32),
                ("mode", ctypes.c_uint16), ("spare0", ctypes.c_uint16),
                ("ino", ctypes.c_uint64), ("size", ctypes.c_uint64),
                ("blocks", ctypes.c_uint64), ("attributes_mask", ctypes.c_uint64),
                ("atime", _Timestamp), ("btime", _Timestamp),
                ("ctime", _Timestamp), ("mtime", _Timestamp),
                ("rdev_major", ctypes.c_uint32), ("rdev_minor", ctypes.c_uint32),
                ("dev_major", ctypes.c_uint32), ("dev_minor", ctypes.c_uint32),
                ("mount_id", ctypes.c_uint64), ("tail", ctypes.c_uint64 * 13)]


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                ("resolve", ctypes.c_uint64)]


def _libc():
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise TransferRefusal("FILESYSTEM_UNSUPPORTED", "Linux x86_64/aarch64 required")
    return ctypes.CDLL(None, use_errno=True)


def _result(value):
    if value < 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    return value


def _openat2(fd, path, flags, mode=0, *, resolve=0x0D):
    # BENEATH | NO_SYMLINKS | NO_XDEV. Syscall 437 on both supported ABIs.
    how = _OpenHow(flags | os.O_CLOEXEC, mode, resolve)
    return _result(_libc().syscall(ctypes.c_long(437), ctypes.c_int(fd),
                                 ctypes.c_char_p(os.fsencode(path)),
                                 ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how))))


def _statx(fd):
    result = _Statx()
    libc = _libc()
    if not hasattr(libc, "statx"):
        raise TransferRefusal("FILESYSTEM_UNSUPPORTED", "statx unavailable")
    _result(libc.statx(ctypes.c_int(fd), ctypes.c_char_p(b""), ctypes.c_int(0x1000),
                       ctypes.c_uint(0x1900), ctypes.byref(result)))
    if result.mask & 0x1900 != 0x1900:
        raise TransferRefusal("FILESYSTEM_UNSUPPORTED", "inode birth time and mount identity required")
    return result


def _relative(path):
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        raise TransferRefusal("PATH_UNSAFE", "canonical relative path required")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise TransferRefusal("PATH_UNSAFE", "canonical relative path required")
    return parts


def link_fd(fd, parent_fd, name):
    """Publish the opened inode exclusively; never resolve a source pathname."""
    if len(_relative(name)) != 1:
        raise TransferRefusal("PATH_UNSAFE")
    libc = _libc()
    result = libc.linkat(ctypes.c_int(fd), ctypes.c_char_p(b""), ctypes.c_int(parent_fd),
                         ctypes.c_char_p(os.fsencode(name)), ctypes.c_int(0x1000))
    if result < 0 and ctypes.get_errno() in {errno.EPERM, errno.ENOENT}:
        # Documented unprivileged O_TMPFILE publication. Only our retained fd can be
        # selected; never accept a caller-supplied procfs path.
        result = libc.linkat(ctypes.c_int(-100), ctypes.c_char_p(f"/proc/self/fd/{fd}".encode()),
                             ctypes.c_int(parent_fd), ctypes.c_char_p(os.fsencode(name)),
                             ctypes.c_int(0x400))
    _result(result)


def rename_noreplace(src_dirfd, src, dst_dirfd, dst):
    """No-replace rename; callers must own the source directory namespace."""
    if len(_relative(src)) != 1 or len(_relative(dst)) != 1:
        raise TransferRefusal("PATH_UNSAFE")
    libc = _libc()
    if not hasattr(libc, "renameat2"):
        raise TransferRefusal("FILESYSTEM_UNSUPPORTED", "renameat2 unavailable")
    _result(libc.renameat2(ctypes.c_int(src_dirfd), ctypes.c_char_p(os.fsencode(src)),
                          ctypes.c_int(dst_dirfd), ctypes.c_char_p(os.fsencode(dst)),
                          ctypes.c_uint(1)))


class BoundTree:
    """Retained root fd; each descendant resolution rejects symlinks and mount crossings."""

    def __init__(self, path, verify=None, writable=False):
        self.path = Path(os.path.abspath(path))
        self.verify = verify
        self.writable = writable
        self.fd = -1
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            # Mount crossing is allowed only while binding the explicitly supplied root.
            self.fd = _openat2(root, str(self.path).lstrip("/") or ".",
                               os.O_RDONLY | os.O_DIRECTORY, resolve=0x0C)
            self._identity = self.identity(self.fd)
            self.mount_id = _statx(self.fd).mount_id
            self.check()
        except BaseException:
            self.close()
            raise
        finally:
            os.close(root)

    @staticmethod
    def identity(fd):
        value = _statx(fd)
        return (os.makedev(value.dev_major, value.dev_minor), value.ino,
                value.btime.sec * 1_000_000_000 + value.btime.nsec)

    def check(self):
        if self.fd < 0:
            raise TransferRefusal("DESTINATION_CHANGED", "closed bound tree")
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        current = -1
        try:
            current = _openat2(root, str(self.path).lstrip("/") or ".",
                              os.O_RDONLY | os.O_DIRECTORY, resolve=0x0C)
            if (self.identity(current) != self._identity or
                    _statx(current).mount_id != self.mount_id):
                raise TransferRefusal("DESTINATION_CHANGED", "bound root or attachment changed")
            if self.verify is not None:
                self.verify()
        finally:
            if current >= 0:
                os.close(current)
            os.close(root)

    def open(self, relative, flags, mode=0o600):
        _relative(relative)
        self.check()
        # openat2 rejects nonzero mode unless creation was requested.
        creation = flags & os.O_CREAT or flags & os.O_TMPFILE == os.O_TMPFILE
        return _openat2(self.fd, relative, flags, mode if creation else 0)

    @contextmanager
    def parent(self, relative):
        parts = _relative(relative)
        self.check()
        fd = (self.open("/".join(parts[:-1]), os.O_RDONLY | os.O_DIRECTORY)
              if len(parts) > 1 else os.dup(self.fd))
        try:
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
