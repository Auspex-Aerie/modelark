"""Non-spawning original-byte verification for an already guarded worker.

Only the execution adapter differs from Slice: format interpretation, bounds,
exact total length and original digest verification use the same neutral reader.
No archive access, retrieval, publication, native format fork or nested worker.
"""
from contextlib import closing, contextmanager
import hashlib
import resource
import sys

from . import artifact_io, streamznn
from .codec_resources import CodecResourceRefusal, available_memory


def require_guard(policy):
    """Observe the irreversible envelope; never install limits in this caller."""
    if (sys.platform != "linux" or not sys.flags.isolated or not sys.flags.no_site
            or resource.getrlimit(resource.RLIMIT_AS) != (
                policy.memory.address_space_bytes, policy.memory.address_space_bytes)):
        raise CodecResourceRefusal("non-spawning decode requires the installed worker guard")


def frame_adapter(policy):
    require_guard(policy)

    @contextmanager
    def frame(stream, stored_length, remaining, prefix=b""):
        require_guard(policy)
        data = streamznn.read_zipnn_frame(
            stream, max_stored_bytes=policy.limits.stored_frame_bytes,
            max_decoded_bytes=policy.limits.decoded_frame_bytes,
            remaining_bytes=remaining, stored_length=stored_length, prefix=prefix,
            read_size=policy.limits.read_bytes)
        yield (data[offset:offset + (64 << 10)] for offset in range(0, len(data), 64 << 10))

    return frame


def verify_original(path, expected_sha256, expected_bytes, policy):
    """The writer's canary: exact length/hash, entirely inside its own guard."""
    require_guard(policy)
    policy.memory.admit(available_memory()["available_bytes"])
    if not expected_sha256:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source, closing(artifact_io.original_stream(
            source, compressed=True, expected_bytes=expected_bytes,
            limits=policy.limits, policy=policy, frame_stream=frame_adapter(policy))) as reader:
        while chunk := reader.read(policy.limits.read_bytes):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256
