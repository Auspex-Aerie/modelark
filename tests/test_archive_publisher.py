"""Owning per-file pipeline: real annex/SQLite/fences; simulated device probes.

Not a live device or completed map-publication qualification. These tests stop
with a durable CATALOG_PUBLISHED file and require the still-pending enclosing work.
"""
import hashlib
import os

import pytest

from modelark import archive_publisher as publisher, publication_store as store, register
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.publication_policy import PublicationRefused
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401
from test_publication_lifecycle import MAP


@pytest.fixture
def fleet(request, con, tmp_path, monkeypatch):
    archive, git = request.getfixturevalue("native_prepared")
    map_root = tmp_path / "map"
    git("clone", "--no-local", "--quiet", str(archive), str(map_root))
    git("-C", str(map_root), "remote", "remove", "origin")
    git("-C", str(map_root), "config", "annex.uuid", MAP)
    git("-C", str(map_root), "annex", "init", "--version=8", "--quiet", "test-map")
    # Registration must have admitted the source's UUID description before a
    # publication may merge its metadata; cloning file history alone does not.
    source_annex = git("rev-parse", "refs/heads/git-annex").decode().strip()
    git("-C", str(map_root), "fetch", "--quiet", str(archive), source_annex + ":refs/remotes/registered/git-annex")
    git("-C", str(map_root), "annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
    identity = git("config", "annex.uuid").decode().strip()
    value = os.statvfs(archive)
    capacity = value.f_blocks * value.f_frsize
    fingerprint = identity_fingerprint_v1(fs_uuid="d0-fs", annex_uuid=identity, serial=None,
                                          filesystem_capacity_bytes=capacity)
    con.execute("UPDATE drives SET identity_fingerprint=?,filesystem_capacity_bytes=? "
                "WHERE drive_label='d0'", [fingerprint, capacity])
    con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
                "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
                "VALUES('d0',1,2,?,?,?,'dedicated_local','fixture','fixture','2026-09-14')",
                [value.f_bavail * value.f_frsize, capacity, fingerprint])
    monkeypatch.setattr(register, "library_root", lambda: map_root)
    monkeypatch.setattr(register, "archive_path", lambda _, label: archive if label == "d0" else None)
    # Exact host observation type, but hardware values are intentionally simulated.
    def observe(root):
        assert str(root) == str(archive)
        current = os.statvfs(archive)
        return register.ArchiveVolumeObservation("d0-fs", identity, None, capacity,
                                                current.f_bavail * current.f_frsize, current.f_frsize)
    monkeypatch.setattr(register, "observe_archive_volume", observe)
    return archive, map_root, git


@pytest.mark.parametrize("filename", ["config.json", ".gitignore", "nested/.gitattributes"])
def test_real_file_publication_does_not_pretend_map_or_drive_complete(con, fleet, tmp_path, filename):
    archive, map_root, _ = fleet
    original = b"*.safetensors\n# exact upstream bytes\n"
    digest = hashlib.sha256(original).hexdigest()
    staged = tmp_path / "downloaded"
    staged.write_bytes(original)
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) VALUES('org/a',?,?, 'aux', ?)",
                [filename, len(original), digest])
    request = publisher.FileRequest("org/a", filename, "d0")
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request]) as operation:
            result = operation.publish(request, staged, original_bytes=len(original), original_sha256=digest,
                                       stored_sha256=digest, compressed=False)
            assert result["phase"] == "CATALOG_PUBLISHED"
            assert (archive / result["stored_path"]).read_bytes() == original
            assert (archive / result["stored_path"]).is_symlink()
            assert not (map_root / result["stored_path"]).exists()
            assert con.execute("SELECT present FROM replicas WHERE rfilename=?", [filename]).fetchone() == (1,)
            assert con.execute("SELECT orig_sha256 FROM archived WHERE rfilename=?", [filename]).fetchone() == (digest,)
            with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
                store.require_clear(con, ["d0"])
    assert con.execute("SELECT phase FROM publication_files").fetchone() == ("CATALOG_PUBLISHED",)
    assert con.execute("SELECT state FROM publication_operations").fetchone() == ("PREPARED",)
    assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE drive_label='d0' AND generation=3").fetchone() == (0,)


def test_bad_original_digest_never_creates_archive_rows_or_target(con, fleet, tmp_path):
    archive, _, _ = fleet
    staged = tmp_path / "downloaded"
    staged.write_bytes(b"bad")
    request = publisher.FileRequest("org/a", "model.safetensors", "d0")
    with pytest.raises(PublicationRefused, match="ORIGINAL_HASH_MISMATCH"):
        with publisher.ArchivePublisher(con, [request]) as operation:
            operation.publish(request, staged, original_bytes=3, original_sha256="0" * 64,
                              stored_sha256=hashlib.sha256(b"bad").hexdigest(), compressed=False)
    assert con.execute("SELECT count(*) FROM archived").fetchone() == (0,)
    assert not (archive / "org/a/model.safetensors").exists()
    assert con.execute("SELECT state FROM publication_operations").fetchone() == ("PREPARED",)
