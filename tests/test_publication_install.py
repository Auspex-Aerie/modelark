"""Actual exclusive installation on disposable repos and verified staged bytes."""
from contextlib import contextmanager
from dataclasses import replace
import errno
import hashlib
import os
import uuid

import pytest

from modelark import publication_actions as actions, publication_artifact as artifacts
from modelark import publication_install as install, publication_locks, publication_native as native
from modelark import publication_store as store
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import BATCH, FILE, MAP, OP
from test_publication_native import git_repository, publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


PAYLOAD = b"metadata: reproducible staged bytes\n"
TARGET = "org/model/config.yaml"


@pytest.fixture(name="con")
def connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture
def prepared(request):
    return request.getfixturevalue("native_prepared")


@contextmanager
def _capabilities(con, prepared, tmp_path):
    archive, _ = prepared
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    source = staging / "verified"
    source.write_bytes(PAYLOAD)
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with artifacts.VerifiedArtifact(scope, source, compressed=False, original_bytes=len(PAYLOAD),
                                            original_sha256=digest, stored_sha256=digest) as artifact:
                yield repository, artifact, source


def _start(repository, artifact, *, target=TARGET):
    scope = repository.scope
    plan = install.preview(repository, artifact, target)
    profiles = {"d0": repository.profile.record()}
    scope.write(lambda _: store.prepare_operation(
        scope, operation_id=OP, kind="fill", profile_digest=store.digest(profiles),
        batch_files={BATCH: [FILE]}, before_state={"profiles": profiles},
    ))
    scope.write(lambda _: store.prepare_file(
        scope, operation_id=OP, batch_id=BATCH, file_id=FILE, intent={"install": plan},
    ))
    action = install.prepare(repository, artifact, file_id=FILE, plan=plan)
    return action, plan


def _temporary(archive, action):
    return archive / "org/model" / (".modelark-publication-" + action + ".partial")


def _copies(con):
    return {table: con.execute(f"SELECT * FROM {table}").fetchall() for table in ("archived", "replicas")}


def test_durable_install_writes_exact_bytes_but_no_catalog_pair_or_clean_anchor(con, prepared, tmp_path):
    archive, _ = prepared
    before = _copies(con)
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        action, plan = _start(repository, artifact)
        assert plan["target_before"] is None and all(row["identity"] is None for row in plan["parents"])
        assert not (archive / "org").exists()
        install.apply(repository, artifact, action)
        assert (archive / TARGET).read_bytes() == PAYLOAD == source.read_bytes()
        assert not _temporary(archive, action).exists()
        assert actions.read(repository.scope, action)["status"] == "VERIFIED"
        assert con.execute("SELECT phase FROM publication_files").fetchone()[0] == "PREPARED"
        assert con.execute("SELECT state FROM publication_operations").fetchone()[0] == "PREPARED"
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=2").fetchone()[0] == 0
    assert _copies(con) == before


@pytest.mark.parametrize("prefix", [0, 8, len(PAYLOAD)])
def test_exact_partial_temporary_resumes_without_replacing_source(con, prepared, tmp_path, prefix):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        action, _ = _start(repository, artifact)
        temporary = _temporary(archive, action)
        temporary.parent.mkdir(parents=True)
        temporary.write_bytes(PAYLOAD[:prefix])
        temporary.chmod(0o600)
        install.apply(repository, artifact, action)
        assert (archive / TARGET).read_bytes() == source.read_bytes() == PAYLOAD
        assert not temporary.exists()


@pytest.mark.parametrize("problem", ["wrong-prefix", "wrong-mode", "hardlink", "fifo", "symlink"])
def test_unproven_temporary_is_preserved_without_overwrite(con, prepared, tmp_path, problem):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        action, _ = _start(repository, artifact)
        temporary = _temporary(archive, action)
        temporary.parent.mkdir(parents=True)
        external = tmp_path / "external"
        if problem == "fifo":
            os.mkfifo(temporary, 0o600)
        elif problem == "symlink":
            external.write_bytes(b"operator bytes")
            temporary.symlink_to(external)
        else:
            temporary.write_bytes(b"wrong" if problem == "wrong-prefix" else PAYLOAD[:8])
            temporary.chmod(0o644 if problem == "wrong-mode" else 0o600)
            if problem == "hardlink":
                os.link(temporary, external)
        revision = con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]
        with pytest.raises(PublicationRefused):
            install.apply(repository, artifact, action)
        assert os.path.lexists(temporary)
        assert not (archive / TARGET).exists()
        assert source.read_bytes() == PAYLOAD
        assert actions.read(repository.scope, action)["status"] == "PREPARED"
        assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == revision
        if problem == "symlink":
            assert external.read_bytes() == b"operator bytes"


