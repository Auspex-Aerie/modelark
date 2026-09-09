"""INTERNAL session-only port; actual kernel-vfat qualification is a release gate.

Ownership lives only in retained descriptors under one consumed host attempt.
No xattrs, persistent inode certificates, unnamed temporaries, or hardlinks.
Trust boundary remains the operator account; no defense against malicious same-UID
mkdir/open or authenticated-name/rename races is claimed. No residue cleanup.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import PurePosixPath
import resource
import stat
import sys

from .destination import _port_io, _valid_owner_token
from .io_errors import classify_io
from .linux import _openat2, rename_noreplace
from .transaction import ObjectInfo, TransferRefusal


@dataclass
class _Object:
    fd: int
    token: str
    kind: str


class _Reader:
    def __init__(self, destination, stream):
        self.destination, self.stream = destination, stream

    def read(self, size=-1):
        try:
            self.destination._live()
            return self.stream.read(size)
        except OSError as exc:
            raise self.destination._io_refusal(exc) from exc


class Fat32Destination:
    def __init__(self, tree, binding, store, tx, *, recheck):
        plan = store.load(tx)
        if not getattr(plan, "session_only", False) or plan.destination != binding:
            raise TransferRefusal("ADMISSION_CORRUPT", "FAT session plan required")
        self.tree, self.binding, self.plan = tree, binding, plan
        self._child = plan.destination.target.child_name
        self._recheck = recheck
        self._objects = {}
        self._authority = None
        self._spent = False
        self.cleanup_errors = ()
        # Retain one descriptor per payload/control/report/directory. Reserve
        # substantial headroom for source readers, SQLite and transient opens.
        paths = [PurePosixPath(self._child) / a.repo_id / a.rfilename for a in plan.proposal.closure]
        directories = {str(p) for path in paths for p in path.parents if str(p) != "."}
        needed = len(paths) + len(directories) + 2 + 64
        current = len(os.listdir("/proc/self/fd"))
        limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if limit != resource.RLIM_INFINITY and current + needed > limit:
            raise TransferRefusal("DESTINATION_LAYOUT_UNSUPPORTED", "retained descriptor budget exceeded")

    def begin_session(self, authority):
        if self._spent or self._authority is not None:
            raise TransferRefusal("FAT32_NEW_ROOT_REQUIRED", "live port cannot be rebound")
        authority.lease.check()
        self._authority = authority
        self._spent = True

    def end_session(self):
        self._spent = True
        self._authority = None
        objects = tuple(self._objects.values())
        self._objects.clear()
        errors = []
        for obj in objects:
            try:
                os.close(obj.fd)
            except OSError as exc:
                errors.append(exc)
        # Never retry a close: even a failed close may have released that number.
        # Preserve any primary transfer failure while retaining cleanup diagnostics.
        if errors:
            self.cleanup_errors = (*self.cleanup_errors, *errors)
        if errors and sys.exc_info()[0] is None:
            raise errors[0]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.end_session()

    def _live(self):
        if self._authority is None:
            raise TransferRefusal("FAT32_NEW_ROOT_REQUIRED", "no retained live authority")
        self._authority.lease.check()
        self._recheck_attachment()

    def _recheck_attachment(self):
        """Read-only parent AND backing evidence, also usable at IO failure boundaries."""
        self.tree.check()
        self._recheck(self.tree)

    def _io_refusal(self, exc):
        code = ("DESTINATION_CAPACITY_WAIT" if exc.errno in {errno.ENOSPC, errno.EDQUOT}
                else "OUTPUT_COLLISION" if exc.errno == errno.EEXIST else "DESTINATION_IO_FAILED")
        return classify_io(exc, self._recheck_attachment, TransferRefusal(code, str(exc)))

    def _path(self, path):
        if (not isinstance(path, str) or not path or "\\" in path or "\0" in path
                or any(p in {"", ".", ".."} for p in path.split("/"))
                or path.split("/")[0] != self._child):
            raise TransferRefusal("PATH_UNSAFE", "outside approved output child")
        return path

    def _open(self, path):
        self._path(path)
        return _openat2(self.tree.fd, path, os.O_RDONLY | os.O_NONBLOCK)

    @staticmethod
    def _identity(fd):
        info = os.fstat(fd)
        return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)

    def _owned(self, path, token=None):
        obj = self._objects.get(path)
        if obj is None or (token is not None and obj.token != token):
            raise TransferRefusal("OUTPUT_COLLISION", "no retained ownership: " + path)
        try:
            fd = self._open(path)
        except FileNotFoundError as exc:
            raise TransferRefusal("DESTINATION_CHANGED", "owned object disappeared") from exc
        try:
            if self._identity(fd) != self._identity(obj.fd):
                raise TransferRefusal("DESTINATION_CHANGED", "owned object replaced")
        finally:
            os.close(fd)
        return obj

    @contextmanager
    def _parent(self, path):
        self._path(path)
        self._live()
        parent = str(PurePosixPath(path).parent)
        fd = self.tree.fd if parent == "." else self._owned(parent).fd
        yield fd, PurePosixPath(path).name

    @_port_io
    def check(self, binding, allocated, required_bytes):
        self._live()
        if binding != self.binding:
            raise TransferRefusal("DESTINATION_CHANGED")
        if any(type(n) is not int or n < 0 for n in (allocated, required_bytes)):
            raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN")
        actual = sum(os.fstat(self._owned(path).fd).st_blocks * 512 for path in self._objects)
        if actual != allocated:
            raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN", "live owned allocation differs")
        space = os.fstatvfs(self.tree.fd)
        if space.f_bavail * space.f_frsize < max(0, required_bytes - actual):
            raise TransferRefusal("DESTINATION_CAPACITY_WAIT")

    @_port_io
    def inspect(self, path):
        self._live()
        if path in self._objects:
            obj = self._owned(path)
            return ObjectInfo(obj.kind, obj.token, os.fstat(obj.fd).st_blocks * 512)
        try:
            fd = self._open(path)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            return ObjectInfo("directory" if stat.S_ISDIR(info.st_mode) else "file", None,
                              info.st_blocks * 512)
        finally:
            os.close(fd)

    def _create(self, path, token, directory):
        if not _valid_owner_token(token) or path in self._objects:
            raise TransferRefusal("OUTPUT_COLLISION")
        if not directory and PurePosixPath(path).name != ".slice-" + token:
            raise TransferRefusal("PATH_UNSAFE", "files must begin as live named staging")
        with self._parent(path) as (parent, name):
            if directory:
                os.mkdir(name, 0o700, dir_fd=parent)
                fd = _openat2(parent, name, os.O_RDONLY | os.O_DIRECTORY)
            else:
                fd = _openat2(parent, name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            self._objects[path] = _Object(fd, token, "directory" if directory else "file")

    @_port_io
    def create_directory(self, path, token):
        self._create(path, token, True)

    @_port_io
    def create_file(self, path, token):
        self._create(path, token, False)

    @_port_io
    def append(self, path, token, data):
        self._live()
        if PurePosixPath(path).name != ".slice-" + token:
            raise TransferRefusal("PATH_UNSAFE", "published output is immutable")
        obj = self._owned(path, token)
        if obj.kind != "file":
            raise TransferRefusal("OUTPUT_COLLISION")
        os.lseek(obj.fd, 0, os.SEEK_END)
        view = memoryview(data)
        while view:
            written = os.write(obj.fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short FAT write")
            view = view[written:]
        os.fsync(obj.fd)  # Settle allocation before the next engine accounting check.

    @contextmanager
    def read(self, path):
        try:
            self._live()
            obj = self._owned(path)
            if obj.kind != "file":
                raise TransferRefusal("OUTPUT_COLLISION")
        except OSError as exc:
            raise self._io_refusal(exc) from exc
        fd, stream = -1, None
        try:
            try:
                fd = os.dup(obj.fd)
                stream = os.fdopen(fd, "rb")
                fd = -1
                stream.seek(0)
            except OSError as exc:
                raise self._io_refusal(exc) from exc
            yield _Reader(self, stream)  # Never classify exceptions raised by the consumer.
        finally:
            primary = sys.exc_info()[1]
            try:
                if stream is not None:
                    stream.close()
                elif fd >= 0:
                    os.close(fd)
            except OSError as exc:
                if primary is None:
                    raise self._io_refusal(exc) from exc
                self.cleanup_errors = (*self.cleanup_errors, exc)

    @_port_io
    def flush(self, path):
        self._live()
        fd = self.tree.fd if path == "." else self._owned(path).fd
        os.fsync(fd)

    @_port_io
    def publish(self, temporary, path, token):
        self._live()
        if (PurePosixPath(temporary).name != ".slice-" + token
                or PurePosixPath(path).name.lower().startswith(".slice-")):
            raise TransferRefusal("PATH_UNSAFE", "publication requires staging to final")
        obj = self._owned(temporary, token)
        if obj.kind != "file" or path in self._objects:
            raise TransferRefusal("OUTPUT_COLLISION")
        with self._parent(temporary) as (src, old), self._parent(path) as (dst, new):
            rename_noreplace(src, old, dst, new)
        self._objects[path] = self._objects.pop(temporary)

    @_port_io
    def discard_temporary(self, path, token):
        self._live()
        self._owned(path, token)
        if PurePosixPath(path).name != ".slice-" + token:
            raise TransferRefusal("PATH_UNSAFE", "only live staging can be discarded")
        with self._parent(path) as (fd, name):
            os.unlink(name, dir_fd=fd)
        os.close(self._objects.pop(path).fd)

    @_port_io
    def list_paths(self, root):
        self._live()
        if self.inspect(root) is None:
            return ()
        self._owned(root)
        pending, result = [root], []
        while pending:
            current = pending.pop()
            obj = self._owned(current)
            for name in sorted(os.listdir(obj.fd)):
                path = current + "/" + name
                result.append(path)
                child = self._objects.get(path)
                if child is not None and child.kind == "directory":
                    pending.append(path)
        return tuple(result)
