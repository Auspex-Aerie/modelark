"""Qualified legacy key compatibility; no remapping or live archive migration."""
from decimal import Decimal
import hashlib
import json
import posixpath

import pytest

from modelark import publication_locks, publication_map_policy, publication_map_tree
from modelark import publication_native, publication_payload, publication_policy, publication_tree
from test_publication_lifecycle import MAP
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401
from test_publication_native import git_repository  # noqa: F401


DRIVE = "00000000-0000-4000-8000-000000000001"
DIGEST = hashlib.sha256(b"data").hexdigest()


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture(name="prepared")
def _prepared(request):
    return request.getfixturevalue("native_prepared")


@pytest.mark.parametrize("suffix", sorted(publication_policy.SHA256E_SUFFIXES))
def test_full_legacy_key_is_preserved_by_pointer_and_map_validators(suffix):
    key = f"SHA256E-s4--{DIGEST}{suffix}"
    assert publication_policy.parse_sha256_key(key) == (4, DIGEST)
    assert publication_policy.sha256_key(4, DIGEST) == f"SHA256-s4--{DIGEST}"
    object_path = f".git/annex/objects/Ab/Cd/{key}/{key}"
    for mode, blob in (("120000", posixpath.relpath(object_path, "org/model").encode()),
                       ("100644", f"/annex/objects/{key}\n".encode())):
        publication_policy.check_committed_pointer(
            mode=mode, blob=blob, stored_path="org/model/config", key=key,
            qualified_object_path=object_path)
        with pytest.raises(publication_policy.PublicationRefused):
            publication_policy.check_committed_pointer(
                mode=mode, blob=blob, stored_path="org/model/config",
                key=key.replace("SHA256E-", "SHA256-"), qualified_object_path=object_path)
    path = publication_map_tree.metadata_path(key)
    log = f"2s 1 {DRIVE}\n".encode()
    args = dict(baseline={}, incoming={path: log}, allowed_key_uuids={key: frozenset({DRIVE})},
                map_uuid=MAP, clock_ceiling=Decimal(3), annotation_writes={}, expected_annotation_seals={})
    admitted = publication_map_policy.admit_input(**args)
    assert set(admitted["location_additions"]) == {key}
    proof = publication_map_policy.validate_candidate(candidate={path: log},
        required_key_uuids={key: frozenset({DRIVE})}, **args)
    assert proof["effective_locations"] == {key: [DRIVE]}
    tag_path = path + ".met"
    annotation = publication_map_policy.AnnotationWrite(
        tag_path, b"", b"2s model +org/model\n", (("model", "org/model"),), "a" * 64)
    tagged = dict(args, incoming={path: log, tag_path: annotation.after},
                  annotation_writes={tag_path: annotation},
                  expected_annotation_seals={tag_path: annotation.seal()})
    publication_map_policy.validate_candidate(candidate=tagged["incoming"], **tagged)


@pytest.mark.parametrize("key", [
    "SHA256E-s01--" + DIGEST, "SHA256E-s-1--" + DIGEST,
    "SHA256E-s4--" + DIGEST.upper(), "SHA256E-s4--" + DIGEST[:-1],
    *(f"SHA256E-s4--{DIGEST}{suffix}" for suffix in
      (".unknown", ".q5.gguf", ".Json", ".🐺", ".é", ".json\n", ".json\0", ".json/extra")),
    f"SHA256-s4--{DIGEST}.json",
])
def test_unsupported_keys_refuse_consistently_before_pointer_or_metadata_use(key):
    with pytest.raises(publication_policy.PublicationRefused, match="KEY_UNQUALIFIED"):
        publication_policy.parse_sha256_key(key)
    with pytest.raises(publication_policy.PublicationRefused, match="KEY_UNQUALIFIED"):
        publication_map_tree.metadata_path(key)
    with pytest.raises(publication_policy.PublicationRefused):
        publication_map_policy.admit_input(
            baseline={}, incoming={}, allowed_key_uuids={key: frozenset({DRIVE})},
            map_uuid=MAP, clock_ceiling=Decimal(3), annotation_writes={}, expected_annotation_seals={})


