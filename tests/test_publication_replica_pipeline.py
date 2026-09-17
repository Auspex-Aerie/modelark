"""Real local source-to-target pipeline; hardware probes are explicitly synthetic."""
import hashlib
import json
import os

import pytest

from modelark import archive_publisher as publisher, compress, publication_install as install
from modelark import publication_native as native
from modelark import publication_replica_pipeline as replica, publication_store as store, register
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.publication_policy import PublicationRefused
from test_archive_publisher import fleet  # noqa: F401
from test_publication_native import _connection, git_repository, publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


DATA = b"# retained exact upstream metadata\n" * 10
DIGEST = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def replica_fleet(con, request, tmp_path, monkeypatch):
    target, map_root, git = request.getfixturevalue("fleet")
    source = tmp_path / "source"
    git("clone", "--no-local", "--quiet", str(target), str(source))
    git("-C", str(source), "remote", "remove", "origin")
    git("-C", str(source), "annex", "init", "--version=8", "--quiet", "replica-source")
    identity = git("-C", str(source), "config", "annex.uuid").decode().strip()
    value = os.statvfs(source)
    capacity = value.f_blocks * value.f_frsize
    fingerprint = identity_fingerprint_v1(fs_uuid="d1-fs", annex_uuid=identity, serial=None,
                                          filesystem_capacity_bytes=capacity)
    con.execute("UPDATE drives SET annex_uuid=?,identity_fingerprint=?,filesystem_capacity_bytes=? "
                "WHERE drive_label='d1'", [identity, fingerprint, capacity])
    con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
                "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
                "VALUES('d1',1,2,?,?,?,'dedicated_local','fixture','fixture','2026-09-14')",
                [value.f_bavail * value.f_frsize, capacity, fingerprint])
    old_observe = register.observe_archive_volume
    def observe(root):
        if str(root) == str(source):
            current = os.statvfs(source)
            return register.ArchiveVolumeObservation("d1-fs", identity, None, capacity,
                                                    current.f_bavail * current.f_frsize, current.f_frsize)
        return old_observe(root)
    monkeypatch.setattr(register, "observe_archive_volume", observe)
    monkeypatch.setattr(register, "archive_path", lambda _, label: {"d0": target, "d1": source}.get(label))
    return target, source, map_root, git


def seed(con, fleet_value, tmp_path, *, filename=".gitignore", compressed=False, backend="SHA256E",
         provenance="hub_confirmed", copy_row=True, unlocked=False):
    _, source, _, git = fleet_value
    relative = "legacy-ignore.json" + (".znn" if compressed else "")
    path = source / "org/a" / relative
    path.parent.mkdir(parents=True)
    if compressed:
        pytest.importorskip("zstandard")
        original = tmp_path / "original"
        original.write_bytes(DATA)
        compress.compress_file(original, path, codec=compress.CODEC_ZSTD)
    else:
        path.write_bytes(DATA)
    stored = path.read_bytes()
    stored_digest = hashlib.sha256(stored).hexdigest()
    git("-C", str(source), "annex", "add", "--force", "--force-large", "--backend=" + backend,
        "--", "org/a/" + relative)
    if unlocked:
        git("-C", str(source), "annex", "unlock", "--", "org/a/" + relative)
    git("-C", str(source), "commit", "-qm", "source fixture")
    key = git("-C", str(source), "annex", "lookupkey", "--", "org/a/" + relative).decode().strip()
    obj = git("-C", str(source), "annex", "examinekey", "--format=${objectpath}", key).decode().strip()
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) VALUES('org/a',?,?,'aux',?)",
                [filename, len(DATA), DIGEST])
    con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,orig_sha256,"
                "znn_sha256,orig_bytes,stored_bytes,compressed,annex_key,orig_sha256_provenance) "
                "VALUES('org/a',?,?,?,'d1',?,?,?,?,?,?,?)",
                [filename, relative, relative, DIGEST, stored_digest if compressed else None,
                 len(DATA), len(stored), int(compressed), key, provenance])
    if copy_row:
        con.execute("INSERT INTO replicas(repo_id,rfilename,drive_label,annex_key,present) VALUES('org/a',?,'d1',?,1)",
                    [filename, key])
    return publisher.FileRequest("org/a", filename, "d0", "d1"), path, source / obj, key, stored


