"""Read-only source-use gate sharing the archive writer's nonblocking drive fence."""
from contextlib import contextmanager

from modelark import drive_fence
from .catalog import read_catalog
from .domain import SliceRefusal, SliceSpec
from .transaction import TransferRefusal


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
        key = (candidate.drive.identity_fingerprint, candidate.drive.identity_epoch)
        try:
            with drive_fence.hold_drives_sorted([key], blocking=False):
                spec = SliceSpec((candidate.copy.repo_id,), "source-evidence", "unused")
                snapshot = read_catalog(self.catalog_path, spec)
                with self.reader.open(candidate) as stream:
                    yield snapshot, stream
        except drive_fence.FenceUnavailable as exc:
            raise TransferRefusal("SOURCE_BUSY", candidate.drive.drive_label) from exc
        except SliceRefusal as exc:
            raise TransferRefusal("SOURCE_EVIDENCE_UNAVAILABLE", str(exc)) from exc
        except FileNotFoundError as exc:
            raise TransferRefusal("SOURCE_MISSING", candidate.drive.drive_label) from exc
