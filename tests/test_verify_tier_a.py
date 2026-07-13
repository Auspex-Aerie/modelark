"""Tier A evidence must never claim more than the remote headers prove."""
from __future__ import annotations

import io
import json
import sqlite3
import struct
from contextlib import redirect_stdout
from unittest import mock

from modelark import verify
from modelark.core import db


def _safetensors_blob(tensors: dict, data_bytes: int) -> bytes:
    header = json.dumps(tensors, separators=(",", ":")).encode()
    return struct.pack("<Q", len(header)) + header + bytes(data_bytes)


def _check_blob(blob: bytes):
    with mock.patch.object(verify, "_range", side_effect=lambda url, start, length: blob[start:start + length]):
        return verify.check_safetensors("https://example.invalid/model.safetensors", len(blob))


def test_safetensors_accepts_exact_shape_and_contiguous_layout():
    blob = _safetensors_blob({
        "first": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]},
        "second": {"dtype": "I8", "shape": [3], "data_offsets": [8, 11]},
    }, 11)
    ok, names, detail = _check_blob(blob)
    assert ok and names == {"first", "second"}, detail
    assert "fully accounted for" in detail


def test_safetensors_rejects_shape_size_mismatch():
    blob = _safetensors_blob({
        "weight": {"dtype": "F32", "shape": [3], "data_offsets": [0, 8]},
    }, 8)
    ok, _, detail = _check_blob(blob)
    assert not ok and "shape/offset size mismatch" in detail, detail


def test_safetensors_rejects_gaps_overlaps_and_unclaimed_tail():
    cases = [
        ({
            "a": {"dtype": "I8", "shape": [2], "data_offsets": [0, 2]},
            "b": {"dtype": "I8", "shape": [2], "data_offsets": [3, 5]},
        }, 5, "gap"),
        ({
            "a": {"dtype": "I8", "shape": [3], "data_offsets": [0, 3]},
            "b": {"dtype": "I8", "shape": [2], "data_offsets": [2, 4]},
        }, 4, "overlap"),
        ({"a": {"dtype": "I8", "shape": [2], "data_offsets": [0, 2]}},
         3, "cover 2 of 3"),
    ]
    for tensors, data_bytes, expected in cases:
        ok, _, detail = _check_blob(_safetensors_blob(tensors, data_bytes))
        assert not ok and expected in detail, detail


def test_safetensors_rejects_duplicate_tensor_names():
    encoded = (
        b'{"weight":{"dtype":"I8","shape":[1],"data_offsets":[0,1]},'
        b'"weight":{"dtype":"I8","shape":[1],"data_offsets":[0,1]}}'
    )
    blob = struct.pack("<Q", len(encoded)) + encoded + b"x"
    ok, _, detail = _check_blob(blob)
    assert not ok and "duplicate JSON key" in detail, detail


def test_range_fallback_honors_offset_when_server_ignores_range():
    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def iter_bytes(self):
            yield b"0123"
            yield b"456789"

    with mock.patch.object(verify._client, "stream", return_value=Response()):
        assert verify._range("https://example.invalid/file", 3, 4) == b"3456"


def test_range_partial_response_must_confirm_the_exact_requested_interval():
    class Response:
        status_code = 206

        def __init__(self, content_range):
            self.headers = {"Content-Range": content_range}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"3456"

    with mock.patch.object(verify._client, "stream", return_value=Response("bytes 3-6/10")):
        assert verify._range("https://example.invalid/file", 3, 4) == b"3456"
    for content_range in ("bytes 0-3/10", "", "garbage"):
        with mock.patch.object(verify._client, "stream", return_value=Response(content_range)):
            try:
                verify._range("https://example.invalid/file", 3, 4)
                raise AssertionError("a mismatched Content-Range must fail closed")
            except RuntimeError as exc:
                assert "unexpected Content-Range" in str(exc), exc


def test_standard_split_sequences_are_proven_or_rejected():
    assert verify._split_sequence_complete(set()) is False
    assert verify._split_sequence_complete({"model.safetensors"}) is True
    assert verify._split_sequence_complete({"model-00001-of-00002.safetensors"}) is False
    assert verify._split_sequence_complete({
        "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"
    }) is True
    assert verify._split_sequence_complete({"a.safetensors", "b.safetensors"}) is None
    assert verify._split_sequence_complete({
        "model-00001-of-00001.safetensors", "adapter.safetensors"
    }) is None


def test_safetensors_index_must_match_tensor_locations():
    response = mock.Mock(
        status_code=200,
        content=json.dumps({"weight_map": {"a": "part-1.safetensors"}}).encode(),
    )
    response.raise_for_status.return_value = None
    shards = [
        {"rfilename": "part-1.safetensors", "size": 10},
        {"rfilename": "part-2.safetensors", "size": 10},
    ]

    def header(url, size):
        name = url.rsplit("/", 1)[-1]
        return True, ({"b"} if name == "part-1.safetensors" else {"a"}), "ok"

    with mock.patch.object(verify._client, "get", return_value=response), \
         mock.patch.object(verify, "check_safetensors", side_effect=header), \
         mock.patch.object(verify, "hf_hub_url", side_effect=lambda repo, name: f"https://hf/{name}"):
        structural, complete, details = verify._structural(
            "org/model", shards, [], {"model.safetensors.index.json"}, []
        )
    assert structural is True and complete is False
    assert any("wrong shards" in detail for detail in details), details


def test_tier_a_requires_both_structure_and_complete_shards():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO models(repo_id,status) VALUES('org/model','discovered')")

    unknown = verify._record(con, "org/model", True, None, "safe", ["ambiguous shards"])
    assert unknown["load_tier_max"] is None
    assert con.execute("SELECT status FROM models WHERE repo_id='org/model'").fetchone()[0] == "discovered"

    passed = verify._record(con, "org/model", True, True, "safe", ["complete"])
    assert passed["load_tier_max"] == "A"
    assert con.execute("SELECT status FROM models WHERE repo_id='org/model'").fetchone()[0] == "inspected"


def test_verify_many_does_not_print_ok_for_incomplete_shards():
    result = {
        "structural_ok": True,
        "shards_complete": False,
        "format_safety": "safe",
        "load_tier_max": None,
    }
    output = io.StringIO()
    with mock.patch.object(verify, "verify_repo", return_value=result), redirect_stdout(output):
        verify.verify_many(["org/model"], con=mock.Mock())
    assert "[FAIL]" in output.getvalue(), output.getvalue()


if __name__ == "__main__":
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
            print(f"ok  {name}")
    print("all passed")
