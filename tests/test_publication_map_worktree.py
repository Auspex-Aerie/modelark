"""Actual confined map-tree observations; only disposable repos are mutated."""
import json
import errno
import os

import pytest

from modelark import publication_locks, publication_native as native, publication_tree
from modelark import publication_map_worktree as worktree
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import MAP
from test_publication_native import git_repository, publication_connection  # noqa: F401
from test_publication_native import prepared as native_prepared  # noqa: F401


@pytest.fixture(name="con")
def connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture
def states(request):
    archive, git = request.getfixturevalue("native_prepared")
    old_target = "../../.git/annex/objects/old/pointer"
    new_target = "../../.git/annex/objects/new/pointer"
    for path in ("old-dir/change.pointer", "remove-dir/deleted.pointer"):
        target = archive / path
        target.parent.mkdir()
        target.symlink_to(old_target)
    git("add", "--", "old-dir", "remove-dir")
    git("commit", "-qm", "old map tree")
    old_oid = git("rev-parse", "HEAD").decode().strip()
    old = publication_tree._entries(git("ls-tree", "-r", "--full-tree", "-z", old_oid))
    (archive / "old-dir/change.pointer").unlink()
    (archive / "old-dir/change.pointer").symlink_to(new_target)
    (archive / "remove-dir/deleted.pointer").unlink()
    (archive / "remove-dir").rmdir()
    (archive / "new-dir").mkdir()
    (archive / "new-dir/added.pointer").symlink_to(new_target)
    git("add", "-A")
    git("commit", "-qm", "new map tree")
    new_oid = git("rev-parse", "HEAD").decode().strip()
    new = publication_tree._entries(git("ls-tree", "-r", "--full-tree", "-z", new_oid))
    return archive, git, old, new, old_oid, new_oid


def test_fully_new_index_and_worktree_are_immutable_json_safe_evidence(con, states):
    archive, git, old, new, _, _ = states
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            index = (archive / ".git/index").read_bytes()
            proof = worktree.observe(repository, old=old, new=new)
            assert proof.replay_state == "complete"
            assert proof.index == new
            assert {directory.path for directory in proof.directories} == {"new-dir", "old-dir"}
            assert json.loads(json.dumps(proof.record())) == proof.record()
            assert (archive / ".git/index").read_bytes() == index
            assert (archive / "unrelated").read_bytes() == b"preserve me\n"
            assert git("ls-files", "--stage")


def test_old_index_allows_exact_partial_old_new_paths_and_new_absence(con, states):
    archive, git, old, new, old_oid, _ = states
    git("read-tree", old_oid)
    (archive / "old-dir/change.pointer").unlink()
    (archive / "old-dir/change.pointer").symlink_to("../../.git/annex/objects/old/pointer")
    (archive / "new-dir/added.pointer").unlink()  # New path may still be absent.
    # The new empty directory is an explicitly intended intermediate state.
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            proof = worktree.observe(repository, old=old, new=new)
            assert proof.replay_state == "old-index-replay"
            assert proof.index == old
            assert "new-dir" in {item.path for item in proof.directories}


@pytest.mark.parametrize("extra", ["untracked-file", "untracked-empty-directory"])
def test_unbound_files_and_empty_directories_refuse(con, states, extra):
    archive, _, old, new, _, _ = states
    if extra == "untracked-file":
        (archive / extra).write_bytes(b"operator bytes")
    else:
        (archive / extra).mkdir()
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="REPLAY_"):
                worktree.observe(repository, old=old, new=new)


def test_new_index_cannot_hide_incomplete_files_or_stale_empty_old_directory(con, states):
    archive, _, old, new, _, _ = states
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            (archive / "remove-dir").mkdir()
            with pytest.raises(PublicationRefused, match="INCOMPLETE_DIRECTORIES"):
                worktree.observe(repository, old=old, new=new)
            (archive / "remove-dir").rmdir()
            (archive / "old-dir/change.pointer").unlink()
            with pytest.raises(PublicationRefused, match="INCOMPLETE_WORKTREE"):
                worktree.observe(repository, old=old, new=new)


def test_mixed_index_refuses_without_write_tree_or_worktree_repair(con, states):
    archive, git, old, new, _, _ = states
    entry = next(entry for entry in old if entry.path == "old-dir/change.pointer")
    git("update-index", "--cacheinfo", entry.mode, entry.oid, entry.path)
    before = (archive / ".git/index").read_bytes()
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="MIXED_INDEX"):
                worktree.observe(repository, old=old, new=new)
    assert (archive / ".git/index").read_bytes() == before