@pytest.mark.parametrize("compressed,unlocked,copy_row,backend,provenance", [
    (False, False, True, "SHA256E", "hub_confirmed"),
    (False, True, False, "SHA256E", "ingestion_computed"),
    (True, False, True, "SHA256E", "archive-head-blob"),
    (False, False, True, "SHA256", "annex_key"),
])
def test_replica_uses_common_pipeline_preserving_full_key_source_and_provenance(
        con, replica_fleet, tmp_path, compressed, unlocked, copy_row, backend, provenance):
    target, source, _, git = replica_fleet
    request, source_path, obj, key, stored = seed(con, replica_fleet, tmp_path, compressed=compressed,
                                                unlocked=unlocked, copy_row=copy_row, backend=backend,
                                                provenance=provenance)
    before = {table: con.execute(f"SELECT * FROM {table} WHERE drive_label='d1'").fetchall()
              for table in ("archived", "replicas")}
    original_identity = (obj.stat().st_dev, obj.stat().st_ino, obj.stat().st_size)
    original_head = git("-C", str(source), "rev-parse", "HEAD")
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            result = replica.publish(owner, request)
            assert result["phase"] == "CATALOG_PUBLISHED"
            assert result["annex_key"] == key
            assert result["stored_path"].endswith(".blob")
            assert (target / result["stored_path"]).is_symlink()
            assert (target / result["stored_path"]).read_bytes() == stored
            assert source_path.read_bytes() == stored and obj.read_bytes() == stored
            assert (obj.stat().st_dev, obj.stat().st_ino, obj.stat().st_size) == original_identity
            assert git("-C", str(source), "rev-parse", "HEAD", fence_fds=owner.child_fence_fds) == original_head
            assert {table: con.execute(f"SELECT * FROM {table} WHERE drive_label='d1'").fetchall()
                    for table in before} == before
            target_row = con.execute("SELECT annex_key,orig_sha256,orig_sha256_provenance,compressed "
                                     "FROM archived WHERE drive_label='d0'").fetchone()
            assert target_row == (key, DIGEST, provenance, int(compressed))
            revision = con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]
            assert replica.publish(owner, request) == result
            assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == revision
            with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
                store.require_clear(con, ["d0", "d1"])


@pytest.mark.parametrize("provenance", [None, "legacy_unknown"])
def test_missing_provenance_is_not_silently_upgraded(con, replica_fleet, tmp_path, provenance):
    request, source_path, _, _, stored = seed(con, replica_fleet, tmp_path, provenance=provenance)
    with pytest.raises(PublicationRefused, match="PROVENANCE_UNPROVEN"):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            replica.publish(owner, request)
    assert source_path.read_bytes() == stored
    assert con.execute("SELECT count(*) FROM archived WHERE drive_label='d0'").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM publication_files").fetchone() == (0,)


