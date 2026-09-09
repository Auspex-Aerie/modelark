"""Linux descriptor confinement primitives for direct delivery.

These primitives prove path confinement, not device eligibility or exclusive namespace
ownership. Callers must separately prove those preconditions. No fallback to path-based IO.
"""
from contextlib import contextmanager
import ctypes
import errno
import os
import platform

from .transaction import TransferRefusal
from .paths import canonical_attachment
from .io_errors import attachment_refusal, probe_io
from .host_observation import parse_mounts, read_fs_text


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


def _required_result(value, capability):
    if value < 0 and ctypes.get_errno() == errno.ENOSYS:
        raise TransferRefusal('FILESYSTEM_UNSUPPORTED',
                              f'direct delivery requires {capability}; Linux 5.8+ capabilities required')
    return _result(value)


def _openat2(fd, path, flags, mode=0, *, resolve=0x0D):
    # BENEATH | NO_SYMLINKS | NO_XDEV. Syscall 437 on both supported ABIs.
    how = _OpenHow(flags | os.O_CLOEXEC, mode, resolve)
    return _required_result(_libc().syscall(ctypes.c_long(437), ctypes.c_int(fd),
                                          ctypes.c_char_p(os.fsencode(path)),
                                          ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how))), 'openat2')


def _statx(fd, *, require_birth=True):
    result = _Statx()
    libc = _libc()
    if not hasattr(libc, "statx"):
        raise TransferRefusal("FILESYSTEM_UNSUPPORTED", "statx unavailable")
    required = 0x1900 if require_birth else 0x1100
    _required_result(libc.statx(ctypes.c_int(fd), ctypes.c_char_p(b""), ctypes.c_int(0x1000),
                              ctypes.c_uint(required), ctypes.byref(result)), 'statx')
    if result.mask & required != required:
        detail = "inode birth time and mount identity required" if require_birth else "inode and mount identity required"
        raise TransferRefusal("FILESYSTEM_UNSUPPORTED", detail)
    return result


def _relative(path):
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        raise TransferRefusal("PATH_UNSAFE", "canonical relative path required")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise TransferRefusal("PATH_UNSAFE", "canonical relative path required")
    return parts


def require_create_access(fd):
    """Kernel effective-credential/ACL check on the retained directory, without writes."""
    # faccessat2(AT_EMPTY_PATH | AT_EACCESS), syscall439 on both supported ABIs.
    # Do not fall back to older glibc faccessat mode-bit emulation (which can ignore ACLs).
    _required_result(_libc().syscall(ctypes.c_long(439), ctypes.c_int(fd), ctypes.c_char_p(b""),
                                   ctypes.c_int(os.W_OK | os.X_OK), ctypes.c_int(0x1200)), 'faccessat2')


@probe_io('DESTINATION_CAPACITY_UNPROVEN', 'cannot prove absence of default ACL')
def require_no_default_acl(fd):
    """Keep inherited xattrs outside the bounded ownership-marker allocation profile."""
    try:
        os.getxattr(fd, 'system.posix_acl_default')
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return
        raise
    raise TransferRefusal('DESTINATION_CAPACITY_UNPROVEN',
                          'default ACL inheritance is unsupported by the ownership-marker profile')


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


@probe_io('DESTINATION_UNPROVEN', 'attachment inventory unavailable')
def _mount_ids():
    """Read attachment presence only; no device discovery or mount actions."""
    return frozenset(mount.mount_id for mount in parse_mounts(read_fs_text('/proc/self/mountinfo')))


class BoundTree:
    """Retained root fd; each descendant resolution rejects symlinks and mount crossings."""

    def __init__(self, path, verify=None, writable=False, *, mount_ids=None):
        self.path = canonical_attachment(path)
        self.verify = verify
        self.writable = writable
        self._mount_ids = mount_ids or _mount_ids
        self._attachment_lost = False
        self.fd = -1
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            # Mount crossing is allowed only while binding the explicitly supplied root.
            self.fd = _openat2(root, str(self.path).lstrip("/") or ".",
                               os.O_RDONLY | os.O_DIRECTORY, resolve=0x0C)
            self._identity = self.identity(self.fd)
            self.mount_id = self._statx(self.fd).mount_id
            self.check()
        except BaseException:
            self.close()
            raise
        finally:
            os.close(root)

    @staticmethod
    def _statx(fd):
        # Native/legacy callers keep the birth requirement. Live-only adapters may
        # override observation without changing any existing default capability.
        return _statx(fd)

    @staticmethod
    def identity(fd):
        value = _statx(fd)
        return (os.makedev(value.dev_major, value.dev_minor), value.ino,
                value.btime.sec * 1_000_000_000 + value.btime.nsec)

    def check(self):
        if self.fd < 0:
            raise TransferRefusal("DESTINATION_CHANGED", "closed bound tree")
        self._check_attachment()
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        current = -1
        try:
            current = _openat2(root, str(self.path).lstrip("/") or ".",
                              os.O_RDONLY | os.O_DIRECTORY, resolve=0x0C)
            if (self.identity(current) != self._identity or
                    self._statx(current).mount_id != self.mount_id):
                # An unmount can expose a perfectly ordinary host directory at the
                # same pathname. Never treat that directory as a replacement writer.
                self._check_attachment()
                raise TransferRefusal("DESTINATION_CHANGED", "bound root or attachment changed")
            if self.verify is not None:
                self.verify()
            self._check_attachment()
        except (OSError, TransferRefusal) as exc:
            # Re-probe after errors: disappearing media can race the initial presence
            # check or the higher-level identity observer. Only absence proof is a wait.
            refusal = attachment_refusal(self._check_attachment)
            if refusal is not None:
                raise refusal from exc
            if isinstance(exc, FileNotFoundError):
                raise TransferRefusal("DESTINATION_CHANGED", "bound root disappeared on attached filesystem") from exc
            raise
        finally:
            if current >= 0:
                os.close(current)
            os.close(root)

    def _check_attachment(self):
        if self._attachment_lost:
            raise TransferRefusal("WAITING_DESTINATION", "attachment was lost; a fresh Start must bind it again")
        ids = self._mount_ids()
        if (not isinstance(ids, (set, frozenset, tuple, list))
                or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids)):
            raise TransferRefusal("DESTINATION_UNPROVEN", "attachment inventory unavailable")
        if self.mount_id not in ids:
            self._attachment_lost = True
            raise TransferRefusal("WAITING_DESTINATION", "bound attachment is no longer mounted")

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
