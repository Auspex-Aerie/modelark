"""Native staging runs in disposable roots/synthetic catalogs, never live maps."""
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import time
import uuid

import pytest

from modelark import publication_locks, publication_map_stage as staging, publication_native as native
from modelark import publication_map_tree as metadata, publication_store as store
from modelark import publication_map_policy as policy, publication_tree
from modelark import publication_actions as actions, publication_policy_setup as setup
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import MAP, OP, BATCH, FILE
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401
from test_publication_native import git_repository  # noqa: F401


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture(name="prepared")
def _prepared(request):
    return request.getfixturevalue("native_prepared")


@pytest.fixture
def library(prepared, tmp_path, monkeypatch):
    source, git = prepared
    map_root = tmp_path / "map"
    map_root.mkdir()
    def map_git(*args, **kwargs):
        return git("-C", str(map_root), *args, **kwargs)
    map_git("init", "--quiet", "--initial-branch=main")
    map_git("config", "annex.uuid", MAP)
    map_git("annex", "init", "--version=8", "--quiet", "stage-test-map")
    map_git("commit", "--allow-empty", "-qm", "independent map baseline")
    # Existing source UUID description is registered BEFORE publication baseline.
    source_annex = git("rev-parse", "refs/heads/git-annex").decode().strip()
    map_git("fetch", "--quiet", str(source), f"{source_annex}:refs/remotes/registered/git-annex")
    map_git("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
    original = staging.tempfile.mkdtemp
    monkeypatch.setattr(staging.tempfile, "mkdtemp", lambda **kwargs: original(dir=tmp_path, **kwargs))
    return source, git, map_root, map_git


@contextmanager
def repositories(con, library):
    source, _, map_root, _ = library
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, map_root) as map_repository:
            with native.QualifiedRepository(scope, source, drive_label="d0") as source_repository:
                yield scope, map_repository, source_repository