def test_same_digest_different_suffix_is_not_interchangeable_pointer_or_bucket():
    first, second = (f"SHA256E-s4--{DIGEST}{suffix}" for suffix in (".json", ".txt"))
    assert publication_policy.parse_sha256_key(first) == publication_policy.parse_sha256_key(second)
    assert publication_map_tree.metadata_path(first) != publication_map_tree.metadata_path(second)
    with pytest.raises(publication_policy.PublicationRefused, match="POINTER_MISMATCH"):
        publication_policy.check_committed_pointer(
            mode="100644", blob=f"/annex/objects/{first}\n".encode(), stored_path="file",
            key=second, qualified_object_path=f".git/annex/objects/Ab/Cd/{second}/{second}")
    wrong_bucket = publication_map_tree.metadata_path(first).rsplit("/", 1)[0]
    metadata = f"2s 1 {DRIVE}\n".encode()
    oid = hashlib.sha1(b"blob " + str(len(metadata)).encode() + b"\0" + metadata).hexdigest()
    with pytest.raises(publication_policy.PublicationRefused, match="KEY_PATH_MISMATCH"):
        publication_map_tree.MetadataBlob(f"{wrong_bucket}/{second}.log", oid, metadata)


def test_native_md5_metadata_buckets_for_every_qualified_suffix(con, prepared):
    archive, _ = prepared
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            for suffix in sorted(publication_policy.SHA256E_SUFFIXES):
                key = f"SHA256E-s4--{DIGEST}{suffix}"
                bucket = hashlib.md5(key.encode("ascii"), usedforsecurity=False).hexdigest()
                expected = f"{bucket[:3]}/{bucket[3:6]}/{key}.log"
                assert reader.metadata_path(key) == publication_map_tree.metadata_path(key) == expected


@pytest.mark.parametrize("unlocked", [False, True])
def test_actual_sha256e_payload_tree_and_metadata_through_qualified_reader(con, prepared, unlocked):
    archive, git = prepared
    git("config", "annex.backend", "SHA256E")
    paths = ("legacy/weights.safetensors", "legacy/config.json", "legacy/weights.safetensors.znn",
             "legacy/weights.q4.gguf", "legacy/mapped.blob", "legacy/case.JSON")
    content = b"data"
    keys = {}
    for path in paths:
        destination = archive / path
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(content)
        git("annex", "add", "--force-large", "--", path)
        keys[path] = git("annex", "lookupkey", "--", path).decode().strip()
    git("commit", "-qm", "existing legacy SHA256E copy")
    if unlocked:
        git("annex", "unlock", "--", *paths)
        git("commit", "-qm", "legacy unlocked copy")
    git("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            before = publication_tree.capture(reader.tree, read_git=reader.read, require_scope=scope.require_io)
            for path, key in keys.items():
                obj = reader.object_path(key)
                payload = publication_payload.verify(
                    reader.tree, stored_path=path, annex_key=key, qualified_object_path=obj,
                    binding_digest="a" * 64, require_scope=scope.require_io)
                tree = publication_tree.verify(
                    reader.tree, read_git=reader.read, require_scope=scope.require_io,
                    before=before, allowed_delta={}, stored_path=path, annex_key=key,
                    qualified_object_path=obj, binding_digest="a" * 64,
                    profile_digest=reader.profile.digest)
                assert payload.stored_sha256 == DIGEST
                assert payload.representation == tree.representation == ("unlocked" if unlocked else "locked")
                assert json.loads(json.dumps(payload.record())) == payload.record()
            metadata = publication_map_tree.capture(reader, ref="refs/heads/git-annex", binding_digest="a" * 64)
            observed = metadata.metadata()
            for key in keys.values():
                expected = reader.metadata_path(key)
                assert expected == publication_map_tree.metadata_path(key)
                assert expected in observed
            baseline = {path: value for path, value in observed.items()
                        if path not in {publication_map_tree.metadata_path(key) for key in keys.values()}}
            actual_uuid = git("config", "annex.uuid").decode().strip()
            allowed = {key: frozenset({actual_uuid}) for key in keys.values()}
            checked = publication_map_policy.validate_candidate(
                baseline=baseline, incoming=observed, candidate=observed, allowed_key_uuids=allowed,
                required_key_uuids=allowed, map_uuid=MAP, clock_ceiling=Decimal("99999999999"),
                annotation_writes={}, expected_annotation_seals={})
            assert set(checked["effective_locations"]) == set(keys.values())
