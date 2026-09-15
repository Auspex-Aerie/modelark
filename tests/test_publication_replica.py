"""Replica import journal/recovery with real native objects and disposable scopes."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import os
from pathlib import PurePosixPath
import posixpath

import pytest

from modelark import publication_actions as actions, publication_artifact as artifacts
from modelark import publication_catalog as catalog, publication_install as install
from modelark import publication_locks, publication_native as native, publication_replica as replica
from modelark import publication_store as store, publication_tree as trees
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import BATCH, FILE, MAP, OP
from test_publication_native import git_repository, publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


DATA = b"replica original metadata\r\n"
DIGEST = hashlib.sha256(DATA).hexdigest()
TARGET = "org/a/__modelark_payload_v1__/p-replica.blob"


@pytest.fixture(name="con")
def connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture
def prepared(request):
    return request.getfixturevalue("native_prepared")


@contextmanager
def setup(con, prepared, tmp_path, *, extended=True, install_copy=True):
    archive, git = prepared
    original = tmp_path / "source-original.json"
    original.write_bytes(DATA)
    key = f"SHA256{'E' if extended else ''}-s{len(DATA)}--{DIGEST}" + (".json" if extended else "")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,sha256) VALUES('org/a','config.json',?,'aux',?)",
                [len(DATA), DIGEST])
    con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,orig_sha256,"
                "orig_bytes,stored_bytes,compressed,annex_key,orig_sha256_provenance) "
                "VALUES('org/a','config.json','config.json','config.json','d1',?,?,?,0,?,'hub_confirmed')",
                [DIGEST, len(DATA), len(DATA), key])
    (archive / ".git/info/exclude").write_text("org/a/__modelark_payload_v1__/\n")
    with publication_locks.hold(con, ["d0", "d1"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with artifacts.VerifiedArtifact(scope, original, compressed=False, original_bytes=len(DATA),
                                            original_sha256=DIGEST, stored_sha256=DIGEST) as artifact:
                before = trees.capture(repository.tree, read_git=repository.read, require_scope=scope.require_io)
                obj = repository.object_path(key)
                plan = install.preview(repository, artifact, TARGET)
                link = posixpath.relpath(obj, str(PurePosixPath(TARGET).parent)).encode()
                entry = trees.TreeEntry(TARGET, "120000", hashlib.sha1(
                    b"blob " + str(len(link)).encode() + b"\0" + link).hexdigest())
                profiles = {"d0": repository.profile.record()}
                scope.write(lambda _: store.prepare_operation(
                    scope, operation_id=OP, kind="replica", profile_digest=store.digest(profiles),
                    batch_files={BATCH: [FILE]}, before_state={"profiles": profiles}))
                frozen = {}
                def prepare(_):
                    pair = catalog.capture(scope, repo_id="org/a", rfilename="config.json",
                                           drive_label="d0", source_drive="d1")
                    archived = {**pair["source"]["archived"], "drive_label": "d0",
                                "stored_name": PurePosixPath(TARGET).name,
                                "stored_relpath": TARGET.removeprefix("org/a/")}
                    copied = {"repo_id": "org/a", "rfilename": "config.json", "drive_label": "d0",
                              "annex_key": key, "present": 1, "verified_at": "frozen", "added_at": "frozen"}
                    frozen.update(stored_path=TARGET, annex_key=key, object_path=obj, install=plan,
                                  tree_before=before.record(), entry=asdict(entry),
                                  catalog_pair=catalog.intended_pair(pair, archived=archived, replicas=copied))
                    return store.prepare_file(scope, operation_id=OP, batch_id=BATCH, file_id=FILE, intent=frozen)
                scope.write(prepare)
                install_id = install.prepare(repository, artifact, file_id=FILE, plan=plan)
                if install_copy:
                    install.apply(repository, artifact, install_id)
                yield repository, artifact, original, frozen, git


def action(repository, name):
    return actions.read(repository.scope, replica._id(FILE, name))


def interrupt_receipt(monkeypatch, name):
    original = actions.verify
    identifier = replica._id(FILE, name)
    def fail(*args, **kwargs):
        if kwargs.get("action_id") == identifier:
            raise RuntimeError("receipt interruption")
        return original(*args, **kwargs)
    monkeypatch.setattr(actions, "verify", fail)
    return original


@pytest.mark.parametrize("extended", [False, True])
def test_replica_stages_exact_owned_pointer_without_catalog_or_phase_advance(con, prepared, tmp_path, extended):
    with setup(con, prepared, tmp_path, extended=extended) as (repository, artifact, original, frozen, _):
        before_catalog = {table: con.execute(f"SELECT * FROM {table}").fetchall() for table in ("archived", "replicas")}
        result = replica.apply(repository, artifact, file_id=FILE)
        assert result.local.annex_key == frozen["annex_key"]
        assert result.local.stored_sha256 == DIGEST
        assert result.local.stored_path == TARGET
        assert result.local.representation == "locked"
        assert all(record["status"] == "VERIFIED" for record in result.actions)
        assert original.read_bytes() == DATA
        assert con.execute("SELECT phase FROM publication_files WHERE file_id=?", [FILE]).fetchone() == ("PREPARED",)
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=2").fetchone() == (0,)
        assert {table: con.execute(f"SELECT * FROM {table}").fetchall() for table in before_catalog} == before_catalog
        revision = con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]
        repeated = replica.apply(repository, artifact, file_id=FILE)
        assert repeated.local.record() == result.local.record()
        assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == revision


@pytest.mark.parametrize("name", ["replica-setkey", "replica-pointer", "replica-stage"])
def test_exact_postmutation_recovery_observes_without_repeating_native_command(con, prepared, tmp_path, monkeypatch, name):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, original, frozen, _):
        original_verify = interrupt_receipt(monkeypatch, name)
        with pytest.raises(RuntimeError, match="receipt interruption"):
            replica.apply(repository, artifact, file_id=FILE)
        assert action(repository, name)["status"] == "PREPARED"
        if name == "replica-setkey":
            assert not os.path.lexists(archive / TARGET)
            assert (archive / frozen["object_path"]).read_bytes() == DATA
        monkeypatch.setattr(actions, "verify", original_verify)
        native_calls = []
        run = repository._run_action
        def record(identifier, *args, **kwargs):
            native_calls.append(identifier)
            return run(identifier, *args, **kwargs)
        monkeypatch.setattr(repository, "_run_action", record)
        result = replica.apply(repository, artifact, file_id=FILE)
        assert replica._id(FILE, name) not in native_calls
        assert result.local.stored_sha256 == DIGEST and original.read_bytes() == DATA


@pytest.mark.parametrize("name", ["replica-setkey", "replica-stage"])
def test_ambiguous_prepared_native_action_is_not_blindly_reexecuted(con, prepared, tmp_path, monkeypatch, name):
    with setup(con, prepared, tmp_path) as (repository, artifact, original, _, _):
        run = repository._run_action
        def fail(identifier, *args, **kwargs):
            if identifier == replica._id(FILE, name):
                raise RuntimeError("native interruption")
            return run(identifier, *args, **kwargs)
        monkeypatch.setattr(repository, "_run_action", fail)
        with pytest.raises(RuntimeError, match="native interruption"):
            replica.apply(repository, artifact, file_id=FILE)
        monkeypatch.setattr(repository, "_run_action", lambda *a, **k: pytest.fail("native action repeated"))
        with pytest.raises(PublicationRefused, match="CONTINUATION_REQUIRED"):
            replica.apply(repository, artifact, file_id=FILE)
        assert original.read_bytes() == DATA
        assert action(repository, name)["status"] == "PREPARED"


def test_existing_valid_object_is_reused_but_only_owned_staged_copy_consumed(con, prepared, tmp_path):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, original, frozen, git):
        first = tmp_path / "fixture-object-input"
        first.write_bytes(DATA)
        git("annex", "setkey", "--", frozen["annex_key"], str(first), fence_fds=repository.scope.child_fence_fds)
        inode = (archive / frozen["object_path"]).stat().st_ino
        replica.apply(repository, artifact, file_id=FILE)
        assert (archive / frozen["object_path"]).stat().st_ino == inode
        assert original.read_bytes() == DATA


@pytest.mark.parametrize("case", ["wrong-bytes", "same-bytes-new-inode", "hardlink", "symlink"])
def test_changed_installed_copy_is_preserved_and_never_passed_to_setkey(con, prepared, tmp_path, monkeypatch, case):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, original, _, _):
        target = archive / TARGET
        if case == "wrong-bytes":
            target.write_bytes(b"wrong bytes")
        elif case == "same-bytes-new-inode":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(DATA)
            os.replace(replacement, target)
        elif case == "hardlink":
            os.link(target, tmp_path / "another-owner")
        else:
            target.unlink()
            target.symlink_to(original)
        monkeypatch.setattr(repository, "_run_action", lambda *a, **k: pytest.fail("unowned copy was consumed"))
        with pytest.raises(PublicationRefused):
            replica.apply(repository, artifact, file_id=FILE)
        assert os.path.lexists(target) and original.read_bytes() == DATA


def test_unverified_installer_and_unselected_file_refuse_without_native_mutation(con, prepared, tmp_path):
    with setup(con, prepared, tmp_path, install_copy=False) as (repository, artifact, _, _, _):
        with pytest.raises(PublicationRefused, match="INSTALL_UNVERIFIED"):
            replica.apply(repository, artifact, file_id=FILE)
        with pytest.raises(PublicationRefused, match="FILE_UNSELECTED"):
            replica.apply(repository, artifact, file_id=BATCH)
        with pytest.raises(PublicationRefused, match="CAPABILITY_REQUIRED"):
            replica.apply(repository, artifact.proof.record(), file_id=FILE)


def test_pointer_collision_after_consumed_input_is_preserved(con, prepared, tmp_path, monkeypatch):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, _, _, _):
        verify = interrupt_receipt(monkeypatch, "replica-setkey")
        with pytest.raises(RuntimeError):
            replica.apply(repository, artifact, file_id=FILE)
        monkeypatch.setattr(actions, "verify", verify)
        target = archive / TARGET
        target.write_bytes(b"operator collision")
        with pytest.raises(PublicationRefused, match="CONTINUATION_REQUIRED"):
            replica.apply(repository, artifact, file_id=FILE)
        assert target.read_bytes() == b"operator collision"


def test_parent_replacement_refuses_even_with_identical_installed_bytes(con, prepared, tmp_path):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, original, _, _):
        moved = archive / "old-org"
        (archive / "org").rename(moved)
        (archive / TARGET).parent.mkdir(parents=True)
        (archive / TARGET).write_bytes(DATA)
        with pytest.raises(PublicationRefused):
            replica.apply(repository, artifact, file_id=FILE)
        assert (moved / TARGET.removeprefix("org/")).read_bytes() == DATA
        assert original.read_bytes() == DATA


@pytest.mark.parametrize("name", ["replica-setkey", "replica-stage"])
def test_native_error_after_successful_disk_mutation_recovers_by_observation(con, prepared, tmp_path, monkeypatch, name):
    with setup(con, prepared, tmp_path) as (repository, artifact, original, _, _):
        run = repository._run_action
        def fail_after(identifier, *args, **kwargs):
            result = run(identifier, *args, **kwargs)
            if identifier == replica._id(FILE, name):
                raise RuntimeError("lost native response")
            return result
        monkeypatch.setattr(repository, "_run_action", fail_after)
        with pytest.raises(RuntimeError, match="lost native response"):
            replica.apply(repository, artifact, file_id=FILE)
        def no_repeat(identifier, *args, **kwargs):
            assert identifier != replica._id(FILE, name)
            return run(identifier, *args, **kwargs)
        monkeypatch.setattr(repository, "_run_action", no_repeat)
        assert replica.apply(repository, artifact, file_id=FILE).local.stored_sha256 == DIGEST
        assert original.read_bytes() == DATA


def test_failed_setkey_consumed_input_without_object_is_not_replayed(con, prepared, tmp_path, monkeypatch):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, original, frozen, _):
        def failed_setkey(identifier, *args, **kwargs):
            assert identifier == replica._id(FILE, "replica-setkey")
            (archive / TARGET).unlink()
            raise RuntimeError("native failure consumed disposable input")
        monkeypatch.setattr(repository, "_run_action", failed_setkey)
        with pytest.raises(RuntimeError, match="native failure"):
            replica.apply(repository, artifact, file_id=FILE)
        monkeypatch.setattr(repository, "_run_action", lambda *a, **k: pytest.fail("failed native action repeated"))
        with pytest.raises(PublicationRefused, match="PAYLOAD_UNAVAILABLE"):
            replica.apply(repository, artifact, file_id=FILE)
        assert not os.path.lexists(archive / TARGET)
        assert not os.path.lexists(archive / frozen["object_path"])
        assert original.read_bytes() == DATA
        assert action(repository, "replica-setkey")["status"] == "PREPARED"


def test_changed_original_source_refuses_before_consuming_installed_copy(con, prepared, tmp_path, monkeypatch):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, original, _, _):
        original.write_bytes(b"source changed")
        monkeypatch.setattr(repository, "_run_action", lambda *a, **k: pytest.fail("changed source accepted"))
        with pytest.raises(PublicationRefused):
            replica.apply(repository, artifact, file_id=FILE)
        assert (archive / TARGET).read_bytes() == DATA


@pytest.mark.parametrize("change", ["object", "index", "pointer", "parent"])
def test_completed_receipts_do_not_authorize_changed_state(con, prepared, tmp_path, monkeypatch, change):
    archive, _ = prepared
    with setup(con, prepared, tmp_path) as (repository, artifact, original, frozen, git):
        replica.apply(repository, artifact, file_id=FILE)
        if change == "object":
            replacement = tmp_path / "replacement-object"
            replacement.write_bytes(DATA)
            obj = archive / frozen["object_path"]
            obj.parent.chmod(obj.parent.stat().st_mode | 0o200)
            os.replace(replacement, obj)
        elif change == "index":
            git("update-index", "--force-remove", "--", TARGET,
                fence_fds=repository.scope.child_fence_fds)
        elif change == "pointer":
            (archive / TARGET).unlink()
            (archive / TARGET).symlink_to(original)
        else:
            (archive / "org").rename(archive / "old-org")
            (archive / TARGET).parent.mkdir(parents=True)
            (archive / TARGET).symlink_to(original)
        monkeypatch.setattr(repository, "_run_action", lambda *a, **k: pytest.fail("completed action replayed"))
        with pytest.raises(PublicationRefused):
            replica.apply(repository, artifact, file_id=FILE)
        assert original.read_bytes() == DATA
