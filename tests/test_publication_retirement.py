"""Disposable end-to-end maintenance retirement and guarded map publication."""
import hashlib

import pytest

from modelark import archive_publisher as publisher, publication_native as native
from modelark import publication_store as store
from modelark.publication_policy import PublicationRefused, payload_relative_path
from test_archive_publisher import fleet as archive_fleet  # noqa: F401
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


@pytest.fixture(name="fleet")
def _fleet(request):
    return request.getfixturevalue("archive_fleet")


def _legacy(con, fleet):
    archive, map_root, git = fleet
    data = b"* annex.largefiles=anything\n"
    digest = hashlib.sha256(data).hexdigest()
    old = "org/a/.gitattributes"
    for root in (archive, map_root):
        target = root / old
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        git("-C", str(root), "add", "--", old)
        git("-C", str(root), "commit", "-qm", "legacy raw payload")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) "
                "VALUES('org/a','.gitattributes',?,'aux',?)", [len(data), digest])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,stored_relpath,orig_sha256,"
        "orig_bytes,stored_bytes,compressed,annex_key,orig_sha256_provenance) "
        "VALUES('org/a','.gitattributes','d0','.gitattributes','.gitattributes',?,?,?,0,NULL,"
        "'archive-head-blob')", [digest, len(data), len(data)])
    return data, digest, old


def test_maintenance_retires_source_then_map_and_closes(con, fleet):
    archive, map_root, _ = fleet
    data, digest, old = _legacy(con, fleet)
    offline_uuid = "99999999-9999-4999-8999-999999999999"
    con.execute("INSERT INTO drives(drive_label,annex_uuid) VALUES('offline',?)", [offline_uuid])
    request = publisher.FileRequest("org/a", ".gitattributes", "d0")
    with publisher.ArchivePublisher(
            con, [request], kind="maintenance", retired_paths={request: old}) as operation:
        operation.publish(
            request, archive / old, original_bytes=len(data), original_sha256=digest,
            stored_sha256=digest, compressed=False)
        assert (archive / old).is_file() and (map_root / old).is_file()
        with pytest.raises(PublicationRefused, match="RETIREMENT_CHANGED"):
            operation.propagate("d0")
        operation.retire(request)
        assert not (archive / old).exists()
        assert (map_root / old).is_file(), "source retirement is not map publication"
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
            store.require_clear(con, ["d0"])
        completed = operation.finish()
        assert completed["phase"] == "CLOSED"
    mapped = "org/a/" + payload_relative_path(".gitattributes")
    assert not (map_root / old).exists()
    assert (archive / mapped).is_symlink() and (map_root / mapped).is_symlink()
    assert con.execute("SELECT annex_key,stored_relpath FROM archived").fetchone()[0].startswith("SHA256-")
    store.require_clear(con, ["d0"], tree_change=True)
    with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
        store.require_clear(con, ["offline"], tree_change=True)
    assert con.execute(
        "SELECT state FROM publication_clone_obligations WHERE annex_uuid=?", [offline_uuid]).fetchone() == ("PENDING",)


@pytest.mark.parametrize("boundary", ["before-commit", "after-commit"])
def test_retirement_resumes_without_rerunning_ambiguous_command(con, fleet, monkeypatch, boundary):
    archive, _, git = fleet
    data, digest, old = _legacy(con, fleet)
    request = publisher.FileRequest("org/a", ".gitattributes", "d0")
    original = native.QualifiedRepository._run_action
    fail = {"commit": True}

    def interrupted(repository, identifier, *args, **kwargs):
        retiring = args and args[0] == "commit" and "retire source" in args[5]
        if fail["commit"] and retiring and boundary == "before-commit":
            raise RuntimeError("injected retirement interruption")
        result = original(repository, identifier, *args, **kwargs)
        if fail["commit"] and retiring and boundary == "after-commit":
            raise RuntimeError("injected retirement interruption")
        return result

    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(
                con, [request], kind="maintenance", retired_paths={request: old}) as operation:
            operation.publish(request, archive / old, original_bytes=len(data), original_sha256=digest,
                              stored_sha256=digest, compressed=False)
            monkeypatch.setattr(native.QualifiedRepository, "_run_action", interrupted)
            with pytest.raises(RuntimeError, match="injected"):
                operation.retire(request)
            assert not (archive / old).exists()
            fail["commit"] = False
            operation.retire(request)
            assert not (archive / old).exists()
            assert git("ls-tree", "--name-only", "HEAD", "--", old) == b""


def test_maintenance_publisher_hard_refuses_live_catalog(con, fleet, monkeypatch):
    archive, _, _ = fleet
    data, digest, old = _legacy(con, fleet)
    request = publisher.FileRequest("org/a", ".gitattributes", "d0")
    from modelark import publication_migrate
    monkeypatch.setattr(publication_migrate, "live_catalog_path",
                        lambda: publication_migrate._catalog_file(con))
    with pytest.raises(PublicationRefused, match="LIVE_CUTOVER_FORBIDDEN"):
        with publisher.ArchivePublisher(
                con, [request], kind="maintenance", retired_paths={request: old}) as operation:
            operation.publish(request, archive / old, original_bytes=len(data), original_sha256=digest,
                              stored_sha256=digest, compressed=False)
    assert con.execute("SELECT count(*) FROM publication_operations").fetchone() == (0,)


def test_same_drive_two_file_retirements_compose_one_map_and_inventory_chain(con, fleet):
    archive, map_root, git = fleet
    selected = []
    for name, data in ((".gitattributes", b"* annex.largefiles=anything\n"),
                       (".gitignore", b"*.tmp\n")):
        digest = hashlib.sha256(data).hexdigest()
        old = "org/a/" + name
        for root in (archive, map_root):
            target = root / old
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            git("-C", str(root), "add", "--", old)
            git("-C", str(root), "commit", "-qm", "legacy raw " + name)
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) "
                    "VALUES('org/a',?,?,'aux',?)", [name, len(data), digest])
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,stored_relpath,orig_sha256,"
            "orig_bytes,stored_bytes,compressed,annex_key,orig_sha256_provenance) "
            "VALUES('org/a',?,'d0',?,?,?, ?,?,0,NULL,'archive-head-blob')",
            [name, name, name, digest, len(data), len(data)])
        selected.append((publisher.FileRequest("org/a", name, "d0"), old, data, digest))
    requests = [item[0] for item in selected]
    retired = {request: old for request, old, _data, _digest in selected}
    with publisher.ArchivePublisher(
            con, requests, kind="maintenance", retired_paths=retired) as operation:
        # Publish all replacement representations before retiring either old
        # path: this proves validation follows the actual commit chain rather
        # than assuming per-file catalog order is Git commit order.
        for request, old, data, digest in selected:
            operation.publish(request, archive / old, original_bytes=len(data), original_sha256=digest,
                              stored_sha256=digest, compressed=False)
        for request, _old, _data, _digest in selected:
            operation.retire(request)
        operation.finish()
    for request, old, _data, _digest in selected:
        mapped = request.repo_id + "/" + payload_relative_path(request.rfilename)
        assert not (archive / old).exists() and not (map_root / old).exists()
        assert (archive / mapped).is_symlink() and (map_root / mapped).is_symlink()
