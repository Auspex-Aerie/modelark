"""Native local-key replica import: actual bytes, pointers and scoped readers."""
import hashlib
import importlib.util
import os
from pathlib import Path
import posixpath
import sys
from unittest import mock

import pytest

from modelark import publication_locks, publication_native, publication_payload, publication_tree
from test_publication_lifecycle import MAP
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401
from test_publication_native import git_repository  # noqa: F401


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    spec = importlib.util.spec_from_file_location("replica_qualification", SCRIPTS / "qualify_annex_replica_pointer.py")
    qualification = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qualification)


@pytest.fixture(scope="module")
def observed():
    for path, digest in qualification.TOOLS.items():
        if not Path(path).is_file() or qualification.sha(Path(path).read_bytes()) != digest:
            pytest.skip(f"qualification requires pinned native tool: {path}")
    probe = qualification.Replica()
    result = probe.cases()
    assert len(probe.checks) == 22
    assert set(result["final_repo_formats"].values()) == {"8"}
    assert result["original_source_sha256"] == hashlib.sha256(qualification.CONTENT).hexdigest()
    assert result["production_writer_qualified"] is False
    return result


@pytest.mark.parametrize("backend", ["SHA256", "SHA256E"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("ignored", [False, True])
def test_exact_full_key_replica_and_ignored_fromkey_partial_failure(observed, backend, force, ignored):
    row = next(row for row in observed["completed"]
               if (row["backend"], row["force"], row["ignored"]) == (backend, force, ignored))
    assert row["fromkey_exit"] == (1 if ignored else 0)
    assert row["explicit_owned_pointer_stage_required"] is ignored
    payload = row["payload"]
    assert payload["annex_key"].startswith(backend + "-")
    assert payload["stored_path"].endswith(".blob")
    if backend == "SHA256E":
        assert payload["annex_key"].endswith(".json")
    assert payload["stored_sha256"] == hashlib.sha256(qualification.CONTENT).hexdigest()
    assert payload["representation"] == "locked"
    assert row["tree"]["representation"] == "locked"


@pytest.mark.parametrize("kind", ["regular", "tracked", "directory", "symlink", "dangling"])
def test_fromkey_force_does_not_overwrite_existing_target(observed, kind):
    row = next(row for row in observed["existing_targets"] if row["kind"] == kind)
    assert row["exit"] == 1
    assert row["path_and_index_preserved"] is True


def test_bad_setkey_bytes_are_not_valid_publication(observed):
    assert len(observed["wrong_content"]) == 2
    for row in observed["wrong_content"]:
        assert row["setkey_exit"] == 1
        assert row["object_exists"] is False
        assert row["independent_result"] == "native-setkey-refused"


def test_interrupted_native_phases_have_distinct_incomplete_evidence(observed):
    assert {row["phase"]: row["refusal"] for row in observed["interruptions"]} == {
        "after-setkey": "PUBLICATION_PAYLOAD_UNAVAILABLE",
        "after-fromkey-before-commit": "PUBLICATION_GIT_INDEX_TREE_MISMATCH",
        "fromkey-without-setkey": "PUBLICATION_PAYLOAD_UNAVAILABLE",
    }


def test_new_fill_force_add_handles_both_root_ignored_path_kinds(observed):
    assert {row["path"] for row in observed["new_fill_force_add"]} == {
        qualification.MAPPED, "ignored/model.json"}
    for row in observed["new_fill_force_add"]:
        assert row["key"].startswith("SHA256-")
        assert row["payload"]["stored_sha256"] == observed["original_source_sha256"]
        assert row["tree"]["representation"] == "locked"


def test_explicit_exclusive_pointer_alternative_is_independently_proven(observed):
    assert {row["backend"] for row in observed["exclusive_pointer"]} == {"SHA256", "SHA256E"}
    for row in observed["exclusive_pointer"]:
        assert row["payload"]["annex_key"] == row["key"]
        assert row["tree"]["representation"] == "locked"


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture(name="prepared")
def _prepared(request):
    return request.getfixturevalue("native_prepared")


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("method", ["fromkey", "exclusive"])
def test_real_scope_native_target_preserves_full_key_and_committed_pointer(con, prepared, tmp_path,
                                                                         extended, method):
    archive, git = prepared
    original = tmp_path / "original.json"
    staged = tmp_path / "owned-copy.stage"
    original.write_bytes(b"original source bytes\r\n")
    staged.write_bytes(original.read_bytes())
    expected = hashlib.sha256(original.read_bytes()).hexdigest()
    key = (f"SHA256{'E' if extended else ''}-s{original.stat().st_size}--{expected}"
           + (".json" if extended else ""))
    target = "org/model/__modelark_payload_v1__/p-replica.blob"
    (archive / target).parent.mkdir(parents=True)
    if method == "exclusive":
        (archive / ".git/info/exclude").write_text("org/model/__modelark_payload_v1__/\n")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            before = publication_tree.capture(reader.tree, read_git=reader.read, require_scope=scope.require_io)
            git("annex", "setkey", "--", key, str(staged), fence_fds=scope.child_fence_fds)
            assert not staged.exists()
            obj = reader.object_path(key)
            link = posixpath.relpath(obj, str(Path(target).parent))
            if method == "fromkey":
                git("annex", "fromkey", "--force", "--", key, target, fence_fds=scope.child_fence_fds)
            else:
                with reader.tree.parent(target) as (parent_fd, leaf):
                    os.symlink(link, leaf, dir_fd=parent_fd)
                    with pytest.raises(FileExistsError):
                        os.symlink("wrong", leaf, dir_fd=parent_fd)
                git("--literal-pathspecs", "add", "-f", "--", target, fence_fds=scope.child_fence_fds)
            payload = publication_payload.verify(
                reader.tree, stored_path=target, annex_key=key, qualified_object_path=obj,
                binding_digest="a" * 64, require_scope=scope.require_io)
            git("commit", "-qm", "exact local replica pointer", fence_fds=scope.child_fence_fds)
            blob = link.encode()
            oid = hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest()
            tree = publication_tree.verify(
                reader.tree, read_git=reader.read, require_scope=scope.require_io, before=before,
                allowed_delta={target: publication_tree.TreeEntry(target, "120000", oid)},
                stored_path=target, annex_key=key, qualified_object_path=obj,
                binding_digest="a" * 64, profile_digest=reader.profile.digest)
            assert payload.annex_key == key
            assert payload.stored_sha256 == hashlib.sha256(original.read_bytes()).hexdigest() == expected
            assert payload.representation == tree.representation == "locked"
