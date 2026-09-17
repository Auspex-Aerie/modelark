"""Real scoped storage + byte/tree proof composition, disposable fixtures only.

No production coordinator/native profile/map publication claim: the test supplies
the native command adapter and a synthetic catalog-to-repository attachment.
Unlike storage-only tests, the LOCAL/TREE receipts come from observed bytes/Git.
"""
import hashlib
import json
from pathlib import Path
import posixpath

import pytest

from modelark import publication_locks, publication_payload, publication_store as store, publication_tree
from modelark.publication_policy import PublicationRefused
from modelark.slice.linux import BoundTree
from test_publication_lifecycle import BATCH, FILE, MAP, OP
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_tree import KEY, OBJECT, PATH
from test_publication_tree import repo as git_repository  # noqa: F401


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture(name="repo")
def _repository(request):
    return request.getfixturevalue("git_repository")


@pytest.mark.parametrize("failure", [None, "missing-object", "unrelated-tree-edit"])
def test_real_proofs_advance_only_the_owned_file_and_never_close_the_generation(con, repo, failure):
    archive, fixture_git = repo
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope, BoundTree(archive) as tree:
        def git(*args):
            scope.require_io()
            assert not con.in_transaction
            result = fixture_git(*args, fence_fds=scope.child_fence_fds)
            scope.require_io()
            return result
        before = publication_tree.capture(tree, read_git=git, require_scope=scope.require_io)
        pointer = posixpath.relpath(OBJECT, str(Path(PATH).parent)).encode()
        entry = publication_tree.TreeEntry(PATH, "120000", hashlib.sha1(
            b"blob " + str(len(pointer)).encode() + b"\0" + pointer).hexdigest())
        scope.write(lambda _: store.prepare_operation(
            scope, operation_id=OP, kind="fill", profile_digest="b" * 64,
            batch_files={BATCH: [FILE]}, before_state=before.record()))
        binding = scope.write(lambda _: store.prepare_file(
            scope, operation_id=OP, batch_id=BATCH, file_id=FILE,
            intent={"tree_before": before.record(), "stored_path": PATH, "annex_key": KEY,
                    "expected_pointer_oid": entry.oid}))
        physical = archive / OBJECT
        physical.parent.mkdir(parents=True)
        physical.write_bytes(b"data")
        mapped = archive / PATH
        mapped.parent.mkdir(parents=True)
        mapped.symlink_to(pointer.decode())
        git("add", "--", PATH)
        if failure == "missing-object":
            physical.unlink()
        try:
            local = publication_payload.verify(
                tree, stored_path=PATH, annex_key=KEY, qualified_object_path=OBJECT,
                binding_digest=binding, require_scope=scope.require_io)
            scope.write(lambda _: store.advance_file(
                scope, operation_id=OP, file_id=FILE, phase="LOCAL_VERIFIED", proof=local.record()))
            if failure == "unrelated-tree-edit":
                (archive / "unrelated").write_bytes(b"unexpected change\n")
                git("add", "--", "unrelated")
            git("commit", "-qm", "publish pointer")
            committed = publication_tree.verify(
                tree, read_git=git, require_scope=scope.require_io, before=before,
                allowed_delta={PATH: entry}, stored_path=PATH, annex_key=KEY,
                qualified_object_path=OBJECT, binding_digest=binding, profile_digest="b" * 64)
            scope.write(lambda _: store.advance_file(
                scope, operation_id=OP, file_id=FILE, phase="TREE_VERIFIED", proof=committed.record()))
        except PublicationRefused as exc:
            assert failure is not None
            assert exc.code == {"missing-object": "PUBLICATION_PAYLOAD_UNAVAILABLE",
                                "unrelated-tree-edit": "PUBLICATION_GIT_UNEXPECTED_TREE_DELTA"}[failure]
        else:
            assert failure is None
        expected = {None: "TREE_VERIFIED", "missing-object": "PREPARED",
                    "unrelated-tree-edit": "LOCAL_VERIFIED"}[failure]
        row = con.execute("SELECT phase,local_proof_json,tree_proof_json FROM publication_files").fetchone()
        assert row[0] == expected
        if failure is None:
            assert json.loads(row[1])["proof"]["stored_sha256"] == hashlib.sha256(b"data").hexdigest()
            assert json.loads(row[2])["proof"]["pointer_oid"] == entry.oid
        assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=2").fetchone()[0] == 0
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
            store.require_clear(con, ["d0"])
