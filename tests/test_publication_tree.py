"""Actual disposable Git trees; no production runner qualification or live archive.

The explicit test reader has isolated configuration and uses only temp repos.
It is not a stand-in implementation of the future qualified production reader.
"""
import hashlib
import json
import os
from pathlib import Path
import posixpath
import subprocess

import pytest

from modelark import publication_tree as publication
from modelark.publication_policy import PublicationRefused
from modelark.slice.linux import BoundTree


KEY = "SHA256-s4--" + hashlib.sha256(b"data").hexdigest()
OBJECT = f".git/annex/objects/aa/bB/{KEY}/{KEY}"
PATH = "org/model/__modelark_payload_v1__/p-test.blob"


@pytest.fixture
def repo(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    # Necessary isolation from operator config and inherited Git routing/helpers.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    def git(*args, data=None, fence_fds=()):
        return subprocess.run(["/usr/bin/git", "-C", str(archive), "-c", "user.name=Tree test",
                               "-c", "user.email=test@invalid", "-c", "commit.gpgsign=false",
                               "-c", "core.hooksPath=/dev/null", *args], env=environment,
                              input=data, pass_fds=fence_fds, capture_output=True, check=True).stdout
    git("init", "-q", "--initial-branch=main")
    (archive / "unrelated").write_bytes(b"preserve me\n")
    git("add", "--", "unrelated")
    git("commit", "-qm", "baseline")
    return archive, git


def capture(tree, git):
    return publication.capture(tree, read_git=git, require_scope=lambda: None)


def pointer(archive, git, *, path=PATH, object_path=OBJECT, unlocked=False):
    target = archive / path
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = (f"/annex/objects/{KEY}\n".encode() if unlocked else
            posixpath.relpath(object_path, str(Path(path).parent)).encode())
    if unlocked:
        target.write_bytes(blob)
    else:
        target.symlink_to(os.fsdecode(blob))
    git("--literal-pathspecs", "add", "--", path)
    oid = hashlib.sha1(b"blob " + str(len(blob)).encode() + b"\0" + blob).hexdigest()
    return publication.TreeEntry(path, "100644" if unlocked else "120000", oid)


def verify(tree, git, before, delta, *, path=PATH, object_path=OBJECT):
    return publication.verify(tree, read_git=git, require_scope=lambda: None, before=before,
                              allowed_delta=delta, stored_path=path, annex_key=KEY,
                              qualified_object_path=object_path, binding_digest="a" * 64,
                              profile_digest="b" * 64)


@pytest.mark.parametrize("unlocked", [False, True])
@pytest.mark.parametrize("path", [PATH, "org/model/tab\tand\nnewline.blob", "org/model/:literal[é]*.blob"])
def test_exact_native_tree_and_pointer_without_worktree_or_object_presence_claim(repo, unlocked, path):
    archive, git = repo
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        before = publication.TreeSnapshot.from_record(json.loads(json.dumps(before.record())))
        entry = pointer(archive, git, path=path, unlocked=unlocked)
        git("commit", "-qm", "publish")
        index_bytes = (archive / ".git/index").read_bytes()
        # A tree proof is not a payload proof: no annex object exists in this repo.
        assert not (archive / OBJECT).exists()
        proof = verify(tree, git, before, {path: entry}, path=path)
        assert proof.representation == ("unlocked" if unlocked else "locked")
        assert proof.pointer_oid == entry.oid
        assert json.loads(json.dumps(proof.record())) == proof.record()
        assert (archive / ".git/index").read_bytes() == index_bytes


def test_verified_existing_commit_is_an_explicit_noop(repo):
    archive, git = repo
    pointer(archive, git)
    git("commit", "-qm", "existing pointer")
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        assert verify(tree, git, before, {}).delta == ()
        git("commit", "--allow-empty", "-qm", "unplanned extra commit")
        with pytest.raises(PublicationRefused, match="NOOP_HEAD_CHANGED"):
            verify(tree, git, before, {})


@pytest.mark.parametrize("change", ["stage", "extra-commit", "delete", "chmod"])
def test_unrelated_change_cannot_hide_behind_valid_pointer(repo, change):
    archive, git = repo
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        entry = pointer(archive, git)
        if change == "delete":
            git("rm", "--", "unrelated")
        elif change == "chmod":
            git("update-index", "--chmod=+x", "unrelated")
        elif change == "extra-commit":
            (archive / "unrelated").write_bytes(b"different\n")
            git("add", "--", "unrelated")
        git("commit", "-qm", "publish")
        if change == "stage":
            (archive / "unrelated").write_bytes(b"staged after commit\n")
            git("add", "--", "unrelated")
        with pytest.raises(PublicationRefused, match="INDEX_TREE_MISMATCH|UNEXPECTED_TREE_DELTA"):
            verify(tree, git, before, {PATH: entry})


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_are_not_a_clean_index(repo, flag):
    archive, git = repo
    git("update-index", flag, "unrelated")
    with BoundTree(archive) as tree, pytest.raises(PublicationRefused, match="INDEX_UNQUALIFIED"):
        capture(tree, git)


def test_unmerged_index_is_refused(repo):
    archive, git = repo
    oid = git("rev-parse", "HEAD:unrelated").strip()
    git("update-index", "--index-info", data=b"0 " + b"0" * 40 + b"\tunrelated\n" +
        b"100644 " + oid + b" 1\tunrelated\n" + b"100644 " + oid + b" 2\tunrelated\n")
    with BoundTree(archive) as tree, pytest.raises(PublicationRefused, match="INDEX_UNQUALIFIED"):
        capture(tree, git)


def test_same_tree_on_a_different_branch_is_not_the_bound_commit(repo):
    archive, git = repo
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        entry = pointer(archive, git)
        git("checkout", "-qb", "other")
        git("commit", "-qm", "publish elsewhere")
        with pytest.raises(PublicationRefused, match="BASELINE_CHANGED"):
            verify(tree, git, before, {PATH: entry})


def test_extra_intervening_commit_is_not_the_single_prepared_delta(repo):
    archive, git = repo
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        git("commit", "--allow-empty", "-qm", "intervening")
        entry = pointer(archive, git)
        git("commit", "-qm", "publish")
        with pytest.raises(PublicationRefused, match="LINEAGE_MISMATCH"):
            verify(tree, git, before, {PATH: entry})


def test_sealed_retirement_and_addition_are_both_required(repo):
    archive, git = repo
    (archive / "old-metadata").write_bytes(b"data")
    git("add", "--", "old-metadata")
    git("commit", "-qm", "old raw metadata")
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        entry = pointer(archive, git)
        git("rm", "--", "old-metadata")
        git("commit", "-qm", "mapped representation and retirement")
        with pytest.raises(PublicationRefused, match="UNEXPECTED_TREE_DELTA"):
            verify(tree, git, before, {PATH: entry})
        assert len(verify(tree, git, before, {PATH: entry, "old-metadata": None}).delta) == 2


def test_pointer_with_right_key_but_wrong_hash_directory_is_refused(repo):
    archive, git = repo
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        entry = pointer(archive, git, object_path=OBJECT.replace("aa/bB", "zz/zz"))
        git("commit", "-qm", "wrong pointer")
        with pytest.raises(PublicationRefused, match="POINTER_MISMATCH"):
            verify(tree, git, before, {PATH: entry})


@pytest.mark.parametrize("during", ["pointer", "index", "head"])
def test_ref_or_index_change_during_observation_is_refused(repo, during):
    archive, git = repo
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        entry = pointer(archive, git)
        git("commit", "-qm", "publish")
        changed = False
        def reader(*args):
            nonlocal changed
            result = git(*args)
            selected = ((during == "pointer" and "blob" in args) or
                        (during == "index" and "ls-files" in args) or
                        (during == "head" and "rev-parse" in args))
            if selected and not changed:
                changed = True
                if during == "index":
                    (archive / "unrelated").write_bytes(b"racing staged edit\n")
                    git("add", "--", "unrelated")
                else:
                    git("commit", "--allow-empty", "-qm", "racing commit")
            return result
        with pytest.raises(PublicationRefused, match="CHANGED|MISMATCH"):
            verify(tree, reader, before, {PATH: entry})


def test_expired_scope_cannot_yield_a_tree_snapshot(repo):
    archive, git = repo
    calls = 0
    def scope():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublicationRefused("PUBLICATION_FENCE_AUTHORITY_MISSING")
    with BoundTree(archive) as tree, pytest.raises(PublicationRefused, match="AUTHORITY_MISSING"):
        publication.capture(tree, read_git=git, require_scope=scope)


@pytest.mark.parametrize("change", [
    lambda record: record.update(unexpected="schema drift"),
    lambda record: record.update(mount_id=True),
    lambda record: record.update(root_identity="not an identity array"),
    lambda record: record.update(head_ref="refs/heads/../elsewhere"),
    lambda record: record["entries"].append(record["entries"][0]),
    lambda record: record["entries"][0].update(mode=[]),
    lambda record: record["entries"][0].update(oid="not an object id"),
])
def test_saved_baseline_rejects_unqualified_or_ambiguous_shape(repo, change):
    archive, git = repo
    with BoundTree(archive) as tree:
        record = capture(tree, git).record()
    change(record)
    with pytest.raises(PublicationRefused):
        publication.TreeSnapshot.from_record(record)


@pytest.mark.parametrize("data,index", [
    (b"100644 blob " + b"a" * 40 + b"\tfile", False),
    (b"160000 commit " + b"a" * 40 + b"\tsubmodule\0", False),
    (b"H 100644 " + b"0" * 64 + b" 0\tfile\0", True),
    ((b"100644 blob " + b"a" * 40 + b"\tfile\0") * 2, False),
    (b"100644 blob " + b"a" * 40 + b"\tbad\xff\0", False),
    (b"100644 blob " + b"a" * 40 + b"\t.git/config\0", False),
])
def test_unqualified_native_framing_or_paths_refuse(data, index):
    with pytest.raises(PublicationRefused):
        publication._entries(data, index=index)


def test_observer_issues_only_raw_read_commands(repo):
    archive, git = repo
    observed = []
    def reader(*args):
        observed.append(args)
        return git(*args)
    with BoundTree(archive) as tree:
        before = capture(tree, reader)
        entry = pointer(archive, git)
        git("commit", "-qm", "publish")
        verify(tree, reader, before, {PATH: entry})
    assert all(args[:2] == ("--no-replace-objects", "--literal-pathspecs") for args in observed)
    assert {args[2] for args in observed} == {"symbolic-ref", "rev-parse", "cat-file", "ls-tree", "ls-files"}


def test_large_raw_git_blob_is_refused_before_reading_it_as_a_pointer(repo):
    archive, git = repo
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        target = archive / PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * 8192)
        git("add", "--", PATH)
        git("commit", "-qm", "raw blob is not a pointer")
        entry = next(e for e in capture(tree, git).entries if e.path == PATH)
        calls = []
        def reader(*args):
            calls.append(args)
            return git(*args)
        with pytest.raises(PublicationRefused, match="OBJECT_TOO_LARGE"):
            verify(tree, reader, before, {PATH: entry})
        assert not any("blob" in args for args in calls)


