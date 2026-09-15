"""Real disposable pinned annex metadata; no live archive or synthetic authority."""
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from modelark import publication_locks, publication_map_tree as observation, publication_native
from modelark.publication_policy import PublicationRefused, sha256_key
from test_publication_lifecycle import MAP
from test_publication_native import prepared as native_prepared  # noqa: F401
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_native import git_repository  # noqa: F401


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture(name="prepared")
def _prepared(request):
    return request.getfixturevalue("native_prepared")


@pytest.mark.parametrize("size,digest", [(0, "0" * 64), (4, hashlib.sha256(b"data").hexdigest()),
                                       (123456789, "abcdef01" * 8)])
def test_native_hashdirlower_matches_pinned_metadata_bucket(prepared, size, digest):
    _, git = prepared
    key = f"SHA256-s{size}--{digest}"
    native_bucket = git("annex", "examinekey", "--format=${hashdirlower}", key).decode().strip()
    hashed = hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()
    assert native_bucket == f"{hashed[:3]}/{hashed[3:6]}/"
    assert observation.metadata_path(key) == native_bucket + key + ".log"


@pytest.fixture
def metadata(prepared):
    archive, git = prepared
    (archive / "payload").write_bytes(b"small model metadata\n")
    git("annex", "add", "--force-large", "--", "payload")
    key = git("annex", "lookupkey", "--", "payload").decode().strip()
    git("annex", "metadata", f"--key={key}", "-s", "model=org/repo", "-s", "format=gguf")
    git("annex", "sync", "--only-annex", "--no-content", "--no-pull", "--no-push", "--no-commit")
    return archive, git, key


def observe(reader, ref="refs/heads/git-annex", **kwargs):
    return observation.capture(reader, ref=ref, binding_digest="a" * 64, **kwargs)


def make_commit(git, files, *, raw_tree=None, parents=()):
    """Create hostile/synthetic immutable objects only in the disposable fixture."""
    def raw_object(kind, data):
        return git("hash-object", "--literally", "-w", "-t", kind, "--stdin", data=data).decode().strip()
    def tree(rows):
        children = {}
        for path, (mode, data) in rows.items():
            first, _, rest = path.partition("/")
            if rest:
                children.setdefault(first, {})[rest] = (mode, data)
            else:
                children[first] = (mode, raw_object("blob", data))
        records = []
        for name, value in children.items():
            mode, oid = ("40000", tree(value)) if isinstance(value, dict) else value
            records.append((name.encode() + (b"/" if mode == "40000" else b""),
                            mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid)))
        return raw_object("tree", b"".join(value for _, value in sorted(records)))
    tree_oid = tree(files) if raw_tree is None else raw_object("tree", raw_tree)
    commit = (f"tree {tree_oid}\n" + "".join(f"parent {oid}\n" for oid in parents)
              + "author Fixture <fixture@invalid> 1 +0000\ncommitter Fixture <fixture@invalid> 1 +0000\n\nfixture\n")
    oid = raw_object("commit", commit.encode())
    git("update-ref", "refs/modelark/quarantine-annex", oid)
    return oid


def test_actual_native_metadata_objects_match_git_and_observation_is_readonly(con, metadata, monkeypatch):
    archive, git, key = metadata
    before_ref = git("rev-parse", "refs/heads/git-annex").decode().strip()
    before_index = (archive / ".git/index").read_bytes()
    commands = []
    original = publication_native._bounded_process
    def process(argv, **kwargs):
        commands.append(argv)
        return original(argv, **kwargs)
    monkeypatch.setattr(publication_native, "_bounded_process", process)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            snapshot = observe(reader, expected_oid=before_ref)
    assert snapshot.commit_oid == before_ref
    assert snapshot.profile_digest and snapshot.digest
    assert snapshot.tree_oid == git("rev-parse", "refs/heads/git-annex^{tree}").decode().strip()
    blobs = snapshot.metadata()
    path = observation.metadata_path(key)
    assert path in blobs and path + ".met" in blobs and "uuid.log" in blobs
    assert blobs[path] == git("show", f"{before_ref}:{path}")
    assert blobs[path + ".met"] == git("show", f"{before_ref}:{path}.met")
    assert json.loads(json.dumps(snapshot.record())) == snapshot.record()
    with pytest.raises(FrozenInstanceError):
        snapshot.ref = "not-allowed"
    blobs[path] = b"detached mutation"
    assert snapshot.metadata()[path] != blobs[path]
    assert (archive / ".git/index").read_bytes() == before_index
    assert git("rev-parse", "refs/heads/git-annex").decode().strip() == before_ref
    assert all("annex" not in command for command in commands)


@pytest.mark.parametrize("use_oid", [False, True])
def test_arbitrary_quarantine_commit_uses_git_only_before_admission(con, prepared, use_oid):
    archive, git = prepared
    files = {"config": ("100644", b"untrusted incoming config\n")}
    oid = make_commit(git, files)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            snapshot = observe(reader, ref=oid if use_oid else "refs/modelark/quarantine-annex", expected_oid=oid)
    # Observation is not admission: parent must apply pure quarantine policy.
    assert snapshot.metadata() == {"config": files["config"][1]}


@pytest.mark.parametrize("mode", ["120000", "100755", "160000"])
def test_nonregular_metadata_modes_refuse(con, prepared, mode):
    archive, git = prepared
    make_commit(git, {"config": (mode, b"hostile\n")})
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            with pytest.raises(PublicationRefused, match="MODE_UNQUALIFIED"):
                observe(reader, ref="refs/modelark/quarantine-annex")


