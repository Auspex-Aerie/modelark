"""Internal assembly for the launch-guarded attended direct Slice CLI.

All catalogs and attachments are explicit or sealed in private state. This module
does not acquire a second application launch gate: the CLI already retains it.
Never call these internal functions to bypass ModelArk's launch exclusion.
"""
from dataclasses import asdict, replace
import hashlib
import importlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from . import domain as d
from . import state as private_state
from . import transaction as t
from .authority import RESUMABLE_STATES
from .catalog import read_catalog
from .destination import UsbDestination, _port_io
from .hardware import LinuxObserver
from .linux import BoundTree
from .local_source import LocalArchiveReader
from .paths import canonical_attachment
from .io_errors import classify_io, io_boundary
from .sources import FencedSources
from .state import Store


def _capacity():
    return importlib.import_module("modelark.slice.capacity")


def _store():
    """Translate known private-state bootstrap/access failures at the operator boundary."""
    try:
        return Store()
    except t.TransferRefusal:
        raise  # Preserve existing typed STATE_BUSY and other precise refusals.
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise t.TransferRefusal("STATE_UNAVAILABLE", str(exc)) from exc


def _archives(catalog_path):
    """Exclude the complete registered archive fleet, not only selected sources."""
    path = Path(catalog_path).expanduser().resolve()
    con = None
    try:
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        if con.execute("PRAGMA user_version").fetchone()[0] != 7:
            raise d.SliceRefusal("CATALOG_VERSION_UNSUPPORTED", "direct delivery requires catalog v7")
        return tuple(SimpleNamespace(fs_uuid=uuid, serial=serial)
                     for uuid, serial in con.execute("SELECT fs_uuid,serial FROM drives ORDER BY drive_label"))
    except sqlite3.Error as exc:
        raise d.SliceRefusal("CATALOG_UNAVAILABLE", str(exc)) from exc
    finally:
        if con is not None:
            con.close()


def _result(outcome, *, stop_requested=False):
    result = asdict(outcome)
    result["ok"] = outcome.state not in {"failed", "invalidated", "blocked_source", "waiting_source",
                                        "waiting_destination"}
    result["stop_requested"] = stop_requested
    return result


def preview(catalog_path, destination_path, repo_ids, root):
    if root is None:
        from .folder_operator import preview as folder_preview
        return folder_preview(catalog_path, destination_path, repo_ids)
    catalog_path = str(Path(catalog_path).expanduser().resolve())
    spec = d.SliceSpec(tuple(repo_ids), "pending-device-observation", root)
    snapshot = read_catalog(catalog_path, spec)
    proposal = d.preview(spec, snapshot)
    if not proposal.source_ready:
        return {"ok": False, "state": "blocked", "executable": False,
                "gaps": [asdict(gap) for gap in proposal.gaps], "preview": asdict(proposal)}
    archives = _archives(catalog_path)
    observer = LinuxObserver()
    path = canonical_attachment(destination_path)
    evidence = observer.observe(path, writable=True, archives=archives)
    proposal = d.preview(replace(spec, destination_id=evidence.device_id), snapshot)
    capacity = _capacity()
    with io_boundary(None, 'DESTINATION_UNPROVEN', 'destination preview setup/teardown failed'), \
            BoundTree(path, writable=True) as tree, io_boundary(
            lambda: observer.recheck_attachment(tree, evidence),
            'DESTINATION_UNPROVEN', 'destination preview failed'):
        caps = capacity.capture(tree, evidence, proposal, private_state.HOST_STATE_DIR.absolute())
        admission = {"version": "modelark.slice.direct.v1", "catalog": catalog_path, "capacity": caps}
        binding = t.DestinationBinding(evidence.device_id, evidence.fs_uuid,
                                       "direct-v1:" + hashlib.sha256(d._json(admission)).hexdigest(),
                                       evidence.available_bytes)
        reserve = capacity.metadata_reserve_bytes(proposal, binding, caps, private_state.HOST_STATE_DIR.absolute())
        plan = t.TransferPlan(proposal, binding, metadata_reserve_bytes=reserve)
        fresh = observer.observe(path, writable=True, archives=archives)
        capacity.verify(tree, fresh, proposal, caps)
    approval = d.approve(proposal, expected_seal=proposal.seal, current_snapshot=snapshot)
    store = _store()
    tx = store.create(plan, approval, admission=admission)
    return {"ok": True, "state": "ready", "executable": False, "transaction_id": tx,
            "seal": plan.seal, "plan": asdict(plan), "admission": admission}


def approve(tx, seal):
    store = _store()
    plan = store.load(tx)
    admission = store.load_admission(tx)
    if seal != plan.seal:
        raise t.TransferRefusal("PREVIEW_STALE", "reviewed transaction seal differs")
    snapshot = read_catalog(admission["catalog"], plan.proposal.spec)
    d.approve(plan.proposal, expected_seal=plan.proposal.seal, current_snapshot=snapshot)
    store.approve(tx, expected_seal=seal)
    return _result(store.status(tx))


