"""Read-only header/resource checks; not full decode or digest verification."""
from . import artifact_io as io, streamznn
from .codec_resources import available_memory


def inspect_original(stream, *, compressed, expected_bytes, stored_bytes, policy, check=lambda: None):
    """Inspect every framed header with bounded checked reads, without native ZipNN.

    No paths, retrieval or destination authority. Consumes the caller-owned stream;
    payload bytes are discarded, never buffered as a frame or interpreted as proof
    of integrity. Actual use must check again and verify original length/hash.
    """
    source = io._CheckedInput(stream, policy.limits.read_bytes, check)
    limits = policy.limits
    if not compressed:
        if expected_bytes != stored_bytes:
            raise io.DecodeError("INVALID", "raw stored/original sizes differ")
        check()
        return
    prefix = io._prefix(source, 5)

    def frame(length=None, prefix=b"", remaining=expected_bytes):
        header = streamznn.read_zipnn_header(
            source, max_stored_bytes=limits.stored_frame_bytes,
            max_decoded_bytes=limits.decoded_frame_bytes, remaining_bytes=remaining,
            stored_length=length, prefix=prefix, read_size=limits.read_bytes)
        original = int.from_bytes(header[16:24], "little")
        stored = int.from_bytes(header[24:32], "little")
        pending = stored - 32
        while pending:
            piece = source.read(min(pending, 64 << 10))
            if not piece:
                raise io.DecodeError("INVALID", "truncated ZipNN payload")
            pending -= len(piece)
        return original

    try:
        if prefix == streamznn.MAGIC:
            total = 0
            for length in streamznn.iter_frame_lengths(
                    source, prefix=prefix, max_stored_frame_bytes=limits.stored_frame_bytes,
                    read_size=limits.read_bytes):
                total += frame(length, remaining=expected_bytes - total)
            if total != expected_bytes:
                raise io.DecodeError("INVALID", "framed total differs from original size")
        elif prefix.startswith(b"ZN"):
            if frame(prefix=prefix) != expected_bytes or source.read(1):
                raise io.DecodeError("INVALID", "whole frame differs from original size or has trailing bytes")
        elif prefix.startswith(b"\x28\xb5\x2f\xfd"):
            io._zstd_header(source, prefix, expected_bytes, limits, qualified=True)
            return  # streaming window guard, not the ZipNN whole-frame helper
        else:
            raise io.DecodeError("UNSUPPORTED", "unknown compressed container")
    except streamznn.DecodeLimitExceeded as exc:
        raise io.DecodeError("LIMIT", str(exc)) from exc
    except streamznn.UnsupportedFrame as exc:
        raise io.DecodeError("UNSUPPORTED", str(exc)) from exc
    except streamznn.StreamZnnError as exc:
        raise io.DecodeError("INVALID", str(exc)) from exc
    check()
    policy.memory.admit(available_memory()["available_bytes"])
    check()
