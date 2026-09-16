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


def _catalog_result(setup):
    from modelark.proposal import GraphResult
    kind, label = setup.intent.get("kind"), setup.intent.get("label")
    if kind == "register_new_identity":
        row = setup.connection.execute(
            "SELECT annex_uuid FROM drives WHERE drive_label=?", [label]).fetchone()
        return GraphResult(proven_noop=True, value={
            "archive_path": setup.intent.get("archive_path"),
            "annex_uuid": None if row is None else row[0],
        })
    if kind in {"register_drive", "register_nas"}:
        row = setup.connection.execute(
            "SELECT plan_id FROM plan_drives WHERE drive_label=?", [label]).fetchone()
        return GraphResult(proven_noop=True, value=None if row is None else row[0])
    return GraphResult(proven_noop=True)


def leftover(con, intent, *, cataloged=False, observed=None):
    """Matching PREPARED registration the owner may resume, or None.

    Kernel device nodes and path spelling are locators, not identity. A
    cataloged leftover additionally requires live durable facts (serial,
    filesystem UUID, annex UUID) to agree with the drives row.
    """
    match = _matching_prepared(con, intent)
    if match is None or not cataloged:
        return match
    operation_id, _batch_id, file_id, saved = match
    row = con.execute(
        "SELECT phase FROM publication_files WHERE operation_id=? AND file_id=?",
        [operation_id, file_id]).fetchone()
    if row is None or row[0] != "CATALOG_PUBLISHED":
        return None
    if saved.get("kind") in {"register_drive", "register_new_identity"}:
        facts = _cataloged_drive_facts(con, saved.get("label") or intent.get("label"))
        if not _durable_match(facts, observed or intent):
            return None
    return match


def observe_locator(dev):
    """Resolve a kernel locator to durable block facts. `/dev` is not stored."""
    try:
        disk = register._parent_disk(dev)
        serial = register._run("lsblk", "-dno", "SERIAL", disk, check=False).stdout.strip() or None
        uuids = register._run("lsblk", "-no", "UUID", dev, check=False).stdout.splitlines()
        fs_uuid = next((line.strip() for line in uuids if line.strip()), None)
        annex_uuid = None
        mount = register._mountpoint(dev)
        if mount:
            archive = Path(mount) / register.ARCHIVE_SUBDIR
            if (archive / ".git").exists():
                annex_uuid = register._git(
                    archive, "config", "--local", "--get", "annex.uuid", check=False) or None
        return {"serial": serial, "fs_uuid": fs_uuid, "annex_uuid": annex_uuid}
    except (OSError, RuntimeError, TypeError, ValueError, FileNotFoundError):
        return {"serial": None, "fs_uuid": None, "annex_uuid": None}


def _cataloged_drive_facts(con, label):
    if not label:
        return None
    row = con.execute(
        "SELECT serial, fs_uuid, annex_uuid FROM drives WHERE drive_label=?", [label]).fetchone()
    if row is None:
        return None
    return {"serial": row[0], "fs_uuid": row[1], "annex_uuid": row[2]}


def _durable_match(cataloged, observed):
    """Same disk iff annex UUID matches, or both filesystem UUID and serial match.

    Annex UUID is the archive. USB-bridge serial spelling must not veto it
    (DEC-133). A cloned filesystem UUID with no serial still must not match.
    """
    if not cataloged or not observed:
        return False
    annex = cataloged.get("annex_uuid") or None
    live_annex = observed.get("annex_uuid") or None
    if annex and live_annex:
        return annex == live_annex
    fs_uuid = cataloged.get("fs_uuid") or None
    serial = cataloged.get("serial") or None
    live_fs = observed.get("fs_uuid") or None
    live_serial = observed.get("serial") or None
    if fs_uuid and live_fs and fs_uuid != live_fs:
        return False
    if serial and live_serial and serial != live_serial:
        return False
    return bool(fs_uuid and serial and fs_uuid == live_fs and serial == live_serial)


