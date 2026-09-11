"""Versioned original-byte decoding policy, independent of storage authority."""
from dataclasses import asdict, dataclass

from .artifact_io import DecodeLimits
from .codec_resources import CodecMemoryPolicy


@dataclass(frozen=True)
class DecodePolicy:
    """Explicit approved bounds; the memory envelope is not a reservation.

    The initial profile uses C1's qualified 8 GiB AS / 2 GiB headroom envelope.
    Individual frame buffers cannot exceed that same AS envelope; there is no
    independent Slice-sized ceiling. Native mappings and simultaneous buffers
    can still make a smaller frame fail inside the enforced child guard.
    """

    limits: DecodeLimits
    memory: CodecMemoryPolicy
    version: str = "modelark.original-decode.v1"
    zstd_window_unit: str = "bytes"

    def __post_init__(self):
        if (self.version != "modelark.original-decode.v1" or self.zstd_window_unit != "bytes"
                or not isinstance(self.limits, DecodeLimits)
                or not isinstance(self.memory, CodecMemoryPolicy)):
            raise ValueError("unsupported original decode policy")
        # Keep parent streaming/window bounds fixed for this qualified version.
        if (self.limits.read_bytes > 1 << 20
                or self.limits.decoded_frame_bytes > self.memory.address_space_bytes
                or self.limits.stored_frame_bytes > self.memory.address_space_bytes
                or self.limits.zstd_window_bytes > 64 << 20):
            raise ValueError("decode bounds exceed qualified policy version")

    def to_record(self):
        return asdict(self)

    @classmethod
    def from_record(cls, record):
        if type(record) is not dict or set(record) != {
                "version", "limits", "memory", "zstd_window_unit"}:
            raise ValueError("invalid decode policy fields")
        limits = record["limits"]
        if type(limits) is not dict or set(limits) != {
                "stored_frame_bytes", "decoded_frame_bytes", "read_bytes", "zstd_window_bytes"}:
            raise ValueError("invalid decode limit fields")
        return cls(DecodeLimits(**limits), CodecMemoryPolicy.from_record(record["memory"]),
                   record["version"], record["zstd_window_unit"])


def qualified_policy():
    memory = CodecMemoryPolicy(8 << 30, 2 << 30)
    return DecodePolicy(DecodeLimits(memory.address_space_bytes, memory.address_space_bytes,
                                     1 << 20, 64 << 20), memory)
