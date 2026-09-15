"""Real map ref/index publication, with disposable bytes and simulated hardware."""
import hashlib

import pytest

from modelark import archive_publisher as publisher, publication_map as publish_map
from modelark import publication_native as native, publication_store as store
from modelark.publication_policy import PublicationRefused
from test_archive_publisher import fleet as archive_fleet  # noqa: F401
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


@pytest.fixture(name="fleet")
def _fleet(request):
    return request.getfixturevalue("archive_fleet")


def _publish(operation, con, tmp_path, request):
    raw = b"# exact ignored upstream metadata\n*.bin\n"
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "download"
    path.write_bytes(raw)
    return operation.publish(request, path, original_bytes=len(raw), original_sha256=digest,
                             stored_sha256=digest, compressed=False)


def _request(con):
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) VALUES('org/a','.gitignore',NULL,'aux')")
    return publisher.FileRequest("org/a", ".gitignore", "d0")


def test_map_publication_checks_refs_index_files_and_no_content(con, fleet, tmp_path):
    archive, map_root, git = fleet
    request = _request(con)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request]) as operation:
            result = _publish(operation, con, tmp_path, request)
            proof = operation.propagate("d0")
            mapped = map_root / result["stored_path"]
            assert mapped.is_symlink() and not mapped.exists()
            assert (archive / result["stored_path"]).read_bytes().startswith(b"# exact")
            assert proof["proof"]["kind"] == "published-map"
            assert proof["proof"]["no_annex_payload"]
            assert con.execute("SELECT phase FROM publication_batches").fetchone() == ("PROPAGATED",)
            with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
                store.require_clear(con, ["d0"])
    assert con.execute("SELECT state FROM publication_operations").fetchone() == ("PREPARED",)


@pytest.mark.parametrize("boundary", ["before-refs", "after-refs", "after-checkout"])
def test_exact_map_resume_after_native_boundary(con, fleet, tmp_path, monkeypatch, boundary):
    _, map_root, _ = fleet
    request = _request(con)
    original = native.QualifiedRepository._run_action
    def interrupted(repository, identifier, *args, **kwargs):
        if repository.tree.path != map_root:
            return original(repository, identifier, *args, **kwargs)
        if boundary == "before-refs" and args == ("update-ref", "--stdin"):
            raise RuntimeError("injected map interruption")
        result = original(repository, identifier, *args, **kwargs)
        if (boundary == "after-refs" and args == ("update-ref", "--stdin")
                or boundary == "after-checkout" and args[:3] == ("read-tree", "--reset", "-u")):
            raise RuntimeError("injected map interruption")
        return result
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request]) as operation:
            _publish(operation, con, tmp_path, request)
            monkeypatch.setattr(native.QualifiedRepository, "_run_action", interrupted)
            with pytest.raises(RuntimeError, match="injected"):
                operation.propagate("d0")
            assert con.execute("SELECT phase FROM publication_batches").fetchone() == ("PREPARED",)
            monkeypatch.setattr(native.QualifiedRepository, "_run_action", original)
            batch_id = con.execute("SELECT batch_id FROM publication_batches").fetchone()[0]
            proof = publish_map.resume(operation.stage, operation.map, batch_id=batch_id)
            assert proof["kind"] == "published-map"
            revision = store._revision(con)
            assert publish_map.resume(operation.stage, operation.map, batch_id=batch_id) == proof
            assert store._revision(con) == revision


def test_enclosing_finish_publishes_fresh_anchor_only_after_complete_map(con, fleet, tmp_path):
    request = _request(con)
    with publisher.ArchivePublisher(con, [request]) as operation:
        _publish(operation, con, tmp_path, request)
        result = operation.finish()
        assert result["phase"] == "CLOSED"
    store.require_clear(con, ["d0"])
    assert con.execute("SELECT state FROM publication_operations").fetchone() == ("CLOSED",)
    assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE drive_label='d0' AND generation=3").fetchone() == (1,)


def test_reopen_same_pending_operation_and_complete_map(con, fleet, tmp_path):
    request = _request(con)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request]) as operation:
            _publish(operation, con, tmp_path, request)
            operation_id = operation.operation_id
    revision = store._revision(con)
    with publisher.ArchivePublisher.resume(con, operation_id) as resumed:
        assert store._revision(con) == revision
        assert resumed.operation_id == operation_id
        resumed.finish()
    assert con.execute("SELECT write_generation FROM drives WHERE drive_label='d0'").fetchone() == (3,)