def test_create_retains_qualified_metadata_only_stage_and_map_is_unchanged(con, library, monkeypatch):
    _, _, map_root, map_git = library
    before_head = map_git("rev-parse", "HEAD")
    before_annex = map_git("rev-parse", "refs/heads/git-annex")
    before_files = {str(path.relative_to(map_root)): path.read_bytes() for path in map_root.iterdir() if path.is_file()}
    observed = []
    original = native._bounded_process
    def process(argv, **kwargs):
        observed.append((argv, kwargs))
        return original(argv, **kwargs)
    monkeypatch.setattr(native, "_bounded_process", process)
    with repositories(con, library) as (scope, map_repository, _):
        with staging.create(map_repository) as stage:
            assert type(stage) is native.QualifiedRepository and stage.scope is scope
            assert stage.profile.annex_uuid == MAP
            assert stage.read("rev-parse", "--verify", staging.ANNEX_REF) == before_annex
            actual = metadata.capture(stage, ref=staging.ANNEX_REF, binding_digest="a" * 64)
            assert actual.commit_oid.encode() + b"\n" == before_annex
            path = stage.tree.path
            assert path != map_root
            assert sorted(value.name for value in path.iterdir()) == [".git"]
            snapshot = publication_tree.capture(stage.tree, read_git=stage.read, require_scope=scope.require_io)
            assert snapshot.head_oid.encode() + b"\n" == before_head
            stage.ensure()
    assert path.exists(), "scratch evidence is deliberately retained"
    assert map_git("rev-parse", "HEAD") == before_head
    assert map_git("rev-parse", "refs/heads/git-annex") == before_annex
    assert {str(path.relative_to(map_root)): path.read_bytes() for path in map_root.iterdir() if path.is_file()} == before_files
    clone_calls = [(argv, kwargs) for argv, kwargs in observed if "clone" in argv]
    assert len(clone_calls) == 1
    argv, kwargs = clone_calls[0]
    assert "--no-local" in argv and "--no-checkout" in argv and "--template=" in argv
    assert any(arg.startswith("/proc/self/fd/") for arg in argv)
    assert kwargs["pass_fds"] and kwargs["environment"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert all("annex" not in argv for argv, _ in observed)


def test_creation_not_allowed_after_operation_or_from_drive_clone(con, library):
    with repositories(con, library) as (scope, map_repository, source_repository):
        with pytest.raises(PublicationRefused, match="CREATE_PHASE_INVALID"):
            with staging.create(source_repository):
                pytest.fail("drive clone is not map staging baseline")
        scope.write(lambda _: store.prepare_operation(
            scope, operation_id=OP, kind="fill", profile_digest=map_repository.profile.digest,
            batch_files={BATCH: [FILE]}, before_state={"fixture": "not a live publication"}))
        with pytest.raises(PublicationRefused, match="CREATE_PHASE_INVALID"):
            with staging.create(map_repository):
                pytest.fail("new staging profile cannot appear after frozen operation")


def test_failed_stage_qualification_retains_directory_and_leaves_map_unchanged(con, library, monkeypatch):
    _, _, _, map_git = library
    before = map_git("rev-parse", "refs/heads/git-annex")
    original = native._bounded_process
    def process(argv, **kwargs):
        if "clone" in argv:
            raise PublicationRefused("FIXTURE_CLONE_FAILED")
        return original(argv, **kwargs)
    monkeypatch.setattr(native, "_bounded_process", process)
    with repositories(con, library) as (_, map_repository, _):
        with pytest.raises(PublicationRefused, match="FIXTURE_CLONE_FAILED") as failed:
            with staging.create(map_repository):
                pytest.fail("failure is not a usable staging repository")
    assert Path(failed.value.evidence["retained_directory"]).is_dir()
    assert map_git("rev-parse", "refs/heads/git-annex") == before


def start(scope, map_repository, source_repository, stage, **extra_profiles):
    profiles = {"@map": map_repository.profile.record(), "d0": source_repository.profile.record(),
                "@stage": stage.profile.record(), **extra_profiles}
    roots = {"@map": str(map_repository.tree.path), "d0": str(source_repository.tree.path),
             "@stage": str(stage.tree.path)}
    scope.write(lambda _: store.prepare_operation(
        scope, operation_id=OP, kind="fill", profile_digest=store.digest(profiles),
        batch_files={BATCH: [FILE]}, before_state={"profiles": profiles, "roots": roots}))


def quarantine_ref(con):
    (raw,) = con.execute("SELECT intent_json FROM publication_actions WHERE kind='map_stage' "
                         "ORDER BY prepared_revision LIMIT 1").fetchone()
    return json.loads(raw)["intent"]["quarantine_ref"]


def add_payload(library, *, annotations=False):
    source, git, _, _ = library
    (source / "published").write_bytes(b"real disposable native source bytes\n")
    git("annex", "add", "--force-large", "--", "published")
    key = git("annex", "lookupkey", "--", "published").decode().strip()
    if annotations:
        git("annex", "metadata", "--key=" + key, "-s", "model=org/tiny", "-s", "format=raw")
    git("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
    return key, git("rev-parse", "refs/heads/git-annex").decode().strip()


def candidate_args(scope, key, oid):
    identities = {key: frozenset({scope.identities["d0"].annex_uuid})}
    return dict(source_annex_oid=oid, allowed_key_uuids=identities, required_key_uuids=identities,
                annotation_writes={}, expected_annotation_seals={}, binding_digest="b" * 64,
                clock_ceiling=Decimal(str(time.time())))


@pytest.mark.parametrize("annotations", [False, True])
def test_real_native_candidate_journaled_and_validated_without_live_map_publication(con, library, annotations):
    _, git, map_root, map_git = library
    old_map = map_git("rev-parse", "refs/heads/git-annex")
    old_head = map_git("rev-parse", "HEAD")
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library, annotations=annotations)
            # Confirm actual native source bytes before this synthetic allowlist.
            native_object = source.object_path(key)
            assert (library[0] / native_object).read_bytes() == b"real disposable native source bytes\n"
            start(scope, map_repository, source, stage)
            args = candidate_args(scope, key, oid)
            if annotations:
                path = metadata.metadata_path(key) + ".met"
                after = git("show", f"{oid}:{path}")
                proof = policy.AnnotationWrite(path, b"", after, (("format", "raw"), ("model", "org/tiny")), "c" * 64)
                args.update(annotation_writes={path: proof}, expected_annotation_seals={path: proof.seal()})
            candidate = staging.candidate(stage, source, **args)
            assert candidate.before.commit_oid.encode() + b"\n" == old_map
            assert candidate.after.commit_oid == stage.ref_oid(staging.ANNEX_REF)
            assert dict(candidate.native_locations)[key] == (scope.identities["d0"].annex_uuid,)
            assert len(candidate.action_receipts) == 3
            assert json.loads(json.dumps(candidate.record())) == candidate.record()
            assert len(candidate.digest) == 64
            assert con.execute("SELECT status FROM publication_actions ORDER BY prepared_revision").fetchall() == [
                ("VERIFIED",), ("VERIFIED",), ("VERIFIED",)]
            assert con.execute("SELECT state FROM publication_operations").fetchone() == ("PREPARED",)
            assert map_git("rev-parse", "refs/heads/git-annex") == old_map
            assert map_git("rev-parse", "HEAD") == old_head
            assert not list((map_root / ".git/annex/objects").glob("**/SHA256-*"))


def test_live_map_repository_cannot_be_used_as_mutable_staging_capability(con, library):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library)
            start(scope, map_repository, source, stage)
            with pytest.raises(PublicationRefused, match="CAPABILITY_MISSING"):
                staging.candidate(map_repository, source, **candidate_args(scope, key, oid))


