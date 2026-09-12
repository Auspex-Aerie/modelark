"""Read-only source-use gate sharing the archive writer's nonblocking drive fence."""
from contextlib import ExitStack, closing, contextmanager
import json
from pathlib import Path
import sqlite3

from modelark import drive_fence
from modelark.drive_identity import FenceIdentity, UnprovenFenceIdentity
from .catalog import read_catalog
from .domain import SliceRefusal, SliceSpec
from .transaction import TransferRefusal


class _SourceRead:
    """Translate only IO performed by the source stream, never its consumer's IO."""
    def __init__(self, stream, label):
        self.stream, self.label = stream, label

    def read(self, size=-1):
        try:
            return self.stream.read(size)
        except FileNotFoundError as exc:
            raise TransferRefusal("SOURCE_MISSING", self.label) from exc
        except OSError as exc:
            raise TransferRefusal("SOURCE_READ_FAILED", f"{self.label}: {exc}") from exc


class FencedSources:
    """Compose fresh catalog evidence with an injected retrieval-disabled local reader.

    reader.open(candidate) must prove attachment identity and descriptor confinement before
    yielding original bytes. Slice 2 intentionally ships no real archive reader/decompressor.
    No controller fence, mutation envelope, schema bootstrap or archive repair is acquired.
    """
    def __init__(self, catalog_path, reader):
        self.catalog_path, self.reader = catalog_path, reader

    @property
    def requires_preflight(self):
        return getattr(self.reader, "policy", None) is not None

    @contextmanager
    def open(self, candidate):
        with self._open(candidate) as result:
            yield result

    @contextmanager
    def open_checked(self, candidate, check):
        with self._open(candidate, check=check) as result:
            yield result

    @contextmanager
    def open_polled(self, candidate, poll):
        """Poll caller authority without destination I/O while reading/decoding.

        Source guards/fences are unchanged. The consumer must perform full
        destination checks before its next destination access; destination loss
        during a source-only wait is not promised immediate detection here.
        """
        with self._open(candidate, check=poll) as result:
            yield result

    @contextmanager
    def _open(self, candidate, *, check=None, inspect=False, artifact=None):
        def identity(drive):
            return FenceIdentity(drive.fs_uuid, drive.annex_uuid, drive.serial,
                                 drive.filesystem_capacity_bytes, drive.identity_epoch,
                                 drive.identity_fingerprint)

        with ExitStack() as stack:
            try:
                captured = identity(candidate.drive)
                keys = captured.lock_keys()
                stack.enter_context(drive_fence.hold_drives_sorted(keys, blocking=False))
                spec = SliceSpec((candidate.copy.repo_id,), "source-evidence", "unused")
                snapshot = read_catalog(self.catalog_path, spec)
                fresh = next((drive for drive in snapshot.drives
                              if drive.drive_label == candidate.drive.drive_label), None)
                if fresh is None or identity(fresh) != captured:
                    raise TransferRefusal("SOURCE_EVIDENCE_UNAVAILABLE", "source identity changed")
                serial_args = {}
                if getattr(self.reader, 'supports_serial_evidence', False):
                    from modelark.serial_evidence import load_bridge_binding
                    from modelark.serial_identity import SerialIdentityUnproven
                    # No caller-provided alias or serialized extension. Resolve
                    # only against the exact source anchor already in the seal,
                    # under these same physical fences and one read transaction.
                    try:
                        with closing(sqlite3.connect(
                                Path(self.catalog_path).resolve().as_uri() + '?mode=ro', uri=True)) as con:
                            con.execute('BEGIN')
                            binding = load_bridge_binding(con, candidate.drive.drive_label,
                                                          sealed_source=candidate)
                    except (sqlite3.Error, SerialIdentityUnproven) as exc:
                        raise TransferRefusal('SOURCE_EVIDENCE_UNAVAILABLE', str(exc)) from exc
                    serial_args['_serial_evidence'] = binding
                if inspect:
                    from .transaction import _same_source
                    if artifact is None or not _same_source(artifact, candidate, snapshot):
                        raise TransferRefusal("SOURCE_CHANGED", candidate.drive.drive_label)
                    stream = stack.enter_context(self.reader.inspect(candidate, check=check, **serial_args))
                elif check is not None and getattr(self.reader, "policy", None) is not None:
                    stream = stack.enter_context(self.reader.open(candidate, check=check, **serial_args))
                else:
                    stream = stack.enter_context(self.reader.open(candidate, **serial_args))
            except UnprovenFenceIdentity as exc:
                raise TransferRefusal("SOURCE_EVIDENCE_UNAVAILABLE", str(exc)) from exc
            except drive_fence.FenceUnavailable as exc:
                raise TransferRefusal("SOURCE_BUSY", candidate.drive.drive_label) from exc
            except SliceRefusal as exc:
                raise TransferRefusal("SOURCE_EVIDENCE_UNAVAILABLE", str(exc)) from exc
            except FileNotFoundError as exc:
                raise TransferRefusal("SOURCE_MISSING", candidate.drive.drive_label) from exc
            # Do not translate exceptions thrown by the destination/consumer inside this yield.
            yield snapshot, _SourceRead(stream, candidate.drive.drive_label)

    def preflight(self, proposal, check=lambda: None):
        """All attached alternatives, before output/one-attempt authority exists.

        An absent alternative is not checked. It preserves the existing wait
        option when no attached candidate is currently usable. One bad encoding
        never invalidates another sealed representation of the same original.
        """
        if getattr(self.reader, "policy", None) is None:
            return
        for artifact in proposal.closure:
            usable, errors = False, []
            for candidate in artifact.sources:
                check()
                try:
                    with self._open(candidate, check=check, inspect=True, artifact=artifact):
                        pass
                    usable = True
                except TransferRefusal as exc:
                    if not (exc.code.startswith("SOURCE_") or exc.code == "WAITING_SOURCE"):
                        raise
                    errors.append({"code": exc.code, "drive_label": candidate.drive.drive_label,
                                   "detail": exc.detail})
            if not usable:
                waiting = any(error["code"] == "WAITING_SOURCE" for error in errors)
                raise TransferRefusal("WAITING_SOURCE" if waiting else "SOURCE_BLOCKED", json.dumps({
                    "repo_id": artifact.repo_id, "rfilename": artifact.rfilename, "candidates": errors}))