@pytest.mark.parametrize("change", ["bytes", "mode", "symlink-target"])
def test_unrelated_edits_or_wrong_pointer_never_become_replay(con, states, change):
    archive, git, old, new, old_oid, _ = states
    git("read-tree", old_oid)
    if change == "bytes":
        (archive / "unrelated").write_bytes(b"changed")
    elif change == "mode":
        (archive / "unrelated").chmod(0o755)
    else:
        (archive / "old-dir/change.pointer").unlink()
        (archive / "old-dir/change.pointer").symlink_to("unrelated-other-target")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="PATH_CHANGED"):
                worktree.observe(repository, old=old, new=new)


def test_symlink_directory_substitution_is_not_traversed(con, states, tmp_path):
    archive, _, old, new, _, _ = states
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("do not traverse")
    (archive / "new-dir/added.pointer").unlink()
    (archive / "new-dir").rmdir()
    (archive / "new-dir").symlink_to(outside, target_is_directory=True)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="REPLAY_"):
                worktree.observe(repository, old=old, new=new)
    assert marker.read_text() == "do not traverse"


def test_file_and_entry_budgets_fail_without_unbounded_payload_reads(con, states):
    archive, _, old, new, _, _ = states
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="OBJECT_TOO_LARGE"):
                worktree.observe(repository, old=old, new=new,
                                 limits=worktree.ObservationLimits(max_file_bytes=1))
            with pytest.raises(PublicationRefused, match="ENTRY_LIMIT"):
                worktree.observe(repository, old=old, new=new,
                                 limits=worktree.ObservationLimits(max_entries=1))


def test_special_file_refused_without_blocking_open(con, states):
    archive, _, old, new, _, _ = states
    os.mkfifo(archive / "unexpected-fifo")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="FILE_TYPE_UNQUALIFIED"):
                worktree.observe(repository, old=old, new=new)


def test_observer_requires_actual_qualified_reader_and_immutable_entries(con, states):
    archive, _, old, new, _, _ = states
    with pytest.raises(PublicationRefused, match="QUALIFIED_REPOSITORY_REQUIRED"):
        worktree.observe(lambda: None, old=old, new=new)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="IMMUTABLE_TREE_REQUIRED"):
                worktree.observe(repository, old=list(old), new=new)


def test_actual_tree_snapshot_input_and_nochange_state(con, states):
    archive, _, _, _, _, _ = states
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            snapshot = publication_tree.capture(repository.tree, read_git=repository.read,
                                                 require_scope=scope.require_io)
            proof = worktree.observe(repository, old=snapshot, new=snapshot)
            assert proof.replay_state == "complete"
            assert proof.old_digest == proof.new_digest


def test_mount_crossing_refusal_from_confined_open_is_preserved(con, states, monkeypatch):
    archive, _, old, new, _, _ = states
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            original = repository.tree.open

            def injected_crossing(path, *args, **kwargs):
                if path == "new-dir":
                    raise OSError(errno.EXDEV, "injected mount crossing")
                return original(path, *args, **kwargs)

            monkeypatch.setattr(repository.tree, "open", injected_crossing)
            with pytest.raises(PublicationRefused, match="WORKTREE_UNPROVEN"):
                worktree.observe(repository, old=old, new=new)


def test_regular_inode_mutation_while_hashing_refuses(con, states, monkeypatch):
    archive, _, old, new, _, _ = states
    target = archive / "unrelated"
    identity = target.stat().st_ino
    original = os.read
    changed = []

    def mutate(fd, size):
        data = original(fd, size)
        if data and not changed and os.fstat(fd).st_ino == identity:
            target.write_bytes(b"changed during hash")
            changed.append(True)
        return data

    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            monkeypatch.setattr(os, "read", mutate)
            with pytest.raises(PublicationRefused, match="PATH_CHANGED"):
                worktree.observe(repository, old=old, new=new)
    assert changed


def test_empty_directory_added_after_scan_is_not_missed(con, states, monkeypatch):
    archive, _, old, new, _, _ = states
    original = worktree._index
    calls = []

    def inject(repository):
        calls.append(True)
        if len(calls) == 2:
            (archive / "old-dir/unbound-empty").mkdir()
        return original(repository)

    monkeypatch.setattr(worktree, "_index", inject)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            with pytest.raises(PublicationRefused, match="PATH_CHANGED"):
                worktree.observe(repository, old=old, new=new)


def test_raw_blob_hash_is_verified_not_only_its_reported_size(con, states, monkeypatch):
    archive, _, old, new, _, _ = states
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as repository:
            original = repository.read

            def corrupt(*args):
                data = original(*args)
                if "blob" in args and data:
                    return b"x" + data[1:]
                return data

            monkeypatch.setattr(repository, "read", corrupt)
            with pytest.raises(PublicationRefused, match="OBJECT_MISMATCH"):
                worktree.observe(repository, old=old, new=new)
