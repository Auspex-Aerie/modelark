"""Physical descriptor proofs, tiny temp files only; no live archive or annex command."""
import hashlib
import json
import os
from pathlib import Path
import posixpath

import pytest

from modelark import publication_payload as payload
from modelark.publication_store import canonical
from modelark.publication_policy import PublicationRefused
from modelark.slice.linux import BoundTree

DATA = b"original lowercase\r\n\xe9\x00unchanged bytes"
SHA = hashlib.sha256(DATA).hexdigest()
KEY = f"SHA256-s{len(DATA)}--{SHA}"
OBJECT = f".git/annex/objects/aa/bB/{KEY}/{KEY}"
STORED = "org/model/__modelark_payload_v1__/p-test.blob"


@pytest.fixture
def fixture(tmp_path):
    archive = tmp_path / "archive"
    physical = archive / OBJECT
    physical.parent.mkdir(parents=True)
    physical.write_bytes(DATA)
    mapped = archive / STORED
    mapped.parent.mkdir(parents=True)
    mapped.symlink_to(posixpath.relpath(OBJECT, str(Path(STORED).parent)))
    return archive, physical, mapped


def verify(tree):
    return payload.verify(tree, stored_path=STORED, annex_key=KEY, qualified_object_path=OBJECT,
                          binding_digest="a" * 64, require_scope=lambda: None)


def test_locked_descriptor_proof_reads_object_and_exact_mapped_pointer(fixture):
    archive, physical, mapped = fixture
    with BoundTree(archive) as tree:
        proof = verify(tree)
        assert proof.stored_sha256 == SHA and proof.stored_bytes == len(DATA)
        assert proof.representation == "locked"
        assert proof.record()["binding_digest"] == "a" * 64
        assert proof.object_identity[1] == physical.stat().st_ino
        assert proof.mapped_identity[1] == mapped.lstat().st_ino
        assert json.loads(canonical(proof.record())) == proof.record()
        assert isinstance(proof.record()["root_identity"], list)
        assert isinstance(proof.record()["object_identity"], list)
        assert isinstance(proof.record()["mapped_identity"], list)


def test_unlocked_requires_actual_mapped_bytes_as_well_as_object(fixture):
    archive, _, mapped = fixture
    mapped.unlink()
    mapped.write_bytes(DATA)
    with BoundTree(archive) as tree:
        assert verify(tree).representation == "unlocked"
        mapped.write_bytes(b"x" * len(DATA))
        with pytest.raises(PublicationRefused, match="HASH_MISMATCH"):
            verify(tree)


@pytest.mark.parametrize("case", ["missing-path", "absolute-link", "extra-link", "wrong-object", "missing-object",
                                  "object-symlink", "parent-symlink", "fifo", "truncated"])
def test_key_or_correct_other_bytes_never_substitute_for_mapped_presence(fixture, tmp_path, case):
    archive, physical, mapped = fixture
    if case == "missing-path":
        mapped.unlink()
    elif case in {"absolute-link", "extra-link"}:
        mapped.unlink()
        target = str(physical) if case == "absolute-link" else "./" + posixpath.relpath(OBJECT, str(Path(STORED).parent))
        mapped.symlink_to(target)
    elif case == "wrong-object":
        physical.write_bytes(b"x" * len(DATA))
    elif case == "missing-object":
        physical.unlink()
    elif case == "object-symlink":
        outside = tmp_path / "elsewhere"
        outside.write_bytes(DATA)
        physical.unlink()
        physical.symlink_to(outside)
    elif case == "parent-symlink":
        parent = mapped.parent
        moved = archive / "moved"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    elif case == "fifo":
        physical.unlink()
        os.mkfifo(physical)
    elif case == "truncated":
        physical.write_bytes(DATA[:-1])
    with BoundTree(archive) as tree, pytest.raises(PublicationRefused):
        verify(tree)


def test_replacement_during_hash_is_detected_even_with_equal_bytes(fixture, monkeypatch):
    archive, physical, _ = fixture
    original = os.read
    changed = False
    def replace(fd, count):
        nonlocal changed
        block = original(fd, count)
        if block and not changed:
            changed = True
            physical.rename(physical.with_name("old"))
            physical.write_bytes(DATA)
        return block
    with BoundTree(archive) as tree:
        monkeypatch.setattr(payload.os, "read", replace)
        with pytest.raises(PublicationRefused, match="CHANGED"):
            verify(tree)


def test_scope_loss_after_io_never_returns_proof(fixture):
    archive, _, _ = fixture
    calls = 0
    def require():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublicationRefused("PUBLICATION_FENCE_AUTHORITY_MISSING")
    with BoundTree(archive) as tree, pytest.raises(PublicationRefused, match="AUTHORITY_MISSING"):
        payload.verify(tree, stored_path=STORED, annex_key=KEY, qualified_object_path=OBJECT,
                       binding_digest="a" * 64, require_scope=require)


def test_empty_payload_hash_and_size_are_not_truthiness_failures(tmp_path):
    key = "SHA256-s0--" + hashlib.sha256(b"").hexdigest()
    obj = f".git/annex/objects/ab/CD/{key}/{key}"
    physical = tmp_path / obj
    physical.parent.mkdir(parents=True)
    physical.touch()
    (tmp_path / "empty").write_bytes(b"")
    with BoundTree(tmp_path) as tree:
        proof = payload.verify(tree, stored_path="empty", annex_key=key, qualified_object_path=obj,
                               binding_digest="a" * 64, require_scope=lambda: None)
        assert proof.stored_bytes == 0
