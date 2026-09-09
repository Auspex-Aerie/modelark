"""Disposable source attachments only; readers never retrieve or repair archives."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from modelark.slice import domain as d
from modelark.slice.transaction import TransferRefusal
from test_slice_domain import facts


@pytest.fixture
def attachment(tmp_path):
    mount = tmp_path / "usb"
    root = mount / "modelark"
    (root / ".git").mkdir(parents=True)
    (root / ".git/config").write_text("[annex]\n uuid = annex-a\n")
    (root / "org/model").mkdir(parents=True)
    (root / "org/model/model.safetensors").write_bytes(b"originaldata")
    snapshot = facts(d)
    candidate = d.SourceEvidence(snapshot.copies[0], snapshot.drives[0], snapshot.anchors[0],
                                 "ingestion_computed")
    from modelark.slice.linux import BoundTree
    with BoundTree(root) as tree:
        mount_id = tree.mount_id
    evidence = SimpleNamespace(fs_uuid="fs-a", serial="serial-a", total_bytes=1000,
                               device_id="usb", mount_id=mount_id, mount_path=str(mount))

    class Observer:
        def __init__(self):
            from modelark.slice.local_source import _identity
            self.identity = _identity(evidence)

        def observe(self, path, **kwargs):
            return evidence

        def check_attachment(self, tree, observed):
            from modelark.slice.local_source import _identity
            tree.check()
            if _identity(evidence) != self.identity:
                raise TransferRefusal("SOURCE_CHANGED", "synthetic attachment proof changed")

    return root, candidate, Observer(), evidence


def test_raw_original_bytes_without_any_subprocess(attachment, monkeypatch):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("no subprocess"))
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(20) == b"originaldata"
        assert stream.read(20) == b""


@pytest.mark.parametrize("path_kind", ["relative", "relative_parent", "nested_parent", "tilde"])
def test_explicit_attachment_paths_are_expanded_before_observation(attachment, monkeypatch, path_kind):
    from pathlib import Path
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, evidence = attachment
    if path_kind == "relative":
        monkeypatch.chdir(root.parent.parent)
        supplied = "usb/modelark"
    elif path_kind == "relative_parent":
        monkeypatch.chdir(root.parent)
        supplied = "../usb/modelark"
    elif path_kind == "nested_parent":
        monkeypatch.chdir(root.parent.parent)
        supplied = "unused/../usb/modelark/../modelark"
    else:
        supplied = "~/usb/modelark"
        expanduser = Path.expanduser
        # Resolve a synthetic home without modifying HOME or creating real home files.
        monkeypatch.setattr(Path, "expanduser", lambda path:
                            root if str(path) == supplied else expanduser(path))
    observed = []

    def observe(path, **kwargs):
        observed.append(path)
        return evidence

    monkeypatch.setattr(observer, "observe", observe)
    with LocalArchiveReader({"drive-a": supplied}, observer=observer).open(candidate) as stream:
        assert stream.read(20) == b"originaldata"
    assert observed and all(path == root for path in observed)


def test_lexical_parent_normalization_never_resolves_remaining_attachment_symlinks(attachment, monkeypatch):
    from pathlib import Path
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, evidence = attachment
    alias = root.parent.parent / "alias"
    alias.symlink_to(root.parent, target_is_directory=True)
    evidence.mount_path = str(alias)
    monkeypatch.chdir(root.parent.parent)
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("no symlink resolution"))
    reader = LocalArchiveReader({"drive-a": "unused/../alias/modelark"}, observer=observer)
    assert reader.attachments["drive-a"] == alias / "modelark"
    with pytest.raises(TransferRefusal, match="SOURCE_PATH_UNSAFE"):
        with reader.open(candidate):
            pytest.fail("remaining attachment symlink followed")


@pytest.mark.parametrize("chunk_size", [1, 6, 64])
def test_source_inventory_and_annex_proof_are_per_artifact_not_per_chunk(attachment, monkeypatch, chunk_size):
    from modelark.slice import local_source
    root, candidate, observer, evidence = attachment
    observed, configs = [], []
    annex_uuid = local_source._annex_uuid
    monkeypatch.setattr(observer, "observe", lambda path, **kwargs: observed.append(path) or evidence)
    monkeypatch.setattr(local_source, "_annex_uuid", lambda tree: configs.append(tree.path) or annex_uuid(tree))
    with local_source.LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        pieces = []
        while chunk := stream.read(chunk_size):
            pieces.append(chunk)
    assert b"".join(pieces) == b"originaldata"
    assert len(observed) == 2
    assert len(configs) == 1


def test_changed_observation_during_artifact_open_is_refused(attachment, monkeypatch):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, evidence = attachment
    calls = []

    def observe(path, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            evidence.serial = "replaced-device"
        return evidence

    monkeypatch.setattr(observer, "observe", observe)
    with pytest.raises(TransferRefusal, match="SOURCE_CHANGED"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            pytest.fail("changed attachment yielded bytes")


def test_multi_megabyte_source_has_constant_full_inventory_count(attachment, monkeypatch):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, evidence = attachment
    payload = b"a" * (3 << 20)
    (root / "org/model/model.safetensors").write_bytes(payload)
    candidate = replace(candidate, copy=replace(candidate.copy, orig_bytes=len(payload), stored_bytes=len(payload)))
    calls = []
    monkeypatch.setattr(observer, "observe", lambda path, **kwargs: calls.append(path) or evidence)
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(1 << 20) == payload[:1 << 20]
        assert stream.read(1 << 20) == payload[:1 << 20]
        assert stream.read(1 << 20) == payload[:1 << 20]
        assert stream.read(1 << 20) == b""
    assert len(calls) == 2


def test_source_observer_and_descriptor_must_be_same_attachment(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, evidence = attachment
    evidence.mount_id = -999
    with pytest.raises(TransferRefusal, match='SOURCE_CHANGED'):
        with LocalArchiveReader({'drive-a': root}, observer=observer).open(candidate):
            pytest.fail('unrelated attachment evidence accepted')


def test_unattached_archive_is_waiting(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    _, candidate, observer, _ = attachment
    with pytest.raises(TransferRefusal, match="WAITING_SOURCE"):
        with LocalArchiveReader({}, observer=observer).open(candidate):
            pytest.fail("unattached source opened")


@pytest.mark.parametrize("field", ["fs_uuid", "serial", "total_bytes", "mount_id"])
def test_identity_changes_refuse_before_next_read(attachment, field):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, evidence = attachment
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        setattr(evidence, field, 2000 if field == "total_bytes" else "changed")
        with pytest.raises(TransferRefusal, match="SOURCE_CHANGED"):
            stream.read(20)


def test_annex_uuid_change_refused(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    (root / ".git/config").write_text("[annex]\n uuid = other\n")
    with pytest.raises(TransferRefusal, match="SOURCE_CHANGED"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            pytest.fail("wrong repository opened")


def test_annex_link_confined_and_key_bound(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    key = candidate.copy.annex_key
    obj = root / ".git/annex/objects/aaa/bbb" / key / key
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"originaldata")
    (root / "org/model/model.safetensors").unlink()
    (root / "org/model/model.safetensors").symlink_to("../../" + obj.relative_to(root).as_posix())
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(20) == b"originaldata"
    obj.unlink()
    with pytest.raises(TransferRefusal, match="SOURCE_MISSING"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            pytest.fail("missing annex object retrieved")


@pytest.mark.parametrize("target", ["../outside", "/etc/passwd", "other",
                                    ".git/annex/objects/aaa/bbb/wrong/wrong"])
def test_non_annex_symlinks_refused(attachment, target):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    (root / "org/model/model.safetensors").unlink()
    (root / "org/model/model.safetensors").symlink_to(target)
    with pytest.raises(TransferRefusal, match="SOURCE_PATH_UNSAFE"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            pytest.fail("unsafe symlink opened")


def test_parent_symlink_is_never_followed(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    (root / "org/model/alias").symlink_to(".")
    candidate = replace(candidate, copy=replace(candidate.copy, stored_relpath="alias/model.safetensors"))
    with pytest.raises(TransferRefusal, match="SOURCE_PATH_UNSAFE"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            pytest.fail("parent symlink followed")


def test_consumer_failure_is_not_translated_to_source(attachment):
    import errno
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    with pytest.raises(OSError) as caught:
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            raise OSError(errno.ENOSPC, "destination full")
    assert caught.value.errno == errno.ENOSPC


def test_archive_replacement_revokes_even_buffered_read(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(1) == b"o"
        root.rename(root.with_name("old"))
        root.mkdir()
        with pytest.raises(TransferRefusal, match="SOURCE_CHANGED"):
            stream.read(1)


def test_disappeared_bound_mount_is_a_source_missing_refusal(attachment, monkeypatch):
    from modelark.slice import linux
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, evidence = attachment
    mounts = set(linux._mount_ids())
    monkeypatch.setattr(linux, "_mount_ids", lambda: mounts)
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(1) == b"o"
        mounts.clear()  # Synthetic disappearance; no real mounts are changed.
        with pytest.raises(TransferRefusal, match="SOURCE_MISSING"):
            stream.read(1)
        mounts.add(evidence.mount_id)
        with pytest.raises(TransferRefusal, match="SOURCE_MISSING"):
            stream.read(1)  # A retained stale source cannot silently resume after reattachment.


def test_lost_backing_device_refuses_even_when_mount_remains(attachment, monkeypatch):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(1) == b"o"

        def gone(tree, observed):
            tree.check()  # The directory and mount still exist in this schedule.
            raise TransferRefusal("WAITING_DESTINATION", "backing USB device disappeared")

        monkeypatch.setattr(observer, "check_attachment", gone)
        with pytest.raises(TransferRefusal, match="SOURCE_MISSING"):
            stream.read(1)


def test_compressed_annex_content_yields_original_bytes(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    from modelark.streamznn import _zipnn
    import hashlib
    root, candidate, observer, _ = attachment
    data = bytes(128)
    blob = bytes(_zipnn(dtype="bfloat16", threads=1).compress(bytearray(data)))
    key = f"SHA256E-s{len(blob)}--{hashlib.sha256(blob).hexdigest()}.znn"
    obj = root / ".git/annex/objects/aaa/bbb" / key / key
    obj.parent.mkdir(parents=True)
    obj.write_bytes(blob)
    (root / "org/model/model.safetensors").unlink()
    (root / "org/model/model.safetensors").symlink_to("../../" + obj.relative_to(root).as_posix())
    candidate = replace(candidate, copy=replace(candidate.copy, compressed=True, annex_key=key,
                                                orig_bytes=len(data), stored_bytes=len(blob)))
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(128) == data
        assert stream.read(1) == b""


def test_repo_relative_path_cannot_be_shadowed_by_archive_root(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    (root / "model.safetensors").write_bytes(b"wrongcontent")
    (root / "different/model").mkdir(parents=True)
    (root / "different/model/model.safetensors").write_bytes(b"anothermodel")
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(20) == b"originaldata"
    (root / "org/model/model.safetensors").unlink()
    with pytest.raises(TransferRefusal, match="SOURCE_MISSING"):
        with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate):
            pytest.fail("root-level file must not substitute for missing repository copy")


def test_nested_stored_path_uses_repository_relative_annex_link(attachment):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    key = candidate.copy.annex_key
    obj = root / ".git/annex/objects/aaa/bbb" / key / key
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"originaldata")
    (root / "org/model/nested").mkdir()
    (root / "org/model/nested/model.safetensors").symlink_to(
        "../../../" + obj.relative_to(root).as_posix())
    candidate = replace(candidate, copy=replace(candidate.copy, stored_relpath="nested/model.safetensors"))
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(20) == b"originaldata"


@pytest.mark.parametrize("when", ["initial", "reading"])
@pytest.mark.parametrize("code,expected", [
    ("WAITING_DESTINATION", "SOURCE_MISSING"),
    ("DESTINATION_UNPROVEN", "SOURCE_IDENTITY_UNPROVEN"),
    ("DESTINATION_CHANGED", "SOURCE_CHANGED"),
    ("DESTINATION_CAPACITY_UNPROVEN", "SOURCE_IDENTITY_UNPROVEN"),
    ("FILESYSTEM_UNSUPPORTED", "SOURCE_IDENTITY_UNPROVEN"),
])
def test_shared_observer_refusals_are_source_side(attachment, monkeypatch, when, code, expected):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment

    def refuse(path, **kwargs):
        raise TransferRefusal(code, "synthetic observation refused")

    reader = LocalArchiveReader({"drive-a": root}, observer=observer)
    if when == "initial":
        monkeypatch.setattr(observer, "observe", refuse)
        with pytest.raises(TransferRefusal) as caught:
            with reader.open(candidate):
                pytest.fail("unproven source opened")
    else:
        with reader.open(candidate) as stream:
            assert stream.read(1) == b"o"
            monkeypatch.setattr(observer, "check_attachment", lambda *args: refuse(None))
            with pytest.raises(TransferRefusal) as caught:
                stream.read(1)
    assert caught.value.code == expected
    assert caught.value.detail == "synthetic observation refused"
