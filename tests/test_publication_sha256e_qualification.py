"""Pinned native SHA256E observations and conservative parser proposal.

These disposable tests do not enable the production profile or its runtime
writers. Native repositories are fresh /tmp paths and retained for inspection.
"""
import importlib.util
from pathlib import Path
import sys
from unittest import mock

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    spec = importlib.util.spec_from_file_location("sha256e_qualification", SCRIPTS / "qualify_annex_sha256e.py")
    qualification = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qualification)


# These are pinned observed native outputs, not a filename-extension algorithm.
EXPECTED_SUFFIXES = {
    "config.json": ".json", "weights.safetensors": "", "weights.safetensors.znn": ".znn",
    "weights.safetensors.zstd": ".zstd", "weights.gguf": ".gguf", "pytorch_model.bin": ".bin",
    "weights.pt": ".pt", "weights.pth": ".pth", "weights.ckpt": ".ckpt", "weights.pkl": ".pkl",
    "weights.pickle": "", "weights.onnx": ".onnx", "weights.npz": ".npz", "weights.npy": ".npy",
    "vocab.model": "", "vocab.vocab": "", "vocab.tiktoken": "", "template.jinja": "",
    "README.md": ".md", "notes.txt": ".txt", "model.py": ".py", "preview.png": ".png",
    "preview.jpg": ".jpg", "vocab.tokenizer": "", "evaluation.yaml": ".yaml",
    "evaluation.yml": ".yml", "mapped.blob": ".blob", "tokenizer": "", ".gitignore": "",
    ".gitattributes": "", "nested/.eval/tasks.yaml": ".yaml", "case.JSON": ".JSON",
    "multi.part.extension": "", "unusual.a_b": "", "unicode.🐺": ".🐺",
    "weights.q4.gguf": ".q4.gguf", "config.json.znn": ".json.znn", "notes.txt.znn": ".txt.znn",
    "weights.gguf.znn": ".gguf.znn", "bundle.tar.gz": ".tar.gz", "config.json.gz": ".json.gz",
    "archive.zip": ".zip", "archive.bz2": ".bz2", "model.zst": ".zst",
    "punctuation-._+ 🐺.yaml": ".yaml", "line\nbreak.json": ".json", "tab\tname.py": ".py",
    "unicode.é": ".é", "extension.a-b": "", "extension.a+b": "", "extension.a b": "",
    "empty.json": ".json",
}


@pytest.fixture(scope="module")
def native():
    for path, digest in qualification.TOOLS.items():
        if not Path(path).is_file() or qualification.sha(Path(path).read_bytes()) != digest:
            pytest.skip(f"qualification requires pinned native tool: {path}")
    probe = qualification.SHA256E()
    result = probe.cases()
    assert len(probe.checks) == len(EXPECTED_SUFFIXES)
    assert result["final_repository_format"] == 8
    return result


@pytest.mark.parametrize("filename,suffix", EXPECTED_SUFFIXES.items())
def test_native_suffix_and_both_committed_representations(native, filename, suffix):
    observation = next(row for row in native["observations"] if row["filename"] == filename)
    assert observation["suffix"] == suffix
    assert observation["key"] == f"SHA256E-s{observation['size']}--{observation['sha256']}{suffix}"
    locked, unlocked = observation["locked"], observation["unlocked"]
    assert locked["mode"] == "120000"
    assert unlocked["mode"] == "100644"
    assert locked["object_path"] == unlocked["object_path"]
    assert locked["object_sha256"] == unlocked["object_sha256"] == observation["sha256"]
    assert bytes.fromhex(unlocked["blob_hex"]) == f"/annex/objects/{observation['key']}\n".encode()
    if suffix.isascii():
        assert qualification.parse_qualified_sha256e_key(observation["key"]) == (
            observation["size"], observation["sha256"])
    else:
        with pytest.raises(ValueError, match="unqualified SHA256E key"):
            qualification.parse_qualified_sha256e_key(observation["key"])


def test_candidate_suffix_allowlist_has_native_evidence_for_every_entry(native):
    from modelark.publication_policy import SHA256E_SUFFIXES
    actual = {row["suffix"] for row in native["observations"] if row["suffix"].isascii()}
    assert qualification.QUALIFIED_ASCII_SUFFIXES == actual == SHA256E_SUFFIXES
    assert native["production_profile_qualified"] is False
    assert native["unknown_suffixes_implicitly_admitted"] is False


def test_all_current_classifier_extensions_have_native_cases():
    from modelark import formats
    from modelark.compress import ZNN_SUFFIX
    for extension in (*formats.AUX_EXTS, *formats.PICKLE_EXTS, ".safetensors", ".gguf", ZNN_SUFFIX):
        assert any(name.endswith(extension) for name in EXPECTED_SUFFIXES), extension


def test_newline_lookupkey_limitation_does_not_replace_index_or_byte_proof(native):
    unsupported = [row["filename"] for row in native["observations"]
                   if not row["argument_lookupkey_supported"]]
    assert unsupported == ["line\nbreak.json"]


@pytest.mark.parametrize("key", [
    None, "", "SHA256E-s01--" + "a" * 64, "SHA256E-s-1--" + "a" * 64,
    "SHA256E-s1--" + "A" * 64, "SHA256E-s1--" + "a" * 63,
    "SHA256-s1--" + "a" * 64 + ".json", "SHA512E-s1--" + "a" * 64 + ".json",
    *("SHA256E-s1--" + "a" * 64 + suffix for suffix in (
        ".unknown", ".json.exe", ".safetensors", ".q5.gguf", ".Yaml", ".🐺", ".é",
        ".json/extra", ".json\\extra", ".json\0", ".json\n", ".json ",
    )),
])
def test_proposed_grammar_refuses_unqualified_keys(key):
    with pytest.raises(ValueError, match="unqualified SHA256E key"):
        qualification.parse_qualified_sha256e_key(key)