def test_unadmitted_source_stays_quarantined_and_never_runs_native_merge(con, library, monkeypatch):
    _, git, _, map_git = library
    old = map_git("rev-parse", "refs/heads/git-annex")
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, _ = add_payload(library)
            git("annex", "setpresentkey", key, MAP, "1")
            git("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
            oid = git("rev-parse", "refs/heads/git-annex").decode().strip()
            start(scope, map_repository, source, stage)
            original, commands = native._bounded_process, []
            def process(argv, **kwargs):
                commands.append(argv)
                return original(argv, **kwargs)
            monkeypatch.setattr(native, "_bounded_process", process)
            with pytest.raises(PublicationRefused, match="MAP_CONTENT_CLAIM"):
                staging.candidate(stage, source, **candidate_args(scope, key, oid))
            assert stage.ref_oid(quarantine_ref(con)) == oid
            assert stage.ref_oid(staging.RECOGNIZED_REF) is None
            assert stage.read("rev-parse", "--verify", staging.ANNEX_REF) == old
            assert all("annex" not in command for command in commands)
            assert con.execute("SELECT kind,status FROM publication_actions").fetchall() == [("map_stage", "VERIFIED")]
    assert map_git("rev-parse", "refs/heads/git-annex") == old


def test_wrong_sealed_source_oid_refuses_before_action_or_fetch(con, library):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, _ = add_payload(library)
            start(scope, map_repository, source, stage)
            with pytest.raises(PublicationRefused, match="SOURCE_CHANGED"):
                staging.candidate(stage, source, **candidate_args(scope, key, "0" * 40))
            assert con.execute("SELECT count(*) FROM publication_actions").fetchone() == (0,)


def test_nochange_boundary_still_has_verified_native_noop_receipt(con, library):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library)
            start(scope, map_repository, source, stage)
            first = staging.candidate(stage, source, **candidate_args(scope, key, oid))
            args = candidate_args(scope, key, oid) | {"binding_digest": "d" * 64}
            second = staging.candidate(stage, source, **args)
            assert first.after.metadata() == second.before.metadata() == second.after.metadata()
            assert json.loads(second.validation_json)["location_additions"] == {}
            assert len(second.action_receipts) == 3
            assert con.execute("SELECT count(*) FROM publication_actions WHERE status='VERIFIED'").fetchone() == (6,)


