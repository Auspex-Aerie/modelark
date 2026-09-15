"""Real annex/SQLite/path inventory; device UUID/statvfs probes are simulated.

These tests do not establish physical USB reliability or whole-drive hashes.
"""
from contextlib import contextmanager
import hashlib
import json
import os

import pytest

from modelark import archive_publisher as publisher, publication_inventory as inventory
from modelark import publication_locks, publication_native as native, publication_payload as payload
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import MAP
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401
from test_archive_publisher import fleet as native_fleet  # noqa: F401


@pytest.fixture(name="fleet")
def _fleet(request):
    return request.getfixturevalue("native_fleet")


@contextmanager
def reader(con, fleet):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, fleet[0], drive_label="d0") as repository:
            yield repository


def test_baseline_binds_existing_extras_without_reading_large_payloads(con, fleet, monkeypatch):
    archive = fleet[0]
    large = archive / "operator-extra"
    with large.open("wb") as stream:
        stream.truncate(8 * 1024**3)
    (archive / "offline-placeholder").symlink_to("/outside/path/is/not/followed")
    (archive / "empty-existing-directory").mkdir()
    def forbidden(*_, **__):
        pytest.fail("baseline is metadata/presence, never a whole-drive hash")
    monkeypatch.setattr(payload, "verify", forbidden)
    before = tuple(con.iterdump())
    with reader(con, fleet) as repository:
        value = inventory.capture_baseline(repository)
    assert json.loads(json.dumps(value.record())) == value.record()
    assert {row["path"] for row in value.record()["namespace"]} == {
        "", "operator-extra", "offline-placeholder", "empty-existing-directory", "unrelated"}
    assert next(row for row in value.record()["namespace"] if row["path"] == "operator-extra")["identity"][3] == 8 * 1024**3
    assert tuple(con.iterdump()) == before


@pytest.mark.parametrize("kind", ["fifo", "nested-git", "entry-limit", "byte-limit", "transaction"])
def test_baseline_refuses_unqualified_namespace_and_transaction(con, fleet, monkeypatch, kind):
    archive = fleet[0]
    if kind == "fifo":
        os.mkfifo(archive / "unknown-pipe")
    elif kind == "nested-git":
        (archive / "nested/.git").mkdir(parents=True)
    elif kind == "entry-limit":
        monkeypatch.setattr(inventory, "_MAX_ENTRIES", 0)
    elif kind == "byte-limit":
        monkeypatch.setattr(inventory, "_MAX_NAMESPACE_BYTES", 0)
    with reader(con, fleet) as repository:
        if kind == "transaction":
            con.execute("BEGIN")
        try:
            with pytest.raises(PublicationRefused):
                inventory.capture_baseline(repository)
        finally:
            if con.in_transaction:
                con.rollback()


def _request(con, tmp_path, name="config.json", data=b"small original bytes\n"):
    digest = hashlib.sha256(data).hexdigest()
    staged = tmp_path / (name.replace("/", "-") + ".download")
    staged.write_bytes(data)
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) VALUES('org/a',?,?,'aux',?)",
                [name, len(data), digest])
    return publisher.FileRequest("org/a", name, "d0"), staged, digest, len(data)


def _publish(operation, request):
    selection, staged, digest, size = request
    return operation.publish(selection, staged, original_bytes=size, original_sha256=digest,
                             stored_sha256=digest, compressed=False)


def _verify(operation):
    baseline = operation._before_state["inventories"]["d0"]
    request = operation._requests[0]
    batch_id = operation._before_state["requests"][operation._files[request]]["batch_id"]
    return inventory.verify(operation._repositories["d0"], baseline, file_rows=operation._batch_rows(batch_id))


def test_completed_native_file_has_fresh_inventory_not_a_clean_anchor(con, fleet, tmp_path):
    archive = fleet[0]
    (archive / "unrelated-extra").write_bytes(b"operator bytes remain")
    (archive / "empty-extra").mkdir()
    request = _request(con, tmp_path)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request[0]]) as operation:
            _publish(operation, request)
            before = tuple(con.iterdump())
            proof = _verify(operation)
            assert proof.observation.identity_proven and proof.observation.free_bytes > 0
            assert proof.record()["whole_drive_content_hashed"] is False
            assert len(proof.record()["changed_file_hashes"]) == 1
            assert proof.record()["unexplained_extras"] == []
            assert (archive / "unrelated-extra").read_bytes() == b"operator bytes remain"
            assert tuple(con.iterdump()) == before, "read-only proof must not publish a clean anchor"
            with pytest.raises(PublicationRefused, match="BASELINE_PHASE_INVALID"):
                inventory.capture_baseline(operation._repositories["d0"])