def test_receipt_failure_preserves_target_and_can_resume_exact_completion(con, prepared, tmp_path, monkeypatch):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        action, _ = _start(repository, artifact)
        original = actions.verify
        monkeypatch.setattr(actions, "verify", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("receipt interruption")))
        with pytest.raises(RuntimeError, match="receipt interruption"):
            install.apply(repository, artifact, action)
        assert (archive / TARGET).read_bytes() == PAYLOAD
        assert actions.read(repository.scope, action)["status"] == "PREPARED"
        monkeypatch.setattr(actions, "verify", original)
        install.apply(repository, artifact, action)
        assert actions.read(repository.scope, action)["status"] == "VERIFIED"
        assert source.read_bytes() == PAYLOAD


def test_insufficient_target_space_refuses_before_copy_and_keeps_journal_pending(
    con, prepared, tmp_path, monkeypatch,
):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        action, _ = _start(repository, artifact)
        actual = os.fstatvfs

        def no_space(fd):
            value = actual(fd)
            return type("Space", (), {"f_bavail": 0, "f_frsize": value.f_frsize})()

        monkeypatch.setattr(os, "fstatvfs", no_space)
        with pytest.raises(PublicationRefused, match="SPACE_INSUFFICIENT") as refused:
            install.apply(repository, artifact, action)
        assert refused.value.evidence == {
            "required_bytes": len(PAYLOAD), "available_bytes": 0,
        }
        assert source.read_bytes() == PAYLOAD
        assert not (archive / TARGET).exists()
        assert actions.read(repository.scope, action)["status"] == "PREPARED"


@pytest.mark.parametrize("problem", ["changed-source", "occupied-target", "symlink-target", "untracked-child"])
def test_changed_source_or_namespace_refuses_without_cleanup(con, prepared, tmp_path, problem):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        action, _ = _start(repository, artifact)
        target = archive / TARGET
        if problem == "changed-source":
            source.write_bytes(b"source changed")
        else:
            target.parent.mkdir(parents=True)
            if problem == "occupied-target":
                target.write_bytes(b"operator bytes")
            elif problem == "symlink-target":
                target.symlink_to(source)
            else:
                (target.parent / "untracked").write_bytes(b"operator bytes")
        with pytest.raises(PublicationRefused):
            install.apply(repository, artifact, action)
        assert actions.read(repository.scope, action)["status"] == "PREPARED"
        if problem == "changed-source":
            assert not (archive / "org").exists()
        elif problem == "occupied-target":
            assert target.read_bytes() == b"operator bytes"
        elif problem == "symlink-target":
            assert target.is_symlink()
        else:
            assert (target.parent / "untracked").read_bytes() == b"operator bytes"


def test_apply_rebinds_durable_action_to_file_intent_not_arbitrary_journal_plan(con, prepared, tmp_path):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        action, plan = _start(repository, artifact)
        record = actions.read(repository.scope, action)
        other = str(uuid.uuid4())
        changed = {**plan, "stored_path": "different", "parents": []}
        # Synthetic journal row is not a production permission to install elsewhere.
        repository.scope.write(lambda _: actions.prepare(
            repository.scope, action_id=other, kind="payload_install",
            intent={**record["intent"], "filesystem_plan": changed},
        ))
        with pytest.raises(PublicationRefused, match="FILE_INTENT_MISMATCH"):
            install.apply(repository, artifact, other)
        assert not (archive / "different").exists()
        with pytest.raises(PublicationRefused, match="FILE_INTENT_MISMATCH"):
            install.prepare(repository, artifact, file_id=FILE, plan=changed)


def test_verified_install_is_not_reexecuted_after_annex_representation_or_missing_target(con, prepared, tmp_path):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        action, _ = _start(repository, artifact)
        install.apply(repository, artifact, action)
        target = archive / TARGET
        target.unlink()
        target.symlink_to(source)  # Simulated later representation; not installer authority.
        with pytest.raises(PublicationRefused, match="ALREADY_VERIFIED"):
            install.apply(repository, artifact, action)
        target.unlink()
        target.parent.rmdir()
        target.parent.parent.rmdir()
        with pytest.raises(PublicationRefused, match="ALREADY_VERIFIED"):
            install.apply(repository, artifact, action)
        assert not (archive / "org").exists()


def test_existing_temporary_uses_no_mount_crossing_open(con, prepared, tmp_path, monkeypatch):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        action, _ = _start(repository, artifact)
        temporary = _temporary(archive, action)
        temporary.parent.mkdir(parents=True)
        temporary.write_bytes(PAYLOAD[:8])
        temporary.chmod(0o600)
        original = repository.tree.open

        def mount_crossing(path, *args, **kwargs):
            if path.endswith(".partial"):
                raise OSError(errno.EXDEV, "injected mount crossing")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(repository.tree, "open", mount_crossing)
        with pytest.raises(PublicationRefused, match="NAMESPACE_UNPROVEN"):
            install.apply(repository, artifact, action)
        assert temporary.read_bytes() == PAYLOAD[:8]


