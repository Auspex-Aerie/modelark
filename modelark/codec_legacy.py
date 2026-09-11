"""Legacy format adapter, invoked only inside an already guarded codec worker.

Shared standalone StreamZNN framing and native ZipNN acceptance are retained.
Unlike Slice's qualified representation contract, legacy calls have no sealed
original size, accept concatenated zstd, and do not restrict native ZipNN modes.
This is format compatibility, not an alternate memory policy or archive access.
"""
from . import streamznn


def chunks(source, dtype, policy):
    from .codec_inprocess import require_guard
    require_guard(policy)
    prefix = b""
    while len(prefix) < 5:
        piece = source.read(5 - len(prefix))
        if not piece:
            break
        prefix += piece
    if prefix == streamznn.MAGIC:
        yield from streamznn.iter_decompress(source, prefix=prefix)
    elif prefix.startswith(b"\x28\xb5\x2f\xfd"):
        import zstandard

        class Prefixed:
            def __init__(self):
                self.pending = prefix

            def read(self, size):
                if self.pending:
                    data, self.pending = self.pending[:size], self.pending[size:]
                    return data
                return source.read(size)

        with zstandard.ZstdDecompressor().stream_reader(Prefixed()) as reader:
            while data := reader.read(64 << 10):
                yield data
    else:
        # Native whole/legacy decoding already requires a complete blob. All
        # buffering and its native allocation stay beneath this child's AS cap.
        from .compress import _zipnn
        blob = bytearray(prefix)
        while data := source.read(64 << 10):
            blob.extend(data)
        yield bytes(_zipnn(dtype, threads=1).decompress(bytes(blob)))
