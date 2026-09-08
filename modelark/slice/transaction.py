"""Internal Slice 2 orchestration over explicitly supplied, trusted adapter ports.

No hardware adapter, catalog writer, remote retriever or public Start entry point lives here.
An implementation of DestinationPort must prove confinement, device identity, allocation and
recoverable owned-object creation. SourcePort holds the archive mutation fence, refreshes the
catalog and opens *local original bytes* under that fence. Real adapters belong to Slice 3.
"""
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
import errno
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import socket
from typing import BinaryIO, Protocol
import uuid

from . import domain as d


_RESUMABLE_STATES = frozenset({"approved", "starting", "transferring", "verifying", "stopped",
                              "waiting_source", "blocked_source", "waiting_destination"})


class TransferRefusal(ValueError):
    def __init__(self, code, detail=""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class DestinationBinding:
    device_id: str
    filesystem_id: str
    mount_id: str
    available_bytes: int

    def __post_init__(self):
        if (not all(isinstance(v, str) and v.strip() for v in
                    (self.device_id, self.filesystem_id, self.mount_id))
                or not d._integer(self.available_bytes)):
            raise TransferRefusal("DESTINATION_UNPROVEN")


@dataclass(frozen=True)
class TransferPlan:
    proposal: d.SlicePreview
    destination: DestinationBinding
    version: str = "modelark.slice.transaction.v3"
    metadata_reserve_bytes: int | None = None

    def __post_init__(self):
        d._verify(self.proposal)
        if not self.proposal.source_ready or self.version not in {
                "modelark.slice.transaction.v1", "modelark.slice.transaction.v2", "modelark.slice.transaction.v3"}:
            raise TransferRefusal("PREVIEW_BLOCKED")
        if self.proposal.spec.destination_id != self.destination.device_id:
            raise TransferRefusal("DESTINATION_CHANGED", "binding differs from reviewed destination intent")
        if self.proposal.total_bytes > self.destination.available_bytes:
            raise TransferRefusal("DESTINATION_CAPACITY_INSUFFICIENT")
        if self.version == "modelark.slice.transaction.v3":
            if not d._integer(self.metadata_reserve_bytes):
                raise TransferRefusal("DESTINATION_CAPACITY_UNPROVEN", "explicit metadata reserve required")
        else:
            object.__setattr__(self, "metadata_reserve_bytes", None)
        root = PurePosixPath(self.proposal.spec.destination_root)
        if root.parts[0] == ".modelark-slice-owner":
            raise TransferRefusal("OUTPUT_COLLISION", "reserved control path")
        if self.version == "modelark.slice.transaction.v3":
            self.required_bytes("")

    @property
    def seal(self):
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def to_json(self):
        payload = asdict(self)
        if self.version != "modelark.slice.transaction.v3":
            payload.pop("metadata_reserve_bytes")
        return d._json(payload).decode()

    def receipt(self, tx, files):
        receipt = {"version": self.version, "transaction": tx, "seal": self.seal,
                   "destination": asdict(self.destination), "files": files}
        if self.version != "modelark.slice.transaction.v1":
            receipt.update(plan=json.loads(self.to_json()), topology="direct", status="complete",
                           verification={"content": "sha256-original-bytes", "layout": "authenticated",
                                         "result": "verified", "file_count": len(files)})
        return receipt

    def required_bytes(self, store_root):
        # Exact upper bound on serialized control/receipt payloads: source choice can vary only
        # within the sealed alternatives. Filesystem allocation padding/metadata is the trusted
        # preflight adapter's responsibility and must also fit its explicit sealed reserve.
        tx = "0" * 32
        files = [{"path": str(PurePosixPath(self.proposal.spec.destination_root) / a.repo_id / a.rfilename),
                  "size": a.size_bytes, "sha256": a.sha256,
                  "source": asdict(max(a.sources, key=lambda source: len(d._json(asdict(source)))))}
                 for a in self.proposal.closure]
        control = d._json({"transaction": tx, "seal": self.seal, "store": str(store_root),
                           "destination": asdict(self.destination)})
        minimum = len(control) + len(d._json(self.receipt(tx, files)))
        reserve = minimum if self.metadata_reserve_bytes is None else self.metadata_reserve_bytes
        if reserve < minimum or self.proposal.total_bytes + reserve > self.destination.available_bytes:
            raise TransferRefusal("DESTINATION_CAPACITY_INSUFFICIENT", "artifact and transaction metadata charge")
        return self.proposal.total_bytes + reserve

    @classmethod
    def from_json(cls, payload):
        try:
            obj = json.loads(payload)
            p = obj["proposal"]
            closure = []
            for item in p["closure"]:
                sources = tuple(d.SourceEvidence(d.CopyFact(**s["copy"]), d.DriveFact(**s["drive"]),
                                                 d.AnchorFact(**s["anchor"]), s["digest_provenance"])
                                for s in item.pop("sources"))
                closure.append(d.Artifact(**item, sources=sources))
            proposal = d.SlicePreview(d.SliceSpec(**p["spec"]), p["snapshot_id"], tuple(closure),
                                      tuple(d.Gap(**g) for g in p["gaps"]), p["seal"], p["version"])
            return cls(proposal, DestinationBinding(**obj["destination"]), obj["version"],
                       obj.get("metadata_reserve_bytes"))
        except (KeyError, TypeError, ValueError) as exc:
            raise TransferRefusal("STATE_CORRUPT", str(exc)) from exc


@dataclass(frozen=True)
class Status:
    transaction_id: str
    state: str
    reason: str = ""
    can_write: bool = False


@dataclass(frozen=True)
class ObjectInfo:
    kind: str
    token: str | None
    allocated_bytes: int


class DestinationPort(Protocol):
    """Bound destination operations; never resolve a mountpoint again during writes.

    check must reject changed/system/archive/ambiguous devices, incompatible capabilities,
    and unexplained capacity drift after subtracting the supplied owned allocations. It must
    reserve required_bytes (artifacts plus transaction metadata), crediting only authenticated
    already-owned allocations. The sealed metadata charge includes filesystem rounding, directory
    and temporary-entry costs; the preflight adapter must prove that charge for the bound device.
    inspect must authenticate creation tokens (not filenames/hashes) against actual objects.
    create_* must be exclusive and recoverable after a crash inside the adapter. flush on a
    directory includes its metadata. publish is atomic no-replace and may retain the temp link.
    list_paths returns every descendant, including directories and unexpected objects.
    A concrete adapter must prove these obligations separately; the test port is not production.
    """
    def check(self, binding: DestinationBinding, allocated: int, required_bytes: int) -> None: ...
    def inspect(self, path: str) -> ObjectInfo | None: ...
    def create_directory(self, path: str, token: str) -> None: ...
    def create_file(self, path: str, token: str) -> None: ...
    def append(self, path: str, token: str, data: bytes) -> None: ...
    def discard_temporary(self, path: str, token: str) -> None: ...
    def read(self, path: str) -> AbstractContextManager[BinaryIO]: ...
    def flush(self, path: str) -> None: ...
    def publish(self, temporary: str, path: str, token: str) -> None: ...
    def list_paths(self, root: str) -> tuple[str, ...]: ...


class SourcePort(Protocol):
    """Nonblocking archive fence through refresh, confined open, read, and stream close.

    Yield a fresh CatalogSnapshot and original-byte stream; never retrieve absent content.
    SOURCE_BUSY/SOURCE_MISSING/WAITING_SOURCE are distinct attended refusal codes.
    """
    def open(self, source: d.SourceEvidence) -> AbstractContextManager[tuple[d.CatalogSnapshot, BinaryIO]]: ...


class _Lease:
    """Linux host-wide process fence, independent of paths and immune to lock-file replacement.

    Closing the parent's descriptor does not unlock an inherited child's descriptor. Workers
    spawning an exec child capable of writing must pass fd explicitly; it is CLOEXEC by default.
    """
    def __init__(self, device):
        self.pid = os.getpid()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        key = hashlib.sha256(device.encode()).hexdigest()
        try:
            self.socket.bind("\0modelark-slice-device-" + key)
        except OSError as exc:
            self.socket.close()
            if exc.errno == errno.EADDRINUSE:
                raise TransferRefusal("DESTINATION_BUSY") from exc
            raise

    def close(self):
        self.socket.close()

    def check(self):
        if os.getpid() != self.pid or self.socket.fileno() < 0:
            raise TransferRefusal("EXECUTION_FENCE_LOST")


def _same_source(artifact, candidate, snapshot):
    files = [f for f in snapshot.files if (f.repo_id, f.rfilename) == (artifact.repo_id, artifact.rfilename)]
    copies = [c for c in snapshot.copies if (c.repo_id, c.rfilename, c.drive_label)
              == (artifact.repo_id, artifact.rfilename, candidate.drive.drive_label)]
    drives = [v for v in snapshot.drives if v.drive_label == candidate.drive.drive_label]
    if len(files) != 1 or len(copies) != 1 or len(drives) != 1:
        return False
    file = files[0]
    if (file.size_bytes, file.format, file.quant) != (artifact.size_bytes, artifact.format, artifact.quant):
        return False
    anchors = d._index(snapshot.anchors, lambda a: (a.drive_label, a.identity_epoch, a.generation))
    result = d._source(file, copies[0], drives[0], anchors)
    if isinstance(result, d.Gap) or result[0] != artifact.sha256:
        return False
    expected, actual = asdict(candidate), asdict(result[1])
    expected["drive"].pop("eligibility")
    actual["drive"].pop("eligibility")
    return expected == actual


def start(store, tx, destination: DestinationPort, sources: SourcePort, *, fault=None):
    plan = store.load(tx)
    status = store.status(tx)
    if status.state == "ready":
        raise TransferRefusal("APPROVAL_MISSING")
    if status.state == "complete":
        return status
    required = plan.required_bytes(store.root)
    serial = store.reserve(tx, plan.destination.device_id)
    (fault or (lambda point: None))("reservation_committed")
    try:
        lease = _Lease(plan.destination.device_id)
    except TransferRefusal:
        current = store.status(tx)
        if current.state == "complete" or store.owner(plan.destination.device_id) == tx:
            return current  # Observation only; never hands out a writer capability.
        raise
    try:
        # Another first starter may have won, completed, and released exclusion while this
        # caller was between durable reservation and process bind. Never downgrade completion.
        current = store.status(tx)
        if current.state == "complete":
            lease.close()
            return current
        if current.state not in _RESUMABLE_STATES:
            raise TransferRefusal("NOT_RESUMABLE", current.state)
        destination.check(plan.destination, 0 if not store.events(tx) else _allocated(store, tx, destination), required)
        store.activate(tx, plan.destination.device_id, serial)
        session = Session(store, tx, plan, destination, sources, lease, fault)
        session._fault("reserved")
        session._audit_layout()
        session._control()
        return session
    except TransferRefusal as exc:
        try:
            if exc.code not in {"DESTINATION_BUSY", "STATE_BUSY", "NOT_RESUMABLE"}:
                store.set_state(tx, _refusal_state(exc.code), str(exc))
        finally:
            lease.close()
        raise
    except BaseException:
        lease.close()
        raise


def _operations(store, tx):
    plan = store.load(tx)
    root = PurePosixPath(plan.proposal.spec.destination_root)
    files = {str(root / f.repo_id / f.rfilename): f for f in plan.proposal.closure}
    directories = {str(p) for path in files for p in PurePosixPath(path).parents if str(p) != "."}
    receipt_path = str(root / ".modelark-slice-receipt.json")
    result = {}
    for event, payload in store.events(tx):
        if event == "operation":
            path = payload.get("path")
            kind = payload.get("kind")
            if (payload.get("seal") != plan.seal or payload.get("device") != plan.destination.device_id
                    or not d._path(path) or payload.get("state") not in {"intent", "prepared", "complete"}
                    or not isinstance(payload.get("token"), str) or len(payload["token"]) != 32
                    or any(c not in "0123456789abcdef" for c in payload["token"])):
                raise TransferRefusal("JOURNAL_CORRUPT")
            if kind == "file":
                artifact = files.get(path)
                if artifact is None or (payload.get("size"), payload.get("sha")) != (artifact.size_bytes, artifact.sha256):
                    raise TransferRefusal("JOURNAL_CORRUPT", path)
                if payload.get("source"):
                    recorded = json.loads(d._json(payload["source"]))
                    try:
                        recorded["drive"].pop("eligibility")
                    except (KeyError, TypeError) as exc:
                        raise TransferRefusal("JOURNAL_CORRUPT") from exc
                    approved = []
                    for source in artifact.sources:
                        value = asdict(source)
                        value["drive"].pop("eligibility")
                        approved.append(value)
                    if recorded not in approved:
                        raise TransferRefusal("JOURNAL_CORRUPT", "unsealed source record")
                elif payload["state"] != "intent":
                    raise TransferRefusal("JOURNAL_CORRUPT", "prepared file lacks source proof")
            elif not ((kind == "directory" and path in directories)
                      or (kind == "control" and path == ".modelark-slice-owner")
                      or (kind == "receipt" and path == receipt_path)):
                raise TransferRefusal("JOURNAL_CORRUPT", path)
            temporary = payload.get("temporary")
            expected = str(PurePosixPath(path).parent / (".slice-" + payload["token"]))
            if temporary != (expected if kind in {"file", "receipt", "control"} else None):
                raise TransferRefusal("JOURNAL_CORRUPT", "temporary identity differs")
            previous = result.get(path)
            if previous and any(previous.get(key) != payload.get(key) for key in
                                ("kind", "token", "temporary", "size", "sha")):
                raise TransferRefusal("JOURNAL_CORRUPT", "operation identity changed")
            result[path] = payload
        elif event != "receipt" or payload.get("seal") != plan.seal or payload.get("transaction") != tx:
            raise TransferRefusal("JOURNAL_CORRUPT", "unexpected event")
    return result


def _refusal_state(code):
    return {"STOPPED": "stopped", "WAITING_SOURCE": "waiting_source",
            "WAITING_DESTINATION": "waiting_destination", "SOURCE_BLOCKED": "blocked_source"}.get(
                code, "invalidated" if code.startswith("DESTINATION_") else "failed")


def _allocated(store, tx, destination):
    total, seen = 0, set()
    for op in _operations(store, tx).values():
        for path in (op["path"], op.get("temporary")):
            if not path or path in seen:
                continue
            seen.add(path)
            info = destination.inspect(path)
            if info is not None:
                if info.token != op["token"] or not d._integer(info.allocated_bytes):
                    raise TransferRefusal("OUTPUT_COLLISION", path)
                # A publication can temporarily have two names for one object; tokens identify it.
                if info.token not in seen:
                    total += info.allocated_bytes
                    seen.add(info.token)
    return total


class Session:
    @property
    def can_write(self):
        return self.lease.socket.fileno() >= 0 and os.getpid() == self.lease.pid

    def __init__(self, store, tx, plan, destination, sources, lease, fault):
        self.store, self.transaction_id, self.plan = store, tx, plan
        self.destination, self.sources, self.lease = destination, sources, lease
        self._fault = fault or (lambda point: None)
        self.ops = _operations(store, tx)
        self.head = store.head(tx)
        self._paths = {}
        self._usage = {}
        self._allocated_bytes = 0
        self._verified_files = set()
        self._terminal = False
        self._required_bytes = plan.required_bytes(store.root)
        for op in self.ops.values():
            for path in (op["path"], op.get("temporary")):
                if path:
                    self._paths[path] = op
                    self._owned(path, op)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.lease.socket.fileno() < 0:
            return
        try:
            if os.getpid() == self.lease.pid and self.store.status(self.transaction_id).state in {"transferring", "verifying"}:
                self.store.set_state(self.transaction_id, "stopped")
        finally:
            self.lease.close()  # Never explicit unlock: inherited writer children retain exclusion.

    def _boundary(self):
        self.lease.check()
        stopped, head, owner = self.store.guard(self.transaction_id, self.plan.destination.device_id)
        if owner != self.transaction_id:
            raise TransferRefusal("EXECUTION_FENCE_LOST")
        if head != self.head:
            raise TransferRefusal("JOURNAL_CORRUPT", "journal changed outside its fenced writer")
        if stopped:
            raise TransferRefusal("STOPPED")
        self.destination.check(self.plan.destination, self._allocated_bytes, self._required_bytes)

    def _check(self):
        self._boundary()
        self._verify_control()

    def _verify_control(self, *, required=False):
        op = self.ops.get(".modelark-slice-owner")
        if op is None or op["state"] != "complete":
            if required:
                raise TransferRefusal("CONTROL_CORRUPT", "ownership record is not complete")
            return  # Initial control creation still has only its durable intent.
        if not self._owned(op["path"], op) or not self._matches(op["path"], op):
            raise TransferRefusal("CONTROL_CORRUPT", "ownership record disappeared or changed")

    def _record(self, op, state):
        op = dict(op, state=state)
        self.head = self.store.append(self.transaction_id, "operation", op, expected_head=self.head)
        self.ops[op["path"]] = op
        for path in (op["path"], op.get("temporary")):
            if path:
                self._paths[path] = op
        return op

    def _op(self, path, kind, *, size=0, sha="", source=None):
        previous = self.ops.get(path)
        if previous:
            if (previous["kind"], previous["size"], previous["sha"]) != (kind, size, sha):
                raise TransferRefusal("JOURNAL_CORRUPT", path)
            return previous
        if self.destination.inspect(path) is not None:
            raise TransferRefusal("OUTPUT_COLLISION", path)
        token = uuid.uuid4().hex
        temporary = str(PurePosixPath(path).parent / (".slice-" + token)) if kind in {"file", "receipt", "control"} else None
        op = dict(path=path, kind=kind, size=size, sha=sha, token=token, temporary=temporary,
                  source=source, seal=self.plan.seal, device=self.plan.destination.device_id)
        op = self._record(op, "intent")
        self._fault(kind + "_intent")
        return op

    def _owned(self, path, op):
        info = self.destination.inspect(path)
        if info and (info.token != op["token"] or info.kind != ("directory" if op["kind"] == "directory" else "file")):
            raise TransferRefusal("OUTPUT_COLLISION", path)
        usage = self._usage.setdefault(op["token"], {})
        before = max(usage.values(), default=0)
        if info is None:
            usage.pop(path, None)
        else:
            if not d._integer(info.allocated_bytes):
                raise TransferRefusal("DESTINATION_ALLOCATION_UNPROVEN", path)
            usage[path] = info.allocated_bytes
        self._allocated_bytes += max(usage.values(), default=0) - before
        return info

    def _refresh_parent(self, path):
        parent = str(PurePosixPath(path).parent)
        if parent in self._paths:
            self._owned(parent, self._paths[parent])

    def _check_parents(self, path):
        # The destination root itself is bound by the port. Every descendant ancestor must
        # already be a completed, currently authenticated directory before any child mutation.
        for parent in reversed(PurePosixPath(path).parents):
            if str(parent) == ".":
                continue
            op = self.ops.get(str(parent))
            if (not op or op["kind"] != "directory" or op["state"] != "complete"
                    or self._owned(str(parent), op) is None):
                raise TransferRefusal("OUTPUT_COLLISION", str(parent))

    def _audit_layout(self):
        # Capacity drift cannot detect foreign empty entries. Inspect the approved tree before
        # resuming any destination mutation, and at each artifact boundary, not per byte chunk.
        self._boundary()
        root = PurePosixPath(self.plan.proposal.spec.destination_root)
        for parent in reversed((root, *root.parents)):
            path = str(parent)
            if path != "." and self.destination.inspect(path) is not None:
                op = self._paths.get(path)
                if not op or self._owned(path, op) is None:
                    raise TransferRefusal("OUTPUT_COLLISION", path)
        for path in self.destination.list_paths(str(root)):
            self._boundary()
            op = self._paths.get(path)
            if not op or self._owned(path, op) is None:
                raise TransferRefusal("OUTPUT_COLLISION", path)

    def _control(self):
        path = ".modelark-slice-owner"
        data = d._json({"transaction": self.transaction_id, "seal": self.plan.seal,
                        "store": str(self.store.root), "destination": asdict(self.plan.destination)})
        op = self._op(path, "control", size=len(data), sha=hashlib.sha256(data).hexdigest())
        if op["state"] in {"prepared", "complete"}:
            self._publish(op)
        else:
            self._write(op, io.BytesIO(data))

    def _directory(self, path):
        op = self._op(path, "directory")
        self._check()
        self._check_parents(path)
        existing = self._owned(path, op)
        if existing is not None and op["state"] == "complete":
            return
        if existing is None:
            self.destination.create_directory(path, op["token"])
            self._owned(path, op)
            self._refresh_parent(path)
            self._fault("directory_created")
        self.destination.flush(path)
        self._fault("directory_flushed")
        self.destination.flush(str(PurePosixPath(path).parent))
        self._fault("directory_parent_flushed")
        self._record(op, "complete")
        self._fault("directory_complete")

    def _parents(self, path):
        for parent in reversed(PurePosixPath(path).parents):
            if str(parent) != ".":
                self._directory(str(parent))

    def _matches(self, path, op):
        digest, size = hashlib.sha256(), 0
        with self.destination.read(path) as stream:
            while data := stream.read(1024 * 1024):
                self._boundary()
                digest.update(data)
                size += len(data)
        return size == op["size"] and digest.hexdigest() == op["sha"]

    def _publish(self, op):
        self._check()
        path, temp, kind = op["path"], op["temporary"], op["kind"]
        self._check_parents(path)
        if self._owned(path, op) is None:
            if self._owned(temp, op) is None or not self._matches(temp, op):
                raise TransferRefusal("RECOVERY_DIGEST_MISMATCH", temp)
            self._check_parents(path)
            try:
                self.destination.publish(temp, path, op["token"])
            except FileExistsError as exc:
                raise TransferRefusal("OUTPUT_COLLISION", path) from exc
            self._owned(path, op)
            self._refresh_parent(path)
            self._fault(kind + "_published")
        elif not self._matches(path, op):
            raise TransferRefusal("RECOVERY_DIGEST_MISMATCH", path)
        self.destination.flush(str(PurePosixPath(path).parent))
        self._fault(kind + "_parent_flushed")
        op = self._record(op, "complete")
        self._fault(kind + "_complete")
        if self._owned(temp, op) is not None:
            self._check_parents(temp)
            self.destination.discard_temporary(temp, op["token"])
            self._owned(temp, op)
            self._refresh_parent(temp)
            self.destination.flush(str(PurePosixPath(temp).parent))
        if kind == "file":
            self._verified_files.add(op["token"])
        return op

    def _write(self, op, stream, source=None):
        path, temp, kind = op["path"], op["temporary"], op["kind"]
        self._check()
        self._check_parents(path)
        if self._owned(path, op) is not None:
            raise TransferRefusal("OUTPUT_COLLISION", path)
        # A missing completed output may be restarted. Clear its old prepared proof durably
        # BEFORE touching temporary bytes, so a crash cannot attribute new bytes to an old read.
        op = self._record(dict(op, source=None), "intent")
        if self._owned(temp, op) is not None:
            self.destination.discard_temporary(temp, op["token"])
            self._owned(temp, op)
        self.destination.create_file(temp, op["token"])
        self._owned(temp, op)
        self._refresh_parent(temp)
        self._fault("temporary_created" if kind == "file" else kind + "_created")
        digest, size = hashlib.sha256(), 0
        while data := stream.read(1024 * 1024):
            self._boundary()
            self._check_parents(temp)
            size += len(data)
            if size > op["size"]:
                raise TransferRefusal("SOURCE_DIGEST_MISMATCH")
            self.destination.append(temp, op["token"], data)
            self._owned(temp, op)
            digest.update(data)
            # Intent and ownership certificate authenticate actual in-flight allocation on recovery.
            self._fault("chunk_written" if kind == "file" else kind + "_chunk_written")
        if size != op["size"] or digest.hexdigest() != op["sha"]:
            raise TransferRefusal("SOURCE_DIGEST_MISMATCH")
        self.destination.flush(temp)
        self._fault(kind + "_flushed")
        op = self._record(dict(op, source=source), "prepared")
        self._fault(kind + "_prepared")
        return self._publish(op)

    def _file(self, artifact):
        path = str(PurePosixPath(self.plan.proposal.spec.destination_root) / artifact.repo_id / artifact.rfilename)
        self._parents(path)
        op = self._op(path, "file", size=artifact.size_bytes, sha=artifact.sha256)
        final = self._owned(path, op)
        if op["state"] in {"prepared", "complete"} and op["source"]:
            if final is not None or self._owned(op["temporary"], op) is not None:
                return self._publish(op)
        elif final is not None:
            raise TransferRefusal("UNPREPARED_PUBLICATION", path)
        errors = []
        for source in artifact.sources:
            try:
                with self.sources.open(source) as (snapshot, stream):
                    if not _same_source(artifact, source, snapshot):
                        raise TransferRefusal("SOURCE_CHANGED", source.drive.drive_label)
                    return self._write(op, stream, asdict(source))
            except TransferRefusal as exc:
                if not (exc.code.startswith("SOURCE_") or exc.code == "WAITING_SOURCE"):
                    raise
                errors.append({"code": exc.code, "drive_label": source.drive.drive_label, "detail": exc.detail})
        waiting = any(error["code"] == "WAITING_SOURCE" for error in errors)
        detail = {"repo_id": artifact.repo_id, "rfilename": artifact.rfilename, "candidates": errors}
        raise TransferRefusal("WAITING_SOURCE" if waiting else "SOURCE_BLOCKED", json.dumps(detail))

    def _finish(self):
        self._verify_control(required=True)
        self.store.set_state(self.transaction_id, "verifying")
        ops = self.ops
        files = []
        for artifact in self.plan.proposal.closure:
            path = str(PurePosixPath(self.plan.proposal.spec.destination_root) / artifact.repo_id / artifact.rfilename)
            op = ops[path]
            if op["state"] != "complete" or not op["source"] or not self._owned(path, op) or not self._matches(path, op):
                raise TransferRefusal("VERIFICATION_FAILED", path)
            files.append({"path": path, "size": op["size"], "sha256": op["sha"], "source": op["source"]})
        self._audit_layout()
        receipt = self.plan.receipt(self.transaction_id, files)
        path = str(PurePosixPath(self.plan.proposal.spec.destination_root) / ".modelark-slice-receipt.json")
        data = d._json(receipt)
        op = self._op(path, "receipt", size=len(data), sha=hashlib.sha256(data).hexdigest())
        if op["state"] in {"prepared", "complete"}:
            self._publish(op)
        else:
            self._write(op, io.BytesIO(data))
        self.head = self.store.append(self.transaction_id, "receipt", receipt, expected_head=self.head)
        self.store.complete(self.transaction_id, self.plan.destination.device_id)

    def step(self):
        if self._terminal:
            raise TransferRefusal("NOT_RESUMABLE", "session encountered a terminal refusal")
        try:
            current = self.store.status(self.transaction_id)
        except TransferRefusal as exc:
            if exc.code == "STATE_BUSY":
                self.lease.close()
            raise
        if current.state == "complete":
            return current
        if current.state in {"failed", "invalidated"}:
            self._terminal = True
            self.lease.close()
            raise TransferRefusal("NOT_RESUMABLE", current.state)
        self.lease.check()
        try:
            self._check()
            self._audit_layout()
            if current.state in {"waiting_source", "blocked_source", "waiting_destination"}:
                self.store.set_state(self.transaction_id, "transferring")
            ops = self.ops
            for artifact in self.plan.proposal.closure:
                path = str(PurePosixPath(self.plan.proposal.spec.destination_root) / artifact.repo_id / artifact.rfilename)
                op = ops.get(path)
                if op and op["state"] == "complete" and self._owned(path, op):
                    if op["token"] not in self._verified_files:
                        if not self._matches(path, op):
                            raise TransferRefusal("RECOVERY_DIGEST_MISMATCH", path)
                        self._verified_files.add(op["token"])
                    if self._owned(op["temporary"], op) is not None:
                        self._publish(op)
                    continue
                self._file(artifact)
                return self.store.status(self.transaction_id)
            self._finish()
        except TransferRefusal as exc:
            if exc.code == "STATE_BUSY":
                # Transient shared-state contention does not revoke the approved transaction.
                # Release this writer and let a fresh Start retry its still-durable authority.
                self.lease.close()
                raise
            states = {"STOPPED": "stopped", "WAITING_SOURCE": "waiting_source",
                      "WAITING_DESTINATION": "waiting_destination", "SOURCE_BLOCKED": "blocked_source"}
            state = _refusal_state(exc.code)
            self._terminal = exc.code not in states
            try:
                self.store.set_state(self.transaction_id, state, str(exc))
            finally:
                if self._terminal:
                    self.lease.close()
            if exc.code not in states:
                raise
        except FileExistsError as exc:
            self._terminal = True
            try:
                self.store.set_state(self.transaction_id, "failed", "OUTPUT_COLLISION")
            finally:
                self.lease.close()
            raise TransferRefusal("OUTPUT_COLLISION", str(exc)) from exc
        return self.store.status(self.transaction_id)

    def run(self):
        while (result := self.step()).state in {"transferring", "verifying"}:
            pass
        return result
