"""Read-only catalog boundary for the internal Slice 1 domain API."""
import importlib
import sqlite3
from unittest import mock

import pytest

from modelark.core import db
from test_slice_domain import SHA, facts, spec


@pytest.fixture
def api():
    try:
        return (importlib.import_module("modelark.slice.domain"),
                importlib.import_module("modelark.slice.catalog"))
    except ModuleNotFoundError as exc:
        if not exc.name.startswith("modelark.slice"):
            raise
        pytest.fail("Slice 1 read-only catalog adapter is not implemented yet")


def seed(path, d):
    s = facts(d)
    con = sqlite3.connect(path, isolation_level=None)
    con.executescript(db.SCHEMA_PATH.read_text())
    con.execute("PRAGMA user_version=7")
    con.execute("INSERT INTO models(repo_id) VALUES('org/model')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) VALUES(?,?,?,?,?,?)",
                ("org/model", "model.safetensors", 12, SHA, "safetensors", "bf16"))
    drive = s.drives[0]
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,identity_epoch,write_generation,"
                "identity_fingerprint,filesystem_capacity_bytes,write_authority) VALUES(?,?,?,?,?,?,?,?,?)",
                (drive.drive_label, drive.fs_uuid, drive.annex_uuid, drive.serial, 1, 2,
                 drive.identity_fingerprint, 1000, "dedicated_local"))
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES('drive-a',1,2,'fixture')")
    con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,identity_fingerprint,"
                "filesystem_capacity_bytes,anchor_free_bytes,write_authority,identity_proof,fence_proof,observed_at) "
                "VALUES('drive-a',1,2,?,1000,900,'dedicated_local','fixture','fixture','2026-09-07')",
                (drive.identity_fingerprint,))
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,stored_relpath,orig_bytes,stored_bytes,"
                "orig_sha256,orig_sha256_provenance,annex_key,compressed) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("org/model", "model.safetensors", "drive-a", "model.safetensors", 12, 12,
                 SHA, "ingestion_computed", f"SHA256E-s12--{SHA}", 0))
    return con


def test_adapter_uses_explicit_read_only_snapshot_without_global_state(api, tmp_path):
    d, catalog = api
    path = tmp_path / "catalog ? #.sqlite"
    con = seed(path, d)
    before = tuple(con.iterdump())
    configured = db.DB_PATH
    with mock.patch.object(db, "connect", side_effect=AssertionError("no global catalog")), \
         mock.patch("subprocess.run", side_effect=AssertionError("no annex")), \
         mock.patch("socket.socket", side_effect=AssertionError("no network")):
        snapshot = catalog.read_catalog(path, spec(d))
        p = d.preview(spec(d), snapshot)
    assert p.source_ready and p.required_drives == ("drive-a",)
    assert db.DB_PATH == configured and tuple(con.iterdump()) == before
    con.close()


def test_missing_and_legacy_catalogs_are_not_created_or_migrated(api, tmp_path):
    d, catalog = api
    path = tmp_path / "absent.sqlite"
    with pytest.raises(d.SliceRefusal, match="CATALOG_UNAVAILABLE"):
        catalog.read_catalog(path, spec(d))
    assert not path.exists()
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version=6")
    con.close()
    before = path.read_bytes()
    with pytest.raises(d.SliceRefusal, match="CATALOG_VERSION_UNSUPPORTED"):
        catalog.read_catalog(path, spec(d))
    assert path.read_bytes() == before


def test_replica_metadata_alone_never_becomes_archive_provenance(api, tmp_path):
    d, catalog = api
    path = tmp_path / "catalog.sqlite"
    con = seed(path, d)
    con.execute("DELETE FROM archived")
    con.execute("INSERT INTO replicas(repo_id,rfilename,drive_label,annex_key) VALUES(?,?,?,?)",
                ("org/model", "model.safetensors", "drive-a", f"SHA256E-s12--{SHA}"))
    p = d.preview(spec(d), catalog.read_catalog(path, spec(d)))
    assert not p.source_ready and p.gaps[0].code == "ARCHIVE_MISSING"
    con.close()