def _intents_match(saved, intent):
    if saved.get("kind") != intent.get("kind"):
        return False
    if saved.get("kind") == "register_nas":
        return saved.get("label") == intent.get("label") and saved.get("remote") == intent.get("remote")
    if saved.get("kind") in {"register_drive", "register_new_identity"}:
        return saved.get("label") == intent.get("label")
    if saved.get("kind") == "ensure_library":
        return True
    return saved == intent


def _matching_prepared(con, intent):
    rows = con.execute(
        "SELECT operation_id, binding_json, binding_digest FROM publication_operations "
        "WHERE kind='registration' AND state='PREPARED'").fetchall()
    if len(rows) != 1:
        return None
    binding = store._unseal(rows[0][1], rows[0][2])
    saved = binding.get("before_state", {}).get("intent") or {}
    if not _intents_match(saved, intent):
        return None
    files = binding.get("batch_files") or {}
    if len(files) != 1:
        return None
    batch_id, children = next(iter(files.items()))
    if len(children) != 1:
        return None
    return rows[0][0], batch_id, children[0], saved


def _stored_physical(setup):
    row = setup.connection.execute(
        "SELECT intent_json, intent_digest FROM publication_files WHERE operation_id=? AND file_id=?",
        [setup.operation_id, setup.file_id]).fetchone()
    if row is None:
        return {}
    frozen = store._unseal(row[0], row[1])
    intent = frozen.get("intent") if isinstance(frozen.get("intent"), dict) else frozen
    physical = (intent.get("catalog_pair") or {}).get("physical")
    return physical if isinstance(physical, dict) else {}


