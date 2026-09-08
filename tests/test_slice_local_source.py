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
    evidence = SimpleNamespace(fs_uuid="fs-a", serial="serial-a", total_bytes=1000,
                               device_id="usb", mount_id="123", mount_path=str(mount))

    class Observer:
        def observe(self, path, **kwargs):
            return evidence

    return root, candidate, Observer(), evidence


def test_raw_original_bytes_without_any_subprocess(attachment, monkeypatch):
    from modelark.slice.local_source import LocalArchiveReader
    root, candidate, observer, _ = attachment
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("no subprocess"))
    with LocalArchiveReader({"drive-a": root}, observer=observer).open(candidate) as stream:
        assert stream.read(20) == b"originaldata"
        assert stream.read(20) == b""


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
            monkeypatch.setattr(observer, "observe", refuse)
            with pytest.raises(TransferRefusal) as caught:
                stream.read(1)
    assert caught.value.code == expected
    assert caught.value.detail == "synthetic observation refused"
