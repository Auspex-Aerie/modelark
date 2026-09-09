"""INTERNAL / UNQUALIFIED Linux destination adapter; not a production entry point.

DEC-124 requires one ModelArk application instance and trusts the operator account. Deliberate
namespace substitution by unrelated same-account processes is outside that boundary: mkdirat
cannot atomically return a descriptor and unlinkat cannot condition deletion on inode identity.
Descriptor confinement alone does not solve those adversarial windows. Production assembly must
hold the launch and transaction authorities, prove device eligibility, and supply a supported
filesystem capacity policy before exposing this internal port. The host registry is trusted.
Files use O_TMPFILE so a crash before certification never exposes an uncertified file.
Directories interrupted before certification are deliberately left for operator intervention.

st_blocks includes an inode's data and charged xattr/directory blocks. It does not account for
every filesystem-global metadata/journal allocation. The strict free-space equation therefore
refuses unexplained drift; it is not a universal proof of future filesystem metadata cost.
"""
from contextlib import ExitStack, contextmanager
import errno
from functools import wraps
import json
import os
from pathlib import PurePosixPath
import sqlite3
import stat

from . import linux
from .io_errors import ProbeFailure, classify_io
from .transaction import ObjectInfo, TransferRefusal


OWNER_XATTR = "user.modelark.slice-owner"
_OWNER_PREFIX = b"modelark.slice.owner.v1:"
_OWNER_PADDING = bytes(1024)


def _valid_owner_token(token):
    return (isinstance(token, str) and len(token) == 32
            and all(c in "0123456789abcdef" for c in token))


def owner_marker(token):
    """Versioned marker forced outside every supported ext4 inode (size <=1024).

    With EA_inode excluded by capacity admission, this value must inhabit the external
    xattr block. Its unique operation token prevents sharing that block with another
    owned inode even when inherited ACLs are identical. The marker remains corroboration,
    not ownership authority: durable host certificates still bind inode birth identity.
    """
    if not _valid_owner_token(token):
        raise TransferRefusal("OUTPUT_COLLISION", "invalid creation token")
    return _OWNER_PREFIX + token.encode("ascii") + _OWNER_PADDING


def decode_owner_marker(value):
    """Recognize new markers and legacy internal tokens; admission can require new ones."""
    if not isinstance(value, bytes):
        return None
    try:
        token = (value.decode("ascii") if len(value) == 32
                 else value[len(_OWNER_PREFIX):len(_OWNER_PREFIX) + 32].decode("ascii"))
    except UnicodeDecodeError:
        return None
    if not _valid_owner_token(token):
        return None
    return token if len(value) == 32 or value == owner_marker(token) else None


def _persistent_identity(identity):
    """Filesystem-scoped inode birth identity; st_dev is attachment-local, not durable."""
    if (not isinstance(identity, (tuple, list)) or len(identity) != 3
            or any(type(value) is not int for value in identity)
            or identity[0] < 0 or identity[1] <= 0 or identity[2] <= 0):
        raise TransferRefusal("STATE_CORRUPT", "malformed legacy or observed inode identity")
    return tuple(identity[1:])


def _port_io(method):
    """Translate only IO executed by a synchronous port operation, never fault hooks."""
    @wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except OSError as exc:
            raise self._io_refusal(exc) from exc
    return call


class _DestinationRead:
    def __init__(self, destination, stream):
        self.destination, self.stream = destination, stream

    def read(self, size=-1):
        try:
            self.destination.tree.check()
            data = self.stream.read(size)
            self.destination.tree.check()
            return data
        except OSError as exc:
            raise self.destination._io_refusal(exc) from exc


