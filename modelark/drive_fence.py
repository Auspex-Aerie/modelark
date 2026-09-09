"""Same-host physical fences for the catalog-v3 mutation envelope (RFC-002 / DEC-049 #35-B slice 1).

Advisory ``flock`` primitives for one controller lock and per-drive locks. The controller lock is
keyed on the catalog identity — every process opening the same catalog contends on it, regardless of
``--state-dir``. The per-drive lock is keyed on the immutable drive identity+epoch — the same physical
drive contends across different catalogs and state directories. Lock files live in a fixed host
directory, never under a caller's state dir, so the namespaces above actually collide.

Workflow adapters expand compatible physical identities before using these raw-key primitives.
Only read-only admission snapshots use shared holds; authority-changing operations remain exclusive.
Drive handles close without explicit unlock so inherited children retain exclusion (DEC-133/134).
"""
from __future__ import annotations

import fcntl
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Fixed host-wide lock directory: independent of --state-dir and of the catalog, so the controller and
# drive lock namespaces contend correctly across processes.
_LOCK_DIR = Path(tempfile.gettempdir()) / "modelark-locks"


class FenceUnavailable(Exception):
    """A non-blocking acquire found the lock already held (by this or another process)."""

    def __init__(self, key, evidence=None):
        super().__init__(key)
        self.key = key
        self.evidence = evidence or {}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def controller_lock_key(catalog_path) -> str:
    """Derive the controller lock key from the canonical catalog path (never the state dir)."""
    return _sha("controller:" + str(Path(catalog_path).expanduser().resolve()))


def drive_lock_key(identity, epoch) -> str:
    """Derive the drive lock key from the proven identity + capacity epoch (never a label/mount)."""
    if not identity or not str(identity).strip():
        raise ValueError("a proven drive identity is required for the drive lock key")
    return _sha(f"drive:{identity}:{int(epoch)}")


def controller_lock_path(catalog_path) -> Path:
    return _LOCK_DIR / f"controller-{controller_lock_key(catalog_path)}.lock"


def drive_lock_path(identity, epoch) -> Path:
    return _LOCK_DIR / f"drive-{drive_lock_key(identity, epoch)}.lock"


def _acquire(path: Path, blocking: bool, *, shared=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")                          # noqa: SIM115 — held open for the lock's lifetime
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    flags = mode if blocking else (mode | fcntl.LOCK_NB)
    try:
        fcntl.flock(handle, flags)
    except OSError as exc:                            # BlockingIOError (held) is a subclass of OSError
        handle.close()
        raise FenceUnavailable(str(path), {"path": str(path)}) from exc
    return handle


@contextmanager
def hold_controller(catalog_path, *, blocking=True):
    handle = _acquire(controller_lock_path(catalog_path), blocking)
    try:
        yield handle
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@contextmanager
def hold_drives_sorted(keyed_drives, *, blocking=True):
    """Hold per-drive locks for ``keyed_drives`` (iterable of ``(identity, epoch)``), acquired in
    canonical sorted unique order and closed in reverse. Closing (not explicitly
    unlocking) preserves an inherited child's hold on the same open-file description."""
    with _hold_drives(keyed_drives, blocking=blocking, shared=False) as handles:
        yield handles


@contextmanager
def hold_drive_reads_sorted(keyed_drives, *, blocking=False):
    """Read-only admission snapshots coexist, but exclude every exclusive writer.

    Callers still expand and hold the entire compatible alias set. This confers no
    mutation authority and does not change writer, approval or child lock modes.
    """
    with _hold_drives(keyed_drives, blocking=blocking, shared=True) as handles:
        yield handles


@contextmanager
def _hold_drives(keyed_drives, *, blocking, shared):
    handles = []
    try:
        for identity, epoch in sorted(set(keyed_drives)):
            handles.append(_acquire(drive_lock_path(identity, epoch), blocking, shared=shared))
        yield handles
    finally:
        for handle in reversed(handles):
            try:
                handle.close()
            except OSError:
                pass
