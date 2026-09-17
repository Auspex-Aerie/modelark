"""Isolated native metadata staging; never publication to the real library map.

Scratch repositories are retained on success and failure. Creation is allowed
only before an operation is prepared, so its actual root/profile can be frozen
with that operation before any live payload mutation. Native candidate commands
are separately journalled; no command exit code substitutes for its postchecks.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid5
from weakref import WeakKeyDictionary

from modelark import publication_actions as actions, publication_map_policy as policy
from modelark import publication_locks, publication_map_tree as metadata
from modelark import publication_native as native, publication_store as store
from modelark.publication_policy import PublicationRefused, _require
from modelark.slice.linux import BoundTree

ANNEX_REF = "refs/heads/git-annex"
QUARANTINE_REF = "refs/modelark/quarantine-annex"
RECOGNIZED_REF = "refs/remotes/sealed/git-annex"
_STAGES = WeakKeyDictionary()


def _actual(repository):
    _require(type(repository) is native.QualifiedRepository, "PUBLICATION_STAGE_REPOSITORY_UNQUALIFIED")
    repository.ensure()


def _head(repository):
    raw = repository.read("symbolic-ref", "HEAD")
    _require(raw.endswith(b"\n") and raw.count(b"\n") == 1, "PUBLICATION_STAGE_HEAD_UNQUALIFIED")
    try:
        ref = raw[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PublicationRefused("PUBLICATION_STAGE_HEAD_UNQUALIFIED") from exc
    _require(ref.startswith("refs/heads/") and ref != ANNEX_REF, "PUBLICATION_STAGE_HEAD_UNQUALIFIED")
    oid = repository.read("rev-parse", "--verify", ref)
    _require(len(oid) == 41 and oid.endswith(b"\n"), "PUBLICATION_STAGE_HEAD_UNQUALIFIED")
    return ref, metadata._oid(oid[:-1].decode("ascii"))


def _no_payload(repository):
    """An empty/absent annex object directory only, using retained descriptors."""
    repository.ensure()
    try:
        descriptor = repository.tree.open(".git/annex/objects", os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        repository.ensure()
        return
    except OSError as exc:
        raise PublicationRefused("PUBLICATION_STAGE_PAYLOAD_UNPROVEN") from exc
    try:
        _require(not os.listdir(descriptor), "PUBLICATION_STAGE_HAS_PAYLOAD")
    finally:
        os.close(descriptor)
    repository.ensure()


@contextmanager
def create(map_repository: native.QualifiedRepository):
    """Yield the actual qualified scratch repository, retaining it after close.

    Only a descriptor-bound, already qualified map may be cloned. No configured
    network remote, hardlink/local optimization, template hook, annex init or new
    UUID is used. The map's live refs/index/worktree are never mutated here.
    """
    _actual(map_repository)
    scope = map_repository.scope
    _require(scope.operation_id is None and map_repository.drive_label is None,
             "PUBLICATION_STAGE_CREATE_PHASE_INVALID")
    baseline = metadata.capture(map_repository, ref=ANNEX_REF,
                                binding_digest=map_repository.profile.digest)
    head = _head(map_repository)
    retained = Path(tempfile.mkdtemp(prefix="modelark-map-stage-"))
    try:
        with BoundTree(retained) as temporary:
            def git(*args):
                _actual(map_repository)
                _require(scope.operation_id is None, "PUBLICATION_STAGE_CREATE_PHASE_INVALID")
                temporary.check()
                argv = ["/usr/bin/git", "--no-pager", "--no-replace-objects", "--literal-pathspecs"]
                for setting in native._OVERRIDES:
                    argv.extend(("-c", setting))
                argv.extend(args)
                result = native._bounded_process(
                    argv, cwd=f"/proc/self/fd/{temporary.fd}",
                    pass_fds=(*scope.child_fence_fds, temporary.fd, map_repository.tree.fd),
                    environment=native._ENV, limit=1024 * 1024)
                temporary.check()
                _actual(map_repository)
                return result

            git("-c", "protocol.file.allow=always", "clone", "--no-local", "--no-checkout", "--quiet",
                "--template=", "--", f"/proc/self/fd/{map_repository.tree.fd}", "repository")
            # An explicitly empty template intentionally omits .git/info. The
            # qualified profile observes absent policy files through these owned
            # directories; create only the empty directories, never policy data.
            for path in ("repository/.git/info", "repository/.git/objects/info"):
                with temporary.parent(path) as (parent, name):
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                descriptor = temporary.open(path, os.O_RDONLY | os.O_DIRECTORY)
                os.close(descriptor)
            git("-C", "repository", "remote", "remove", "origin")
            for name, value in {"annex.uuid": scope.library[1], "annex.version": "8", **native.FILTERS,
                                "user.name": "ModelArk publication", "user.email": "publication@modelark.invalid"}.items():
                git("-C", "repository", "config", "--local", name, value)
            git("-C", "repository", "update-ref", ANNEX_REF, baseline.commit_oid)
            # Index-only baseline, with no worktree materialization or filters.
            # The parent binds the exact committed file tree before preparation.
            git("-C", "repository", "read-tree", head[1])
            with native.QualifiedRepository(scope, retained / "repository", drive_label=None) as stage:
                observed = metadata.capture(stage, ref=ANNEX_REF, binding_digest=stage.profile.digest,
                                            expected_oid=baseline.commit_oid)
                _require(observed.metadata() == baseline.metadata() and _head(stage) == head,
                         "PUBLICATION_STAGE_BASELINE_MISMATCH")
                _no_payload(stage)
                _require(_head(map_repository) == head, "PUBLICATION_STAGE_MAP_CHANGED")
                metadata.capture(map_repository, ref=ANNEX_REF, binding_digest=map_repository.profile.digest,
                                 expected_oid=baseline.commit_oid)
                _require(stage.ref_oid(RECOGNIZED_REF) is None, "PUBLICATION_STAGE_BASELINE_MISMATCH")
                _STAGES[stage] = (scope, stage.profile.digest)
                try:
                    yield stage
                finally:
                    _STAGES.pop(stage, None)
    except PublicationRefused as exc:
        # Retention is deliberate recovery/debug evidence, not a cleanup leak to
        # repair by recursively deleting an operator archive or arbitrary path.
        exc.evidence.setdefault("retained_directory", str(retained))
        raise


def adopt_policy(stage: native.QualifiedRepository, action_id: str):
    """Advance only this active scratch capability through a verified setup action."""
    from modelark import publication_policy_setup as setup
    _actual(stage)
    issued = _STAGES.get(stage)
    _require(issued is not None and issued[0] is stage.scope, "PUBLICATION_STAGE_CAPABILITY_MISSING")
    record = actions.read(stage.scope, action_id)
    _require(record["status"] == "VERIFIED" and record["kind"] == "policy_setup",
             "PUBLICATION_STAGE_POLICY_UNVERIFIED")
    plan, _, after = setup._validated(stage, record)
    receipt = record["receipt"]["receipt"]
    expected = {"version": 1, "profile": plan["after_profile"], "paths": plan["paths"],
                "attributes_sha256": hashlib.sha256(after).hexdigest()}
    _require(store.digest(plan["before_profile"]) == issued[1]
             and plan["after_profile"] == stage.profile.record() and receipt == expected
             and stage._file(plan["path"], absent=True) == after,
             "PUBLICATION_STAGE_POLICY_BINDING_MISMATCH")
    stage.ensure()
    _STAGES[stage] = (stage.scope, stage.profile.digest)


def require_stage(stage: native.QualifiedRepository):
    """Require the active, profile-current scratch issuer—not a live map reader."""
    _actual(stage)
    _require(_STAGES.get(stage) == (stage.scope, stage.profile.digest),
             "PUBLICATION_STAGE_CAPABILITY_MISSING")


def _saved_operation(scope):
    _require(type(scope) is publication_locks._FenceScope and scope.operation_id is not None,
             "PUBLICATION_STAGE_RESUME_UNQUALIFIED")
    scope.require_io()
    scope.connection.execute("BEGIN")
    try:
        scope.require()
        binding, _, _ = store._load_bound_operation(scope, scope.operation_id)
        return json.loads(store.canonical(binding))
    finally:
        scope.connection.rollback()


@contextmanager
def resume(scope):
    """Reopen only the exact retained scratch root/profile frozen by this owner.

    Never clones, initializes, creates a directory or substitutes participants.
    A physically applied post-policy profile needs the exact VERIFIED policy
    receipt; the parent may finish a prepared setup via its ordinary policy
    adapter before retrying resume. No arbitrary current profile is adopted.
    """
    binding = _saved_operation(scope)
    state = binding.get("before_state", {})
    roots, profiles = state.get("roots"), state.get("profiles")
    _require(isinstance(roots, dict) and isinstance(profiles, dict),
             "PUBLICATION_STAGE_RESUME_BINDING_MISSING")
    root, prior = roots.get("@stage"), profiles.get("@stage")
    post = profiles.get("@stage:policy")
    _require(isinstance(root, str) and Path(root).is_absolute() and os.path.abspath(root) == root
             and isinstance(prior, dict) and root not in [path for label, path in roots.items() if label != "@stage"],
             "PUBLICATION_STAGE_RESUME_ROOT_UNQUALIFIED")
    with native.QualifiedRepository(scope, root) as stage:
        actual = stage.profile.record()
        _require(actual == prior or isinstance(post, dict) and actual == post,
                 "PUBLICATION_STAGE_RESUME_PROFILE_MISMATCH")
        _no_payload(stage)
        _STAGES[stage] = (scope, store.digest(prior))
        try:
            if actual != prior:
                matching = []
                identifiers = scope.connection.execute(
                    "SELECT action_id FROM publication_actions WHERE operation_id=? AND kind='policy_setup'",
                    [scope.operation_id]).fetchall()
                for (identifier,) in identifiers:
                    record = actions.read(scope, identifier)
                    plan = record["intent"].get("filesystem_plan", {})
                    if (record["status"] == "VERIFIED" and plan.get("before_profile") == prior
                            and plan.get("after_profile") == post):
                        matching.append(identifier)
                _require(len(matching) == 1, "PUBLICATION_STAGE_POLICY_UNVERIFIED")
                adopt_policy(stage, matching[0])
            require_stage(stage)
            _require(_saved_operation(scope) == binding, "PUBLICATION_STAGE_RESUME_BINDING_CHANGED")
            yield stage
        finally:
            _STAGES.pop(stage, None)


@dataclass(frozen=True)
class StagedCandidate:
    """Verified scratch result only; not a receipt for the live map publication."""

    binding_digest: str
    operation_id: str
    batch_id: str | None
    stage_profile_digest: str
    source_profile_digest: str
    before: metadata.MetadataSnapshot
    source: metadata.MetadataSnapshot
    after: metadata.MetadataSnapshot
    file_head: tuple[str, str]
    admission_json: str
    validation_json: str
    native_locations: tuple[tuple[str, tuple[str, ...]], ...]
    action_receipts: tuple[tuple[str, str], ...]

    def record(self):
        return {"version": 1, "kind": "native-staged-candidate", "binding_digest": self.binding_digest,
                "operation_id": self.operation_id, "batch_id": self.batch_id,
                "stage_profile_digest": self.stage_profile_digest,
                "source_profile_digest": self.source_profile_digest,
                "before": self.before.record(), "source": self.source.record(), "after": self.after.record(),
                "file_head": list(self.file_head), "admission": json.loads(self.admission_json),
                "validation": json.loads(self.validation_json),
                "native_locations": {key: list(identities) for key, identities in self.native_locations},
                "action_receipts": [{"action_id": identifier, "receipt_digest": seal}
                                    for identifier, seal in self.action_receipts]}

    @property
    def digest(self):
        return store.digest(self.record())


def candidate(stage: native.QualifiedRepository, source: native.QualifiedRepository, *,
              source_annex_oid: str, allowed_key_uuids, annotation_writes,
              expected_annotation_seals, binding_digest: str, clock_ceiling,
              required_key_uuids=None, batch_id: str | None = None) -> StagedCandidate:
    """Journal, quarantine, admit, native-merge and independently validate scratch.

    Caller freezes this staging profile with the operation before any live
    publication work. Only repositories issued by an active ``create`` or
    ``resume`` context may be mutated here; a live map is not a staging capability.
    Continuation uses the original journalled baseline and permissions, never
    current state as a replacement baseline. Exact completed effects are checked
    and acknowledged; only a proven old state may execute its prepared command.
    """
    require_stage(stage)
    _actual(source)
    scope = stage.scope
    _require(scope.operation_id is not None and source.scope is scope
             and source.drive_label in scope.identities, "PUBLICATION_STAGE_SOURCE_UNQUALIFIED")
    frozen_profiles = _saved_operation(scope).get("before_state", {}).get("profiles", {})
    _require(isinstance(frozen_profiles, dict) and source.profile.record() in frozen_profiles.values(),
             "PUBLICATION_STAGE_SOURCE_PROFILE_UNSELECTED")
    store._require_digest(binding_digest)
    metadata._oid(source_annex_oid)
    if batch_id is not None:
        store.canonical_uuid(batch_id)
    allowed_key_uuids = policy._contract(allowed_key_uuids, scope.library[1], clock_ceiling)
    selected = frozenset(identity.annex_uuid for identity in scope.identities.values())
    _require(all(identities <= selected for identities in allowed_key_uuids.values()),
             "PUBLICATION_STAGE_LOCATION_UNSELECTED")
    _require(isinstance(annotation_writes, Mapping) and isinstance(expected_annotation_seals, Mapping),
             "PUBLICATION_STAGE_ANNOTATION_BINDING_INVALID")
    annotation_writes, expected_annotation_seals = dict(annotation_writes), dict(expected_annotation_seals)
    required_key_uuids = policy._contract({} if required_key_uuids is None else required_key_uuids,
                                         scope.library[1], clock_ceiling)
    source_expected = source_annex_oid.encode() + b"\n"
    _require(source.read("rev-parse", "--verify", ANNEX_REF) == source_expected,
             "PUBLICATION_STAGE_SOURCE_CHANGED")
    _no_payload(stage)
    profile = stage.profile.record()
    receipts = []
    phase = {"entity": "operation" if batch_id is None else "batch",
             "id": scope.operation_id if batch_id is None else batch_id, "phase": "PREPARED"}
    permissions = {
        "allowed_key_uuids": {key: sorted(value) for key, value in sorted(allowed_key_uuids.items())},
        "required_key_uuids": {key: sorted(value) for key, value in sorted(required_key_uuids.items())},
        "annotation_seals": dict(sorted(expected_annotation_seals.items())),
        "clock_ceiling": policy._clock(clock_ceiling),
    }
    fixed = {
        "expected_phase": phase, "profile_digest": stage.profile.digest,
        "root": {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")},
        "binding_digest": binding_digest, "source_profile_digest": source.profile.digest,
        "source_annex_oid": source_annex_oid, "source_annex_ref": ANNEX_REF,
        "permissions": permissions,
    }
    namespace = UUID(scope.operation_id)

    def action_id(step):
        # Stable boundary+participant IDs ensure changed source OIDs/profiles
        # conflict with the old intent rather than silently creating new work.
        return str(uuid5(namespace, ":".join(("map-stage", binding_digest, batch_id or "operation",
                                             source.drive_label, step))))

    quarantine_ref = QUARANTINE_REF + "/" + action_id("quarantine")
    fixed["quarantine_ref"] = quarantine_ref

    def existing(step):
        identifier = action_id(step)
        if not scope.connection.execute(
                "SELECT 1 FROM publication_actions WHERE operation_id=? AND action_id=?",
                [scope.operation_id, identifier]).fetchone():
            return None
        return actions.read(scope, identifier)

    original = {step: existing(step) for step in ("quarantine", "admit", "native-merge")}
    if original["quarantine"] is None:
        _require(original["admit"] is None and original["native-merge"] is None
                 and stage.ref_oid(quarantine_ref) is None, "PUBLICATION_STAGE_RECOVERY_CONFLICT")
        before = metadata.capture(stage, ref=ANNEX_REF, binding_digest=binding_digest)
        head = _head(stage)
        old_recognized = stage.ref_oid(RECOGNIZED_REF) or "0" * 40
        base_intent = {**fixed, "baseline_annex_oid": before.commit_oid,
                       "baseline_snapshot": before.record(), "file_head": list(head),
                       "old_recognized_oid": old_recognized}
    else:
        saved = original["quarantine"]["intent"]
        _require(all(saved.get(name) == value for name, value in fixed.items()),
                 "PUBLICATION_STAGE_RECOVERY_BINDING_MISMATCH")
        baseline_oid = metadata._oid(saved.get("baseline_annex_oid"))
        old_recognized = metadata._oid(saved.get("old_recognized_oid"))
        # Read the immutable historical objects, not whatever refs currently say.
        before = replace(metadata.capture(stage, ref=baseline_oid, binding_digest=binding_digest,
                                           expected_oid=baseline_oid), ref=ANNEX_REF)
        _require(before.record() == saved.get("baseline_snapshot"),
                 "PUBLICATION_STAGE_RECOVERY_BASELINE_MISMATCH")
        saved_head = saved.get("file_head")
        _require(type(saved_head) is list and len(saved_head) == 2 and tuple(saved_head) == _head(stage),
                 "PUBLICATION_STAGE_HEAD_CHANGED")
        head = tuple(saved_head)
        base_intent = {**fixed, "baseline_annex_oid": baseline_oid,
                       "baseline_snapshot": before.record(), "file_head": saved_head,
                       "old_recognized_oid": old_recognized}

    def prepare(step, command, **extra):
        identifier = action_id(step)
        intent = {**base_intent, "command": {"argv": list(command),
                  "stdin_digest": hashlib.sha256(b"").hexdigest()},
                  "after": [{"action_id": item, "receipt_digest": seal} for item, seal in receipts], **extra}
        prior = original[step]
        if prior is not None:
            _require(prior["kind"] == "map_stage" and prior["intent"] == intent,
                     "PUBLICATION_STAGE_RECOVERY_BINDING_MISMATCH")
            return identifier, prior
        scope.write(lambda _: actions.prepare(scope, action_id=identifier, kind="map_stage", intent=intent))
        return identifier, actions.read(scope, identifier)

    def verified(identifier, receipt):
        record = actions.read(scope, identifier)
        scope.write(lambda _: actions.verify(scope, action_id=identifier,
                                             intent_digest=record["intent_digest"], receipt=receipt))
        record = actions.read(scope, identifier)
        receipts.append((identifier, record["receipt_digest"]))

    def common():
        require_stage(stage)
        _require(_head(stage) == head, "PUBLICATION_STAGE_HEAD_CHANGED")
        _require(source.read("rev-parse", "--verify", ANNEX_REF) == source_expected,
                 "PUBLICATION_STAGE_SOURCE_CHANGED")
        _no_payload(stage)

    def unchanged_old():
        common()
        _require(stage.read("rev-parse", "--verify", ANNEX_REF) == before.commit_oid.encode() + b"\n",
                 "PUBLICATION_STAGE_BASELINE_CHANGED")

    # A forward annex state is possible only after its journalled merge. Do not
    # begin earlier commands against an unrelated/partially changed branch.
    common()
    if stage.ref_oid(ANNEX_REF) != before.commit_oid:
        _require(original["native-merge"] is not None and original["admit"] is not None
                 and original["admit"]["status"] == "VERIFIED"
                 and original["quarantine"]["status"] == "VERIFIED",
                 "PUBLICATION_STAGE_BASELINE_CHANGED")

    fetch, fetch_record = prepare("quarantine", ("fetch-local-object", source.profile.digest,
                                                source_annex_oid, quarantine_ref))
    fetched_oid = stage.ref_oid(quarantine_ref)
    if fetched_oid is None:
        _require(fetch_record["status"] == "PREPARED", "PUBLICATION_STAGE_VERIFIED_EFFECT_MISSING")
        unchanged_old()
        stage._fetch_action(fetch, source, source_annex_oid, quarantine_ref)
    else:
        _require(fetched_oid == source_annex_oid, "PUBLICATION_STAGE_QUARANTINE_CHANGED")
    quarantined = metadata.capture(stage, ref=quarantine_ref, binding_digest=binding_digest,
                                   expected_oid=source_annex_oid)
    common()
    if fetch_record["status"] == "PREPARED":
        unchanged_old()
    verified(fetch, {"quarantined_snapshot": quarantined.record(), "file_head": list(head),
                     "unchanged_annex_oid": before.commit_oid})

    admitted = policy.admit_input(
        baseline=before.metadata(), incoming=quarantined.metadata(), allowed_key_uuids=allowed_key_uuids,
        map_uuid=scope.library[1], clock_ceiling=clock_ceiling, annotation_writes=annotation_writes,
        expected_annotation_seals=expected_annotation_seals)
    recognize, recognize_record = prepare("admit", ("update-ref", RECOGNIZED_REF, source_annex_oid, old_recognized),
                                         admitted_input=admitted)
    recognized_oid = stage.ref_oid(RECOGNIZED_REF) or "0" * 40
    if recognized_oid != source_annex_oid:
        _require(recognize_record["status"] == "PREPARED" and recognized_oid == old_recognized,
                 "PUBLICATION_STAGE_RECOGNIZED_REF_CHANGED")
        unchanged_old()
        stage._run_action(recognize, "update-ref", RECOGNIZED_REF, source_annex_oid, old_recognized)
    elif original["admit"] is None:
        _require(old_recognized == source_annex_oid, "PUBLICATION_STAGE_RECOGNIZED_REF_CHANGED")
    recognized = metadata.capture(stage, ref=RECOGNIZED_REF, binding_digest=binding_digest,
                                  expected_oid=source_annex_oid)
    _require(recognized.metadata() == quarantined.metadata(), "PUBLICATION_STAGE_SOURCE_CHANGED")
    common()
    if recognize_record["status"] == "PREPARED":
        unchanged_old()
    verified(recognize, {"recognized_snapshot": recognized.record(), "admitted_input": admitted,
                         "unchanged_annex_oid": before.commit_oid, "file_head": list(head)})

    merge_command = ("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
    merge, merge_record = prepare("native-merge", merge_command, admitted_input_digest=store.digest(admitted))

    def observe_candidate():
        common()
        _require(stage.ref_oid(RECOGNIZED_REF) == source_annex_oid
                 and stage.ref_oid(quarantine_ref) == source_annex_oid,
                 "PUBLICATION_STAGE_RECOGNIZED_REF_CHANGED")
        current = metadata.capture(stage, ref=ANNEX_REF, binding_digest=binding_digest)
        validation = policy.validate_candidate(
            baseline=before.metadata(), incoming=quarantined.metadata(), candidate=current.metadata(),
            allowed_key_uuids=allowed_key_uuids, map_uuid=scope.library[1], clock_ceiling=clock_ceiling,
            annotation_writes=annotation_writes, expected_annotation_seals=expected_annotation_seals,
            required_key_uuids=required_key_uuids)
        locations = {}
        for key in sorted(allowed_key_uuids):
            _require(stage.metadata_path(key) == metadata.metadata_path(key), "PUBLICATION_STAGE_KEY_PATH_MISMATCH")
            identities = sorted(stage.effective_locations(key, current.commit_oid))
            _require(identities == validation["effective_locations"].get(key, []),
                     "PUBLICATION_STAGE_NATIVE_LOCATIONS_MISMATCH")
            locations[key] = tuple(identities)
        common()
        _require(stage.ref_oid(RECOGNIZED_REF) == source_annex_oid
                 and stage.ref_oid(quarantine_ref) == source_annex_oid,
                 "PUBLICATION_STAGE_RECOGNIZED_REF_CHANGED")
        final = metadata.capture(stage, ref=ANNEX_REF, binding_digest=binding_digest, expected_oid=current.commit_oid)
        _require(final == current, "PUBLICATION_STAGE_CANDIDATE_CHANGED")
        return current, validation, locations

    def merge_inputs():
        unchanged_old()
        _require(stage.ref_oid(RECOGNIZED_REF) == source_annex_oid
                 and stage.ref_oid(quarantine_ref) == source_annex_oid,
                 "PUBLICATION_STAGE_RECOGNIZED_REF_CHANGED")

    if original["native-merge"] is None:
        merge_inputs()
        stage._run_action(merge, *merge_command)
        after, validation, observed = observe_candidate()
    else:
        try:
            after, validation, observed = observe_candidate()
        except PublicationRefused:
            # Only an exact original baseline permits the already prepared
            # command. A foreign/partly merged tree is never retried over.
            _require(merge_record["status"] == "PREPARED", "PUBLICATION_STAGE_VERIFIED_EFFECT_CHANGED")
            merge_inputs()
            current = metadata.capture(stage, ref=ANNEX_REF, binding_digest=binding_digest)
            _require(current == before, "PUBLICATION_STAGE_BASELINE_CHANGED")
            stage._run_action(merge, *merge_command)
            after, validation, observed = observe_candidate()
    verified(merge, {"candidate_snapshot": after.record(), "validation": validation,
                     "native_locations": {key: list(value) for key, value in observed.items()},
                     "file_head": list(head), "no_annex_payload": True})
    return StagedCandidate(binding_digest, scope.operation_id, batch_id, stage.profile.digest,
                           source.profile.digest, before, quarantined, after, head,
                           store.canonical(admitted), store.canonical(validation),
                           tuple(sorted(observed.items())), tuple(receipts))