@pytest.mark.parametrize("path,error", [
    (f"000/000/{sha256_key(4, 'a' * 64)}.log", "KEY_PATH_MISMATCH"),
    (f"000/000/{sha256_key(4, 'a' * 64)}.log.met", "KEY_PATH_MISMATCH"),
    (f"000/000/SHA256E-s4--{'a' * 64}.unknown.log", "KEY_UNQUALIFIED"),
    (f"{sha256_key(4, 'a' * 64)}.log", "KEY_PATH_MISMATCH"),
    (".git/config", "PATH_INVALID"),
])
def test_key_bucket_and_native_key_grammar_are_not_inferred_from_path(con, prepared, path, error):
    archive, git = prepared
    make_commit(git, {path: ("100644", b"metadata\n")})
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            with pytest.raises(PublicationRefused, match=error):
                observe(reader, ref="refs/modelark/quarantine-annex")


def test_oversized_blob_refuses_before_blob_content_command(con, prepared, monkeypatch):
    archive, git = prepared
    oid = make_commit(git, {"config": ("100644", b"x" * 129)})
    blob_oid = git("rev-parse", oid + ":config").decode().strip()
    monkeypatch.setattr(observation, "_BLOB_LIMIT", 128)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            original, commands = reader.read, []
            def read(*args):
                commands.append(args)
                return original(*args)
            monkeypatch.setattr(reader, "read", read)
            with pytest.raises(PublicationRefused, match="OBJECT_TOO_LARGE"):
                observe(reader, ref="refs/modelark/quarantine-annex")
    assert ("cat-file", "-s", blob_oid) in commands
    assert ("cat-file", "blob", blob_oid) not in commands


def test_aggregate_blob_limit_checked_before_second_allocation(con, prepared, monkeypatch):
    archive, git = prepared
    make_commit(git, {"a": ("100644", b"a" * 70), "b": ("100644", b"b" * 70)})
    monkeypatch.setattr(observation, "_BLOB_TOTAL_LIMIT", 128)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            with pytest.raises(PublicationRefused, match="OBJECT_TOO_LARGE"):
                observe(reader, ref="refs/modelark/quarantine-annex")


def test_named_ref_drift_after_blob_read_refuses_snapshot(con, metadata, monkeypatch):
    archive, git, _ = metadata
    old = git("rev-parse", "refs/heads/git-annex").decode().strip()
    other = make_commit(git, {"different": ("100644", b"x")})
    git("update-ref", "refs/modelark/quarantine-annex", old)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            original = reader.read
            changed = False
            def read(*args):
                nonlocal changed
                result = original(*args)
                if args[:2] == ("cat-file", "blob") and not changed:
                    changed = True
                    git("update-ref", "refs/modelark/quarantine-annex", other)
                return result
            monkeypatch.setattr(reader, "read", read)
            with pytest.raises(PublicationRefused, match="REF_CHANGED"):
                observe(reader, ref="refs/modelark/quarantine-annex", expected_oid=old)


def test_expected_ref_mismatch_and_closed_scope_refuse(con, metadata):
    archive, _, _ = metadata
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            with pytest.raises(PublicationRefused, match="REF_CHANGED"):
                observe(reader, expected_oid="0" * 40)
        with pytest.raises(PublicationRefused, match="SCOPE_CLOSED"):
            observe(reader)


def test_actual_reader_not_a_caller_supplied_success_callback():
    with pytest.raises(PublicationRefused, match="READER_UNQUALIFIED"):
        observe(lambda: None)


@pytest.mark.parametrize("case,error", [
    ("duplicate", "DUPLICATE_PATH"), ("order", "TREE_ORDER_INVALID"),
    ("truncated", "TREE_INVALID"), ("empty-directory", "EMPTY_DIRECTORY"),
])
def test_raw_tree_grammar_and_invisible_empty_subtree_refuse(con, prepared, case, error):
    archive, git = prepared
    blob = git("hash-object", "-w", "--stdin", data=b"x").decode().strip()
    item = lambda name: b"100644 " + name + b"\0" + bytes.fromhex(blob)
    if case == "duplicate":
        raw = item(b"a") + item(b"a")
    elif case == "order":
        raw = item(b"b") + item(b"a")
    elif case == "truncated":
        raw = item(b"a")[:-1]
    else:
        empty = git("hash-object", "-w", "-t", "tree", "--stdin", data=b"").decode().strip()
        raw = b"40000 invisible\0" + bytes.fromhex(empty)
    make_commit(git, {}, raw_tree=raw)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            with pytest.raises(PublicationRefused, match=error):
                observe(reader, ref="refs/modelark/quarantine-annex")


def test_object_hash_rechecked_not_just_native_size(con, prepared, monkeypatch):
    archive, git = prepared
    make_commit(git, {"config": ("100644", b"same size\n")})
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            original = reader.read
            def read(*args):
                result = original(*args)
                return b"evil size\n" if args[:2] == ("cat-file", "blob") else result
            monkeypatch.setattr(reader, "read", read)
            with pytest.raises(PublicationRefused, match="OBJECT_MISMATCH"):
                observe(reader, ref="refs/modelark/quarantine-annex")


@pytest.mark.parametrize("reference", ["HEAD", "refs/x/../x", "refs/.private/x", "refs/x.lock/y",
                                        "refs/x^{tree}", "refs//x", "refs/x\n", "a" * 64])
def test_revision_expressions_and_unqualified_ref_grammar_refuse(con, prepared, reference):
    archive, _ = prepared
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            with pytest.raises(PublicationRefused, match="REF_INVALID"):
                observe(reader, ref=reference)


def test_active_catalog_transaction_cannot_host_metadata_io(con, prepared):
    archive, _ = prepared
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with publication_native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            con.execute("BEGIN")
            try:
                with pytest.raises(PublicationRefused, match="IO_TRANSACTION_ACTIVE"):
                    observe(reader)
            finally:
                con.rollback()