def _map_receipt(map_uuid, root=None):
    root = Path(root) if root is not None else register.library_root()
    if not root.exists():
        raise PublicationRefused("PUBLICATION_MAP_ROOT_MISSING", root=str(root))
    observed, identity = registration_publication._identity(root)
    if observed != map_uuid:
        raise PublicationRefused("PUBLICATION_MAP_UUID_MISMATCH", expected=map_uuid, observed=observed)
    head = register._git(root, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    annex = register._git(root, "rev-parse", "--verify", "refs/heads/git-annex", check=False)
    remotes = [line for line in register._git(root, "remote", check=False).splitlines() if line]
    refs = {}
    if head:
        refs["HEAD"] = head
    if annex:
        refs["git-annex"] = annex
    return {"map_uuid": map_uuid, "root": str(root.resolve()), "identity": list(identity),
            "refs": refs, "remotes": remotes}


def _abort_unfinished(scope, operation_id):
    """Drop a PREPARED registration that never published catalog rows.

    CATALOG_PUBLISHED leftovers stay durable for later resume. This only unblocks
    retry after a failed attempt that did not insert membership. Uses graph_write
    directly so a CLOSED operation does not go through load_owned_operation.
    """
    from modelark.proposal import GraphResult, graph_write

    def write(con):
        row = con.execute("SELECT state FROM publication_operations WHERE operation_id=?",
                          [operation_id]).fetchone()
        if row is None or row[0] != "PREPARED":
            return GraphResult(proven_noop=True)
        if con.execute("SELECT 1 FROM publication_files WHERE operation_id=? AND phase='CATALOG_PUBLISHED'",
                       [operation_id]).fetchone():
            return GraphResult(proven_noop=True)
        con.execute("DELETE FROM publication_actions WHERE operation_id=?", [operation_id])
        con.execute("DELETE FROM publication_files WHERE operation_id=?", [operation_id])
        con.execute("DELETE FROM publication_batches WHERE operation_id=?", [operation_id])
        con.execute("DELETE FROM publication_participants WHERE operation_id=?", [operation_id])
        con.execute("DELETE FROM publication_operations WHERE operation_id=?", [operation_id])
        return GraphResult(proven_noop=False)

    graph_write(scope.connection, write)


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

    def _file_phase(self):
        row = self.connection.execute(
            "SELECT phase FROM publication_files WHERE operation_id=? AND file_id=?",
            [self.operation_id, self.file_id]).fetchone()
        return None if row is None else row[0]

    def _batch_phase(self):
        row = self.connection.execute(
            "SELECT phase FROM publication_batches WHERE operation_id=? AND batch_id=?",
            [self.operation_id, self.batch_id]).fetchone()
        return None if row is None else row[0]

    def publish(self, *, physical, catalog):
        if self.scope is None:
            raise PublicationRefused("PUBLICATION_COORDINATOR_INACTIVE")
        pair = {"kind": self.intent["kind"], "physical": {
            "archive_path": physical.get("archive_path"),
            "annex_uuid": physical.get("annex_uuid"),
        }}
        receipt = _map_receipt(store.library(self.connection)[1], self.intent.get("path"))
        self.map_receipt = receipt
        operation_id, batch_id, file_id = self.operation_id, self.batch_id, self.file_id
        outcome = {}
        phase = self._file_phase()
        if phase is None:
            self.scope.write(lambda _: store.prepare_file(
                self.scope, operation_id=operation_id, batch_id=batch_id, file_id=file_id,
                intent={"catalog_pair": pair, "setup": self.intent}))
            phase = "PREPARED"
        if phase == "PREPARED":
            self.scope.write(lambda _: store.advance_file(
                self.scope, operation_id=operation_id, file_id=file_id, phase="LOCAL_VERIFIED",
                proof={"physical": physical}))
            phase = "LOCAL_VERIFIED"
        if phase == "LOCAL_VERIFIED":
            self.scope.write(lambda _: store.advance_file(
                self.scope, operation_id=operation_id, file_id=file_id, phase="TREE_VERIFIED",
                proof={"map": receipt}))
            phase = "TREE_VERIFIED"
        self.base_revision = store._revision(self.connection)
        if phase == "TREE_VERIFIED":
            def cas(con, frozen):
                store.require_catalog_transition(self.scope, frozen["catalog_pair"])
                outcome["result"] = catalog(con)

            self.scope.write(lambda _: store.advance_file(
                self.scope, operation_id=operation_id, file_id=file_id, phase="CATALOG_PUBLISHED",
                proof={"catalog": pair}, catalog_cas=cas))
            phase = "CATALOG_PUBLISHED"
        elif phase == "CATALOG_PUBLISHED":
            outcome["result"] = _catalog_result(self)
        if self._batch_phase() == "PREPARED":
            self.scope.write(lambda _: store.propagate_batch(
                self.scope, operation_id=operation_id, batch_id=batch_id, map_proof=receipt))
        if self.connection.execute(
                "SELECT state FROM publication_operations WHERE operation_id=?",
                [operation_id]).fetchone()[0] != "CLOSED":
            self.scope.write(lambda _: store.close_operation(
                self.scope, operation_id=operation_id, observations={}, inventory_proofs={},
                now=datetime.now(timezone.utc).isoformat()))
        return outcome.get("result")


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
    leftover = _matching_prepared(con, intent)
    if leftover is None:
        store.require_clear(con, tree_change=True)
    setup = RegistrationSetup(con, intent)
    if leftover is not None:
        setup.operation_id, setup.batch_id, setup.file_id, setup.intent = leftover
    with locks.hold(con, (), map_uuid=identity[1], map_only=True,
                    operation_id=setup.operation_id if leftover else None) as scope:
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

            if leftover is None:
                setup.scope.write(prepare)
            setup.base_revision = store._revision(con)
            try:
                yield setup
            finally:
                _abort_unfinished(scope, setup.operation_id)
        finally:
            if token is not None:
                registration_publication._SETUP.reset(token)
            if token_m is not None:
                registration_publication._MAP.reset(token_m)
            registration_publication._CONTROLLER.reset(token_c)
