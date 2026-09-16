"""Registration exclusion; v9 catalog admission uses registration_setup.

Physical preparation never owns a SQLite write transaction. The real map UUID,
not its path or a catalog copy, keys the shared exclusion. These locks do not
turn legacy sync into a receipt.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import stat
import threading

from modelark import drive_fence
from modelark.core import db


_CONTROLLER = ContextVar("registration_controller", default=None)
_BOOTSTRAP = ContextVar("registration_bootstrap", default=None)
_MAP = ContextVar("registration_map", default=None)
_SETUP = ContextVar("registration_setup", default=None)


def child_fds() -> tuple[int, ...]:
    """Keep exclusion in a subprocess if its parent exits during physical IO."""
    controller = _CONTROLLER.get()
    if controller is None:
        return ()
    path, owner, handle, con = controller
    if owner != threading.get_ident():
        raise RuntimeError("registration scope belongs to another thread")
    if con is not None and con.in_transaction:
        raise RuntimeError("registration IO cannot run inside a SQLite transaction")
    handles = [handle]
    bootstrap = _BOOTSTRAP.get()
    if bootstrap is not None:
        handles.append(bootstrap[1])
    current_map = _MAP.get()
    if current_map is not None:
        handles.append(current_map[2])
    return tuple(handle.fileno() for handle in handles)


@contextmanager
def controller(con=None):
    """Hold the catalog controller before any map lock or registration IO."""
    if con is not None and con.in_transaction:
        raise RuntimeError("registration IO cannot run inside a SQLite transaction")
    paths = ([] if con is None else [
        row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main"
    ])
    path = Path(paths[0] if paths and paths[0] else db.DB_PATH).expanduser().resolve()
    active = _CONTROLLER.get()
    if active is not None:
        if active[1] != threading.get_ident() or (con is not None and active[0] != path):
            raise RuntimeError("registration controller scope mismatch")
        child_fds()
        yield
        return
    with drive_fence.hold_controller(path) as handle:
        token = _CONTROLLER.set((path, threading.get_ident(), handle, con))
        try:
            yield
        finally:
            _CONTROLLER.reset(token)


def bootstrap_lock_identity(path):
    """Path-scoped exclusion exists before a newly created map has an annex UUID."""
    return Path(path).expanduser().resolve() / ".modelark-map-bootstrap"


@contextmanager
def bootstrap(path):
    """Serialize every ensure-library caller before creating or selecting a map.

    Callers retain this lock while acquiring the real map UUID lock. The path
    lock never replaces UUID exclusion once that independent identity exists.
    """
    if _CONTROLLER.get() is None:
        raise RuntimeError("registration requires controller before map bootstrap")
    child_fds()
    path = Path(path).expanduser().resolve()
    active = _BOOTSTRAP.get()
    if active is not None:
        if active[0] != path:
            raise RuntimeError("registration bootstrap scope mismatch")
        yield
        return
    if _MAP.get() is not None:
        raise RuntimeError("registration bootstrap must precede map UUID exclusion")
    with drive_fence.hold_controller(bootstrap_lock_identity(path)) as handle:
        token = _BOOTSTRAP.set((path, handle))
        try:
            yield
        finally:
            _BOOTSTRAP.reset(token)


def _identity(path):
    # Git config inspection does not initialize or invoke an annex client.
    from modelark import register

    if path.is_symlink() or (path / ".git").is_symlink():
        raise RuntimeError("registration map namespace is unsafe")
    try:
        root = path.stat()
        gitdir = (path / ".git").stat()
    except OSError as exc:
        raise RuntimeError("registration map namespace is unavailable") from exc
    if not stat.S_ISDIR(root.st_mode) or not stat.S_ISDIR(gitdir.st_mode):
        raise RuntimeError("registration map requires ordinary repository directories")
    annex_uuid = register._git(path, "config", "--local", "--get", "annex.uuid")
    drive_fence.map_lock_path(annex_uuid)  # Canonical UUID grammar; no invented identity.
    return annex_uuid, (root.st_dev, root.st_ino, gitdir.st_dev, gitdir.st_ino)


@contextmanager
def map_write(path):
    """Hold the actual map identity; recheck its namespace around acquisition."""
    if _CONTROLLER.get() is None:
        raise RuntimeError("registration requires controller before map exclusion")
    child_fds()
    path = Path(path).expanduser().absolute()
    identity = _identity(path)
    active = _MAP.get()
    if active is not None:
        if active[:2] != (path, identity):
            raise RuntimeError("registration map scope mismatch")
        yield
        return
    with drive_fence.hold_map(identity[0]) as handle:
        if _identity(path) != identity:
            raise RuntimeError("registration map identity changed while acquiring exclusion")
        token = _MAP.set((path, identity, handle))
        try:
            yield
            if _identity(path) != identity:
                raise RuntimeError("registration map identity changed during preparation")
        finally:
            _MAP.reset(token)


def setup_active():
    return _SETUP.get() is not None


def require_legacy_registration(con):
    """v7/v8 proceeds; v9 requires the durable registration setup adapter."""
    from modelark import proposal, publication_store
    from modelark.execution_session import require_no_live_session
    from modelark.publication_policy import PublicationRefused

    require_no_live_session(con)
    try:
        if publication_store.library(con) is None:
            publication_store.require_clear(con, tree_change=True)
            return
        if _SETUP.get() is None:
            publication_store.require_clear(con, tree_change=True)
            raise proposal.Refusal(
                "REGISTRATION_PUBLICATION_ADAPTER_REQUIRED", {},
                ("complete_registration_publication_setup",),
            )
    except PublicationRefused as exc:
        raise proposal.Refusal(exc.code, exc.evidence, ("inspect_archive_publication",)) from exc


def require_library_setup():
    """Check catalog obligations under the retained controller, before map IO.

    Reuse a caller's catalog connection when nested; standalone initialization
    only reads an existing catalog and must not create or migrate one.
    """
    active = _CONTROLLER.get()
    if active is None:
        raise RuntimeError("library setup requires catalog controller")
    child_fds()
    if active[3] is not None:
        require_legacy_registration(active[3])
    elif active[0].exists():
        con = db.connect(read_only=True)
        try:
            require_legacy_registration(con)
        finally:
            con.close()