def test_temporary_creation_is_exclusive_and_replay_flushes_created_parent(con, prepared, tmp_path, monkeypatch):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        action, _ = _start(repository, artifact)
        (archive / "org").mkdir()  # Simulate interruption after mkdir, before parent fsync.
        opened, flushed = [], []
        original_open, original_fsync = os.open, os.fsync

        def opening(path, flags, *args, **kwargs):
            if str(path).endswith(".partial"):
                opened.append(flags)
            return original_open(path, flags, *args, **kwargs)

        def fsync(fd):
            flushed.append(os.fstat(fd).st_ino)
            return original_fsync(fd)

        monkeypatch.setattr(os, "open", opening)
        monkeypatch.setattr(os, "fsync", fsync)
        install.apply(repository, artifact, action)
        assert opened and all(flags & os.O_EXCL and flags & os.O_CREAT for flags in opened)
        assert archive.stat().st_ino in flushed


def test_zero_progress_write_refuses_without_hanging_or_cleanup(con, prepared, tmp_path, monkeypatch):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        action, _ = _start(repository, artifact)
        monkeypatch.setattr(os, "write", lambda *args: 0)
        with pytest.raises(PublicationRefused, match="WRITE_FAILED"):
            install.apply(repository, artifact, action)
        assert _temporary(archive, action).exists()
        assert not (archive / TARGET).exists()


def test_record_or_false_hash_is_not_an_artifact_capability(con, prepared, tmp_path):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, source):
        with pytest.raises(PublicationRefused, match="CAPABILITY_REQUIRED"):
            install.preview(repository, artifact.proof, TARGET)
        with pytest.raises(PublicationRefused, match="CAPABILITY_REQUIRED"):
            install.preview(repository, artifact.proof.record(), TARGET)
        with pytest.raises(AttributeError):
            artifact.proof = replace(artifact.proof, original_sha256="f" * 64)
        with pytest.raises(PublicationRefused, match="STORED_HASH_MISMATCH"):
            artifacts.VerifiedArtifact(repository.scope, source, compressed=False, original_bytes=len(PAYLOAD),
                                        original_sha256=hashlib.sha256(PAYLOAD).hexdigest(), stored_sha256="f" * 64)
    assert not (archive / "org").exists()


def test_preexisting_parent_identity_is_rechecked_after_preview(con, prepared, tmp_path):
    archive, _ = prepared
    (archive / "org/model").mkdir(parents=True)
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        action, plan = _start(repository, artifact)
        assert all(parent["identity"] is not None for parent in plan["parents"])
        (archive / "org").rename(archive / "old-org")
        (archive / "org/model").mkdir(parents=True)
        with pytest.raises(PublicationRefused, match="PARENT_CHANGED"):
            install.apply(repository, artifact, action)
        assert not (archive / TARGET).exists()
        assert (archive / "old-org/model").is_dir()


def test_symlink_parent_is_never_followed_even_in_preview(con, prepared, tmp_path):
    archive, _ = prepared
    outside = tmp_path / "outside"
    outside.mkdir()
    (archive / "org").symlink_to(outside, target_is_directory=True)
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        with pytest.raises(PublicationRefused, match="NAMESPACE_UNPROVEN"):
            install.preview(repository, artifact, TARGET)
    assert list(outside.iterdir()) == []


def test_target_and_temporary_together_are_not_a_valid_rename_replay(con, prepared, tmp_path):
    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        action, _ = _start(repository, artifact)
        temporary = _temporary(archive, action)
        temporary.parent.mkdir(parents=True)
        temporary.write_bytes(PAYLOAD)
        temporary.chmod(0o600)
        (archive / TARGET).write_bytes(PAYLOAD)
        with pytest.raises(PublicationRefused, match="TEMPORARY_CONFLICT"):
            install.apply(repository, artifact, action)
        assert temporary.read_bytes() == (archive / TARGET).read_bytes() == PAYLOAD
        assert actions.read(repository.scope, action)["status"] == "PREPARED"


def test_named_target_replacement_after_hash_cannot_receive_receipt(con, prepared, tmp_path, monkeypatch):
    from modelark import publication_payload

    archive, _ = prepared
    with _capabilities(con, prepared, tmp_path) as (repository, artifact, _):
        action, _ = _start(repository, artifact)
        original = publication_payload._regular
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"concurrent replacement")

        def replace_after_hash(tree, path, size, digest):
            identity = original(tree, path, size, digest)
            if path == TARGET:
                os.replace(replacement, archive / TARGET)
            return identity

        monkeypatch.setattr(publication_payload, "_regular", replace_after_hash)
        with pytest.raises(PublicationRefused, match="PAYLOAD_CHANGED"):
            install.apply(repository, artifact, action)
        assert (archive / TARGET).read_bytes() == b"concurrent replacement"
        assert actions.read(repository.scope, action)["status"] == "PREPARED"
