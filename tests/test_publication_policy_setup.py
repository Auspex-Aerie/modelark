"""Actual scoped attribute setup; disposable native repository, no live archive."""
import hashlib
from pathlib import Path
import uuid

import pytest

from modelark import publication_actions as actions, publication_locks, publication_native as native
from modelark import publication_policy_setup as setup, publication_store as store, publication_tree
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import BATCH, FILE, MAP, OP
from test_publication_native import _connection, publication_connection, git_repository  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


@pytest.fixture(name="prepared")
def _prepared(request):
    return request.getfixturevalue("native_prepared")


def _start(scope, repository, paths):
    plan = setup.preview(repository, paths)
    profiles = {"d0": plan["before_profile"], "d0-policy": plan["after_profile"]}
    scope.write(lambda _: store.prepare_operation(scope, operation_id=OP, kind="fill",
                profile_digest=store.digest(profiles), batch_files={BATCH: [FILE]},
                before_state={"profiles": profiles}))
    action = str(uuid.uuid4())
    profile = repository.profile.record()
    intent = {"expected_phase": {"entity": "operation", "id": OP, "phase": "PREPARED"},
              "profile_digest": repository.profile.digest,
              "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
              "filesystem_plan": plan}
    scope.write(lambda _: actions.prepare(scope, action_id=action, kind="policy_setup", intent=intent))
    return action, plan


@pytest.mark.parametrize("path", ["org/a/config.json", "org/a/__modelark_payload_v1__/p-hash.blob",
                                  "org/a/name with spaces.json", "org/a/tab\tname.yaml",
                                  "org/a/new\nline.json", "org/a/🐺.json", "org/a/[test]*?.json"])
def test_exact_path_policy_and_native_attributes(con, prepared, path):
    archive, git = prepared
    original = b"unrelated -diff\n"
    (archive / ".git/info/attributes").write_bytes(original)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            action, plan = _start(scope, repository, [path])
            assert (archive / ".git/info/attributes").read_bytes() == original
            setup.apply(repository, action)
            assert repository.profile.record() == plan["after_profile"]
            assert (archive / ".git/info/attributes").read_bytes().startswith(original)
            assert repository.attributes(path)["filter"] == "annex"
            assert git("check-attr", "filter", "--", "unrelated").endswith(b"filter: unspecified\n")
            revision = con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]
            assert setup.apply(repository, action).proven_noop
            assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == revision
            assert actions.read(scope, action)["status"] == "VERIFIED"


def test_policy_resume_after_rename_before_receipt(con, prepared, monkeypatch):
    archive, _ = prepared
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            action, plan = _start(scope, repository, ["org/a/config.json"])
            original = actions.verify
            def fail(*args, **kwargs):
                raise RuntimeError("receipt interruption")
            monkeypatch.setattr(actions, "verify", fail)
            with pytest.raises(RuntimeError, match="receipt interruption"):
                setup.apply(repository, action)
            assert actions.read(scope, action)["status"] == "PREPARED"
            assert repository.profile.record() == plan["after_profile"]
            monkeypatch.setattr(actions, "verify", original)
        with native.QualifiedRepository(scope, archive, drive_label="d0") as reopened:
            setup.apply(reopened, action)
            assert actions.read(scope, action)["status"] == "VERIFIED"
            assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=2").fetchone()[0] == 0


def test_policy_external_edit_refuses_and_preserves_bytes(con, prepared):
    archive, _ = prepared
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            action, _ = _start(scope, repository, ["org/a/config.json"])
            (archive / ".git/info/attributes").write_bytes(b"external change\n")
            with pytest.raises(PublicationRefused, match="PROFILE_CHANGED"):
                setup.apply(repository, action)
            assert (archive / ".git/info/attributes").read_bytes() == b"external change\n"
            assert actions.read(scope, action)["status"] == "PREPARED"


def test_native_mutation_requires_exact_durable_action_and_does_not_claim_receipt(con, prepared):
    archive, git = prepared
    path = "org/a/__modelark_payload_v1__/p-test.blob"
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            action, _ = _start(scope, repository, [path])
            setup.apply(repository, action)
            target = archive / path
            target.parent.mkdir(parents=True)
            target.write_bytes(b"data")  # Explicit synthetic fixture; not coordinator payload IO.
            native_action = str(uuid.uuid4())
            argv = ("annex", "add", "--force", "--force-large", "--backend=SHA256", "--", path)
            profile = repository.profile.record()
            scope.write(lambda _: actions.prepare(scope, action_id=native_action, kind="annex_add", intent={
                "expected_phase": {"entity": "operation", "id": OP, "phase": "PREPARED"},
                "profile_digest": repository.profile.digest,
                "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
                "command": {"argv": list(argv), "stdin_digest": hashlib.sha256(b"").hexdigest()},
            }))
            with pytest.raises(PublicationRefused, match="COMMAND_INTENT_MISMATCH"):
                repository._run_action(native_action, *argv[:-1], "unrelated")
            try:
                repository._run_action(native_action, *argv)
            except PublicationRefused as exc:
                raise AssertionError(exc.evidence) from exc
            assert target.is_symlink()
            assert actions.read(scope, native_action)["status"] == "PREPARED"
            entries = publication_tree._entries(repository.read("ls-files", "--full-name", "--stage", "-v", "-z"), index=True)
            assert next(entry for entry in entries if entry.path == path).mode == "120000"
            key = "SHA256-s4--" + hashlib.sha256(b"data").hexdigest()
            assert Path(archive / repository.object_path(key)).read_bytes() == b"data"
            assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 0
