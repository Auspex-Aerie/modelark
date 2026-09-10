"""Shared, operation-neutral memory admission for isolated codec workers.

Stage A: qualification consumers only. No live writer/reader policy changes.
RLIMIT_AS is a hard virtual-address-space ceiling, NOT an RSS reservation.
Available memory is a fresh caller observation of the smallest host/cgroup
headroom, not MemFree and not a promise against unrelated concurrent allocation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import sys


class CodecResourceRefusal(RuntimeError):
    """The requested resource envelope cannot be admitted or enforced."""


def _bytes(value, name, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'} integer bytes")
    return value


@dataclass(frozen=True)
class CodecMemoryPolicy:
    """One explicit envelope used by both compression and decompression.

    No compression-size multiplier or consumer-specific default lives here.
    The complete round trip must be qualified against the same envelope.
    Conservatively require physical headroom for the entire AS ceiling plus
    reserve; subtracting lazy native mappings would need a different proof.
    """

    address_space_bytes: int
    reserve_bytes: int
    version: str = "modelark.codec-memory.v1"

    def __post_init__(self):
        _bytes(self.address_space_bytes, "address_space_bytes")
        _bytes(self.reserve_bytes, "reserve_bytes", zero=True)
        if self.version != "modelark.codec-memory.v1":
            raise ValueError("unknown codec memory policy")

    def to_record(self):
        return asdict(self)

    @classmethod
    def from_record(cls, record):
        if type(record) is not dict or set(record) != {
                "version", "address_space_bytes", "reserve_bytes"}:
            raise ValueError("invalid codec memory policy fields")
        return cls(**record)

    def admit(self, available_bytes: int) -> None:
        """Use identical admission math for every codec operation.

        Caller must account for applicable cgroup ancestors and concurrent jobs.
        This sample admits a launch; it is not a durable resource reservation.
        """
        _bytes(available_bytes, "available_bytes", zero=True)
        required = self.address_space_bytes + self.reserve_bytes
        if available_bytes < required:
            raise CodecResourceRefusal(
                f"headroom {available_bytes} is below policy requirement {required}")

    def install_in_worker(self) -> None:
        """Irreversibly constrain THIS process; call only in a fresh child.

        Install before importing native codecs. Never call in the service or
        use preexec_fn in a multithreaded parent. Failure means abort that child;
        do not fall back to an unguarded decode. Set the core-file limit to zero;
        this does not change the host's separate crash-reporting policy.
        """
        if sys.platform != "linux":
            raise CodecResourceRefusal("codec AS guard is only qualified on Linux")
        import resource

        try:
            inherited = resource.getrlimit(resource.RLIMIT_AS)
            if any(limit != resource.RLIM_INFINITY and limit < self.address_space_bytes
                   for limit in inherited):
                raise CodecResourceRefusal("inherited AS ceiling is below requested policy")
            resource.setrlimit(resource.RLIMIT_AS,
                               (self.address_space_bytes, self.address_space_bytes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if resource.getrlimit(resource.RLIMIT_AS) != (
                    self.address_space_bytes, self.address_space_bytes):
                raise CodecResourceRefusal("AS ceiling was not installed")
        except (OSError, ValueError, OverflowError) as exc:
            raise CodecResourceRefusal("could not install codec memory guard") from exc
