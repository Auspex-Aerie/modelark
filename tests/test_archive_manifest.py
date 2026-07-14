"""The canonical manifest is shared, deterministic, and bulk-queryable."""
from __future__ import annotations

import sqlite3
from unittest import mock

from modelark import archive_manifest
from modelark.core import db


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    return con


def test_bulk_manifest_preserves_weight_precedence_and_storage_actions():
    con = _mem()
    con.executemany("INSERT INTO models(repo_id) VALUES(?)", [("a/model",), ("b/model",)])
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) VALUES(?,?,?,?,?,?)",
        [
            ("a/model", "model.safetensors", 80, "a", "safetensors", "bf16"),
            ("a/model", "pytorch_model.bin", 100, "b", "pytorch", "fp16"),
            ("a/model", "config.json", 10, None, "aux", None),
            ("b/model", "model.gguf", 60, "c", "gguf", "q4"),
            ("b/model", "notes.txt", 2, None, "aux", None),
        ],
    )
    selects = []
    con.set_trace_callback(lambda sql: selects.append(sql) if "FROM files" in sql else None)
    result = archive_manifest.manifests_for_repos(
        con, ["b/model", "a/model"], archive_manifest.ArchivePolicy(allow_pickle=False)
    )
    con.set_trace_callback(None)

    assert len(selects) == 1, selects
    assert [(f.rfilename, f.storage_action) for f in result["a/model"]] == [
        ("config.json", "raw"), ("model.safetensors", "compress")
    ]
    assert [(f.rfilename, f.storage_action) for f in result["b/model"]] == [
        ("model.gguf", "raw"), ("notes.txt", "raw")
    ]


def test_batch_retains_policy_errors_without_aux_only_manifest():
    con = _mem()
    con.execute("INSERT INTO models(repo_id) VALUES('pickle/model')")
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES('pickle/model',?,?,?,?)",
        [("pytorch_model.bin", 100, "pytorch", "fp16"), ("config.json", 10, "aux", None)],
    )
    batch = archive_manifest.inspect_manifests_for_repos(
        con, ["pickle/model"], archive_manifest.ArchivePolicy(allow_pickle=False)
    )
    assert batch.manifests == {}
    assert "pickle/model" in batch.errors
    assert "pickle-only weights are blocked" in str(batch.errors["pickle/model"])

    recovered = archive_manifest.manifest_for_repo(
        con, "pickle/model", archive_manifest.recovery_policy()
    )
    assert {item.rfilename for item in recovered} == {"pytorch_model.bin", "config.json"}


def test_default_policy_is_resolved_once_for_a_bulk_batch():
    con = _mem()
    con.executemany("INSERT INTO models(repo_id) VALUES(?)", [("a",), ("b",)])
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format) VALUES(?,?,?,?)",
        [("a", "x.bin", 1, "pytorch"), ("b", "x.bin", 1, "pytorch")],
    )
    with mock.patch.object(archive_manifest.wishlist, "exclude_pickle_only", return_value=False) as policy:
        result = archive_manifest.manifests_for_repos(con, ["a", "b"])
    assert set(result) == {"a", "b"}
    policy.assert_called_once_with()