class UsbDestination:
    """Internal DestinationPort; operator assembly supplies device/capacity admission."""

    def __init__(self, tree, binding, store, tx):
        self.tree, self.binding, self.store, self.tx = tree, binding, store, tx
        plan = store.load(tx)
        if plan.destination != binding:
            raise TransferRefusal("DESTINATION_CHANGED")
        self.seal = plan.seal
        self._scope = (tx, self.seal, binding.device_id, binding.filesystem_id, binding.mount_id)
        tree.check()
        self._root_attachment_identity = tuple(tree.identity(tree.fd))
        self._root_identity = _persistent_identity(self._root_attachment_identity)
        root = store.root
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise TransferRefusal("STATE_CORRUPT", "certificate directory is not private")
        self._database = root / "destination-certificates.sqlite"
        try:
            fd = os.open(self._database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        info = self._database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise TransferRefusal("STATE_CORRUPT", "unsafe certificate database")
        with self._connection(write=True) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise TransferRefusal("STATE_CORRUPT", "unknown certificate schema")
            con.execute("CREATE TABLE IF NOT EXISTS roots (tx TEXT PRIMARY KEY, seal TEXT NOT NULL,"
                        "device TEXT NOT NULL, filesystem TEXT NOT NULL, mount TEXT NOT NULL,"
                        "identity TEXT NOT NULL, blocks INTEGER NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS certificates (tx TEXT NOT NULL, seal TEXT NOT NULL,"
                        "device TEXT NOT NULL, filesystem TEXT NOT NULL, mount TEXT NOT NULL,"
                        "path TEXT NOT NULL, token TEXT NOT NULL, identity TEXT NOT NULL, kind TEXT NOT NULL,"
                        "PRIMARY KEY(tx,seal,device,filesystem,mount,path,identity))")
            if version == 1:
                # Trusted private legacy records already bind the sealed filesystem
                # and parent disk. Remove only transient st_dev, atomically across
                # roots and certificates; malformed/colliding rows roll back it all.
                for table in ("roots", "certificates"):
                    for rowid, encoded in con.execute(f"SELECT rowid,identity FROM {table}").fetchall():
                        try:
                            identity = _persistent_identity(json.loads(encoded))
                        except (TypeError, json.JSONDecodeError) as exc:
                            raise TransferRefusal("STATE_CORRUPT", "malformed legacy inode identity") from exc
                        con.execute(f"UPDATE {table} SET identity=? WHERE rowid=?", (json.dumps(identity), rowid))
            con.execute("PRAGMA user_version=2")
            row = con.execute("SELECT seal,device,filesystem,mount,identity,blocks FROM roots WHERE tx=?",
                              (tx,)).fetchone()
            identity = json.dumps(self._root_identity)
            if row is None:
                self._root_blocks = self._blocks(os.fstat(tree.fd))
                con.execute("INSERT INTO roots VALUES(?,?,?,?,?,?,?)", (*self._scope, identity, self._root_blocks))
            else:
                if row[:5] != (*self._scope[1:], identity):
                    raise TransferRefusal("DESTINATION_CHANGED", "certificate root differs")
                self._root_blocks = row[5]

    def _recheck_attachment(self):
        self.tree.check()

    def _io_refusal(self, exc):
        code = "OUTPUT_COLLISION" if isinstance(exc, FileExistsError) or exc.errno in {
            errno.ELOOP, errno.EXDEV, errno.ENOTDIR} else "DESTINATION_IO_FAILED"
        return classify_io(exc, self._recheck_attachment, TransferRefusal(code, str(exc)))

    @contextmanager
    def _connection(self, *, write=False):
        con = None
        try:
            con = sqlite3.connect(self._database, timeout=0)
            con.execute("PRAGMA synchronous=FULL")
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield con
            con.commit()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise TransferRefusal("STATE_BUSY", "destination certificate registry") from exc
            raise TransferRefusal("STATE_CORRUPT", "destination certificate registry") from exc
        except sqlite3.DatabaseError as exc:
            raise TransferRefusal("STATE_CORRUPT", "destination certificate registry") from exc
        finally:
            if con is not None:
                con.close()

    @staticmethod
    def _blocks(info):
        if not isinstance(info.st_blocks, int) or info.st_blocks < 0:
            raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN")
        return info.st_blocks * 512

    @staticmethod
    def _path(path, *, root=False):
        if root and path == ".":
            return path
        if not isinstance(path, str):
            raise TransferRefusal("OUTPUT_COLLISION", "non-canonical destination path")
        value = PurePosixPath(path)
        if (not value.parts or value.is_absolute()
                or str(value) != path or any(p in {".", ".."} for p in value.parts)):
            raise TransferRefusal("OUTPUT_COLLISION", "non-canonical destination path")
        return path

    def _token(self, fd, path):
        identity = json.dumps(_persistent_identity(self.tree.identity(fd)))
        try:
            token = decode_owner_marker(os.getxattr(fd, OWNER_XATTR))
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                return None
            raise
        if token is None:
            return None
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        with self._connection() as con:
            found = con.execute("SELECT 1 FROM certificates WHERE tx=? AND seal=? AND device=?"
                                " AND filesystem=? AND mount=? AND path=? AND token=? AND identity=? AND kind=?",
                                (*self._scope, path, token, identity, kind)).fetchone()
        return token if found else None

    def _certify(self, fd, path, token, kind):
        identity = json.dumps(_persistent_identity(self.tree.identity(fd)))
        with self._connection(write=True) as con:
            con.execute("INSERT OR IGNORE INTO certificates VALUES(?,?,?,?,?,?,?,?,?)",
                        (*self._scope, path, token, identity, kind))

    @contextmanager
    def _parent(self, path):
        self.tree.check()
        self._path(path)
        with self.tree.parent(path) as (fd, name):
            parent = str(PurePosixPath(path).parent)
            if parent == ".":
                if tuple(self.tree.identity(fd)) != self._root_attachment_identity:
                    raise TransferRefusal("DESTINATION_CHANGED")
            elif self._token(fd, parent) is None:
                raise TransferRefusal("OUTPUT_COLLISION", parent)
            # A valid immediate parent must not be reached through foreign ancestors.
            for ancestor in PurePosixPath(parent).parents:
                if str(ancestor) == ".":
                    continue
                other = self.tree.open(str(ancestor), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    if self._token(other, str(ancestor)) is None:
                        raise TransferRefusal("OUTPUT_COLLISION", str(ancestor))
                finally:
                    os.close(other)
            yield fd, name

    @contextmanager
    def _owned(self, path, token=None, *, write=False):
        self._path(path)
        self.tree.check()
        fd = self.tree.open(path, (os.O_WRONLY | os.O_APPEND if write else os.O_RDONLY) | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            actual = self._token(fd, path)
            if (actual is None or (token is not None and token != actual)
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise TransferRefusal("OUTPUT_COLLISION", path)
            yield fd
        finally:
            os.close(fd)

    def _available(self):
        info = os.fstatvfs(self.tree.fd)
        return info.f_bavail * info.f_frsize

    @_port_io
    def check(self, binding, allocated, required_bytes):
        self.tree.check()
        if binding != self.binding:
            raise TransferRefusal("DESTINATION_CHANGED")
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (allocated, required_bytes)):
            raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN")
        actual = self._audit_allocated()
        root_delta = self._blocks(os.fstat(self.tree.fd)) - self._root_blocks
        if actual != allocated or root_delta < 0:
            raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN", "owned allocation differs")
        available = self._available()
        if available + actual + root_delta != binding.available_bytes:
            raise TransferRefusal("DESTINATION_CAPACITY_CHANGED", "unexplained free-space change")
        if (required_bytes > binding.available_bytes or actual + root_delta > required_bytes
                or available < required_bytes - actual - root_delta):
            raise TransferRefusal("DESTINATION_CAPACITY_INSUFFICIENT")

    def _audit_allocated(self):
        return self._audit_allocation()[0]

    def _audit_allocation(self):
        """Authenticate owned allocation/link counts independently of capacity policy."""
        seen, links, actual = set(), {}, 0
        with self._connection() as con:
            paths = con.execute("SELECT DISTINCT path FROM certificates WHERE tx=? AND seal=? AND device=?"
                                " AND filesystem=? AND mount=?", self._scope).fetchall()
        for (path,) in paths:
            try:
                with self._owned(path) as fd:
                    identity = tuple(self.tree.identity(fd))
                    info = os.fstat(fd)
                    if stat.S_ISREG(info.st_mode):
                        count, previous_links = links.get(identity, (0, info.st_nlink))
                        if previous_links != info.st_nlink:
                            raise TransferRefusal("OUTPUT_COLLISION", "inode link count changed during audit")
                        links[identity] = (count + 1, info.st_nlink)
                    if identity not in seen:
                        actual += self._blocks(info)
                        seen.add(identity)
            except FileNotFoundError:
                continue
        if any(count != total for count, total in links.values()):
            raise TransferRefusal("OUTPUT_COLLISION", "unexplained hardlinks to delivery objects")
        return actual, len(seen)

    @_port_io
    def inspect(self, path):
        self._path(path)
        self.tree.check()
        try:
            with self.tree.parent(path) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                return ObjectInfo("unknown", None, self._blocks(info))
            fd = self.tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            self.tree.check()
            return None
        try:
            info = os.fstat(fd)
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            return ObjectInfo(kind, self._token(fd, path), self._blocks(info))
        finally:
            os.close(fd)

    @staticmethod
    def _validate_token(token):
        if not _valid_owner_token(token):
            raise TransferRefusal("OUTPUT_COLLISION", "invalid creation token")

    @_port_io
    def create_directory(self, path, token):
        self._validate_token(token)
        with self._parent(path) as (parent, name):
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError as exc:
                raise TransferRefusal("OUTPUT_COLLISION", "existing or uncertified directory: " + path) from exc
            fd = self.tree.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.setxattr(fd, OWNER_XATTR, owner_marker(token), os.XATTR_CREATE)
                os.fsync(fd)
                os.fsync(parent)
                self._certify(fd, path, token, "directory")
            finally:
                os.close(fd)

    def _temporary(self, path, token):
        self._validate_token(token)
        self._path(path)
        if PurePosixPath(path).name != ".slice-" + token:
            raise TransferRefusal("OUTPUT_COLLISION", "not the operation's temporary name")

    @_port_io
    def create_file(self, path, token):
        self._temporary(path, token)
        with self._parent(path) as (parent, name):
            try:
                fd = os.open(".", os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=parent)
            except OSError as exc:
                if exc.errno in {errno.EOPNOTSUPP, errno.EINVAL, errno.EISDIR, errno.ENOSYS}:
                    raise ProbeFailure(exc, "DESTINATION_UNPROVEN", "O_TMPFILE is required") from exc
                raise
            try:
                os.setxattr(fd, OWNER_XATTR, owner_marker(token), os.XATTR_CREATE)
                os.fsync(fd)
                self._certify(fd, path, token, "file")
                try:
                    linux.link_fd(fd, parent, name)
                except FileExistsError as exc:
                    raise TransferRefusal("OUTPUT_COLLISION", path) from exc
                os.fsync(parent)
            finally:
                os.close(fd)

    @_port_io
    def append(self, path, token, data):
        self._temporary(path, token)
        with self._parent(path), self._owned(path, token, write=True) as fd:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise TransferRefusal("OUTPUT_COLLISION", "temporary has unexpected links")
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if not written:
                    raise OSError(errno.EIO, "short destination write")
                view = view[written:]
            # Settle delayed allocation before the next capacity/ownership boundary.
            os.fsync(fd)

    @_port_io
    def discard_temporary(self, path, token):
        self._temporary(path, token)
        with self._parent(path) as (parent, name), self._owned(path, token) as fd:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise TransferRefusal("OUTPUT_COLLISION", path)
            live = self.tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                if self.tree.identity(live) != self.tree.identity(fd):
                    raise TransferRefusal("OUTPUT_COLLISION", path)
            finally:
                os.close(live)
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)

    @contextmanager
    def read(self, path):
        stack = ExitStack()
        try:
            try:
                fd = stack.enter_context(self._owned(path))
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise TransferRefusal("OUTPUT_COLLISION", path)
                read_fd = os.dup(fd)
                try:
                    stream = os.fdopen(read_fd, "rb")
                except BaseException:
                    os.close(read_fd)
                    raise
                stream = stack.enter_context(stream)
            except OSError as exc:
                raise self._io_refusal(exc) from exc
            # No exception catcher spans this yield: consumer/source failures must
            # never be attributed to destination reads merely for using this context.
            yield _DestinationRead(self, stream)
            try:
                self.tree.check()
            except OSError as exc:
                raise self._io_refusal(exc) from exc
        finally:
            try:
                stack.close()
            except OSError as exc:
                raise self._io_refusal(exc) from exc

    @_port_io
    def flush(self, path):
        self.tree.check()
        self._path(path, root=True)
        if path == ".":
            os.fsync(self.tree.fd)
        else:
            with self._owned(path) as fd:
                os.fsync(fd)

    @_port_io
    def publish(self, temporary, path, token):
        self._temporary(temporary, token)
        with self._parent(temporary):
            with self._parent(path) as (target_parent, target_name), self._owned(temporary, token) as fd:
                if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                    raise TransferRefusal("OUTPUT_COLLISION", temporary)
                self._certify(fd, path, token, "file")
                try:
                    # Link the authenticated retained inode, not a mutable source pathname.
                    # The engine separately discards the temporary link after journal commit.
                    linux.link_fd(fd, target_parent, target_name)
                except FileExistsError as exc:
                    raise TransferRefusal("OUTPUT_COLLISION", path) from exc
                os.fsync(target_parent)

    @_port_io
    def list_paths(self, root):
        self._path(root)
        self.tree.check()
        result, pending = [], [root]
        while pending:
            prefix = pending.pop()
            try:
                directory = self.tree.open(prefix, os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                self.tree.check()
                if prefix == root:
                    return ()
                raise
            try:
                for name in sorted(os.listdir(directory)):
                    path = prefix + "/" + name
                    result.append(path)
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
            finally:
                # Reopen queued directories through BoundTree's confined resolver.
                # Neither Python stack depth nor retained descriptors scale with depth.
                os.close(directory)
        return tuple(sorted(result))
