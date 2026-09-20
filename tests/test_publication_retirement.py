"""Disposable end-to-end maintenance retirement and guarded map publication."""
import hashlib
import os

import pytest

from modelark import archive_publisher as publisher, publication_native as native, register
from modelark import publication_store as store
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.publication_policy import PublicationRefused, payload_relative_path
from test_archive_publisher import fleet as archive_fleet  # noqa: F401
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


@pytest.fixture(name="fleet")
def _fleet(request):
    return request.getfixturevalue("archive_fleet")


def _legacy(con, fleet, name=".gitattributes", data=b"* annex.largefiles=anything\n"):
    archive, map_root, git = fleet
    digest = hashlib.sha256(data).hexdigest()
    old = "org/a/" + name
    for root in (archive, map_root):
        target = root / old
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        git("-C", str(root), "add", "--", old)
        git("-C", str(root), "commit", "-qm", "legacy raw payload")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) "
                "VALUES('org/a',?,?,'aux',?)", [name, len(data), digest])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,stored_relpath,orig_sha256,"
        "orig_bytes,stored_bytes,compressed,annex_key,orig_sha256_provenance) "
        "VALUES('org/a',?,'d0',?,?,?, ?,?,0,NULL,'archive-head-blob')",
        [name, name, name, digest, len(data), len(data)])
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


def test_maintenance_rejects_duplicate_drive_retirement_path_before_entry(con):
    first = publisher.FileRequest("org/a", ".gitattributes", "d0")
    second = publisher.FileRequest("org/a", ".gitignore", "d0")
    retired = {first: "org/a/shared", second: "org/a/shared"}
    with pytest.raises(PublicationRefused, match="RETIREMENT_PATH_COLLISION"):
        publisher.ArchivePublisher(con, [first, second], kind="maintenance", retired_paths=retired)
    assert con.execute("SELECT count(*) FROM publication_operations").fetchone() == (0,)


def test_map_retirement_requires_exact_old_source_entry(con, fleet):
    archive, map_root, git = fleet
    data, digest, old = _legacy(con, fleet)
    (map_root / old).write_bytes(b"independently repaired map value\n")
    git("-C", str(map_root), "add", "--", old)
    git("-C", str(map_root), "commit", "-qm", "diverge central map legacy entry")
    request = publisher.FileRequest("org/a", ".gitattributes", "d0")
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(
                con, [request], kind="maintenance", retired_paths={request: old}) as operation:
            operation.publish(request, archive / old, original_bytes=len(data), original_sha256=digest,
                              stored_sha256=digest, compressed=False)
            operation.retire(request)
            with pytest.raises(PublicationRefused, match="MAP_FILE_RETIREMENT_CHANGED"):
                operation.finish()
    assert (map_root / old).read_bytes() == b"independently repaired map value\n"


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


def test_nested_last_raw_file_may_remove_its_empty_legacy_parent(con, fleet):
    archive, map_root, _ = fleet
    name = "nested/.gitignore"
    data, digest, old = _legacy(con, fleet, name=name, data=b"*.tmp\n")
    request = publisher.FileRequest("org/a", name, "d0")
    with publisher.ArchivePublisher(
            con, [request], kind="maintenance", retired_paths={request: old}) as operation:
        operation.publish(request, archive / old, original_bytes=len(data), original_sha256=digest,
                          stored_sha256=digest, compressed=False)
        operation.retire(request)
        operation.finish()
    mapped = "org/a/" + payload_relative_path(name)
    assert not (archive / "org/a/nested").exists()
    assert not (map_root / "org/a/nested").exists()
    assert (archive / mapped).is_symlink() and (map_root / mapped).is_symlink()


