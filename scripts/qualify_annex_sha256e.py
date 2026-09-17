"""Disposable native SHA256E compatibility probe; never a production admission.

Only creates fresh /tmp repositories; no destination, catalog, network or cleanup.
The pinned annex-8 client is verified before execution. Unknown suffixes are
observations, not an implicitly admitted extension grammar. Run without -O.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tempfile
import traceback

from qualify_annex_publication import Protocol, TOOLS, require, sha


# Suffixes below are inputs, NOT inferred backend outputs. The report retains
# exact observed keys; tests pin the output after native qualification.
FILENAMES = (
    "config.json", "weights.safetensors", "weights.safetensors.znn",
    "weights.safetensors.zstd", "weights.gguf", "pytorch_model.bin",
    "weights.pt", "weights.pth", "weights.ckpt", "weights.pkl", "weights.pickle",
    "weights.onnx", "weights.npz", "weights.npy", "vocab.model", "vocab.vocab",
    "vocab.tiktoken", "template.jinja", "README.md", "notes.txt", "model.py",
    "preview.png", "preview.jpg", "vocab.tokenizer",
    "evaluation.yaml", "evaluation.yml", "mapped.blob", "tokenizer",
    ".gitignore", ".gitattributes", "nested/.eval/tasks.yaml",
    "case.JSON", "multi.part.extension", "unusual.a_b", "unicode.🐺",
    "weights.q4.gguf", "config.json.znn", "notes.txt.znn", "weights.gguf.znn",
    "bundle.tar.gz", "config.json.gz", "archive.zip", "archive.bz2", "model.zst",
    "punctuation-._+ 🐺.yaml", "line\nbreak.json", "tab\tname.py",
    "unicode.é", "extension.a-b", "extension.a+b", "extension.a b",
    "empty.json",
)

# Conservative candidate for production review, not automatic production
# admission. These exact ASCII suffixes were observed on the pinned profile;
# the native Unicode suffix observations deliberately remain outside this set.
QUALIFIED_ASCII_SUFFIXES = frozenset({
    "", ".json", ".znn", ".zstd", ".gguf", ".bin", ".pt", ".pth", ".ckpt",
    ".pkl", ".onnx", ".npz", ".npy", ".md", ".txt", ".py", ".png", ".jpg",
    ".yaml", ".yml", ".blob", ".JSON", ".q4.gguf", ".json.znn", ".txt.znn",
    ".gguf.znn", ".tar.gz", ".json.gz", ".zip", ".bz2", ".zst",
})


def parse_qualified_sha256e_key(key):
    """Executable parser proposal only; unknown suffixes fail explicitly."""
    match = re.fullmatch(r"SHA256E-s(0|[1-9][0-9]*)--([0-9a-f]{64})(.*)", key) if isinstance(key, str) else None
    require(match is not None and match[3] in QUALIFIED_ASCII_SUFFIXES,
            "unqualified SHA256E key")
    return int(match[1]), match[2]


class SHA256E(Protocol):
    def __init__(self):
        # Explicit /tmp, independent of any operator TMPDIR. No user HOME edits.
        self.root = Path(tempfile.mkdtemp(prefix="modelark-sha256e-qualification-", dir="/tmp"))
        self.checks = []
        self.details = {}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        # Necessary fixture-only isolation from global/system filters and routing.
        self.env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

    def git(self, repo, *args, data=None, ok=True):
        Path(repo).resolve().relative_to(self.root.resolve())
        result = subprocess.run([
            "/usr/bin/git", "-c", "user.name=ModelArk SHA256E qualification",
            "-c", "user.email=qualification@invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
            "-c", "core.excludesFile=/dev/null", "-c", "protocol.allow=never",
            "-C", str(repo), *args,
        ], input=data, capture_output=True, env=self.env)
        require(not ok or result.returncode == 0,
                f"native command failed: {args!r}: {result.stderr.decode(errors='replace')}")
        return result

    def init(self, name):
        self.tools()
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "annex", "init", "--version=8", "--quiet", name)
        self.git(repo, "config", "annex.backend", "SHA256E")
        # Deliberate fixture-only permission to observe legacy hidden basename
        # keys. This is not the qualified production neutral mapping policy.
        self.git(repo, "config", "annex.dotfiles", "true")
        (repo / ".git/info/attributes").write_text(
            "** filter=annex -text -ident !working-tree-encoding !eol "
            "annex.largefiles=anything annex.backend=SHA256E\n")
        require(self.out(repo, "config", "annex.version") == "8", "format drift")
        return repo

    def pointer(self, repo, stored, key, data):
        tree_record = self.git(repo, "ls-tree", "-z", "HEAD", "--", stored).stdout
        require(tree_record.endswith(b"\0") and tree_record.count(b"\0") == 1,
                "tree entry framing")
        header, name = tree_record[:-1].split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        require(name.decode() == stored and kind == "blob", "wrong tree entry")
        blob = self.git(repo, "cat-file", "blob", oid).stdout
        object_path = self.out(repo, "annex", "examinekey", "--format=${objectpath}", key)
        require(re.fullmatch(r"\.git/annex/objects/[A-Za-z0-9]{2}/[A-Za-z0-9]{2}/" +
                             re.escape(key) + "/" + re.escape(key), object_path) is not None,
                "unexpected native object grammar")
        require((repo / object_path).read_bytes() == data, "object bytes changed")
        require((repo / stored).read_bytes() == data, "mapped bytes changed")
        if mode == "120000":
            expected = posixpath.relpath(object_path, str(PurePosixPath(stored).parent)).encode()
        else:
            require(mode == "100644", "unexpected pointer mode")
            expected = f"/annex/objects/{key}\n".encode()
        require(blob == expected, "pointer is not exact native representation")
        require(sha(blob) != sha(data), "raw payload committed")
        return {"mode": mode, "blob_hex": blob.hex(), "object_path": object_path,
                "object_sha256": sha((repo / object_path).read_bytes())}

    def cases(self):
        repo = self.init("native-sha256e")
        observations = []
        for index, name in enumerate(FILENAMES):
            # Each case has its own parent to prevent .gitignore/.gitattributes
            # fixture bytes from applying policy to other probe names.
            stored = f"case-{index}/{name}"
            data = (b"" if name == "empty.json" else
                    f"synthetic metadata {index}\r\n".encode() + b"\xe9\x00exact original bytes\n")
            path = repo / stored
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            self.git(repo, "annex", "add", "--force", "--force-large", "--", stored)
            lookup = self.git(repo, "annex", "lookupkey", "--", stored, ok=False)
            lookup_supported = lookup.returncode == 0 and bool(lookup.stdout.strip())
            # The pinned client cannot lookupkey certain literal filenames. Its
            # NUL-framed Git index is independently authoritative for the actual
            # staged pointer; object bytes and native examinekey are still checked.
            index_record = self.git(repo, "ls-files", "--stage", "-z", "--", stored).stdout
            require(index_record.endswith(b"\0") and index_record.count(b"\0") == 1,
                    "index framing")
            index_header, index_name = index_record[:-1].split(b"\t", 1)
            mode, oid, stage = index_header.decode().split()
            require(mode == "120000" and stage == "0" and index_name.decode() == stored,
                    "add did not produce exact locked index entry")
            index_blob = self.git(repo, "cat-file", "blob", oid).stdout
            key = index_blob.decode().rsplit("/", 1)[-1]
            if lookup_supported:
                require(lookup.stdout.strip().decode() == key, "lookupkey/index mismatch")
            prefix = f"SHA256E-s{len(data)}--{sha(data)}"
            require(key.startswith(prefix), "SHA256E size/digest not exact")
            suffix = key[len(prefix):]
            require(not any(char in key for char in "\0/\\\n\r"), "unsafe native key")
            self.git(repo, "commit", "-qm", f"locked fixture {index}")
            locked = self.pointer(repo, stored, key, data)
            require(locked["mode"] == "120000", "initial add was not locked")
            self.git(repo, "annex", "unlock", "--", stored)
            self.git(repo, "commit", "-qm", f"unlocked fixture {index}")
            unlocked = self.pointer(repo, stored, key, data)
            require(unlocked["mode"] == "100644", "unlock did not commit unlocked pointer")
            require(not path.is_symlink(), "unlock retained symlink")
            self.git(repo, "annex", "lock", "--", stored)
            self.git(repo, "commit", "-qm", f"relocked fixture {index}")
            relocked = self.pointer(repo, stored, key, data)
            require(relocked == locked, "relock changed pointer or object")
            final_lookup = self.git(repo, "annex", "lookupkey", "--", stored, ok=False)
            if lookup_supported:
                require(final_lookup.returncode == 0 and final_lookup.stdout.strip().decode() == key,
                        "key changed through lock/unlock")
            observations.append({"filename": name, "stored_path": stored, "key": key,
                                 "suffix": suffix, "size": len(data), "sha256": sha(data),
                                 "argument_lookupkey_supported": lookup_supported,
                                 "locked": locked, "unlocked": unlocked})
            self.check(f"sha256e-{name}-exact-bytes-and-committed-lock-unlock-relock")
        require(self.out(repo, "config", "annex.version") == "8", "final format drift")
        self.details = {"observations": observations, "tools": TOOLS,
                        "final_repository_format": 8, "production_profile_qualified": False,
                        "fixture_annex_dotfiles": True,
                        "candidate_exact_ascii_suffixes": sorted(QUALIFIED_ASCII_SUFFIXES),
                        "unknown_suffixes_implicitly_admitted": False}
        return self.details


def main():
    if not __debug__:
        raise RuntimeError("Qualification requires assertions enabled")
    probe = SHA256E()
    result = {"status": "running", "root": str(probe.root), "checks": probe.checks,
              "live_archive_or_catalog_access": False, "stage_1_complete": False}
    try:
        result["details"] = probe.cases()
        result["status"] = "passed"
    except Exception:
        result["status"] = "failed"
        result["failure"] = traceback.format_exc()
    result["source_sha256"] = {
        path.name: sha(path.read_bytes()) for path in
        (Path(__file__), Path(__file__).with_name("qualify_annex_publication.py"),
         Path(__file__).with_name("qualify_annex_payload.py"))}
    output = probe.root / "sha256e-result.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "checks": len(probe.checks), "result": str(output)}))
    if result["status"] != "passed":
        print(result["failure"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
