"""Pure FAT32 candidate layout tests: not filesystem qualification or writer authority."""
from dataclasses import FrozenInstanceError

import pytest

from modelark.slice import fat32_layout as fat, state
from modelark.slice.transaction import TransferRefusal


def admit(files=(("org/model/weights.bin", 12),), **changes):
    options = dict(output_root="delivery", parent_path="/exports", control_size=1024,
                   receipt_size=2048)
    options.update(changes)
    return fat.admit_layout(files, **options)


def test_candidate_layout_contains_artifacts_metadata_directories_and_temporary_paths():
    layout = admit()
    assert layout.execution_ready is False
    assert dict(layout.files) == {
        "delivery/org/model/weights.bin": 12,
        "delivery/.modelark-slice-owner": 1024,
        "delivery/.modelark-slice-receipt.json": 2048,
    }
    assert set(layout.directories) == {"delivery", "delivery/org", "delivery/org/model"}
    assert isinstance(layout.files, tuple)
    assert isinstance(layout.directories, tuple)
    assert isinstance(layout.temporary_paths, tuple)
    assert "delivery/org/model/.slice-" + "0" * 32 in layout.temporary_paths
    assert "delivery/.slice-" + "0" * 32 in layout.temporary_paths
    with pytest.raises(FrozenInstanceError):
        layout.files = ()


def test_accepts_generators_and_safe_ascii_internal_spaces_without_filesystem_access():
    files = ((name, size) for name, size in [("Org Name/Model-v1/weights_2.bin", 0)])
    result = admit(files, parent_path="/does-not-exist/exports", output_root="Delivery 1")
    assert ("Delivery 1/Org Name/Model-v1/weights_2.bin", 0) in result.files
    assert not result.execution_ready


@pytest.mark.parametrize("name", [
    "", ".", "..", " trailing", "trailing ", "trailing.",
    "a~1.bin", "a:b", "a\\b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b",
    "a\x00b", "a\x1fb", "a\x7fb", "a\tb", "a\nb", "café", "\udcff",
    "CON", "con.bin", "PrN.dat", "AUX", "NUL.txt", "CLOCK$", "CLOCK$.bin",
    "COM1", "com9.dat", "LPT1", "lPt9.bin", ".slice-anything", ".SLICE-abc",
])
def test_unsafe_candidate_component_refused_in_artifact_directory_leaf_and_output_root(name):
    for path in ("org/" + name, name + "/weights.bin"):
        with pytest.raises(TransferRefusal):
            admit(((path, 12),))
    with pytest.raises(TransferRefusal):
        admit(output_root=name)


@pytest.mark.parametrize("path", ["/absolute", "//double", "a//b", "a/./b", "a/../b", "a/",
                                 "./a", "../a"])
def test_artifact_paths_require_canonical_relative_components(path):
    with pytest.raises(TransferRefusal):
        admit(((path, 12),))


@pytest.mark.parametrize("files", [None, (), [], "org/model/a.bin", b"bytes",
                                  {"org/model/a.bin": 12}, (None,), (("a.bin",),),
                                  (("a.bin", 12, "extra"),), ((None, 12),), ((123, 12),)])
def test_empty_or_malformed_file_collections_are_not_candidate_layouts(files):
    with pytest.raises(TransferRefusal):
        admit(files)


@pytest.mark.parametrize("root", [None, 123, "a/b", "/absolute", "a//b"])
def test_output_root_must_be_one_component(root):
    with pytest.raises(TransferRefusal):
        admit(output_root=root)


@pytest.mark.parametrize("parent", ["relative", "", "//exports", "/a/../b", "/a/./b",
                                   "/a//b", "/exports/", "/bad\x00path", "/\udcff"])
def test_parent_path_requires_canonical_absolute_utf8(parent):
    with pytest.raises(TransferRefusal):
        admit(parent_path=parent)


def test_parent_path_is_not_subject_to_fat_artifact_ascii_restriction():
    # This is the host path to an existing parent, not a created FAT filename.
    assert not admit(parent_path="/media/utilisateur/clé").execution_ready
    assert not admit(parent_path="/").execution_ready