def test_partial_drive_workset_closes_operation_but_not_layout_obligation(con, fleet):
    archive, map_root, _ = fleet
    first = _legacy(con, fleet, name=".gitattributes")
    _legacy(con, fleet, name=".gitignore", data=b"*.tmp\n")
    data, digest, old = first
    request = publisher.FileRequest("org/a", ".gitattributes", "d0")
    with publisher.ArchivePublisher(
            con, [request], kind="maintenance", retired_paths={request: old}) as operation:
        operation.publish(request, archive / old, original_bytes=len(data), original_sha256=digest,
                          stored_sha256=digest, compressed=False)
        operation.retire(request)
        assert operation.finish()["phase"] == "CLOSED"
    assert con.execute(
        "SELECT state FROM publication_clone_obligations WHERE annex_uuid="
        "(SELECT annex_uuid FROM drives WHERE drive_label='d0')").fetchone() == ("PENDING",)
    with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
        store.require_clear(con, ["d0"], tree_change=True)
    assert (archive / "org/a/.gitignore").is_file()
    assert (map_root / "org/a/.gitignore").is_file()


def test_same_logical_retirement_on_two_drives_is_one_shared_map_delta(
        con, fleet, tmp_path, monkeypatch):
    archive, map_root, git = fleet
    data, digest, old = _legacy(con, fleet)
    second = tmp_path / "archive-d1"
    git("clone", "--no-local", "--quiet", str(archive), str(second))
    git("-C", str(second), "remote", "remove", "origin")
    git("-C", str(second), "annex", "init", "--version=8", "--quiet", "test-d1")
    git("-C", str(second), "config", "annex.backend", "SHA256")
    annex_uuid = git("-C", str(second), "config", "annex.uuid").decode().strip()
    capacity = os.statvfs(second).f_blocks * os.statvfs(second).f_frsize
    fingerprint = identity_fingerprint_v1(
        fs_uuid="d1-fs", annex_uuid=annex_uuid, serial=None, filesystem_capacity_bytes=capacity)
    con.execute("UPDATE drives SET annex_uuid=?,identity_fingerprint=?,filesystem_capacity_bytes=? "
                "WHERE drive_label='d1'", [annex_uuid, fingerprint, capacity])
    con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
                "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
                "VALUES('d1',1,2,?,?,?,'dedicated_local','fixture','fixture','2026-09-19')",
                [os.statvfs(second).f_bavail * os.statvfs(second).f_frsize, capacity, fingerprint])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,stored_relpath,orig_sha256,"
        "orig_bytes,stored_bytes,compressed,annex_key,orig_sha256_provenance) "
        "VALUES('org/a','.gitattributes','d1','.gitattributes','.gitattributes',?,?,?,0,NULL,"
        "'archive-head-blob')", [digest, len(data), len(data)])
    annex_ref = git("-C", str(second), "rev-parse", "refs/heads/git-annex").decode().strip()
    git("-C", str(map_root), "fetch", "--quiet", str(second),
        annex_ref + ":refs/remotes/registered-d1/git-annex")
    git("-C", str(map_root), "annex", "sync", "--only-annex", "--no-content", "--no-pull",
        "--no-push", "--no-commit")
    monkeypatch.setattr(register, "archive_path", lambda _, label: {"d0": archive, "d1": second}.get(label))

    def observe(root):
        path = archive if str(root) == str(archive) else second
        label = "d0" if path == archive else "d1"
        uuid = git("-C", str(path), "config", "annex.uuid").decode().strip()
        value = os.statvfs(path)
        return register.ArchiveVolumeObservation(
            label + "-fs", uuid, None, value.f_blocks * value.f_frsize,
            value.f_bavail * value.f_frsize, value.f_frsize)

    monkeypatch.setattr(register, "observe_archive_volume", observe)
    requests = [publisher.FileRequest("org/a", ".gitattributes", label) for label in ("d0", "d1")]
    with publisher.ArchivePublisher(
            con, requests, kind="maintenance", retired_paths={request: old for request in requests}) as operation:
        for request, root in zip(requests, (archive, second), strict=True):
            operation.publish(request, root / old, original_bytes=len(data), original_sha256=digest,
                              stored_sha256=digest, compressed=False)
            operation.retire(request)
        operation.finish()
    mapped = "org/a/" + payload_relative_path(".gitattributes")
    assert not (map_root / old).exists() and (map_root / mapped).is_symlink()
