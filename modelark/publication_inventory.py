"""Read-only generation closure observations, not a clean-anchor publisher.

The baseline binds namespace/stat facts and existing catalog-copy presence, not
whole-drive content hashes. Only files touched by this operation are rehashed.
Native .git internals are not a worktree-extra namespace; claimed annex objects
are checked separately. No download or temporary worktree namespace is ignored.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import PurePosixPath
import posixpath
import stat
from uuid import UUID, uuid5

from modelark import publication_actions as actions, publication_attachment as attachment
from modelark import publication_catalog as catalog
from modelark import publication_payload as payload, publication_store as store, publication_tree as trees
from modelark.drive_mutation import Observation
from modelark.publication_native import QualifiedRepository
from modelark.publication_policy import PublicationRefused, _require, check_committed_pointer, parse_sha256_key, relative_path
from modelark.slice.transaction import TransferRefusal

_MAX_ENTRIES = 100_000
_MAX_DEPTH = 128
_MAX_NAMESPACE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class InventoryBaseline:
    document_json: str

    def record(self):
        return json.loads(self.document_json)

    @property
    def digest(self):
        return store.digest(self.record())


@dataclass(frozen=True)
class InventoryProof:
    document_json: str
    observation: Observation

    def record(self):
        return json.loads(self.document_json)


def _actual(repository):
    _require(type(repository) is QualifiedRepository, "PUBLICATION_INVENTORY_REPOSITORY_REQUIRED")
    repository.ensure()
    _require(repository.drive_label in repository.scope.identities,
             "PUBLICATION_INVENTORY_DRIVE_REQUIRED")


def _snapshot(repository):
    return trees.capture(repository.tree, read_git=repository.read, require_scope=repository.scope.require_io)


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _scan(repository):
    """No-follow bounded namespace inventory; regular file bytes are not read."""
    _actual(repository)
    tree = repository.tree
    rows = []
    count = 0
    byte_budget = 256

    def visit(path, depth):
        nonlocal count, byte_budget
        _require(depth <= _MAX_DEPTH, "PUBLICATION_INVENTORY_DEPTH_LIMIT")
        fd = os.dup(tree.fd) if not path else tree.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            before = os.fstat(fd)
            rows.append({"path": path, "kind": "directory", "identity": list(_identity(before)), "link_hex": None})
            names = []
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    if not path and entry.name == ".git":
                        continue
                    count += 1
                    _require(count <= _MAX_ENTRIES, "PUBLICATION_INVENTORY_ENTRY_LIMIT")
                    names.append(entry.name)
            for name in sorted(names):
                relative = name if not path else path + "/" + name
                relative_path(relative)
                byte_budget += len(relative.encode("utf-8")) + 256
                _require(byte_budget <= _MAX_NAMESPACE_BYTES, "PUBLICATION_INVENTORY_BYTE_LIMIT")
                before_child = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(before_child.st_mode):
                    visit(relative, depth + 1)
                else:
                    _require(stat.S_ISREG(before_child.st_mode) or stat.S_ISLNK(before_child.st_mode),
                             "PUBLICATION_INVENTORY_TYPE_UNQUALIFIED", path=relative)
                    owned = tree.open(relative, os.O_PATH | os.O_NOFOLLOW)
                    try:
                        _require(_identity(os.fstat(owned)) == _identity(before_child),
                                 "PUBLICATION_INVENTORY_PATH_CHANGED", path=relative)
                    finally:
                        os.close(owned)
                    link = os.readlink(os.fsencode(name), dir_fd=fd) if stat.S_ISLNK(before_child.st_mode) else None
                    _require(link is None or len(link) <= 4096, "PUBLICATION_INVENTORY_LINK_LIMIT")
                    byte_budget += len(link) * 2 if link is not None else 0
                    _require(byte_budget <= _MAX_NAMESPACE_BYTES, "PUBLICATION_INVENTORY_BYTE_LIMIT")
                    rows.append({"path": relative, "kind": "symlink" if link is not None else "regular",
                                 "identity": list(_identity(before_child)), "link_hex": None if link is None else link.hex()})
                _require(_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) == _identity(before_child),
                         "PUBLICATION_INVENTORY_PATH_CHANGED", path=relative)
            _require(_identity(os.fstat(fd)) == _identity(before), "PUBLICATION_INVENTORY_DIRECTORY_CHANGED", path=path)
        finally:
            os.close(fd)

    try:
        visit("", 0)
        # Recheck every retained observation after the walk, not just each leaf
        # while visiting it. This does not follow an annex or external symlink.
        for row in rows:
            fd = os.dup(tree.fd) if not row["path"] else tree.open(row["path"], os.O_PATH | os.O_NOFOLLOW)
            try:
                _require(list(_identity(os.fstat(fd))) == row["identity"], "PUBLICATION_INVENTORY_PATH_CHANGED")
            finally:
                os.close(fd)
        _actual(repository)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_INVENTORY_UNPROVEN", detail=str(exc)) from exc
    return sorted(rows, key=lambda row: row["path"])


def _claims(con, label):
    catalog._schema(con)
    for table in ("archived", "replicas"):
        count = con.execute(f"SELECT count(*) FROM {table} WHERE drive_label=?", [label]).fetchone()[0]
        _require(count <= _MAX_ENTRIES, "PUBLICATION_INVENTORY_CLAIM_LIMIT")
    return {table: [dict(zip(catalog._COLUMNS[table], row)) for row in con.execute(
        f"SELECT {','.join(catalog._COLUMNS[table])} FROM {table} WHERE drive_label=? ORDER BY repo_id,rfilename", [label])]
        for table in ("archived", "replicas")}


def _read_state(repository, *, operation=False):
    scope = repository.scope
    scope.require_io()
    con = scope.connection
    con.execute("BEGIN")
    try:
        scope.require()
        claims = _claims(con, repository.drive_label)
        if not operation:
            return claims, store._revision(con)
        binding, digest, revision = store._load_bound_operation(scope, scope.operation_id)
        rows = []
        _require(sum(len(files) for files in binding["batch_files"].values()) <= _MAX_ENTRIES,
                 "PUBLICATION_INVENTORY_ENTRY_LIMIT")
        for file_id in sorted(file for files in binding["batch_files"].values() for file in files):
            row, frozen, seal = store._file_chain(con, scope.operation_id, file_id, digest)
            _require(row[3] == "CATALOG_PUBLISHED", "PUBLICATION_INVENTORY_FILES_INCOMPLETE")
            if frozen["intent"]["request"]["drive_label"] == repository.drive_label:
                pair = frozen["intent"]["catalog_pair"]
                actual = catalog._capture_rows(scope, **frozen["intent"]["request"])
                _require(actual["files"] == pair["before"]["files"]
                         and actual["source"] == pair["before"]["source"],
                         "PUBLICATION_INVENTORY_SOURCE_FACTS_CHANGED")
                rows.append((file_id, row[2], frozen["intent"], store._unseal(row[8], row[9])["proof"], row[10], seal))
        return json.loads(store.canonical({"binding": binding, "operation_digest": digest,
                                          "revision": revision, "rows": rows, "claims": claims}))
    finally:
        con.rollback()


def _regular_presence(tree, path, size):
    fd = tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        _require(stat.S_ISREG(value.st_mode) and value.st_size == size,
                 "PUBLICATION_INVENTORY_COPY_SIZE_OR_TYPE", path=path)
        return list(_identity(value))
    finally:
        os.close(fd)


def _presence(repository, claims):
    """Claimed copy presence/size only. No annex location log substitutes for IO."""
    result, by_key = [], {}
    archived = {(row["repo_id"], row["rfilename"]): row for row in claims["archived"]}
    for row in claims["replicas"]:
        if row["present"]:
            claim = archived.get((row["repo_id"], row["rfilename"]))
            _require(claim is not None and row["annex_key"] == claim["annex_key"],
                     "PUBLICATION_INVENTORY_COPY_CLAIM_UNMAPPED")
    try:
        for row in claims["archived"]:
            _require(isinstance(row["repo_id"], str) and isinstance(row["stored_relpath"], str),
                     "PUBLICATION_INVENTORY_COPY_PATH_UNQUALIFIED")
            path = row["repo_id"] + "/" + row["stored_relpath"]
            relative_path(path)
            size, key = row["stored_bytes"], row["annex_key"]
            _require(type(size) is int and size >= 0, "PUBLICATION_INVENTORY_COPY_SIZE_OR_TYPE")
            with repository.tree.parent(path) as (parent, name):
                value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                link = os.readlink(os.fsencode(name), dir_fd=parent) if stat.S_ISLNK(value.st_mode) else None
            if key:
                _require(parse_sha256_key(key)[0] == size, "PUBLICATION_INVENTORY_COPY_SIZE_OR_TYPE")
                if link is not None:
                    try:
                        object_path = posixpath.normpath(posixpath.join(str(PurePosixPath(path).parent), link.decode("ascii")))
                    except UnicodeDecodeError as exc:
                        raise PublicationRefused("PUBLICATION_INVENTORY_POINTER_INVALID") from exc
                    check_committed_pointer(mode="120000", blob=link, stored_path=path,
                                            key=key, qualified_object_path=object_path)
                else:
                    object_path = by_key.get(key) or repository.object_path(key)
                    by_key[key] = object_path
                object_identity = _regular_presence(repository.tree, object_path, size)
            else:
                _require(link is None, "PUBLICATION_INVENTORY_RAW_SYMLINK_UNQUALIFIED")
                object_path, object_identity = None, None
            if link is None:
                mapped = _regular_presence(repository.tree, path, size)
            else:
                fd = repository.tree.open(path, os.O_PATH | os.O_NOFOLLOW)
                try:
                    mapped = list(_identity(os.fstat(fd)))
                    _require(mapped == list(_identity(value)), "PUBLICATION_INVENTORY_PATH_CHANGED")
                finally:
                    os.close(fd)
            result.append({"path": path, "annex_key": key, "mapped_identity": mapped,
                           "object_path": object_path, "object_identity": object_identity})
        _actual(repository)
    except (OSError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_INVENTORY_CLAIM_UNAVAILABLE", detail=str(exc)) from exc
    return sorted(result, key=lambda row: row["path"])


def capture_baseline(repository) -> InventoryBaseline:
    """Capture before preparation/policy/payload work and freeze in the operation."""
    _actual(repository)
    _require(repository.scope.operation_id is None, "PUBLICATION_INVENTORY_BASELINE_PHASE_INVALID")
    claims, revision = _read_state(repository)
    before = _snapshot(repository)
    namespace = _scan(repository)
    presence = _presence(repository, claims)
    _require(_scan(repository) == namespace and _presence(repository, claims) == presence
             and _snapshot(repository) == before and _read_state(repository) == (claims, revision),
             "PUBLICATION_INVENTORY_BASELINE_CHANGED")
    record = {"version": 1, "kind": "publication-inventory-baseline", "drive_label": repository.drive_label,
              "profile": repository.profile.record(), "root": str(repository.tree.path),
              "tree": before.record(), "namespace": namespace, "claims": claims, "presence": presence}
    return InventoryBaseline(store.canonical(record))


def _row_key(row):
    return row["repo_id"], row["rfilename"]


def verify(repository, baseline_record, *, file_rows) -> InventoryProof:
    """Independently observe exact sealed file/catalog deltas and fresh capacity.

    Caller-supplied rows/baseline are only selectors: compare them to the actual
    same-scope sealed catalog records before any file IO. Closure remains solely
    the parent's guarded transaction, not this observation helper.
    """
    _actual(repository)
    scope, label = repository.scope, repository.drive_label
    _require(scope.operation_id is not None, "PUBLICATION_INVENTORY_OPERATION_REQUIRED")
    initial = _read_state(repository, operation=True)
    state = initial["binding"]["before_state"]
    _require(type(baseline_record) is dict and baseline_record == state.get("inventories", {}).get(label)
             and baseline_record.get("kind") == "publication-inventory-baseline"
             and baseline_record.get("version") == 1 and baseline_record.get("drive_label") == label
             and baseline_record.get("root") == state["roots"][label] == str(repository.tree.path)
             and baseline_record.get("profile") == state["profiles"][label],
             "PUBLICATION_INVENTORY_BASELINE_BINDING_MISMATCH")
    _require(repository.profile.record() in (state["profiles"][label], state["profiles"].get(label + ":policy")),
             "PUBLICATION_INVENTORY_PROFILE_MISMATCH")
    _require(type(file_rows) is list and all(type(row) is list and len(row) == 6 for row in file_rows)
             and sorted(file_rows, key=lambda row: row[0]) == initial["rows"],
             "PUBLICATION_INVENTORY_FILE_BINDING_MISMATCH")
    expected_tree = trees.TreeSnapshot.from_record(baseline_record["tree"])
    _require(expected_tree.record() == state["trees"][label], "PUBLICATION_INVENTORY_TREE_BINDING_MISMATCH")
    expected_claims = {table: {_row_key(row): row for row in baseline_record["claims"][table]}
                       for table in ("archived", "replicas")}
    touched, hashes, touched_keys, retired = {}, [], set(), set()
    transitions = {}

    def add_transition(before, after):
        _require(before.head_oid not in transitions and after.parents == (before.head_oid,)
                 and after.head_ref == before.head_ref
                 and after.root_identity == before.root_identity and after.mount_id == before.mount_id,
                 "PUBLICATION_INVENTORY_TREE_CHAIN_CHANGED")
        before_tree, before_parents = trees._commit(repository.read, before.head_oid)
        after_tree, after_parents = trees._commit(repository.read, after.head_oid)
        _require(before_tree == before.tree_oid and before_parents == before.parents
                 and trees._entries(trees._read(
                     repository.read, "ls-tree", "-r", "--full-tree", "-z", before_tree)) == before.entries
                 and after_tree == after.tree_oid and after_parents == after.parents
                 and trees._entries(trees._read(
                     repository.read, "ls-tree", "-r", "--full-tree", "-z", after_tree)) == after.entries,
                 "PUBLICATION_INVENTORY_COMMIT_CHANGED")
        transitions[before.head_oid] = after

    ordered_rows = sorted(initial["rows"], key=lambda row: row[4])
    latest_objects = {row[2]["annex_key"]: row[3]["local"]["object_identity"] for row in ordered_rows}
    for file_id, digest, intent, proof, _revision, seal in ordered_rows:
        path, key = intent["stored_path"], intent["annex_key"]
        entry = trees.TreeEntry(**intent["entry"])
        pointer_before = trees.TreeSnapshot.from_record(intent["tree_before"])
        _require(entry.path == path and path not in touched
                 and path not in {old.path for old in pointer_before.entries},
                 "PUBLICATION_INVENTORY_TREE_CHAIN_CHANGED")
        after = trees.TreeSnapshot.from_record(proof["tree"]["after"])
        _require(after.entries == tuple(sorted((*pointer_before.entries, entry))),
                 "PUBLICATION_INVENTORY_TREE_CHAIN_CHANGED")
        add_transition(pointer_before, after)
        object_path = repository.object_path(key)
        _require(object_path == intent["object_path"], "PUBLICATION_INVENTORY_OBJECT_CHANGED")
        pointer = trees._object(repository.read, "blob", entry.oid, limit=4096)
        check_committed_pointer(mode=entry.mode, blob=pointer, stored_path=path, key=key, qualified_object_path=object_path)
        observed = payload.verify(repository.tree, stored_path=path, annex_key=key, qualified_object_path=object_path,
                                  binding_digest=digest, require_scope=scope.require_io)
        # Native deduplication can update a shared object's identity during a
        # later selected file. Bind its latest actual receipt, not a stale first
        # receipt, while keeping each mapped reader's own identity exact.
        expected_local = {**proof["local"], "object_identity": latest_objects[key]}
        _require(observed.record() == expected_local, "PUBLICATION_INVENTORY_TOUCHED_COPY_CHANGED")
        touched[path] = observed
        retired_path = intent.get("retired_path")
        retirement = None
        if retired_path is not None:
            retired_path = relative_path(retired_path).as_posix()
            _require(retired_path not in retired and retired_path != path
                     and any(item.path == retired_path for item in expected_tree.entries),
                     "PUBLICATION_INVENTORY_RETIREMENT_CHANGED")
            identifier = str(uuid5(UUID(file_id), "source-retirement"))
            action = actions.read(scope, identifier)
            retirement = action.get("receipt", {}).get("receipt", {})
            retirement_intent = action.get("intent", {}).get("retirement", {})
            retirement_before = trees.TreeSnapshot.from_record(retirement_intent.get("before"))
            retirement_after = trees.TreeSnapshot.from_record(retirement.get("after"))
            _require(action["kind"] == "source_retirement" and action["status"] == "VERIFIED"
                     and retirement.get("version") == 1 and retirement.get("kind") == "retired-source"
                     and retirement.get("file_id") == file_id and retirement.get("path") == retired_path
                     and retirement_intent.get("file_id") == file_id
                     and retirement_intent.get("path") == retired_path
                     and any(item.path == retired_path for item in retirement_before.entries)
                     and retirement_after.entries == tuple(
                         item for item in retirement_before.entries if item.path != retired_path),
                     "PUBLICATION_INVENTORY_RETIREMENT_CHANGED")
            add_transition(retirement_before, retirement_after)
            retired.add(retired_path)
        hashes.append({"file_id": file_id, "catalog_receipt_digest": seal,
                       "payload": observed.record(), "retirement": retirement})
        touched_keys.add(key)
        pair = intent["catalog_pair"]
        catalog._validate(pair)
        for table in expected_claims:
            row_key = _row_key(pair["before"]["key"])
            _require(expected_claims[table].get(row_key) == pair["before"][table],
                     "PUBLICATION_INVENTORY_CATALOG_CHAIN_CHANGED")
            if pair["after"][table] is not None:
                expected_claims[table][row_key] = pair["after"][table]
    used = set()
    while expected_tree.head_oid in transitions:
        _require(expected_tree.head_oid not in used, "PUBLICATION_INVENTORY_TREE_CHAIN_CHANGED")
        used.add(expected_tree.head_oid)
        expected_tree = transitions[expected_tree.head_oid]
    _require(len(used) == len(transitions), "PUBLICATION_INVENTORY_TREE_CHAIN_CHANGED")
    expected_claims = {table: [rows[key] for key in sorted(rows)] for table, rows in expected_claims.items()}
    _require(initial["claims"] == expected_claims, "PUBLICATION_INVENTORY_CATALOG_CHANGED")
    _require(_snapshot(repository) == expected_tree, "PUBLICATION_INVENTORY_FINAL_TREE_CHANGED")
    namespace = _scan(repository)
    old_nodes = {row["path"]: row for row in baseline_record["namespace"]}
    new_nodes = {row["path"]: row for row in namespace}
    parents = {""} if touched else set()
    parents.update(str(parent) for path in touched for parent in relative_path(path).parents if str(parent) != ".")
    retired_ancestors = {
        str(parent)
        for path in retired
        for parent in relative_path(path).parents
        if str(parent) != "."
    }
    vanished_directories = {
        path for path in retired_ancestors
        if path in old_nodes and old_nodes[path]["kind"] == "directory" and path not in new_nodes
    }
    _require(set(new_nodes) == (set(old_nodes) - retired - vanished_directories) | set(touched) | parents,
             "PUBLICATION_INVENTORY_UNEXPLAINED_PATHS")
    for path, before in old_nodes.items():
        if path in retired or path in vanished_directories:
            continue
        actual = new_nodes[path]
        if path in touched:
            continue
        if before["kind"] == "directory" and path in parents:
            _require(actual["kind"] == "directory" and actual["identity"][:3] == before["identity"][:3],
                     "PUBLICATION_INVENTORY_DIRECTORY_REPLACED")
        else:
            _require(actual == before, "PUBLICATION_INVENTORY_UNTOUCHED_PATH_CHANGED", path=path)
    for path in parents - set(old_nodes):
        _require(new_nodes[path]["kind"] == "directory", "PUBLICATION_INVENTORY_PARENT_INVALID")
    presence = _presence(repository, initial["claims"])
    current_presence = {row["path"]: row for row in presence}
    for old in baseline_record["presence"]:
        if old["path"] in touched or old["path"] in retired:
            continue
        current = current_presence[old["path"]]
        if old["annex_key"] in touched_keys:
            current = {**current, "object_identity": old["object_identity"]}
        _require(current == old, "PUBLICATION_INVENTORY_UNTOUCHED_COPY_CHANGED", path=old["path"])
    _require(_scan(repository) == namespace and _presence(repository, initial["claims"]) == presence
             and _snapshot(repository) == expected_tree and _read_state(repository, operation=True) == initial,
             "PUBLICATION_INVENTORY_CHANGED_DURING_OBSERVATION")
    # Capacity is deliberately observed after namespace/byte checks, not before
    # potentially long inventory IO. The observer revalidates its attachment and
    # this scope's identity/generation before returning.
    physical = attachment.capture(scope, label)
    _actual(repository)
    _require(physical.root == str(repository.tree.path) and physical.root_identity == expected_tree.root_identity
             and physical.mount_id == expected_tree.mount_id and physical.annex_uuid == repository.expected_uuid,
             "PUBLICATION_INVENTORY_ATTACHMENT_CHANGED")
    identity = {"v": 1, "fs_uuid": physical.fs_uuid, "annex_uuid": physical.annex_uuid, "serial": physical.serial}
    fence = {**identity, "serial": physical.observed_serial}
    observation = Observation(True, physical.free_bytes, physical.filesystem_capacity_bytes,
                              physical.identity_fingerprint, store.canonical(identity), store.canonical(fence))
    record = {"version": 1, "kind": "publication-generation-inventory", "operation_digest": initial["operation_digest"],
              "drive_label": label, "baseline_digest": store.digest(baseline_record),
              "tree": expected_tree.record(), "namespace": namespace, "claims": initial["claims"],
              "presence": presence, "changed_file_hashes": hashes, "attachment": physical.record(),
              "whole_drive_content_hashed": False, "unexplained_extras": []}
    return InventoryProof(store.canonical(record), observation)
