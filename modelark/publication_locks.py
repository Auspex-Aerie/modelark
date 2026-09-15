"""Controller → shared map → physical drives → caller-owned short transaction.

This is the publication lock/transaction-authority adapter, not a payload proof.
All child mutations must inherit child_fence_fds. Scope entry never opens a DB
write transaction, repairs identity, advances a generation or bypasses an operation.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from threading import get_ident
from types import MappingProxyType
from typing import Mapping

from modelark import catalog_write_context as writes, drive_fence, publication_store
from modelark.drive_identity import compatible_keys
from modelark.publication_policy import PublicationRefused


_ACTIVE = ContextVar("modelark_publication_fences", default=None)


@dataclass(frozen=True)
class _FenceAuthority:
    connection: object
    library: tuple[str, str]
    identities: Mapping
    writer: writes.WriteIdentity
    handles: tuple
    thread: int


@dataclass
class _FenceScope:
    _authority: _FenceAuthority
    active: bool = True
    operation_id: str | None = None

    @property
    def connection(self):
        return self._authority.connection

    @property
    def library(self):
        return self._authority.library

    @property
    def identities(self):
        return self._authority.identities

    @property
    def writer(self):
        return self._authority.writer

    @property
    def handles(self):
        return self._authority.handles

    @property
    def thread(self):
        return self._authority.thread

    @property
    def child_fence_fds(self):
        self.require()
        return tuple(handle.fileno() for handle in self.handles)

    def require(self):
        if not self.active or _ACTIVE.get() is not self or self.thread != get_ident():
            raise PublicationRefused("PUBLICATION_FENCE_AUTHORITY_MISSING")
        try:
            for handle in self.handles:
                os.fstat(handle.fileno())
        except (OSError, ValueError) as exc:
            raise PublicationRefused("PUBLICATION_FENCE_AUTHORITY_MISSING") from exc
        if publication_store.library(self.connection) != self.library:
            raise PublicationRefused("PUBLICATION_LIBRARY_CHANGED")
        from modelark.drive_mutation import _fence_identity
        if any(_fence_identity(self.connection, label) != identity
               for label, identity in self.identities.items()):
            raise PublicationRefused("PUBLICATION_DRIVE_IDENTITY_CHANGED")

    def require_transaction(self, con):
        if con is not self.connection:
            raise PublicationRefused("PUBLICATION_CONNECTION_MISMATCH")
        self.require()
        writes.require_write(con, self.writer)
        _require_owner(con, self.writer)

    def require_io(self):
        """Revalidate the current owner/intent before and after proof-factory IO.

        Never hold a catalog transaction over hashing or a native subprocess.
        The brief observation below does not update markers or planner_revision;
        the later owning phase transition still performs its full revision CAS.
        """
        if self.connection.in_transaction:
            raise PublicationRefused("PUBLICATION_IO_TRANSACTION_ACTIVE")
        self.require()
        self.connection.execute("BEGIN")
        try:
            self.require()
            _require_owner(self.connection, self.writer)
            if self.operation_id is not None:
                publication_store._load_bound_operation(self, self.operation_id)
        finally:
            self.connection.rollback()

    def write(self, callback):
        """Existing graph/session adapter plus this operation's revision bookkeeping.

        Fill's own event/progress writes legitimately bump planner_revision too.
        Advance the bound operation marker with those writes under this scope;
        unrelated writers without this capability never refresh its expected
        revision. No nested transaction or second revision bump is introduced.
        """
        self.require()
        previous_operation = self.operation_id

        def apply(con):
            self.require_transaction(con)
            previous_revision = None
            if previous_operation is not None:
                _, _, previous_revision = publication_store.load_owned_operation(self, previous_operation)
            result = callback(con)
            self.require()
            from modelark.proposal import GraphResult
            proven_noop = isinstance(result, GraphResult) and result.proven_noop
            if previous_operation is not None:
                if self.operation_id != previous_operation:
                    raise PublicationRefused("PUBLICATION_OPERATION_BINDING_CHANGED")
                row = con.execute("SELECT state FROM publication_operations WHERE operation_id=?",
                                  [previous_operation]).fetchone()
                if row is None:
                    raise PublicationRefused("PUBLICATION_OPERATION_BINDING_CHANGED")
                if row[0] == "PREPARED" and not proven_noop:
                    publication_store._stamp_operation(self, previous_operation, previous_revision)
                elif row[0] not in {"PREPARED", "CLOSED"}:
                    raise PublicationRefused("PUBLICATION_OPERATION_BINDING_CHANGED")
            return result

        try:
            if self.writer.kind == "session":
                from modelark.execution_session import session_write
                return session_write(self.connection, self.writer.session_id, self.writer.fencing_token, apply)
            from modelark.proposal import graph_write
            return graph_write(self.connection, apply)
        except BaseException:
            self.operation_id = previous_operation
            raise


def _require_owner(con, writer):
    if writer.kind == "session":
        from modelark.drive_mutation import _require_live_session_token
        _require_live_session_token(con, writer.session_id, writer.fencing_token)
    else:
        from modelark.execution_session import require_no_live_session
        require_no_live_session(con)


@contextmanager
def hold(con, drive_labels, *, map_uuid, session_id=None, fencing_token=None, operation_id=None, blocking=False):
    """Acquire a fresh complete publication scope; never reenter existing fences.

    `map_uuid` must come from the independently qualified map profile; comparing
    it with the durable library binding here does not qualify a caller's profile.
    Conversion remains unavailable; maintenance ownership is checked separately
    against each captured terminal/sessionless dirty-generation binding.
    """
    if con.in_transaction:
        raise PublicationRefused("PUBLICATION_LOCK_ORDER_TRANSACTION_ACTIVE")
    if _ACTIVE.get() is not None:
        raise PublicationRefused("PUBLICATION_LOCK_SCOPE_NESTED")
    if (session_id is None) != (fencing_token is None):
        raise PublicationRefused("PUBLICATION_OWNER_PAIR_REQUIRED")
    if operation_id is not None:
        publication_store.canonical_uuid(operation_id)
    labels = tuple(sorted(set(drive_labels)))
    if not labels:
        raise PublicationRefused("PUBLICATION_PARTICIPANTS_REQUIRED")
    identity = publication_store.library(con)
    if identity is None or identity[1] != map_uuid:
        raise PublicationRefused("PUBLICATION_LIBRARY_UNPROVEN")
    paths = [r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"]
    if len(paths) != 1 or not paths[0]:
        raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_UNPROVEN")
    writer = writes.WriteIdentity("graph") if session_id is None else writes.WriteIdentity(
        "session", str(session_id), int(fencing_token))
    from modelark.drive_mutation import _fence_identity
    with ExitStack() as stack:
        controller = stack.enter_context(drive_fence.hold_controller(paths[0], blocking=blocking))
        if publication_store.library(con) != identity:
            raise PublicationRefused("PUBLICATION_LIBRARY_CHANGED")
        map_handle = stack.enter_context(drive_fence.hold_map(map_uuid, blocking=blocking))
        identities = {label: _fence_identity(con, label) for label in labels}
        handles = stack.enter_context(drive_fence.hold_drives_sorted(
            compatible_keys(identities.values()), blocking=blocking))
        _require_owner(con, writer)
        authority = _FenceAuthority(con, identity, MappingProxyType(dict(identities)), writer,
                                    (controller, map_handle, *handles), get_ident())
        scope = _FenceScope(authority, operation_id=operation_id)
        token = _ACTIVE.set(scope)
        try:
            scope.require()
            yield scope
        finally:
            scope.active = False
            _ACTIVE.reset(token)
