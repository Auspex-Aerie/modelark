"""Native folder port: shared filesystem space, certified child-tree ownership.

The retained tree is the existing parent, not an owned filesystem. The engine's
first directory operation creates/certifies the only child this port may mutate.
Assembly must supply a qualified observer recheck and hold launch/attempt authority.
"""
import errno
import os
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath

from .destination import UsbDestination, _port_io
from .io_errors import classify_io
from .folder_observation import check_directory
from .transaction import TransferRefusal


class NativeFolderDestination(UsbDestination):
    def __init__(self, tree, binding, store, tx, *, recheck):
        plan = store.load(tx)
        if not plan.is_folder:
            raise TransferRefusal("ADMISSION_CORRUPT", "native adapter requires a native plan")
        self._child = plan.destination.target.child_name
        self._required_inodes = plan.required_inodes
        self._native_recheck = recheck
        super().__init__(tree, binding, store, tx)

    def _path(self, path, *, root=False):
        UsbDestination._path(path, root=root)
        if root and path == ".":
            return path  # Only flush uses this exception to persist child creation.
        if PurePosixPath(path).parts[0] != self._child:
            raise TransferRefusal("OUTPUT_COLLISION", "path is outside the approved output folder")
        return path

    def _recheck_attachment(self):
        self.tree.check()
        self._native_recheck(self.tree)

    @contextmanager
    def _parent(self, path):
        with super()._parent(path) as (fd, name):
            check_directory(fd)
            yield fd, name

    @contextmanager
    def _owned(self, path, token=None, *, write=False):
        with super()._owned(path, token, write=write) as fd:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                check_directory(fd)
            yield fd

    def _io_refusal(self, exc):
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            # A partial append may already have allocated blocks. End this attempt;
            # the next Start rebuilds actual allocation from authenticated objects.
            return classify_io(exc, self._recheck_attachment,
                               TransferRefusal("DESTINATION_CAPACITY_WAIT", str(exc)))
        return super()._io_refusal(exc)

    @_port_io
    def check(self, binding, allocated, required_bytes):
        self._recheck_attachment()
        if binding != self.binding:
            raise TransferRefusal("DESTINATION_CHANGED", "folder binding changed")
        if any(type(value) is not int or value < 0 for value in (allocated, required_bytes)):
            raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN")
        actual, owned_inodes = self._audit_allocation()
        if actual != allocated:
            raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN", "owned allocation differs")
        # Parent growth and sibling bytes/inodes are not part of this transaction.
        # An existing certified root disappearing is not a new-root opportunity.
        with self._connection() as con:
            certified = con.execute("SELECT 1 FROM certificates WHERE tx=? AND seal=? AND device=? "
                                    "AND filesystem=? AND mount=? AND path=? AND kind='directory' LIMIT 1",
                                    (*self._scope, self._child)).fetchone()
        if certified and self.inspect(self._child) is None:
            raise TransferRefusal("DESTINATION_CHANGED", "certified output root disappeared")
        free = os.fstatvfs(self.tree.fd)
        available = free.f_bavail * free.f_frsize
        remaining_inodes = max(0, self._required_inodes - owned_inodes)
        if (available < max(0, required_bytes - actual)
                or min(free.f_favail, free.f_ffree) < remaining_inodes):
            raise TransferRefusal("DESTINATION_CAPACITY_WAIT", "shared filesystem space/inodes insufficient")