class _CheckedDestination(UsbDestination):
    def __init__(self, tree, binding, store, tx, observer, path, proposal, caps, catalog, evidence):
        self._observer, self._attachment_path = observer, path
        self._proposal, self._caps, self._catalog = proposal, caps, catalog
        self._evidence = evidence
        self._budget = None
        super().__init__(tree, binding, store, tx)

    @_port_io
    def check(self, binding, allocated, required_bytes):
        # Do not put capacity.verify into BoundTree.verify: it itself opens
        # confined paths. Core boundaries call this gate before every mutation.
        first = self._budget is None
        self._budget = (binding, allocated, required_bytes)
        self._validate(refresh=first)

    def _validate(self, *, refresh, reconcile=True):
        binding, allocated, required_bytes = self._budget
        archives = _archives(self._catalog)
        self._observer.check_attachment(self.tree, self._evidence, archives=archives)
        if refresh:
            self._evidence = self._observer.observe(self._attachment_path, writable=True, archives=archives)
        if (self._evidence.device_id, self._evidence.fs_uuid) != (binding.device_id, binding.filesystem_id):
            raise t.TransferRefusal("DESTINATION_CHANGED", "destination differs from reviewed device")
        _capacity().verify(self.tree, self._evidence, self._proposal, self._caps, adapter=self,
                           refresh_volume=refresh)
        if reconcile:
            super().check(binding, allocated, required_bytes)

    @_port_io
    def create_file(self, path, token):
        # Recovery may have just discarded an old temp since the engine's last
        # check. Refresh physical eligibility, not its now-stale allocation total.
        self._validate(refresh=True, reconcile=False)
        return super().create_file(path, token)

    @_port_io
    def publish(self, temporary, path, token):
        self._validate(refresh=True, reconcile=False)
        return super().publish(temporary, path, token)

    @_port_io
    def list_paths(self, root):
        # Core audits occur at Start, each artifact/attended resume and final receipt.
        self._validate(refresh=True, reconcile=False)
        return super().list_paths(root)

    def _recheck_attachment(self):
        self._observer.recheck_attachment(self.tree, self._evidence)


def _acknowledge_interrupt(store, tx, destination, sources, session=None):
    store.request_stop(tx)
    try:
        if session is not None:
            return session.run()
        # Interrupted Start releases its kernel attempt in transaction.start's
        # BaseException handler. A fresh claim sees the unacknowledged stop and
        # acknowledges it BEFORE destination checking or control/object writes.
        result = t.start(store, tx, destination, sources)
        if isinstance(result, t.Status):
            return result
        with result:
            return result.run()
    except t.TransferRefusal as exc:
        if exc.code != "STOPPED":
            raise
        return t.Status(tx, "stopped", "STOPPED")


def start(tx, destination_path, attachments):
    store = _store()
    plan = store.load(tx)
    current = store.status(tx)
    if current.state == "complete":
        return _result(current)
    if current.state == "ready":
        raise t.TransferRefusal("APPROVAL_MISSING")
    if getattr(plan, "session_only", False):
        from .fat32_operator import start as fat32_start
        return fat32_start(store, tx, plan, destination_path, attachments)
    if current.state not in RESUMABLE_STATES:
        raise t.TransferRefusal("NOT_RESUMABLE", current.state)
    if plan.is_folder:
        from .folder_operator import start as folder_start
        return folder_start(store, tx, plan, destination_path, attachments)
    admission = store.load_admission(tx)
    allowed = {source.drive.drive_label for artifact in plan.proposal.closure for source in artifact.sources}
    if not isinstance(attachments, dict) or set(attachments) - allowed:
        raise t.TransferRefusal("SOURCE_ATTACHMENT_UNSEALED", "attachment label is not a reviewed source")
    archives = _archives(admission["catalog"])
    observer = LinuxObserver()
    path = canonical_attachment(destination_path)
    bound = False
    try:
        with BoundTree(path, writable=True) as tree:
            bound = True
            evidence = observer.observe(path, writable=True, archives=archives)
            if (evidence.device_id, evidence.fs_uuid) != (plan.destination.device_id, plan.destination.filesystem_id):
                raise t.TransferRefusal("DESTINATION_CHANGED", "destination differs from reviewed device")
            # Recovery may already own charged allocations. Full free-space
            # reconciliation belongs to the authenticated adapter, not fresh-start admission.
            destination = _CheckedDestination(tree, plan.destination, store, tx, observer, path,
                                              plan.proposal, admission["capacity"], admission["catalog"], evidence)
            sources = FencedSources(admission["catalog"], LocalArchiveReader(attachments, observer=observer))
            try:
                session = t.start(store, tx, destination, sources)
            except KeyboardInterrupt:
                return _result(_acknowledge_interrupt(store, tx, destination, sources), stop_requested=True)
            if isinstance(session, t.Status):
                return _result(session)
            with session:
                try:
                    return _result(session.run())
                except KeyboardInterrupt:
                    return _result(_acknowledge_interrupt(store, tx, destination, sources, session),
                                   stop_requested=True)
    except FileNotFoundError as exc:
        if bound:
            raise t.TransferRefusal('DESTINATION_IO_FAILED',
                                    'destination setup/teardown failed: ' + str(exc)) from exc
        raise t.TransferRefusal("WAITING_DESTINATION", "destination attachment is absent; no new attempt retained") from exc
    except OSError as exc:
        # Port operations classify while their descriptors are alive. This is only
        # bootstrap/teardown IO, without trustworthy retained evidence for a wait.
        raise classify_io(exc, None, t.TransferRefusal(
            'DESTINATION_IO_FAILED', 'destination setup/teardown failed: ' + str(exc))) from exc


def status(tx):
    store = _store()
    store.load(tx)
    return _result(store.status(tx), stop_requested=store.stop_requested(tx))


def stop(tx):
    store = _store()
    store.load(tx)
    current = store.status(tx)
    if current.state not in {"complete", "failed", "invalidated"}:
        store.request_stop(tx)
    return _result(store.status(tx), stop_requested=store.stop_requested(tx))