@pytest.mark.parametrize("change", ["object-bytes", "mapped-path", "committed-pointer", "copy-key", "original-hash", "unknown-key-suffix"])
def test_divergent_source_refuses_before_any_target_file(con, replica_fleet, tmp_path, change):
    _, source, _, git = replica_fleet
    request, source_path, obj, _, _ = seed(con, replica_fleet, tmp_path)
    if change == "object-bytes":
        obj.chmod(obj.stat().st_mode | 0o200)
        obj.write_bytes(b"wrong" * (len(DATA) // 5))
    elif change == "mapped-path":
        source_path.unlink()
        source_path.write_bytes(b"not the archive object")
    elif change == "committed-pointer":
        git("-C", str(source), "update-index", "--force-remove", "--", str(source_path.relative_to(source)))
        git("-C", str(source), "commit", "-qm", "remove pointer fixture")
    elif change == "copy-key":
        con.execute("UPDATE replicas SET annex_key='SHA256-s0--' || ? WHERE drive_label='d1'", ["0" * 64])
    elif change == "original-hash":
        con.execute("UPDATE archived SET orig_sha256=? WHERE drive_label='d1'", ["0" * 64])
    else:
        con.execute("UPDATE archived SET annex_key=annex_key || '.unknown' WHERE drive_label='d1'")
        con.execute("UPDATE replicas SET annex_key=annex_key || '.unknown' WHERE drive_label='d1'")
    with pytest.raises(PublicationRefused):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            replica.publish(owner, request)
    assert con.execute("SELECT count(*) FROM archived WHERE drive_label='d0'").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM publication_files").fetchone() == (0,)


def test_source_rechecked_after_target_install_before_setkey(con, replica_fleet, tmp_path, monkeypatch):
    request, _, obj, _, _ = seed(con, replica_fleet, tmp_path)
    original = install.apply
    def changed_after_install(*args, **kwargs):
        result = original(*args, **kwargs)
        obj.chmod(obj.stat().st_mode | 0o200)
        obj.write_bytes(b"changed after copy")
        return result
    monkeypatch.setattr(install, "apply", changed_after_install)
    with pytest.raises(PublicationRefused):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            replica.publish(owner, request)
    assert con.execute("SELECT count(*) FROM archived WHERE drive_label='d0'").fetchone() == (0,)
    assert con.execute("SELECT phase FROM publication_files").fetchone() == ("PREPARED",)
    actions = [json.loads(row[0]) for row in con.execute("SELECT intent_json FROM publication_actions")]
    assert not any(row.get("intent", {}).get("command", {}).get("argv", [])[:2] == ["annex", "setkey"]
                   for row in actions)


@pytest.mark.parametrize("change", ["object", "catalog", "head"])
def test_source_rechecked_immediately_before_catalog_cas(con, replica_fleet, tmp_path, monkeypatch, change):
    _, source_root, _, git = replica_fleet
    request, _, obj, _, _ = seed(con, replica_fleet, tmp_path)
    original = native.QualifiedRepository._run_action
    active = []
    def change_after_tags(repository, action_id, *argv, **kwargs):
        result = original(repository, action_id, *argv, **kwargs)
        if argv[0] == "modelark-tag-and-flush":
            owner = active[0]
            if change == "object":
                obj.chmod(obj.stat().st_mode | 0o200)
                obj.write_bytes(b"source changed before catalog publication")
            elif change == "catalog":
                owner.scope.write(lambda c: c.execute("UPDATE archived SET verified_at='changed' WHERE drive_label='d1'"))
            else:
                git("-C", str(source_root), "commit", "--allow-empty", "-qm", "source advanced",
                    fence_fds=owner.child_fence_fds)
        return result
    monkeypatch.setattr(native.QualifiedRepository, "_run_action", change_after_tags)
    with pytest.raises(PublicationRefused):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            active.append(owner)
            replica.publish(owner, request)
    assert con.execute("SELECT count(*) FROM archived WHERE drive_label='d0'").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM replicas WHERE drive_label='d0'").fetchone() == (0,)
    assert con.execute("SELECT phase FROM publication_files").fetchone() == ("TREE_VERIFIED",)


def test_exact_source_capability_is_required_and_closes_with_context(con, replica_fleet, tmp_path):
    request, _, _, _, _ = seed(con, replica_fleet, tmp_path)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            with replica._ReplicaSource(owner, request) as source:
                with pytest.raises(PublicationRefused, match="CAPABILITY_REQUIRED"):
                    replica.require_source(source.pair, owner, request, source._artifact)
                detached = source.pair
                detached["source"]["archived"]["annex_key"] = "forged"
                assert source.key != "forged"
                record = source.record()
                assert record["pair"]["source"]["archived"]["orig_sha256_provenance"] == "hub_confirmed"
                assert record["local"]["annex_key"] == source.key
                assert record["tree"]["annex_key"] == source.key
                assert record["artifact"]["original_sha256"] == DIGEST
                record["pair"]["source"]["archived"]["orig_sha256_provenance"] = "forged"
                assert source.record()["pair"]["source"]["archived"]["orig_sha256_provenance"] == "hub_confirmed"
                # A pure journal value may be frozen in the SQL snapshot. It is
                # not permission to move physical verification into SQL.
                con.execute("BEGIN")
                try:
                    assert source.record()["kind"] == "observed-replica-source"
                finally:
                    con.rollback()
                replica.require_source(source, owner, request, source._artifact)
            with pytest.raises(PublicationRefused, match="SOURCE_CLOSED"):
                source.recheck()
    with pytest.raises(PublicationRefused, match="OWNER_REQUIRED"):
        replica.publish(object(), request)


def test_replica_continues_exact_native_effect_without_repeating_setkey(con, replica_fleet, tmp_path, monkeypatch):
    request, _, obj, key, stored = seed(con, replica_fleet, tmp_path)
    original = native.QualifiedRepository._run_action
    setkey_calls = []
    def lost_reply(repository, action_id, *argv, **kwargs):
        if argv[:2] == ("annex", "setkey"):
            setkey_calls.append(action_id)
            assert len(setkey_calls) == 1, "setkey must not be replayed"
            original(repository, action_id, *argv, **kwargs)
            raise RuntimeError("lost setkey reply")
        return original(repository, action_id, *argv, **kwargs)
    monkeypatch.setattr(native.QualifiedRepository, "_run_action", lost_reply)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            with pytest.raises(RuntimeError, match="lost setkey reply"):
                replica.publish(owner, request)
            assert con.execute("SELECT phase FROM publication_files").fetchone() == ("PREPARED",)
            result = replica.publish(owner, request)
            assert result["annex_key"] == key
            assert result["phase"] == "CATALOG_PUBLISHED"
            assert len(setkey_calls) == 1 and obj.read_bytes() == stored
            # A completed phase allows its exact after-state, not an arbitrary
            # new target row that happens to retain the same annex key.
            owner.scope.write(lambda c: c.execute("UPDATE archived SET verified_at='external drift' WHERE drive_label='d0'"))
            with pytest.raises(PublicationRefused, match="SOURCE_CHANGED"):
                replica.publish(owner, request)


def test_late_continuation_rejects_replacement_source_object_even_with_identical_bytes(
        con, replica_fleet, tmp_path, monkeypatch):
    request, _, obj, _, stored = seed(con, replica_fleet, tmp_path)
    original = native.QualifiedRepository._run_action
    def lost_metadata_reply(repository, action_id, *argv, **kwargs):
        result = original(repository, action_id, *argv, **kwargs)
        if argv[0] == "modelark-tag-and-flush":
            raise RuntimeError("lost metadata reply")
        return result
    monkeypatch.setattr(native.QualifiedRepository, "_run_action", lost_metadata_reply)
    with pytest.raises(PublicationRefused, match="ENCLOSING_COMPLETION_REQUIRED"):
        with publisher.ArchivePublisher(con, [request], kind="replica") as owner:
            with pytest.raises(RuntimeError, match="lost metadata reply"):
                replica.publish(owner, request)
            assert con.execute("SELECT phase FROM publication_files").fetchone() == ("TREE_VERIFIED",)
            replacement = tmp_path / "same-bytes-different-owner"
            replacement.write_bytes(stored)
            replacement.chmod(obj.stat().st_mode)
            parent_mode = obj.parent.stat().st_mode
            obj.parent.chmod(parent_mode | 0o200)
            os.replace(replacement, obj)
            obj.parent.chmod(parent_mode)
            monkeypatch.setattr(native.QualifiedRepository, "_run_action", original)
            with pytest.raises(PublicationRefused):
                replica.publish(owner, request)
            assert con.execute("SELECT count(*) FROM archived WHERE drive_label='d0'").fetchone() == (0,)
            assert con.execute("SELECT phase FROM publication_files").fetchone() == ("TREE_VERIFIED",)