def test_head_moving_during_final_index_read_cannot_yield_a_snapshot(repo):
    archive, git = repo
    calls = 0
    def reader(*args):
        nonlocal calls
        result = git(*args)
        if "ls-files" in args:
            calls += 1
            if calls == 2:
                git("commit", "--allow-empty", "-qm", "changed during final index read")
        return result
    with BoundTree(archive) as tree, pytest.raises(PublicationRefused, match="HEAD_CHANGED"):
        capture(tree, reader)


def test_replacement_refs_cannot_substitute_an_unrelated_commit(repo):
    archive, git = repo
    old = git("rev-parse", "HEAD").strip().decode()
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        entry = pointer(archive, git)
        git("commit", "-qm", "publish")
        new = git("rev-parse", "HEAD").strip().decode()
        git("replace", new, old)
        proof = verify(tree, git, before, {PATH: entry})
        assert proof.after.head_oid == new
        assert any(e.path == PATH for e in proof.after.entries)


def test_real_pinned_annex_locked_and_unlocked_committed_representations(repo):
    annex = Path("/usr/bin/git-annex")
    if not annex.is_file() or hashlib.sha256(annex.read_bytes()).hexdigest() != (
            "5de67e4fd40d011af9f99f563df80238b1d74c001c00cc1ed64227eb2e79ccc7"):
        pytest.skip("requires qualified annex 8.20210223")
    archive, git = repo
    git("annex", "init", "--version=8", "--quiet", "disposable-tree-test")
    git("config", "annex.backend", "SHA256")
    target = archive / PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    with BoundTree(archive) as tree:
        before = capture(tree, git)
        git("annex", "add", "--force-large", "--", PATH)
        object_path = git("annex", "examinekey", "--format=${objectpath}", KEY).decode().strip()
        git("commit", "-qm", "annex locked")
        committed = capture(tree, git)
        entry = next(e for e in committed.entries if e.path == PATH)
        assert verify(tree, git, before, {PATH: entry}, object_path=object_path).representation == "locked"
        git("annex", "unlock", "--", PATH)
        git("add", "--", PATH)
        git("commit", "-qm", "annex unlocked")
        unlocked = next(e for e in capture(tree, git).entries if e.path == PATH)
        assert verify(tree, git, committed, {PATH: unlocked}, object_path=object_path).representation == "unlocked"
        assert target.read_bytes() == b"data"
        assert git("config", "annex.version").strip() == b"8"
