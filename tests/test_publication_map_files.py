"""Actual scoped archive receipts and disposable private Git candidate trees."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
import posixpath

import pytest

from modelark import archive_publisher as publisher, publication_map_files as files
from modelark import publication_tree as trees
from modelark.publication_policy import PublicationRefused, payload_relative_path, sha256_key
from test_archive_publisher import fleet as archive_fleet  # noqa: F401
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


@pytest.fixture
def fleet(request):
    return request.getfixturevalue("archive_fleet")


@contextmanager
def publishing(con, tmp_path, names=("config.json",)):
    requests = [publisher.FileRequest("org/a", name, "d0") for name in names]
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, requests) as operation:
            selected = []
            for index, request in enumerate(requests):
                data = f"test {index}".encode()
                digest = hashlib.sha256(data).hexdigest()
                staged = tmp_path / f"download-{index}"
                staged.write_bytes(data)
                con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) "
                            "VALUES('org/a',?,?,'aux',?)", [request.rfilename, len(data), digest])
                result = operation.publish(request, staged, original_bytes=len(data), original_sha256=digest,
                                           stored_sha256=digest, compressed=False)
                frozen = json.loads(con.execute("SELECT intent_json FROM publication_files WHERE file_id=?",
                                                [result["file_id"]]).fetchone()[0])
                selected.append(files.FileSelection(result["file_id"], trees.TreeEntry(**frozen["intent"]["entry"])))
            repository = operation._repositories["d0"]
            source = files.SourceFiles(repository, files._snapshot(repository).head_oid, tuple(selected))
            batch = operation._before_state["requests"][selected[0].file_id]["batch_id"]
            yield operation, source, batch


def test_candidate_selected_pointers_only_and_real_map_unchanged(con, fleet, tmp_path):
    archive, map_root, git = fleet
    # An unrelated source-only committed path is fetched, but must not enter the candidate.
    (archive / "unrelated.txt").write_text("unrelated")
    git("add", "unrelated.txt")
    git("commit", "--quiet", "-m", "unrelated source file")
    with publishing(con, tmp_path, ("config.json", "nested/.gitignore")) as (operation, source, batch):
        before = files._snapshot(operation.map)
        with pytest.raises(PublicationRefused, match="WORKSET_MISMATCH"):
            files.candidate(operation.stage, operation.map,
                            sources=(replace(source, files=source.files[:1]),), batch_id=batch)
        candidate = files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch)
        assert candidate.before == before
        assert len(candidate.delta) == 2
        assert "unrelated.txt" not in {entry.path for entry in candidate.entries}
        assert files._snapshot(operation.map) == before
        assert files._index(operation.stage) == candidate.entries
        assert trees._commit(operation.stage.read, candidate.new_head) == (candidate.new_tree, (before.head_oid,))
        assert candidate.new_head != before.head_oid
        assert len(candidate.action_receipts) == 4
        assert con.execute("SELECT count(*) FROM publication_actions WHERE kind='map_stage' AND status='VERIFIED'").fetchone() == (4,)
        assert not (map_root / source.files[0].entry.path).exists()
        assert candidate.record()["selection"][0]["catalog_receipt_digest"]
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=3").fetchone() == (0,)


@pytest.mark.parametrize("problem", ["stale_head", "missing_selection", "wrong_entry", "live_stage", "dirty_index"])
def test_refuses_unbound_or_changed_selection_before_actions(con, fleet, tmp_path, problem):
    archive, _, git = fleet
    with publishing(con, tmp_path) as (operation, source, batch):
        stage = operation.stage
        if problem == "stale_head":
            source = replace(source, head_oid="0" * 40)
        elif problem == "missing_selection":
            source = replace(source, files=())
        elif problem == "wrong_entry":
            source = replace(source, files=(replace(source.files[0], entry=replace(source.files[0].entry, path="other")),))
        elif problem == "live_stage":
            stage = operation.map
        else:
            (archive / "dirty").write_text("dirty")
            git("add", "dirty")
        with pytest.raises(PublicationRefused):
            files.candidate(stage, operation.map, sources=(source,), batch_id=batch)
        assert con.execute("SELECT count(*) FROM publication_actions WHERE kind='map_stage'").fetchone() == (0,)


@pytest.mark.parametrize("problem", ["tampered", "not_published"])
def test_durable_receipt_tampering_is_not_candidate_authority(con, fleet, tmp_path, problem):
    with publishing(con, tmp_path) as (operation, source, batch):
        if problem == "tampered":
            con.execute("UPDATE publication_files SET catalog_proof_json='{}'")
        else:
            con.execute("UPDATE publication_files SET phase='TREE_VERIFIED',catalog_proof_json=NULL,"
                        "catalog_proof_digest=NULL,committed_revision=NULL")
        with pytest.raises(PublicationRefused):
            files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch)
        assert con.execute("SELECT count(*) FROM publication_actions WHERE kind='map_stage'").fetchone() == (0,)


def seed_map(fleet, *, different=False):
    _, map_root, git = fleet
    key = sha256_key(6, hashlib.sha256(b"test 0").hexdigest())
    object_path = git("annex", "examinekey", "--format=${objectpath}", key).decode().strip()
    path = "org/a/" + payload_relative_path("config.json")
    target = map_root / path
    target.parent.mkdir(parents=True)
    os.symlink("wrong-target" if different else posixpath.relpath(object_path, posixpath.dirname(path)), target)
    git("-C", str(map_root), "add", "--", path)
    git("-C", str(map_root), "commit", "-qm", "existing map pointer")


def test_identical_map_pointer_is_noop_without_fabricated_commit(con, fleet, tmp_path):
    seed_map(fleet)
    with publishing(con, tmp_path) as (operation, source, batch):
        before = files._snapshot(operation.map)
        candidate = files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch)
        assert candidate.new_head == before.head_oid
        assert candidate.new_tree == before.tree_oid
        assert candidate.entries == before.entries
        assert candidate.delta == ()
        assert len(candidate.action_receipts) == 1  # Actual Git-only quarantine fetch.
        assert files._snapshot(operation.map) == before
        assert files._snapshot(operation.stage).head_oid == before.head_oid
        revision = con.execute("SELECT last_revision FROM publication_operations").fetchone()
        assert files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch,
                               resume=True) == candidate
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone() == revision


def test_existing_different_pointer_is_conflict_without_any_stage_actions(con, fleet, tmp_path):
    seed_map(fleet, different=True)
    with publishing(con, tmp_path) as (operation, source, batch):
        before = files._snapshot(operation.map)
        with pytest.raises(PublicationRefused, match="PATH_CONFLICT"):
            files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch)
        assert con.execute("SELECT count(*) FROM publication_actions WHERE kind='map_stage'").fetchone() == (0,)
        assert files._snapshot(operation.map) == before


@pytest.mark.parametrize("step", ["fetch-local-object", "update-index", "write-tree", "commit-tree"])
def test_failed_action_receipt_keeps_stage_and_continues_explicitly(con, fleet, tmp_path, monkeypatch, step):
    _, _, git = fleet
    with publishing(con, tmp_path) as (operation, source, batch):
        before = files._snapshot(operation.map)
        original = files.actions.verify
        def fail(*args, **kwargs):
            record = files.actions._load(operation.scope, kwargs["action_id"], operation.binding_digest)
            if record["intent"]["command"]["argv"][0] == step:
                raise PublicationRefused("INJECTED_RECEIPT_FAILURE")
            return original(*args, **kwargs)
        monkeypatch.setattr(files.actions, "verify", fail)
        with pytest.raises(PublicationRefused, match="INJECTED_RECEIPT_FAILURE"):
            files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch)
        monkeypatch.setattr(files.actions, "verify", original)
        assert files._snapshot(operation.map) == before
        assert con.execute("SELECT count(*) FROM publication_actions WHERE kind='map_stage' "
                           "AND status='PREPARED'").fetchone() == (1,)
        with pytest.raises(PublicationRefused, match="RECOVERY_REQUIRED"):
            files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch)
        candidate = files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch, resume=True)
        assert candidate.new_head != before.head_oid
        committed = files.actions.read(operation.scope, candidate.action_receipts[-1][0])
        assert committed["receipt"]["receipt"]["head"] == candidate.new_head
        assert files._snapshot(operation.map) == before
        assert con.execute("SELECT count(*) FROM publication_actions WHERE kind='map_stage' "
                           "AND status='VERIFIED'").fetchone() == (4,)
        revision = con.execute("SELECT last_revision FROM publication_operations").fetchone()
        def no_mutation(*args, **kwargs):
            raise AssertionError("verified repeat must not run native writes")
        monkeypatch.setattr(files.native.QualifiedRepository, "_run_action", no_mutation)
        monkeypatch.setattr(files.native.QualifiedRepository, "_fetch_action", no_mutation)
        repeated = files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch, resume=True)
        assert repeated == candidate
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone() == revision
        if step == "commit-tree":
            # Simulated outside interference in this disposable scratch only.
            stage_path = str(operation.stage.tree.path)
            entry = before.entries[0]
            git("-C", stage_path, "update-index", "--add", "--cacheinfo",
                f"{entry.mode},{entry.oid},unbound")
            with pytest.raises(PublicationRefused, match="STAGE_INDEX_MISMATCH"):
                files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch, resume=True)
            git("-C", stage_path, "read-tree", before.head_oid)
            # Old index is not a valid replay of a VERIFIED index step.
            with pytest.raises(PublicationRefused, match="STAGE_INDEX_MISMATCH"):
                files.candidate(operation.stage, operation.map, sources=(source,), batch_id=batch, resume=True)
