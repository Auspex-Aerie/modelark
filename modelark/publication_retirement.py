"""Recoverable, receipt-bound retirement of one superseded Git payload path.

The replacement representation and catalog transition must already be durable.
This module removes only the exact frozen source path and commits only that path.
It never enables conversion admission or decides which source may be retired.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
import stat
from uuid import UUID, uuid5

from modelark import publication_actions as actions, publication_store as store
from modelark import publication_tree as trees
from modelark.publication_native import QualifiedRepository
from modelark.publication_policy import PublicationRefused, _require, relative_path


def _id(file_id, name):
    return str(uuid5(UUID(file_id), name))


def _snapshot(repository):
    return trees.capture(repository.tree, read_git=repository.read,
                         require_scope=repository.scope.require_io)


def _index(repository):
    return trees._entries(trees._read(
        repository.read, "ls-files", "--full-name", "--stage", "-v", "-z"), index=True)


def _saved(repository, file_id):
    scope, con = repository.scope, repository.scope.connection
    scope.require_io()
    con.execute("BEGIN")
    try:
        scope.require()
        binding, operation_digest, _ = store._load_bound_operation(scope, scope.operation_id)
        row, frozen, _ = store._file_chain(con, scope.operation_id, file_id, operation_digest)
        _require(row[3] == "CATALOG_PUBLISHED"
                 and file_id in binding["batch_files"].get(row[0], ()),
                 "PUBLICATION_RETIREMENT_FILE_NOT_PUBLISHED")
        return frozen["intent"]
    finally:
        con.rollback()


def _source_bytes(repository, evidence):
    path = evidence["path"]
    with repository.tree.parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            before = os.fstat(fd)
            _require(stat.S_ISREG(before.st_mode) and before.st_size == evidence["bytes"],
                     "PUBLICATION_RETIREMENT_SOURCE_CHANGED")
            digest = hashlib.sha256()
            while block := os.read(fd, 4 << 20):
                digest.update(block)
            after = os.fstat(fd)
            _require((before.st_dev, before.st_ino, before.st_mode, before.st_size,
                      before.st_mtime_ns, before.st_ctime_ns)
                     == (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                         after.st_mtime_ns, after.st_ctime_ns)
                     and digest.hexdigest() == evidence["sha256"],
                     "PUBLICATION_RETIREMENT_SOURCE_CHANGED")
        finally:
            os.close(fd)


def _absent(repository, path):
    with repository.tree.parent(path) as (parent, name):
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
    raise PublicationRefused("PUBLICATION_RETIREMENT_SOURCE_PRESENT", path=path)


def retire(repository, *, file_id):
    """Retire one frozen old path, acknowledging only exact replay states."""
    _require(type(repository) is QualifiedRepository and repository.drive_label is not None,
             "PUBLICATION_RETIREMENT_REPOSITORY_REQUIRED")
    scope = repository.scope
    intent = _saved(repository, store.canonical_uuid(file_id))
    evidence = intent.get("retired_source")
    path = relative_path(intent.get("retired_path", "")).as_posix()
    _require(type(evidence) is dict and set(evidence) == {"path", "entry", "bytes", "sha256"}
             and evidence["path"] == path and path != intent.get("stored_path")
             and type(evidence["bytes"]) is int and evidence["bytes"] >= 0,
             "PUBLICATION_RETIREMENT_INTENT_INVALID")
    store._require_digest(evidence["sha256"])
    old_entry = trees.TreeEntry(**evidence["entry"])
    _require(old_entry.path == path and old_entry.mode == "100644",
             "PUBLICATION_RETIREMENT_INTENT_INVALID")
    profile = repository.profile.record()
    root = {name: profile[name] for name in ("root_identity", "mount_id", "annex_uuid")}
    removal_id, commit_id = _id(file_id, "source-removal"), _id(file_id, "source-retirement")
    present = scope.connection.execute(
        "SELECT 1 FROM publication_actions WHERE operation_id=? AND action_id=?",
        [scope.operation_id, removal_id]).fetchone()
    if present:
        saved_removal = actions.read(scope, removal_id)
        before = trees.TreeSnapshot.from_record(saved_removal["intent"]["retirement"]["before"])
    else:
        before = _snapshot(repository)
    entries = {entry.path: entry for entry in before.entries}
    _require(entries.get(path) == old_entry, "PUBLICATION_RETIREMENT_SOURCE_CHANGED")
    _require(evidence["bytes"] <= 64 * 1024 * 1024,
             "PUBLICATION_RETIREMENT_SOURCE_TOO_LARGE")
    blob = trees._object(repository.read, "blob", old_entry.oid,
                         limit=max(1, evidence["bytes"]))
    _require(len(blob) == evidence["bytes"] and hashlib.sha256(blob).hexdigest() == evidence["sha256"],
             "PUBLICATION_RETIREMENT_SOURCE_CHANGED")
    expected = tuple(entry for entry in before.entries if entry.path != path)
    common = {"expected_phase": {"entity": "file", "id": file_id, "phase": "CATALOG_PUBLISHED"},
              "profile_digest": repository.profile.digest, "root": root,
              "retirement": {"file_id": file_id, "path": path, "before": before.record(),
                             "after_entries": [asdict(entry) for entry in expected]}}
    rm = ("rm", "-q", "--", path)
    commit = ("commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m",
              "ModelArk retire source " + scope.operation_id, "--", path)

    def prepare(identifier, argv, step):
        return scope.write(lambda _: actions.prepare(
            scope, action_id=identifier, kind="source_retirement",
            intent={**common, "step": step,
                    "command": {"argv": list(argv), "stdin_digest": hashlib.sha256(b"").hexdigest()}}))

    removal = prepare(removal_id, rm, "remove-index-and-worktree")
    final = prepare(commit_id, commit, "commit-retirement")
    current_head = trees._head(repository.read)
    current_index = _index(repository)
    if current_head == (before.head_ref, before.head_oid) and current_index == before.entries:
        _source_bytes(repository, evidence)
        _require(removal.value["status"] == "PREPARED", "PUBLICATION_RETIREMENT_REPLAY_INVALID")
        repository._run_action(removal_id, *rm)
        current_head, current_index = trees._head(repository.read), _index(repository)
    if current_head == (before.head_ref, before.head_oid) and current_index == expected:
        _absent(repository, path)
        record = actions.read(scope, removal_id)
        if record["status"] == "PREPARED":
            scope.write(lambda _: actions.verify(
                scope, action_id=removal_id, intent_digest=record["intent_digest"],
                receipt={"version": 1, "kind": "staged-source-removal", "file_id": file_id,
                         "path": path, "head": before.head_oid,
                         "entries": [asdict(entry) for entry in expected]}))
        final = actions.read(scope, commit_id)
        _require(final["status"] == "PREPARED", "PUBLICATION_RETIREMENT_REPLAY_INVALID")
        repository._run_action(commit_id, *commit)
    after = _snapshot(repository)
    _absent(repository, path)
    _require(after.entries == expected and after.parents == (before.head_oid,)
             and after.head_ref == before.head_ref and after.root_identity == before.root_identity
             and after.mount_id == before.mount_id, "PUBLICATION_RETIREMENT_REPLAY_INVALID")
    removal = actions.read(scope, removal_id)
    if removal["status"] == "PREPARED":
        scope.write(lambda _: actions.verify(
            scope, action_id=removal_id, intent_digest=removal["intent_digest"],
            receipt={"version": 1, "kind": "committed-source-removal", "file_id": file_id,
                     "path": path, "after": after.record()}))
    final = actions.read(scope, commit_id)
    receipt = {"version": 1, "kind": "retired-source", "file_id": file_id,
               "path": path, "after": after.record()}
    if final["status"] == "PREPARED":
        scope.write(lambda _: actions.verify(
            scope, action_id=commit_id, intent_digest=final["intent_digest"], receipt=receipt))
    else:
        _require(final["receipt"]["receipt"] == receipt,
                 "PUBLICATION_RETIREMENT_RECEIPT_CHANGED")
    return receipt
