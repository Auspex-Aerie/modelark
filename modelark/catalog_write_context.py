"""Connection-scoped revision finalization for existing catalog write adapters.

Only graph_write/session_write install a context, after their authority checks.
Publication callbacks join that transaction; they never BEGIN/COMMIT or bump a
revision themselves. This context proves transaction ownership ONLY, not physical
fences, source identity, publication proofs or maintenance permission.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import get_ident
from typing import Callable


class WriteContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class WriteIdentity:
    kind: str
    session_id: str | None = None
    fencing_token: int | None = None


@dataclass
class _OwnedWrite:
    connection: object
    identity: WriteIdentity
    thread: int = field(default_factory=get_ident)
    finalizers: list[Callable[[int], None]] = field(default_factory=list)
    finalizer_bindings: dict = field(default_factory=dict)
    finalizing: bool = False
    closed: bool = False


_ACTIVE: ContextVar[_OwnedWrite | None] = ContextVar("modelark_catalog_owned_write", default=None)


def _enter(con, identity: WriteIdentity):
    """Adapter-private: enter only AFTER BEGIN and current authority revalidation."""
    previous = _ACTIVE.get()
    if previous is not None and not previous.closed and previous.connection is con:
        raise WriteContextError("CATALOG_WRITE_CONTEXT_NESTED")
    if not con.in_transaction:
        raise WriteContextError("CATALOG_WRITE_TRANSACTION_MISSING")
    return _ACTIVE.set(_OwnedWrite(con, identity))


def _current(con) -> _OwnedWrite:
    current = _ACTIVE.get()
    if (current is None or current.closed or current.connection is not con
            or current.thread != get_ident() or not con.in_transaction):
        raise WriteContextError("CATALOG_WRITE_AUTHORITY_MISSING")
    return current


def require_write(con, expected: WriteIdentity) -> None:
    """Validate explicit adapter identity on this connection, never infer from TX."""
    current = _current(con)
    if current.identity != expected or current.finalizing:
        raise WriteContextError("CATALOG_WRITE_AUTHORITY_MISMATCH")


def defer_revision(con, expected: WriteIdentity, finalize: Callable[[int], None]) -> None:
    """Register a small DB-only marker update in the owning transaction.

    Caller must separately establish its publication/fence authority. Callback
    may raise on a CAS mismatch; that rolls back markers, graph changes, revision
    and session revision together. Callback must not manage transactions or wait
    for locks/processes. No callback may enqueue another callback while finalizing.
    """
    require_write(con, expected)
    if not callable(finalize):
        raise WriteContextError("CATALOG_WRITE_FINALIZER_INVALID")
    _current(con).finalizers.append(finalize)


def defer_revision_once(con, expected, *, key, binding, finalize):
    """Coalesce the same operation's marker within ONE adapter-owned transaction.

    Different bindings on a reused key are a conflict, never last-writer-wins.
    Per-file markers remain separate; this handles the shared operation revision
    when a scoped progress write and a publication transition share the adapter.
    """
    require_write(con, expected)
    current = _current(con)
    if key in current.finalizer_bindings:
        if current.finalizer_bindings[key] != binding:
            raise WriteContextError("CATALOG_WRITE_FINALIZER_CONFLICT")
        return
    defer_revision(con, expected, finalize)
    current.finalizer_bindings[key] = binding


def _finish(con, revision: int | None) -> None:
    """Adapter-private: after the ONE bump, immediately before owning COMMIT."""
    current = _current(con)
    if current.finalizing:
        raise WriteContextError("CATALOG_WRITE_ALREADY_FINALIZED")
    current.finalizing = True
    if revision is None:
        if current.finalizers:
            raise WriteContextError("CATALOG_WRITE_NOOP_HAS_MARKERS")
        return
    if type(revision) is not int or revision < 1:
        raise WriteContextError("CATALOG_WRITE_REVISION_INVALID")
    for callback in current.finalizers:
        callback(revision)


def _exit(token) -> None:
    current = _ACTIVE.get()
    if current is not None:
        current.closed = True
        current.finalizers.clear()
        current.finalizer_bindings.clear()
    _ACTIVE.reset(token)
