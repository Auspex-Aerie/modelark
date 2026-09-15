"""Native interrupted file phases use the same engine as initial publication."""
import hashlib

import pytest

from modelark import archive_publisher as publisher, publication_native as native, publication_store as store
from modelark.publication_policy import PublicationRefused
from test_archive_publisher import fleet as archive_fleet  # noqa: F401
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


@pytest.mark.parametrize("boundary", ["annex-add", "file-commit", "metadata"])
def test_reopen_and_observe_completed_native_file_effect(con, request, tmp_path, monkeypatch, boundary):
    request.getfixturevalue("archive_fleet")
    file = publisher.FileRequest("org/a", ".gitignore", "d0")
    raw = b"# identical original bytes across interruption\n"
    staged = tmp_path / "download"
    staged.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) VALUES('org/a','.gitignore',?,'aux',?)",
                [len(raw), digest])
    original = native.QualifiedRepository._run_action
    def injected(repository, identifier, *args, **kwargs):
        result = original(repository, identifier, *args, **kwargs)
        if (boundary == "annex-add" and args[:2] == ("annex", "add")
                or boundary == "file-commit" and args[0] == "commit"
                or boundary == "metadata" and args[0] == "modelark-tag-and-flush"):
            raise RuntimeError("injected file interruption")
        return result
    monkeypatch.setattr(native.QualifiedRepository, "_run_action", injected)
    with pytest.raises(RuntimeError, match="injected"):
        with publisher.ArchivePublisher(con, [file]) as operation:
            operation_id = operation.operation_id
            operation.publish(file, staged, original_bytes=len(raw), original_sha256=digest,
                              stored_sha256=digest, compressed=False)
    monkeypatch.setattr(native.QualifiedRepository, "_run_action", original)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher.resume(con, operation_id) as resumed:
            result = resumed.publish(file, staged, original_bytes=len(raw), original_sha256=digest,
                                     stored_sha256=digest, compressed=False)
            assert result["phase"] == "CATALOG_PUBLISHED"
            revision = store._revision(con)
            assert resumed.publish(file, staged, original_bytes=len(raw), original_sha256=digest,
                                   stored_sha256=digest, compressed=False) == result
            assert store._revision(con) == revision