def test_interrupted_fetch_acknowledged_only_after_exact_observation_without_repeat(con, library, monkeypatch):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library)
            start(scope, map_repository, source, stage)
            original = stage._fetch_action
            calls = 0
            def interrupted(*args):
                nonlocal calls
                calls += 1
                original(*args)
                raise PublicationRefused("FIXTURE_INTERRUPTED_AFTER_FETCH")
            monkeypatch.setattr(stage, "_fetch_action", interrupted)
            args = candidate_args(scope, key, oid)
            with pytest.raises(PublicationRefused, match="FIXTURE_INTERRUPTED"):
                staging.candidate(stage, source, **args)
            assert stage.ref_oid(quarantine_ref(con)) == oid
            assert stage.ref_oid(staging.RECOGNIZED_REF) is None
            completed = staging.candidate(stage, source, **args)
            assert completed.after.commit_oid == stage.ref_oid(staging.ANNEX_REF)
            assert calls == 1
            assert con.execute("SELECT status FROM publication_actions").fetchall() == [("VERIFIED",)] * 3


def test_native_reader_disagreement_never_verifies_merge_or_changes_live_map(con, library, monkeypatch):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            old = map_repository.read("rev-parse", "--verify", staging.ANNEX_REF)
            key, oid = add_payload(library)
            start(scope, map_repository, source, stage)
            monkeypatch.setattr(stage, "effective_locations", lambda *_: frozenset({MAP}))
            with pytest.raises(PublicationRefused, match="NATIVE_LOCATIONS_MISMATCH"):
                staging.candidate(stage, source, **candidate_args(scope, key, oid))
            assert con.execute("SELECT status FROM publication_actions ORDER BY prepared_revision").fetchall() == [
                ("VERIFIED",), ("VERIFIED",), ("PREPARED",)]
            assert map_repository.read("rev-parse", "--verify", staging.ANNEX_REF) == old


def test_stage_policy_capability_adopts_only_exact_verified_transition(con, library):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library)
            plan = setup.preview(stage, ["org/tiny/config.json"])
            start(scope, map_repository, source, stage, **{"@stage:policy": plan["after_profile"]})
            action = str(uuid.uuid4())
            profile = stage.profile.record()
            scope.write(lambda _: actions.prepare(scope, action_id=action, kind="policy_setup", intent={
                "expected_phase": {"entity": "operation", "id": OP, "phase": "PREPARED"},
                "profile_digest": stage.profile.digest,
                "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
                "filesystem_plan": plan}))
            with pytest.raises(PublicationRefused, match="POLICY_UNVERIFIED"):
                staging.adopt_policy(stage, action)
            setup.apply(stage, action)
            with pytest.raises(PublicationRefused, match="CAPABILITY_MISSING"):
                staging.candidate(stage, source, **candidate_args(scope, key, oid))
            with pytest.raises(PublicationRefused, match="CAPABILITY_MISSING"):
                staging.adopt_policy(map_repository, action)
            staging.adopt_policy(stage, action)
            checked = staging.candidate(stage, source, **candidate_args(scope, key, oid))
            assert checked.stage_profile_digest == store.digest(plan["after_profile"])
            with pytest.raises(PublicationRefused, match="POLICY_UNVERIFIED"):
                staging.adopt_policy(stage, checked.action_receipts[-1][0])


