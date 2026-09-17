"""Fresh byte/decode proofs, using disposable files and actual publication scopes."""
import hashlib
import json
import os

import pytest

from modelark import publication_artifact as artifact, publication_locks
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import MAP
from test_publication_lifecycle import con as publication_connection  # noqa: F401


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


def _open(scope, path, original=b"data", **updates):
    return artifact.VerifiedArtifact(scope, path, **{
        "compressed": False, "original_bytes": len(original),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "stored_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **updates})


@pytest.mark.parametrize("original", [b"", b"data", b"z" * (2 << 20)])
def test_fresh_raw_proof_retains_source_until_closed(con, tmp_path, original):
    path = tmp_path / "staged"
    path.write_bytes(original)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with _open(scope, path, original) as value:
            assert value.proof.original_sha256 == value.proof.stored_sha256
            assert json.loads(json.dumps(value.proof.record())) == value.proof.record()
            chunks = []
            while block := value.read(1 << 20):
                chunks.append(block)
            assert b"".join(chunks) == original
            value.rewind()
            assert value.read(4) == original[:4]
        with pytest.raises(PublicationRefused, match="ARTIFACT_CLOSED"):
            value.read(4)


@pytest.mark.parametrize("updates,match", [
    ({"stored_sha256": "0" * 64}, "STORED_HASH_MISMATCH"),
    ({"original_sha256": "0" * 64}, "ORIGINAL_HASH_MISMATCH"),
])
def test_bad_digests_are_not_proofs(con, tmp_path, updates, match):
    path = tmp_path / "staged"
    path.write_bytes(b"data")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match=match):
            _open(scope, path, **updates)
        assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 0


def test_replaced_path_or_mutated_inode_refuses(con, tmp_path):
    path = tmp_path / "staged"
    path.write_bytes(b"data")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with _open(scope, path) as value:
            alternate = tmp_path / "replacement"
            alternate.write_bytes(b"data")
            os.replace(alternate, path)
            with pytest.raises(PublicationRefused, match="ARTIFACT_CHANGED"):
                value.read(4)


def test_zstd_uses_shared_original_reader_and_limits(con, tmp_path):
    pytest.importorskip("zstandard")
    original = b"metadata yaml\n" * 100
    path = tmp_path / "staged.znn"
    # Use the repository's exact framed zstd writer, not an invented container.
    from modelark import compress
    source = tmp_path / "original"
    source.write_bytes(original)
    compress.compress_file(source, path, codec=compress.CODEC_ZSTD)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with _open(scope, path, original, compressed=True) as value:
            assert value.proof.compressed
            assert value.proof.original_bytes == len(original)


def test_no_transaction_or_fabricated_scope(con, tmp_path):
    path = tmp_path / "staged"
    path.write_bytes(b"data")
    with pytest.raises(PublicationRefused, match="AUTHORITY_MISSING"):
        _open(lambda: None, path)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        con.execute("BEGIN")
        try:
            with pytest.raises(PublicationRefused, match="IO_TRANSACTION_ACTIVE"):
                _open(scope, path)
        finally:
            con.rollback()