@pytest.mark.parametrize("change,error", [
    ("extra", "UNEXPLAINED_PATHS"),
    ("changed-extra", "UNTOUCHED_PATH_CHANGED"),
    ("directory", "DIRECTORY_REPLACED"),
    ("payload", "HASH_MISMATCH"),
    ("index", "INDEX_TREE_MISMATCH"),
    ("catalog", "CATALOG_CHANGED"),
    ("wrong-baseline", "BASELINE_BINDING_MISMATCH"),
    ("missing-file", "FILE_BINDING_MISMATCH"),
])
def test_closure_refuses_unexplained_drift_without_catalog_writes(con, fleet, tmp_path, change, error):
    archive, _, git = fleet
    (archive / "existing-extra").write_bytes(b"keep")
    # Parent directory exists before preparation so replacement is not an
    # approved consequence of the selected new file's creation.
    (archive / "org/a").mkdir(parents=True)
    request = _request(con, tmp_path)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request[0]]) as operation:
            result = _publish(operation, request)
            if change == "extra":
                (archive / "download-was-not-journalled").write_bytes(b"must not ignore")
            elif change == "changed-extra":
                (archive / "existing-extra").write_bytes(b"edit")
            elif change == "directory":
                os.chmod(archive / "org", 0o700)
            elif change == "payload":
                object_path = operation._repositories["d0"].object_path(result["annex_key"])
                os.chmod(archive / object_path, 0o600)
                (archive / object_path).write_bytes(b"X" * request[3])
            elif change == "index":
                git("update-index", "--force-remove", "--", "unrelated")
            elif change == "catalog":
                # Simulate a rogue unrevisioned edit; real writers are fenced.
                con.execute("UPDATE archived SET verified_at='foreign-time' WHERE drive_label='d0'")
            baseline = operation._before_state["inventories"]["d0"]
            batch_id = operation._before_state["requests"][operation._files[request[0]]]["batch_id"]
            rows = operation._batch_rows(batch_id) if change != "catalog" else None
            if change == "catalog":
                # Direct store-chain rows avoid the parent's independent catalog
                # refusal so this test exercises the inventory's own comparison.
                rows = inventory._read_state(operation._repositories["d0"], operation=True)["rows"]
            if change == "wrong-baseline":
                baseline = {**baseline, "root": "/not-the-selected-drive"}
            elif change == "missing-file":
                rows = []
            before = tuple(con.iterdump())
            with pytest.raises(PublicationRefused, match=error):
                inventory.verify(operation._repositories["d0"], baseline, file_rows=rows)
            assert tuple(con.iterdump()) == before


def test_shared_native_key_uses_latest_object_receipt_and_hashes_each_mapped_reader(con, fleet, tmp_path):
    first = _request(con, tmp_path, "first.json")
    second = _request(con, tmp_path, "second.json")
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [first[0], second[0]]) as operation:
            one = _publish(operation, first)
            two = _publish(operation, second)
            assert one["annex_key"] == two["annex_key"]
            proof = _verify(operation)
            assert len(proof.record()["changed_file_hashes"]) == 2


def test_detach_during_final_observation_refuses(con, fleet, tmp_path, monkeypatch):
    request = _request(con, tmp_path)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request[0]]) as operation:
            _publish(operation, request)
            def detached(*_, **__):
                raise PublicationRefused("FIXTURE_DRIVE_DETACHED")
            monkeypatch.setattr(inventory.attachment, "capture", detached)
            with pytest.raises(PublicationRefused, match="FIXTURE_DRIVE_DETACHED"):
                _verify(operation)


def test_unproven_catalog_claim_refuses_baseline_even_with_existing_other_files(con, fleet):
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,stored_relpath,stored_bytes,compressed) "
                "VALUES('org/a','model.safetensors','d0','model.safetensors','model.safetensors',100,0)")
    with reader(con, fleet) as repository:
        with pytest.raises(PublicationRefused, match="CLAIM_UNAVAILABLE"):
            inventory.capture_baseline(repository)


def _old_claim(con, fleet):
    archive, _, git = fleet
    path = archive / "org/a/old.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"old untouched payload\n"
    path.write_bytes(data)
    git("annex", "add", "--force-large", "--", "org/a/old.json")
    git("commit", "-qm", "existing archived claim before publication")
    git("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
    key = git("annex", "lookupkey", "--", "org/a/old.json").decode().strip()
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) VALUES('org/a','old.json',?,'aux',?)",
                [len(data), hashlib.sha256(data).hexdigest()])
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,stored_relpath,stored_bytes,orig_bytes,"
                "compressed,annex_key,orig_sha256) VALUES('org/a','old.json','d0','old.json','old.json',?,?,0,?,?)",
                [len(data), len(data), key, hashlib.sha256(data).hexdigest()])
    con.execute("INSERT INTO replicas(repo_id,rfilename,drive_label,annex_key,present) VALUES('org/a','old.json','d0',?,1)",
                [key])
    return key, path.resolve()


@pytest.mark.parametrize("change", [None, "missing", "same-size-corruption"])
def test_untouched_claim_requires_presence_and_original_stat_without_rehash(con, fleet, tmp_path, monkeypatch, change):
    old_key, old_object = _old_claim(con, fleet)
    request = _request(con, tmp_path)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request[0]]) as operation:
            _publish(operation, request)
            original = payload.verify
            checked = []
            def observe(*args, **kwargs):
                checked.append(kwargs["annex_key"])
                assert kwargs["annex_key"] != old_key, "old weight is presence-checked, not bulk hashed"
                return original(*args, **kwargs)
            monkeypatch.setattr(payload, "verify", observe)
            if change == "missing":
                os.chmod(old_object.parent, 0o700)  # Native annex protects its object directories.
                old_object.rename(old_object.with_name("retained-missing-fixture"))
                error = "CLAIM_UNAVAILABLE"
            elif change == "same-size-corruption":
                size = old_object.stat().st_size
                os.chmod(old_object, 0o600)
                old_object.write_bytes(b"X" * size)
                error = "UNTOUCHED_COPY_CHANGED"
            if change:
                with pytest.raises(PublicationRefused, match=error):
                    _verify(operation)
            else:
                proof = _verify(operation)
                assert len(proof.record()["presence"]) == 2
                assert len(proof.record()["changed_file_hashes"]) == 1
            assert len(checked) == 1
