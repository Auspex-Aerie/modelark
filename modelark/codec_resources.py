"""Shared, operation-neutral memory admission for isolated codec workers.

Shared by qualification and the opt-in codec supervisor. No live caller rollout.
RLIMIT_AS is a hard virtual-address-space ceiling, NOT an RSS reservation.
Available memory is a fresh caller observation of the smallest host/cgroup
headroom, not MemFree and not a promise against unrelated concurrent allocation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sys


class CodecResourceRefusal(RuntimeError):
    """The requested resource envelope cannot be admitted or enforced."""


class CodecReadUnavailable(RuntimeError):
    """Decoder execution could not certify bytes; not evidence of corruption."""


def available_memory() -> dict:
    """Conservative visible Linux memory headroom; unknown accounting refuses."""
    try:
        return _available_memory()
    except (OSError, ValueError, OverflowError) as exc:
        raise CodecResourceRefusal("could not establish memory headroom") from exc


def _available_memory():
    host_available = None
    for line in Path("/proc/meminfo").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == "MemAvailable:":
            if len(fields) != 3 or fields[2] != "kB":
                raise CodecResourceRefusal("unrecognized MemAvailable accounting")
            host_available = int(fields[1]) * 1024
            if host_available < 0:
                raise CodecResourceRefusal("negative host memory accounting")
    if host_available is None:
        raise CodecResourceRefusal("host available memory is unknown")
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()
              if " - cgroup2 " in line]
    if len(mounts) != 1 or mounts[0][3:5] != ["/", "/sys/fs/cgroup"]:
        raise CodecResourceRefusal("full cgroup2 hierarchy is not visible")
    groups = Path("/proc/self/cgroup").read_text().splitlines()
    if len(groups) != 1 or not groups[0].startswith("0::/"):
        raise CodecResourceRefusal("unrecognized unified cgroup membership")
    relative = groups[0][4:]
    if relative and any(part in {"", ".", ".."} for part in relative.split("/")):
        raise CodecResourceRefusal("invalid cgroup membership")
    root = Path("/sys/fs/cgroup")
    current = root / relative
    cgroup_headroom = {}
    while True:
        # Initial hierarchy roots have no memory.max; a visible namespace root
        # may have one and its limit must not be discarded.
        if current != root or (current / "memory.max").exists():
            maximum = (current / "memory.max").read_text().strip()
            used = int((current / "memory.current").read_text().strip())
            if used < 0 or maximum != "max" and int(maximum) < 0:
                raise CodecResourceRefusal("negative cgroup memory accounting")
            if maximum != "max":
                cgroup_headroom[str(current.relative_to(root))] = max(0, int(maximum) - used)
        if current == root:
            break
        current = current.parent
    return {"available_bytes": min([host_available, *cgroup_headroom.values()]),
            "observations": {"host_available_bytes": host_available,
                             "cgroup_headroom_bytes": cgroup_headroom}}


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
        self.check_worker_environment()

    def check_worker_environment(self) -> None:
        """Read-only prerequisites shared by admission and actual guard install.

        Inherited soft AND hard ceilings must permit this exact policy. This is
        not proof of later setrlimit success; the fresh child still installs and
        verifies its guard. Never lower or relax the caller's process limits.
        """
        if sys.platform != "linux":
            raise CodecResourceRefusal("codec AS guard is only qualified on Linux")
        try:
            import resource
            inherited = resource.getrlimit(resource.RLIMIT_AS)
        except (ImportError, OSError, ValueError, OverflowError) as exc:
            raise CodecResourceRefusal("could not inspect inherited codec memory guard") from exc
        if any(limit != resource.RLIM_INFINITY and limit < self.address_space_bytes
               for limit in inherited):
            raise CodecResourceRefusal("inherited AS ceiling is below requested policy")

    def install_in_worker(self) -> None:
        """Irreversibly constrain THIS process; call only in a fresh child.

        Install before importing native codecs. Never call in the service or
        use preexec_fn in a multithreaded parent. Failure means abort that child;
        do not fall back to an unguarded decode. Set the core-file limit to zero;
        this does not change the host's separate crash-reporting policy.
        """
        self.check_worker_environment()
        import resource

        try:
            resource.setrlimit(resource.RLIMIT_AS,
                               (self.address_space_bytes, self.address_space_bytes))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if resource.getrlimit(resource.RLIMIT_AS) != (
                    self.address_space_bytes, self.address_space_bytes):
                raise CodecResourceRefusal("AS ceiling was not installed")
        except (OSError, ValueError, OverflowError) as exc:
            raise CodecResourceRefusal("could not install codec memory guard") from exc