@pytest.mark.parametrize("boundary", ["before-fetch", "after-recognize", "before-merge", "after-merge"])
def test_exact_interrupted_command_continues_after_stage_reopen(con, library, monkeypatch, boundary):
    """Actual native effects survive adapter closure; no synthetic merge result."""
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library)
            start(scope, map_repository, source, stage)
            args = candidate_args(scope, key, oid)
            root = stage.tree.path
            fetch, run = native.QualifiedRepository._fetch_action, native.QualifiedRepository._run_action
            calls = []
            interrupted = False

            def call_fetch(repository, *values):
                nonlocal interrupted
                if boundary == "before-fetch" and not interrupted:
                    interrupted = True
                    raise PublicationRefused("FIXTURE_INTERRUPTION")
                calls.append("fetch")
                return fetch(repository, *values)

            def call_run(repository, identifier, *command):
                nonlocal interrupted
                label = "merge" if command[0] == "annex" else "recognize"
                if boundary == "before-merge" and label == "merge" and not interrupted:
                    interrupted = True
                    raise PublicationRefused("FIXTURE_INTERRUPTION")
                calls.append(label)
                result = run(repository, identifier, *command)
                if boundary == "after-" + label and not interrupted:
                    interrupted = True
                    raise PublicationRefused("FIXTURE_INTERRUPTION")
                return result

            monkeypatch.setattr(native.QualifiedRepository, "_fetch_action", call_fetch)
            monkeypatch.setattr(native.QualifiedRepository, "_run_action", call_run)
            with pytest.raises(PublicationRefused, match="FIXTURE_INTERRUPTION"):
                staging.candidate(stage, source, **args)
        with staging.resume(scope) as reopened:
            assert reopened.tree.path == root
            staging.require_stage(reopened)
            completed = staging.candidate(reopened, source, **args)
            assert completed.after.commit_oid == reopened.ref_oid(staging.ANNEX_REF)
            assert calls == ["fetch", "recognize", "merge"]
            assert con.execute("SELECT status FROM publication_actions").fetchall() == [("VERIFIED",)] * 3
            revision = store._revision(con)
            replay = staging.candidate(reopened, source, **args)
            assert replay.record() == completed.record()
            assert calls == ["fetch", "recognize", "merge"]
            assert store._revision(con) == revision
        assert root.is_dir()


@pytest.mark.parametrize("change", ["required", "clock", "source"])
def test_prepared_boundary_cannot_substitute_permission_or_source_facts(con, library, monkeypatch, change):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library)
            start(scope, map_repository, source, stage)
            args = candidate_args(scope, key, oid)
            def stop(*_):
                raise PublicationRefused("FIXTURE_BEFORE_FETCH")
            monkeypatch.setattr(stage, "_fetch_action", stop)
            with pytest.raises(PublicationRefused, match="FIXTURE_BEFORE_FETCH"):
                staging.candidate(stage, source, **args)
            changed = dict(args)
            if change == "required":
                changed["required_key_uuids"] = {}
            elif change == "clock":
                changed["clock_ceiling"] += Decimal(1)
            else:
                # Same valid metadata at a different native Git commit is not
                # the source OID that this boundary originally admitted.
                git = library[1]
                tree = git("rev-parse", oid + "^{tree}").decode().strip()
                new_oid = git("commit-tree", tree, "-p", oid, "-m", "foreign new source").decode().strip()
                git("update-ref", staging.ANNEX_REF, new_oid, oid)
                changed["source_annex_oid"] = new_oid
            with pytest.raises(PublicationRefused, match="RECOVERY_BINDING_MISMATCH"):
                staging.candidate(stage, source, **changed)
            assert con.execute("SELECT count(*) FROM publication_actions").fetchone() == (1,)