@pytest.mark.parametrize("files", [
    (("org/model/a.bin", 1), ("org/model/A.bin", 1)),
    (("org/model/a.bin", 1), ("ORG/model/b.bin", 1)),
    (("org/model/a.bin", 1), ("org/Model/b.bin", 1)),
    (("org/model", 1), ("org/model/a.bin", 1)),
    (("org/MODEL", 1), ("org/model/a.bin", 1)),
    (("org/model/a.bin", 1), ("org/model/a.bin", 1)),
    ((".modelark-slice-owner", 1),),
    ((".MODELARK-SLICE-OWNER", 1),),
    ((".modelark-slice-receipt.json", 1),),
    ((".MODELARK-SLICE-RECEIPT.JSON", 1),),
    ((".modelark-slice-owner/a.bin", 1),),
])
def test_entire_tree_case_collisions_and_generated_metadata_collisions_refused(files):
    with pytest.raises(TransferRefusal):
        admit(files)
    with pytest.raises(TransferRefusal):
        admit(tuple(reversed(files)))


def test_same_spelled_shared_directories_and_non_reserved_dos_lookalikes_are_allowed():
    files = (("org/model/COM0.bin", 1), ("org/model/COM10.bin", 1),
             ("org/model/LPT0.bin", 1), ("org/model/conversation.bin", 1))
    assert len(admit(files).files) == len(files) + 2


def test_maximum_file_size_includes_control_and_receipt_and_zero_byte_artifacts():
    assert fat.MAX_FILE_BYTES == (1 << 32) - 1
    layout = admit((("org/model/weights.bin", fat.MAX_FILE_BYTES),),
                   control_size=fat.MAX_FILE_BYTES, receipt_size=fat.MAX_FILE_BYTES)
    assert all(size == fat.MAX_FILE_BYTES for _, size in layout.files)
    assert ("delivery/org/model/empty.bin", 0) in admit((("org/model/empty.bin", 0),)).files


@pytest.mark.parametrize("size", [1 << 32, -1, True, False, 1.0, "12", None])
def test_invalid_or_oversized_payload_and_metadata_sizes_refused(size):
    with pytest.raises(TransferRefusal):
        admit((("org/model/weights.bin", size),))
    for field in ("control_size", "receipt_size"):
        with pytest.raises(TransferRefusal):
            admit(**{field: size})


def test_component_limit_accepts_255_and_rejects_256_in_all_created_positions():
    assert fat.MAX_COMPONENT == 255
    assert admit((("a" * 255, 12),), output_root="b" * 255)
    for path in ("a" * 256, "a" * 256 + "/weights.bin"):
        with pytest.raises(TransferRefusal):
            admit(((path, 12),))
    with pytest.raises(TransferRefusal):
        admit(output_root="b" * 256)


def parent_with_length(length):
    # Canonical absolute host components, each below NAME_MAX.
    count, remainder = divmod(length, 201)
    result = "/" + "/".join(["p" * 200] * count)
    if remainder:
        result += "/" + "q" * (remainder - 1) if count else "q" * (remainder - 1)
    if result.endswith("/") and len(result) > 1:
        result = result[:-1] + "q"
    assert len(result) == length
    return result


def test_total_path_limit_accounts_for_generated_temporary_name_and_nul():
    assert fat.MAX_PATH_BYTES == 4096
    suffix = "/delivery/org/model/.slice-" + "0" * 32
    parent = parent_with_length(fat.MAX_PATH_BYTES - 1 - len(suffix))
    layout = admit((("org/model/w", 1),), parent_path=parent)
    assert len(parent + "/" + max(layout.temporary_paths, key=len)) == 4095
    # All final names still fit; the generated temporary name is what crosses PATH_MAX.
    with pytest.raises(TransferRefusal):
        admit((("org/model/w", 1),), parent_path=parent + "q")


def test_total_path_limit_also_checks_final_names_longer_than_temporary_names():
    leaf = "w" * fat.MAX_COMPONENT
    suffix = "/delivery/org/model/" + leaf
    parent = parent_with_length(fat.MAX_PATH_BYTES - 1 - len(suffix))
    assert admit((("org/model/" + leaf, 1),), parent_path=parent)
    with pytest.raises(TransferRefusal):
        admit((("org/model/" + leaf, 1),), parent_path=parent + "q")


def test_parent_path_limit_counts_utf8_bytes_not_characters():
    suffix = "/delivery/org/model/.slice-" + "0" * 32
    parent = parent_with_length(fat.MAX_PATH_BYTES - 1 - len(suffix))
    # Replacing one ASCII character adds one UTF-8 byte without changing character count.
    with pytest.raises(TransferRefusal):
        admit((("org/model/w", 1),), parent_path=parent[:-1] + "é")


def test_layout_observation_cannot_be_created_as_a_legacy_executable_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "private")
    store = state.Store()
    with pytest.raises(TransferRefusal, match="LEGACY_PLAN"):
        store.create(admit(), None)
    with store._connection(write=False) as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