def test_manifest_selection_uses_recovery_policy_but_preserves_unknown_sizes(api, tmp_path):
    d, catalog = api
    path = tmp_path / "catalog.sqlite"
    con = seed(path, d)
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) VALUES('org/model','other.gguf',99,'gguf')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) VALUES('org/model','config.json',NULL,'aux')")
    snapshot = catalog.read_catalog(path, spec(d))
    assert {f.rfilename for f in snapshot.files} == {"model.safetensors", "config.json"}
    assert next(f for f in snapshot.files if f.rfilename == "config.json").size_bytes is None
    assert "ARTIFACT_SIZE_UNKNOWN" in {g.code for g in d.preview(spec(d), snapshot).gaps}
    con.close()


def test_read_snapshot_is_consistent_across_concurrent_catalog_commit(api, tmp_path):
    d, catalog = api
    path = tmp_path / "catalog.sqlite"
    writer = seed(path, d)
    writer.execute("PRAGMA journal_mode=WAL")
    connect = sqlite3.connect
    connections = []
    mutated = False

    class Reader:
        def __init__(self, con):
            self.con = con

        def execute(self, sql, *args):
            nonlocal mutated
            if "FROM files " in sql and not mutated:
                mutated = True
                # The read transaction is already pinned by user_version. A later writer commits
                # successfully, but the reader must not combine its old copies with a new manifest.
                writer.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                               "VALUES('org/model','new-config.json',5,'aux')")
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    self.con.execute("DELETE FROM archived")
            return self.con.execute(sql, *args)

        def close(self):
            self.con.close()

    def reader_connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro") and kwargs["uri"] is True
        con = connect(database_uri, **kwargs)
        connections.append(con)
        return Reader(con)

    with mock.patch.object(catalog.sqlite3, "connect", side_effect=reader_connect):
        snapshot = catalog.read_catalog(path, spec(d))
    assert mutated and d.preview(spec(d), snapshot).source_ready
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    fresh = catalog.read_catalog(path, spec(d))
    assert fresh.snapshot_id != snapshot.snapshot_id
    assert d.preview(spec(d), fresh).gaps[0].rfilename == "new-config.json"
    writer.close()


# 2026-09-07: "other" is not positive weight evidence; cover it as blocked below.
@pytest.mark.parametrize("format", ["onnx", "mlx"])
def test_foreign_archives_are_recoverable_without_hiding_declared_gaps(api, tmp_path, format):
    d, catalog = api
    path = tmp_path / "catalog.sqlite"
    con = seed(path, d)
    name = f"model.{format}"
    con.execute("UPDATE files SET rfilename=?,format=?", (name, format))
    con.execute("UPDATE archived SET rfilename=?,stored_relpath=?", (name, name))
    p = d.preview(spec(d), catalog.read_catalog(path, spec(d)))
    assert p.source_ready and p.closure[0].rfilename == name
    # The legacy restore fallback lists only archived rows. A slice must also expose any declared
    # but unarchived artifact in this requested repository instead of silently narrowing its scope.
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                "VALUES('org/model','config.json',5,'aux')")
    p = d.preview(spec(d), catalog.read_catalog(path, spec(d)))
    assert not p.source_ready
    assert [(g.rfilename, g.code) for g in p.gaps] == [("config.json", "ARCHIVE_MISSING")]
    con.execute("DELETE FROM archived")
    p = d.preview(spec(d), catalog.read_catalog(path, spec(d)))
    assert {g.rfilename for g in p.gaps} == {name, "config.json"}
    con.close()


@pytest.mark.parametrize("name,format", [
    ("config.json", "aux"),
    ("model.safetensors.index.json", "aux"),
    ("model.other", "other"),
    ("unknown.dat", None),
])
def test_manifest_failure_without_recognized_weights_stays_blocked(api, tmp_path, name, format):
    d, catalog = api
    path = tmp_path / "catalog.sqlite"
    con = seed(path, d)
    con.execute("UPDATE files SET rfilename=?,format=?", (name, format))
    con.execute("UPDATE archived SET rfilename=?,stored_relpath=?", (name, name))
    p = d.preview(spec(d), catalog.read_catalog(path, spec(d)))
    assert not p.source_ready
    assert "MANIFEST_UNAVAILABLE" in {g.code for g in p.gaps}
    with pytest.raises(d.SliceRefusal):
        d.approve(p, expected_seal=p.seal, current_snapshot=catalog.read_catalog(path, spec(d)))
    con.close()