@pytest.mark.parametrize("change", ["quarantine", "recognized", "annex", "missing-verified-fetch"])
def test_recovery_refuses_foreign_or_missing_verified_effects(con, library, monkeypatch, change):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            key, oid = add_payload(library)
            start(scope, map_repository, source, stage)
            args = candidate_args(scope, key, oid)
            original = stage._run_action
            def stop_merge(identifier, *command):
                if command[0] == "annex":
                    raise PublicationRefused("FIXTURE_BEFORE_MERGE")
                return original(identifier, *command)
            monkeypatch.setattr(stage, "_run_action", stop_merge)
            with pytest.raises(PublicationRefused, match="FIXTURE_BEFORE_MERGE"):
                staging.candidate(stage, source, **args)
            baseline = stage.ref_oid(staging.ANNEX_REF)
            git = library[1]
            def raw(*command):
                return git("-C", str(stage.tree.path), *command)
            if change == "missing-verified-fetch":
                raw("update-ref", "-d", quarantine_ref(con))
                error = "VERIFIED_EFFECT_MISSING"
            elif change == "quarantine":
                raw("update-ref", quarantine_ref(con), baseline)
                error = "QUARANTINE_CHANGED"
            elif change == "recognized":
                raw("update-ref", staging.RECOGNIZED_REF, baseline)
                error = "RECOGNIZED_REF_CHANGED"
            else:
                tree = raw("rev-parse", baseline + "^{tree}").decode().strip()
                foreign = raw("commit-tree", tree, "-p", baseline, "-m", "foreign baseline lookalike").decode().strip()
                raw("update-ref", staging.ANNEX_REF, foreign)
                error = "BASELINE_CHANGED"
            with pytest.raises(PublicationRefused, match=error):
                staging.candidate(stage, source, **args)
            assert con.execute("SELECT status FROM publication_actions ORDER BY prepared_revision").fetchall() == [
                ("VERIFIED",), ("VERIFIED",), ("PREPARED",)]


def test_resume_requires_verified_exact_policy_transition(con, library, monkeypatch):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            plan = setup.preview(stage, ["org/tiny/config.json"])
            start(scope, map_repository, source, stage, **{"@stage:policy": plan["after_profile"]})
            root = stage.tree.path
            action = str(uuid.uuid4())
            profile = stage.profile.record()
            scope.write(lambda _: actions.prepare(scope, action_id=action, kind="policy_setup", intent={
                "expected_phase": {"entity": "operation", "id": OP, "phase": "PREPARED"},
                "profile_digest": stage.profile.digest,
                "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
                "filesystem_plan": plan}))
            original = actions.verify
            def stop(*_, **__):
                raise PublicationRefused("FIXTURE_BEFORE_POLICY_RECEIPT")
            monkeypatch.setattr(actions, "verify", stop)
            with pytest.raises(PublicationRefused, match="FIXTURE_BEFORE_POLICY_RECEIPT"):
                setup.apply(stage, action)
        with pytest.raises(PublicationRefused, match="POLICY_UNVERIFIED"):
            with staging.resume(scope):
                pytest.fail("unverified physical policy must not issue a staging capability")
        monkeypatch.setattr(actions, "verify", original)
        with native.QualifiedRepository(scope, root) as actual:
            setup.apply(actual, action)
        with staging.resume(scope) as resumed:
            assert resumed.profile.record() == plan["after_profile"]
            staging.require_stage(resumed)


def test_resume_refuses_replaced_root_and_nonselected_profile(con, library):
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            start(scope, map_repository, source, stage)
            root = stage.tree.path
        # Changing a real profile is not an invitation to freeze fresh facts.
        git = library[1]
        git("-C", str(root), "config", "--local", "user.name", "unexpected fixture identity")
        with pytest.raises(PublicationRefused, match="RESUME_PROFILE_MISMATCH"):
            with staging.resume(scope):
                pytest.fail("foreign profile cannot be adopted")
        root.rename(root.with_name("retained-original"))
        root.mkdir()
        with pytest.raises((PublicationRefused, OSError)):
            with staging.resume(scope):
                pytest.fail("replacement path is not the frozen root")


def test_resume_requires_actual_owned_scope_and_no_active_transaction(con, library):
    with pytest.raises(PublicationRefused, match="RESUME_UNQUALIFIED"):
        with staging.resume(object()):
            pytest.fail("caller object is not a scope")
    with repositories(con, library) as (scope, map_repository, source):
        with staging.create(map_repository) as stage:
            start(scope, map_repository, source, stage)
        con.execute("BEGIN")
        try:
            with pytest.raises(PublicationRefused):
                with staging.resume(scope):
                    pytest.fail("read-only opening still cannot happen during catalog transaction")
        finally:
            con.rollback()
