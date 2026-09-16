"""v9 registration adapter: durable setup intent, qualified map IO, catalog CAS.

Physical preparation stays outside SQLite. Conversion remains disabled.
Fill/replica keep using ArchivePublisher; this adapter is map-only.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import threading
import uuid

from modelark import publication_locks as locks, publication_store as store, register
from modelark import registration_publication
from modelark.publication_policy import PublicationRefused


def _id(parent, name):
    return str(uuid.uuid5(uuid.UUID(parent), name))


def _map_receipt(map_uuid, root=None):
    root = Path(root) if root is not None else register.library_root()
    if not root.exists():
        raise PublicationRefused("PUBLICATION_MAP_ROOT_MISSING", root=str(root))
    observed, identity = registration_publication._identity(root)
    if observed != map_uuid:
        raise PublicationRefused("PUBLICATION_MAP_UUID_MISMATCH", expected=map_uuid, observed=observed)
    return {"map_uuid": map_uuid, "root": str(root.resolve()), "identity": list(identity)}


class RegistrationSetup:
    """One PREPARED registration operation under a map-only publication scope."""

    def __init__(self, con, intent):
        if not isinstance(intent, dict) or intent.get("kind") not in {
                "register_new_identity", "register_drive", "register_nas", "ensure_library"}:
            raise PublicationRefused("PUBLICATION_REGISTRATION_INTENT_INVALID")
        self.connection = con
        self.intent = dict(intent)
        self.scope = None
        self.operation_id = str(uuid.uuid4())
        self.batch_id = _id(self.operation_id, "batch:map")
        self.file_id = _id(self.operation_id, "setup")
        self.map_receipt = None
        self.base_revision = None

    def publish(self, *, physical, catalog):
        if self.scope is None:
            raise PublicationRefused("PUBLICATION_COORDINATOR_INACTIVE")
        pair = {"kind": self.intent["kind"], "physical": {
            "archive_path": physical.get("archive_path"),
            "annex_uuid": physical.get("annex_uuid"),
        }}
        receipt = self.map_receipt
        operation_id, batch_id, file_id = self.operation_id, self.batch_id, self.file_id
        outcome = {}
        self.scope.write(lambda _: store.prepare_file(
            self.scope, operation_id=operation_id, batch_id=batch_id, file_id=file_id,
            intent={"catalog_pair": pair, "setup": self.intent}))
        self.scope.write(lambda _: store.advance_file(
            self.scope, operation_id=operation_id, file_id=file_id, phase="LOCAL_VERIFIED",
            proof={"physical": physical}))
        self.scope.write(lambda _: store.advance_file(
            self.scope, operation_id=operation_id, file_id=file_id, phase="TREE_VERIFIED",
            proof={"map": receipt}))
        self.base_revision = store._revision(self.connection)

        def cas(con, frozen):
            store.require_catalog_transition(self.scope, frozen["catalog_pair"])
            outcome["result"] = catalog(con)

        self.scope.write(lambda _: store.advance_file(
            self.scope, operation_id=operation_id, file_id=file_id, phase="CATALOG_PUBLISHED",
            proof={"catalog": pair}, catalog_cas=cas))
        self.scope.write(lambda _: store.propagate_batch(
            self.scope, operation_id=operation_id, batch_id=batch_id, map_proof=receipt))
        self.scope.write(lambda _: store.close_operation(
            self.scope, operation_id=operation_id, observations={}, inventory_proofs={},
            now=datetime.now(timezone.utc).isoformat()))
        return outcome["result"]


@contextmanager
def hold(con, intent):
    """Outer v9 registration scope. Nested callers reuse the same operation."""
    active = registration_publication._SETUP.get()
    if active is not None:
        if active.connection is not con:
            raise PublicationRefused("PUBLICATION_LOCK_SCOPE_NESTED")
        if active.intent != intent:
            raise PublicationRefused("PUBLICATION_REGISTRATION_INTENT_MISMATCH")
        yield active
        return
    identity = store.library(con)
    if identity is None:
        raise PublicationRefused("PUBLICATION_MIGRATION_REQUIRED")
    store.require_clear(con, tree_change=True)
    setup = RegistrationSetup(con, intent)
    with locks.hold(con, (), map_uuid=identity[1], map_only=True) as scope:
        setup.scope = scope
        paths = [row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main"]
        controller_handle, map_handle = scope._authority.handles[0], scope._authority.handles[1]
        token_c = registration_publication._CONTROLLER.set(
            (Path(paths[0]).expanduser().resolve(), threading.get_ident(), controller_handle, con))
        token_m = token = None
        try:
            setup.map_receipt = _map_receipt(identity[1], setup.intent.get("path"))
            map_path = Path(setup.map_receipt["root"]).expanduser().absolute()
            token_m = registration_publication._MAP.set(
                (map_path, registration_publication._identity(map_path), map_handle))
            token = registration_publication._SETUP.set(setup)

            def prepare(_con):
                return store.prepare_operation(
                    scope, operation_id=setup.operation_id, kind="registration",
                    profile_digest=store.digest(setup.map_receipt),
                    batch_files={setup.batch_id: [setup.file_id]},
                    before_state={"intent": setup.intent, "map": setup.map_receipt})

            setup.scope.write(prepare)
            setup.base_revision = store._revision(con)
            yield setup
        finally:
            if token is not None:
                registration_publication._SETUP.reset(token)
            if token_m is not None:
                registration_publication._MAP.reset(token_m)
            registration_publication._CONTROLLER.reset(token_c)
