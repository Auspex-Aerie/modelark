"""Receipt-bound file-tree candidates in an issued, payload-free private stage.

The returned value proves scratch construction, not publication to the map. Raw
tree/blob helpers are deliberately shared with the read-only tree observer; no
Git filters, checkout, real-map ref writes, or catalog CAS occur here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from uuid import UUID, uuid5

from modelark import publication_actions as actions, publication_map_stage as staging
from modelark import publication_native as native, publication_store as store, publication_tree as trees
from modelark.publication_policy import PublicationRefused, _require, check_committed_pointer, relative_path


@dataclass(frozen=True)
class FileSelection:
    file_id: str
    entry: trees.TreeEntry
    retired_path: str | None = None


@dataclass(frozen=True)
class SourceFiles:
    repository: native.QualifiedRepository
    head_oid: str
    files: tuple[FileSelection, ...]


@dataclass(frozen=True)
class FileCandidate:
    operation_id: str
    batch_id: str
    binding_digest: str
    stage_profile_digest: str
    before: trees.TreeSnapshot
    new_head: str
    new_tree: str
    entries: tuple[trees.TreeEntry, ...]
    delta: tuple[trees.TreeEntry, ...]
    retired: tuple[str, ...]
    selection_json: str
    action_receipts: tuple[tuple[str, str], ...]

    def record(self):
        return {"version": 1, "kind": "staged-file-candidate",
                "operation_id": self.operation_id, "batch_id": self.batch_id,
                "binding_digest": self.binding_digest, "stage_profile_digest": self.stage_profile_digest,
                "before": self.before.record(), "new_head": self.new_head, "new_tree": self.new_tree,
                "entries": [asdict(entry) for entry in self.entries],
                "delta": [asdict(entry) for entry in self.delta], "retired": list(self.retired),
                "selection": json.loads(self.selection_json),
                "action_receipts": [{"action_id": key, "receipt_digest": seal}
                                    for key, seal in self.action_receipts]}

    @property
    def digest(self):
        return store.digest(self.record())


def _snapshot(repository):
    return trees.capture(repository.tree, read_git=repository.read, require_scope=repository.scope.require_io)


def _index(repository):
    return trees._entries(trees._read(repository.read, "ls-files", "--full-name", "--stage", "-v", "-z"),
                          index=True)


def _is_ancestor(repository, ancestor, descendant, *, limit=10_000):
    """Bounded first-parent proof for the qualified single-parent file branch."""
    current = trees._oid(descendant)
    ancestor = trees._oid(ancestor)
    for _ in range(limit):
        if current == ancestor:
            return True
        _tree, parents = trees._commit(repository.read, current)
        if len(parents) != 1:
            return False
        current = parents[0]
    raise PublicationRefused("PUBLICATION_GIT_HISTORY_LIMIT")


def _receipt_rows(scope, sources, batch_id):
    """Only durable current-chain observations happen in this short read TX."""
    scope.require_io()
    scope.connection.execute("BEGIN")
    try:
        scope.require()
        binding, operation_digest, _ = store._load_bound_operation(scope, scope.operation_id)
        identifiers = [item.file_id for source in sources for item in source.files]
        _require(len(identifiers) == len(set(identifiers)) and sorted(identifiers)
                 == binding["batch_files"].get(batch_id), "PUBLICATION_MAP_FILE_WORKSET_MISMATCH")
        result = {}
        for identifier in identifiers:
            row, frozen, seal = store._file_chain(scope.connection, scope.operation_id, identifier,
                                                   operation_digest)
            _require(row[0] == batch_id and row[3] == "CATALOG_PUBLISHED",
                     "PUBLICATION_MAP_FILE_NOT_PUBLISHED")
            result[identifier] = (row[2], frozen["intent"],
                                  store._unseal(row[6], row[7])["proof"],
                                  store._unseal(row[8], row[9])["proof"], seal)
    finally:
        scope.connection.rollback()
    scope.require_io()
    return binding, operation_digest, result


def _source_proofs(source, rows):
    repository = source.repository
    current = _snapshot(repository)
    _require(current.head_oid == source.head_oid, "PUBLICATION_MAP_FILE_SOURCE_CHANGED")
    current_entries = {entry.path: entry for entry in current.entries}
    selected, dependencies = [], []
    for item in source.files:
        file_digest, intent, committed, catalog, seal = rows[item.file_id]
        entry = item.entry
        profile = repository.profile.record()
        _require(intent.get("entry") == asdict(entry) and intent.get("stored_path") == entry.path
                 and intent.get("request", {}).get("drive_label") == repository.drive_label
                 and committed.get("kind") == "committed-git-tree" and committed.get("version") == 1
                 and committed.get("binding_digest") == file_digest
                 and committed.get("profile_digest") == repository.profile.digest
                 and committed.get("stored_path") == entry.path
                 and committed.get("annex_key") == intent.get("annex_key")
                 and committed.get("qualified_object_path") == intent.get("object_path")
                 and committed.get("pointer_oid") == entry.oid and catalog.get("tree") == committed,
                 "PUBLICATION_MAP_FILE_RECEIPT_MISMATCH")
        saved = trees.TreeSnapshot.from_record(committed["after"])
        _require(saved.root_identity == current.root_identity and saved.mount_id == current.mount_id
                 and entry in saved.entries and current_entries.get(entry.path) == entry,
                 "PUBLICATION_MAP_FILE_ENTRY_CHANGED")
        # Re-read the saved commit itself: its sealed JSON is not raw Git proof.
        saved_tree, saved_parents = trees._commit(repository.read, saved.head_oid)
        _require(saved_tree == saved.tree_oid and saved_parents == saved.parents
                 and trees._entries(trees._read(repository.read, "ls-tree", "-r", "--full-tree", "-z",
                                                saved_tree)) == saved.entries,
                 "PUBLICATION_MAP_FILE_SAVED_TREE_CHANGED")
        local = catalog.get("local", {})
        _require(local.get("kind") == "descriptor-stored-payload" and local.get("version") == 1
                 and local.get("binding_digest") == file_digest
                 and local.get("root_identity") == profile["root_identity"]
                 and local.get("mount_id") == profile["mount_id"]
                 and local.get("stored_path") == entry.path
                 and local.get("object_path") == intent.get("object_path")
                 and local.get("annex_key") == intent.get("annex_key"),
                 "PUBLICATION_MAP_FILE_RECEIPT_MISMATCH")
        object_path = repository.object_path(intent["annex_key"])
        _require(object_path == intent["object_path"], "PUBLICATION_MAP_FILE_OBJECT_CHANGED")
        blob = trees._object(repository.read, "blob", entry.oid, limit=64 * 1024)
        check_committed_pointer(mode=entry.mode, blob=blob, stored_path=entry.path,
                                key=intent["annex_key"], qualified_object_path=object_path)
        retirement = None
        if item.retired_path is not None:
            retired = relative_path(item.retired_path).as_posix()
            _require(retired != entry.path and intent.get("retired_path") == retired
                     and retired not in current_entries, "PUBLICATION_MAP_FILE_RETIREMENT_CHANGED")
            identifier = str(uuid5(UUID(item.file_id), "source-retirement"))
            record = actions.read(repository.scope, identifier)
            receipt = record.get("receipt", {}).get("receipt", {})
            retirement = record.get("intent", {}).get("retirement", {})
            retired_before = trees.TreeSnapshot.from_record(retirement.get("before"))
            retired_after = trees.TreeSnapshot.from_record(receipt.get("after"))
            expected_after = tuple(item for item in retired_before.entries if item.path != retired)
            _require(record["kind"] == "source_retirement" and record["status"] == "VERIFIED"
                     and receipt.get("version") == 1 and receipt.get("kind") == "retired-source"
                     and receipt.get("file_id") == item.file_id and receipt.get("path") == retired
                     and retirement.get("file_id") == item.file_id and retirement.get("path") == retired
                     and any(item.path == retired for item in retired_before.entries)
                     and retired_after.entries == expected_after
                     and retired_after.parents == (retired_before.head_oid,)
                     and retired_after.head_ref == retired_before.head_ref == current.head_ref
                     and retired_after.root_identity == retired_before.root_identity == current.root_identity
                     and retired_after.mount_id == retired_before.mount_id == current.mount_id
                     and trees._commit(repository.read, retired_after.head_oid)
                     == (retired_after.tree_oid, retired_after.parents)
                     and _is_ancestor(repository, retired_after.head_oid, current.head_oid),
                     "PUBLICATION_MAP_FILE_RETIREMENT_CHANGED")
            retirement = {"path": retired, "action_id": identifier,
                          "receipt_digest": record["receipt_digest"],
                          "after_head": retired_after.head_oid}
            dependencies.append((identifier, record["receipt_digest"]))
        selected.append({"file_id": item.file_id, "file_digest": file_digest, "catalog_receipt_digest": seal,
                         "entry": asdict(entry), "annex_key": intent["annex_key"],
                         "source_profile_digest": repository.profile.digest, "source_head": current.head_oid,
                         "retirement": retirement})
    _require(_snapshot(repository) == current, "PUBLICATION_MAP_FILE_SOURCE_CHANGED")
    return current, selected, dependencies


def candidate(stage: native.QualifiedRepository, map_repository: native.QualifiedRepository, *,
              sources: tuple[SourceFiles, ...], batch_id: str, resume: bool = False) -> FileCandidate:
    """Construct exact pointers, or explicitly continue the same issued scratch.

    Continuation re-observes immutable journal intent and raw postconditions. It
    does not issue a stage capability after restart or admit a replacement root.
    An interrupted commit-tree can leave an unreachable object; creating an
    equivalent candidate is safe, and its exact OID is sealed in the verified
    receipt before returning or allowing another action to depend on it.
    """
    staging.require_stage(stage)
    staging._actual(map_repository)
    scope = stage.scope
    _require(type(resume) is bool, "PUBLICATION_MAP_FILE_SELECTION_INVALID")
    store.canonical_uuid(batch_id)
    _require(scope.operation_id is not None and map_repository.scope is scope
             and map_repository.drive_label is None and stage is not map_repository
             and stage.profile.root_identity != map_repository.profile.root_identity,
             "PUBLICATION_MAP_FILE_SCOPE_MISMATCH")
    _require(type(sources) is tuple and bool(sources) and all(type(source) is SourceFiles for source in sources),
             "PUBLICATION_MAP_FILE_SELECTION_INVALID")
    labels = set()
    for source in sources:
        staging._actual(source.repository)
        _require(source.repository.scope is scope and source.repository.drive_label in scope.identities
                 and source.repository.drive_label not in labels and type(source.files) is tuple
                 and bool(source.files) and all(type(item) is FileSelection and type(item.entry) is trees.TreeEntry
                                               and (item.retired_path is None or type(item.retired_path) is str)
                                               for item in source.files),
                 "PUBLICATION_MAP_FILE_SOURCE_UNQUALIFIED")
        labels.add(source.repository.drive_label)
        trees._oid(source.head_oid)
        for item in source.files:
            store.canonical_uuid(item.file_id)
    sources = tuple(sorted(sources, key=lambda source: source.repository.drive_label))
    binding, operation_digest, rows = _receipt_rows(scope, sources, batch_id)
    frozen_profiles = binding["before_state"].get("profiles", {})
    _require(all(repository.profile.record() in frozen_profiles.values()
                 for repository in (stage, map_repository, *(source.repository for source in sources))),
             "PUBLICATION_MAP_FILE_PROFILE_UNBOUND")
    before = _snapshot(map_repository)
    initial_index = _index(stage)
    stage_head = staging._head(stage)
    staging._no_payload(stage)
    selection, observations, source_dependencies = [], [], []
    for source in sources:
        observation, selected, dependencies = _source_proofs(source, rows)
        observations.append(observation)
        selection.extend(selected)
        source_dependencies.extend(dependencies)
    selection.sort(key=lambda item: item["file_id"])
    selected_entries = [item.entry for source in sources for item in source.files]
    _require(len({entry.path for entry in selected_entries}) == len(selected_entries),
             "PUBLICATION_MAP_FILE_PATH_COLLISION")
    union = {entry.path: entry for entry in before.entries}
    retired = tuple(sorted(item.retired_path for source in sources for item in source.files
                           if item.retired_path is not None))
    _require(len(retired) == len(set(retired)) and not set(retired) & {entry.path for entry in selected_entries},
             "PUBLICATION_MAP_FILE_PATH_COLLISION")
    for path in retired:
        _require(path in union, "PUBLICATION_MAP_FILE_RETIREMENT_MISSING")
        del union[path]
    delta = []
    for entry in sorted(selected_entries):
        existing = union.get(entry.path)
        _require(existing is None or existing == entry, "PUBLICATION_MAP_FILE_PATH_CONFLICT")
        if existing is None:
            delta.append(entry)
            union[entry.path] = entry
    expected = tuple(sorted(union.values()))
    _require(initial_index in (before.entries, expected), "PUBLICATION_MAP_FILE_STAGE_INDEX_MISMATCH")
    # Reject file/directory prefix collisions before touching even the scratch index.
    paths = set(union)
    _require(not any("/".join(path.split("/")[:index]) in paths for path in paths
                     for index in range(1, len(path.split("/")))), "PUBLICATION_MAP_FILE_PATH_CONFLICT")
    receipts = list(source_dependencies)
    selection_seal = store.digest(selection)
    profile = stage.profile.record()
    base = {"expected_phase": {"entity": "batch", "id": batch_id, "phase": "PREPARED"},
            "profile_digest": stage.profile.digest,
            "root": {key: profile[key] for key in ("root_identity", "mount_id", "annex_uuid")},
            "binding_digest": operation_digest, "selection": selection, "before": before.record(),
            "purpose": "map-file-candidate", "stage_head": list(stage_head),
            "sources": [{"profile_digest": source.repository.profile.digest, "snapshot": snapshot.record()}
                        for source, snapshot in zip(sources, observations, strict=True)]}

    def action_id(step):
        return str(uuid5(UUID(scope.operation_id), f"map-files:{batch_id}:{step}"))

    # Examine all batch candidate intents, not just the first requested source:
    # changing a selected set/order cannot evade the original baseline binding.
    existing_actions = {}
    for identifier, in scope.connection.execute(
            "SELECT action_id FROM publication_actions WHERE operation_id=? AND kind='map_stage'",
            [scope.operation_id]).fetchall():
        record = actions.read(scope, identifier)
        intent = record["intent"]
        if (intent.get("purpose") == "map-file-candidate"
                and intent.get("expected_phase", {}).get("id") == batch_id):
            _require(resume, "PUBLICATION_MAP_FILE_RECOVERY_REQUIRED")
            _require(all(intent.get(key) == value for key, value in base.items()),
                     "PUBLICATION_MAP_FILE_CONTINUATION_MISMATCH")
            existing_actions[identifier] = record
    expected_actions = {action_id("fetch:" + source.repository.profile.annex_uuid) for source in sources}
    changed = bool(delta or retired)
    if changed:
        expected_actions.update(action_id(step) for step in ("index", "tree", "commit"))
    _require(set(existing_actions) <= expected_actions, "PUBLICATION_MAP_FILE_CONTINUATION_MISMATCH")
    if changed and initial_index == expected:
        _require(action_id("index") in existing_actions, "PUBLICATION_MAP_FILE_INDEX_INTENT_MISSING")

    def unchanged():
        staging.require_stage(stage)
        staging._no_payload(stage)
        _require(_snapshot(map_repository) == before and staging._head(stage) == stage_head,
                 "PUBLICATION_MAP_FILE_BASELINE_CHANGED")
        for source, observed in zip(sources, observations, strict=True):
            _require(_snapshot(source.repository) == observed, "PUBLICATION_MAP_FILE_SOURCE_CHANGED")
        _require(_receipt_rows(scope, sources, batch_id)[2] == rows, "PUBLICATION_MAP_FILE_RECEIPT_CHANGED")

    def prepare(step, command, stdin=b""):
        unchanged()
        identifier = action_id(step)
        intent = {**base, "command": {"argv": list(command), "stdin_digest": hashlib.sha256(stdin).hexdigest()},
                  "after": [{"action_id": key, "receipt_digest": seal} for key, seal in receipts]}
        if identifier in existing_actions:
            record = actions.read(scope, identifier)
            _require(record["intent"] == intent and record["kind"] == "map_stage",
                     "PUBLICATION_MAP_FILE_CONTINUATION_MISMATCH")
            return record
        scope.write(lambda _: actions.prepare(scope, action_id=identifier, kind="map_stage", intent=intent))
        return actions.read(scope, identifier)

    def verified(record, proof):
        unchanged()
        identifier = record["action_id"]
        if record["status"] == "VERIFIED":
            _require(record["receipt"]["receipt"] == proof, "PUBLICATION_MAP_FILE_RECEIPT_CHANGED")
        else:
            scope.write(lambda _: actions.verify(scope, action_id=identifier,
                                                 intent_digest=record["intent_digest"], receipt=proof))
        record = actions.read(scope, identifier)
        receipts.append((identifier, record["receipt_digest"]))

    for source in sources:
        repository = source.repository
        ref = f"refs/modelark/quarantine-files/{scope.operation_id}/{batch_id}/{repository.profile.annex_uuid}"
        command = ("fetch-local-object", repository.profile.digest, source.head_oid, ref)
        record = prepare("fetch:" + repository.profile.annex_uuid, command)
        current_ref = stage.ref_oid(ref)
        if current_ref is None and record["status"] == "PREPARED":
            stage._fetch_action(record["action_id"], repository, source.head_oid, ref)
        _require(stage.ref_oid(ref) == source.head_oid, "PUBLICATION_MAP_FILE_FETCH_MISMATCH")
        for item in source.files:
            _require(trees._object(stage.read, "blob", item.entry.oid, limit=64 * 1024)
                     == trees._object(repository.read, "blob", item.entry.oid, limit=64 * 1024),
                     "PUBLICATION_MAP_FILE_FETCH_MISMATCH")
        _require(_index(stage) in (before.entries, expected), "PUBLICATION_MAP_FILE_STAGE_INDEX_MISMATCH")
        verified(record, {"ref": ref, "head": source.head_oid, "selection_digest": selection_seal})

    new_tree, new_head = before.tree_oid, before.head_oid
    if changed:
        removals = b"".join(f"0 {'0' * 40}\t{path}".encode() + b"\0" for path in retired)
        additions = b"".join(f"{entry.mode} {entry.oid}\t{entry.path}".encode() + b"\0" for entry in delta)
        stdin = removals + additions
        _require(len(stdin) <= 1024 * 1024, "PUBLICATION_MAP_FILE_DELTA_LIMIT")
        command = ("update-index", "-z", "--index-info")
        record = prepare("index", command, stdin)
        current_index = _index(stage)
        _require(current_index in (before.entries, expected), "PUBLICATION_MAP_FILE_STAGE_INDEX_MISMATCH")
        if current_index != expected and record["status"] == "PREPARED":
            stage._run_action(record["action_id"], *command, input_data=stdin)
        _require(_index(stage) == expected, "PUBLICATION_MAP_FILE_STAGE_INDEX_MISMATCH")
        verified(record, {"entries": [asdict(entry) for entry in expected]})
        record = prepare("tree", ("write-tree",))
        if record["status"] == "VERIFIED":
            new_tree = trees._oid(record["receipt"]["receipt"]["tree"])
        else:
            new_tree = trees._oid(trees._line(stage._run_action(record["action_id"], "write-tree")))
        _require(trees._entries(trees._read(stage.read, "ls-tree", "-r", "--full-tree", "-z", new_tree))
                 == expected and _index(stage) == expected, "PUBLICATION_MAP_FILE_TREE_MISMATCH")
        verified(record, {"tree": new_tree, "entries": [asdict(entry) for entry in expected]})
        command = ("commit-tree", new_tree, "-p", before.head_oid, "-m",
                   "ModelArk map publication " + scope.operation_id)
        record = prepare("commit", command)
        if record["status"] == "VERIFIED":
            new_head = trees._oid(record["receipt"]["receipt"]["head"])
        else:
            new_head = trees._oid(trees._line(stage._run_action(record["action_id"], *command)))
        _require(trees._commit(stage.read, new_head) == (new_tree, (before.head_oid,))
                 and trees._object(stage.read, "commit", new_head, limit=1024 * 1024).split(b"\n\n", 1)[1]
                 == (command[-1] + "\n").encode() and _index(stage) == expected,
                 "PUBLICATION_MAP_FILE_COMMIT_MISMATCH")
        verified(record, {"head": new_head, "tree": new_tree, "parent": before.head_oid})
    unchanged()
    _require(_index(stage) == expected, "PUBLICATION_MAP_FILE_STAGE_INDEX_MISMATCH")
    return FileCandidate(scope.operation_id, batch_id, operation_digest, stage.profile.digest,
                         before, new_head, new_tree, expected, tuple(delta), retired,
                         store.canonical(selection), tuple(receipts))
