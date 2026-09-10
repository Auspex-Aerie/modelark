"""Read-only source-use gate sharing the archive writer's nonblocking drive fence."""
from contextlib import ExitStack, contextmanager

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

    @contextmanager
    def open(self, candidate):
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
                stream = stack.enter_context(self.reader.open(candidate))
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
