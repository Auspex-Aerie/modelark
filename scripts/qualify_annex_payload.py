"""Stage-0 experiment ONLY: tiny synthetic repositories and catalogs in fresh /tmp dirs.

This is not a migration implementation. It accepts no archive/catalog destination and
retains its fixtures. No network remotes, global config edits, live app CLI, or cleanup.
Run from the checkout with the development Python. Assertions require normal (not -O) Python.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace


PROJECT = Path(__file__).resolve().parents[1]
PREFIX = "org/repo/__modelark_payload_v1__/"
ORIGINAL = "org/repo/.gitattributes"
ATTRS = b"* filter=qualification text eol=lf working-tree-encoding=ISO-8859-1 annex.largefiles=nothing\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Experiment:
    def __init__(self, annex_directory=None):
        self.root = Path(tempfile.mkdtemp(prefix="modelark-annex-payload-stage0-"))
        self.checks = []
        # Required fixture isolation: inherited Git routing must never redirect
        # these commands to the operator's index/repo/object store. Do not alter HOME.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        if annex_directory is not None:
            directory = Path(annex_directory).resolve(strict=True)
            if not (directory / "git-annex").is_file():
                raise ValueError("Standalone directory lacks git-annex")
            # Process-local version selection, not a system PATH/config change.
            self.env["PATH"] = str(directory) + os.pathsep + self.env.get("PATH", "")

    def git(self, repo, *args, data=None, ok=True):
        Path(repo).resolve().relative_to(self.root.resolve())
        cmd = ["git", "-c", "user.name=ModelArk qualification", "-c",
               "user.email=qualification@invalid", "-c", "commit.gpgsign=false",
               "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
               "-c", "core.excludesFile=/dev/null", "-C", str(repo), *args]
        result = subprocess.run(cmd, input=data, capture_output=True, env=self.env)
        if ok and result.returncode:
            raise RuntimeError(f"{args}: {result.stderr.decode(errors='replace')}")
        return result

    def out(self, repo, *args, **kw):
        return self.git(repo, *args, **kw).stdout.decode().strip()

    def check(self, name, **details):
        self.checks.append({"name": name, "status": "passed", **details})

    def init(self, name):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "annex.largefiles", "anything")
        self.git(repo, "config", "annex.backend", "SHA256")
        self.git(repo, "config", "filter.qualification.clean", "tr a-z A-Z")
        self.git(repo, "config", "filter.qualification.smudge", "cat")
        # Request format 8, but record actual final formats too: newer clients may
        # automatically upgrade during later operations. Never infer compatibility.
        self.git(repo, "annex", "init", "--version=8", "--quiet", name)
        assert self.out(repo, "config", "annex.version") == "8"
        path = repo / ORIGINAL
        path.parent.mkdir(parents=True)
        path.write_bytes(ATTRS)
        # Seed an exact historical Git blob without running the hostile filter.
        oid = self.out(repo, "hash-object", "-w", "--stdin", "--no-filters", data=ATTRS)
        self.git(repo, "update-index", "--add", "--cacheinfo", "100644", oid, ORIGINAL)
        self.git(repo, "commit", "-qm", "synthetic old Git payload")
        return repo

    def isolate(self, repo):
        # Highest-precedence, exact fixture namespace; not global annex.dotfiles.
        (repo / ".git/info/attributes").write_text(
            f"{PREFIX}** filter=annex -text -ident !working-tree-encoding !eol "
            "annex.largefiles=anything annex.backend=SHA256\n")

    def add(self, repo, logical, content):
        relative = PREFIX + "p-" + sha(logical.encode()) + ".blob"
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Fixture no-clobber reservation. This is NOT the production publication protocol.
        with path.open("xb") as out:
            out.write(content)
        self.git(repo, "annex", "add", "--force", "--force-large", "--", relative)
        key = self.out(repo, "annex", "lookupkey", "--", relative)
        assert key == f"SHA256-s{len(content)}--{sha(content)}", key
        location = self.out(repo, "annex", "contentlocation", key)
        assert (repo / location).read_bytes() == content
        assert path.read_bytes() == content
        return relative, key

    def representation(self):
        repo = self.init("source")
        sample = b"lowercase\r\n\xe9 and metadata\r\n"
        relative = PREFIX + "probe.blob"
        raw = self.out(repo, "hash-object", "--stdin", "--no-filters", data=sample)
        transformed = self.out(repo, "hash-object", "--stdin", f"--path={relative}", data=sample)
        assert transformed != raw, "negative control did not exercise inherited attributes"
        self.check("negative-control-neutral-name-does-not-isolate-attributes")
        # Returning clone starts with ordinary Git bytes, before any neutral path exists.
        clone = self.root / "returning"
        self.git(self.root, "clone", "--no-local", "--quiet", str(repo), str(clone))
        self.git(clone, "annex", "init", "--version=8", "--quiet", "returning")
        # Record original bytes first: no checkout of the later retirement tree yet.
        assert (clone / ORIGINAL).read_bytes() == ATTRS
        self.isolate(repo)
        isolated = self.out(repo, "hash-object", "--stdin", f"--path={relative}", data=sample)
        assert isolated == raw
        self.check("info-attributes-isolate-filter-encoding-eol-during-duplicate-interval")
        # Force is required for an ignored neutral namespace; scope remains exact.
        (repo / "org/repo/.gitignore").write_text("__modelark_payload_v1__/\n")
        cases = [(".gitattributes", ATTRS), (".gitignore", b"*.bin\n"),
                 (".eval/nested.yaml", sample), ("nested/.gitignore", b"cache/\n"),
                 ("-odd\t\n🐺.yaml", b"odd name\n")]
        manifests = []
        for logical, content in cases:
            path, key = self.add(repo, logical, content)
            assert (repo / ORIGINAL).read_bytes() == ATTRS
            self.git(repo, "annex", "unlock", "--", path)
            assert not (repo / path).is_symlink() and (repo / path).read_bytes() == content
            self.git(repo, "annex", "lock", "--", path)
            assert (repo / path).read_bytes() == content
            manifests.append({"original": logical, "stored": path, "key": key,
                              "sha256": sha(content), "size": len(content)})
        self.git(repo, "commit", "-qm", "synthetic additive payload mapping")
        for entry in manifests:
            committed = self.git(repo, "cat-file", "blob", f"HEAD:{entry['stored']}").stdout
            assert entry["key"].encode() in committed
            assert sha(committed) != entry["sha256"], "raw payload leaked into Git tree"
        self.check("five-original-names-exact-sha256-locked-unlocked-and-ignore-refusal-override")
        # Ordinary tracked Git add may exit successfully but provide no annex key.
        control = self.init("tracked-negative-control")
        before_index = self.out(control, "write-tree")
        self.git(control, "annex", "add", "--", ORIGINAL)
        result = self.git(control, "annex", "lookupkey", "--", ORIGINAL, ok=False)
        assert not result.stdout.strip()
        self.check("successful-add-of-tracked-dotfile-does-not-prove-annex-key",
                   index_changed=self.out(control, "write-tree") != before_index)
        return repo, clone, manifests

    def portable_and_refs(self, repo, clone, manifests):
        old = self.out(clone, "rev-parse", "HEAD")
        entry = manifests[0]
        # Object transfer alone: destination still has old tree and no mapped path.
        self.git(repo, "remote", "add", "returning", str(clone))
        self.git(repo, "annex", "copy", f"--key={entry['key']}", "--to=returning")
        location = self.out(clone, "annex", "contentlocation", entry["key"])
        assert (clone / location).read_bytes() == ATTRS
        assert not (clone / entry["stored"]).exists()
        assert (clone / ORIGINAL).read_bytes() == ATTRS
        self.check("key-copy-has-object-but-no-mapped-worktree-path")
        # Dedicated ref is a portable test manifest, NOT an implemented trusted registry.
        manifest = json.dumps({"version": 1, "library": "synthetic", "files": manifests},
                              sort_keys=True).encode()
        blob = self.out(repo, "hash-object", "-w", "--stdin", data=manifest)
        ref = "refs/modelark/qualification-payload-v1"
        self.git(repo, "update-ref", ref, blob, "0" * 40)
        self.git(clone, "fetch", "--quiet", "origin", f"{ref}:{ref}")
        received = self.git(clone, "cat-file", "blob", ref).stdout
        assert sha(received) == sha(manifest)
        assert json.loads(received)["files"][0] == entry
        self.check("dedicated-manifest-ref-portable-without-file-tree-checkout")
        # Model the attended pre-sync preservation step, not a generic auto-sync.
        self.isolate(clone)
        path = clone / entry["stored"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((clone / ORIGINAL).read_bytes())
        self.git(clone, "annex", "add", "--force-large", "--", entry["stored"])
        assert self.out(clone, "annex", "lookupkey", "--", entry["stored"]) == entry["key"]
        assert path.read_bytes() == ATTRS
        self.git(clone, "commit", "-qm", "synthetic local preservation before retirement")
        new = self.out(clone, "rev-parse", "HEAD")
        self.git(clone, "update-ref", "refs/modelark/qualification-cutover", old)
        self.git(clone, "update-ref", "refs/modelark/qualification-cutover", new, old)
        stale = self.git(clone, "update-ref", "refs/modelark/qualification-cutover", old, old, ok=False)
        assert stale.returncode and self.out(clone, "rev-parse", "refs/modelark/qualification-cutover") == new
        assert path.read_bytes() == ATTRS and (clone / ORIGINAL).read_bytes() == ATTRS
        self.check("local-source-preserved-before-tree-sync-and-stale-ref-cas-refused")
        # Native multi-ref atomicity negative control: failure of the second CAS
        # must not publish the first update. This does not simulate crash durability.
        transaction = (f"start\nupdate refs/modelark/qualification-cutover {old} {new}\n"
                       f"update {ref} {blob} {'0' * 40}\nprepare\ncommit\n").encode()
        failed = self.git(clone, "update-ref", "--stdin", data=transaction, ok=False)
        assert failed.returncode
        assert self.out(clone, "rev-parse", "refs/modelark/qualification-cutover") == new
        assert self.out(clone, "rev-parse", ref) == blob
        self.check("failed-second-ref-cas-leaves-entire-ref-transaction-unchanged")
        self.consumers(clone, entry)
        self.map_tree(repo, old, manifests)

    def map_tree(self, source, old, manifests):
        # Exact metadata-only publication rehearsal. Retire just this synthetic
        # original; it remains recoverable in old commit and the returning clone.
        assert (source / ORIGINAL).read_bytes() == ATTRS
        assert self.git(source, "cat-file", "blob", f"HEAD:{ORIGINAL}").stdout == ATTRS
        # Git rm's filtered dirty check can disagree even when raw bytes match.
        # Remove only the proven index entry, then the exact synthetic live path.
        self.git(source, "update-index", "--force-remove", "--", ORIGINAL)
        (source / ORIGINAL).unlink()
        self.git(source, "commit", "-qm", "synthetic old-path retirement")
        new = self.out(source, "rev-parse", "HEAD")
        changed = set(self.out(source, "diff-tree", "--no-commit-id", "--name-only", "-r", old, new).splitlines())
        assert changed == {ORIGINAL, *(x["stored"] for x in manifests)}, changed
        target = self.root / "map"
        self.git(self.root, "clone", "--no-local", "--no-checkout", "--quiet", str(source), str(target))
        self.git(target, "annex", "init", "--version=8", "--quiet", "synthetic-map")
        self.isolate(target)
        self.git(target, "checkout", "--quiet", "--detach", old)
        old_index = self.out(target, "write-tree")
        self.git(target, "update-ref", "HEAD", new, old)
        # Important crash boundary: publishing a ref does NOT publish its index/worktree.
        assert self.out(target, "write-tree") == old_index
        assert (target / ORIGINAL).read_bytes() == ATTRS
        self.git(target, "read-tree", "--reset", "-u", new)
        assert self.out(target, "write-tree") == self.out(target, "rev-parse", f"{new}^{{tree}}")
        assert not (target / ORIGINAL).exists()
        for entry in manifests:
            assert self.out(target, "annex", "lookupkey", "--", entry["stored"]) == entry["key"]
            assert not self.out(target, "annex", "contentlocation", entry["key"], ok=False)
        self.check("exact-map-tree-delta-and-index-replay-without-annex-payload-transfer")

    def consumers(self, repo, entry):
        sys.path.insert(0, str(PROJECT))
        from modelark.slice.linux import BoundTree
        from modelark.slice.local_source import _open_content
        from modelark.restore import _materialize
        candidate = SimpleNamespace(copy=SimpleNamespace(
            repo_id="org/repo", stored_relpath=entry["stored"].removeprefix("org/repo/"),
            annex_key=entry["key"], stored_bytes=entry["size"]))
        for mode in ("lock", "unlock"):
            self.git(repo, "annex", mode, "--", entry["stored"])
            with BoundTree(repo) as tree:
                with os.fdopen(_open_content(tree, candidate), "rb") as content:
                    assert content.read() == ATTRS
            dest = self.root / f"restored-{mode}" / ".gitattributes"
            _materialize(repo / entry["stored"], dest, {"compressed": False}, entry["sha256"])
            assert dest.read_bytes() == ATTRS
        self.check("actual-slice-confined-opener-and-restore-materializer-read-mapped-locked-unlocked")

    def reader_floor(self):
        # Process-private module configuration points ONLY at this new synthetic directory.
        sys.path.insert(0, str(PROJECT))
        from modelark.core import db
        from modelark.slice import catalog, domain
        for reader in ("core-ro", "core-rw", "slice"):
            root = self.root / reader
            root.mkdir()
            db.configure(data_dir=root, state_dir=root / "state")
            path = root / "catalog.sqlite"
            con = sqlite3.connect(path)
            con.execute("PRAGMA user_version=9")
            con.close()
            before = path.read_bytes()
            try:
                if reader == "slice":
                    catalog.read_catalog(path, domain.SliceSpec(
                        repo_ids=("qualification/tiny",), destination_id="synthetic",
                        destination_root="qualification"))
                else:
                    opened = db.connect(read_only=reader == "core-ro")
                    opened.close()
            except (RuntimeError, domain.SliceRefusal) as exc:
                if reader == "slice":
                    assert isinstance(exc, domain.SliceRefusal) and exc.code == "CATALOG_VERSION_UNSUPPORTED"
                else:
                    assert "newer than this ModelArk build (v8)" in str(exc), str(exc)
            else:
                raise AssertionError(f"{reader} accepted v9")
            assert path.read_bytes() == before
            self.check(f"current-{reader}-refuses-v9-before-catalog-mutation")

    def run(self):
        version = self.out(self.root, "annex", "version")
        status, error = "passed-bounded-probes", None
        try:
            repo, clone, manifest = self.representation()
            self.portable_and_refs(repo, clone, manifest)
            self.reader_floor()
        except Exception as exc:
            status, error = "failed", repr(exc)
        report = {"status": status, "error": error, "git_annex_version": version,
                  "checks": self.checks, "retained_directory": str(self.root),
                  "physical_USB_or_archive_test": False, "stage_0_complete": False,
                  "archive_repository_formats": {
                      p.name: self.out(p, "config", "annex.version", ok=False)
                      for p in self.root.iterdir() if (p / ".git").is_dir()},
                  "not_proven": ["mixed-binary interoperation", "full ref/annex-metadata crash protocol",
                                 "production admission guards", "trusted ownership validation",
                                 "public Slice/restore workflow and authority",
                                 "v7/v8 to new-schema migration"]}
        (self.root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0 if error is None else 1


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Assertions required: do not use -O")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annex-directory", type=Path, help="Isolated standalone package; no install")
    raise SystemExit(Experiment(parser.parse_args().annex_directory).run())
