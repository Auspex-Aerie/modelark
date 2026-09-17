"""Pinned local replica import in fresh /tmp fixtures; no production authority.

Qualifies setkey/fromkey semantics using synthetic copied bytes and the shared
byte/tree validators. No live catalog/archive, configured remote, network, global
configuration edit, destination argument, or cleanup. Run without -O.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import posixpath
import sys
import tempfile
import traceback

from qualify_annex_publication import TOOLS, require, sha
from qualify_annex_sha256e import SHA256E

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from modelark import publication_payload, publication_tree  # noqa: E402
from modelark.publication_policy import PublicationRefused  # noqa: E402
from modelark.slice.linux import BoundTree  # noqa: E402


MAPPED = "org/repo/__modelark_payload_v1__/p-replica.blob"
CONTENT = b"source original bytes\r\n\xe9\x00exact\n"


class Replica(SHA256E):
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="modelark-replica-qualification-", dir="/tmp"))
        self.checks, self.details = [], {}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        self.source = self.root / "source-original.json"
        self.source.write_bytes(CONTENT)
        self.counter = 0

    def init(self, name, *, ignored=False):
        self.tools()
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q", "--initial-branch=main")
        self.git(repo, "annex", "init", "--version=8", "--quiet", name)
        self.git(repo, "config", "annex.backend", "SHA256")
        self.git(repo, "commit", "--allow-empty", "-qm", "disposable baseline")
        (repo / ".git/info/exclude").write_text("org/repo/__modelark_payload_v1__/\n" if ignored else "")
        (repo / ".git/info/attributes").write_text(
            "org/repo/__modelark_payload_v1__/** filter=annex -text -ident "
            "!working-tree-encoding !eol annex.largefiles=anything annex.backend=SHA256\n")
        require((self.git(repo, "check-ignore", "--", MAPPED, ok=False).returncode == 0) == ignored,
                "target ignore control did not match")
        return repo

    def stage(self, data=CONTENT):
        self.counter += 1
        staged = self.root / f"owned-stage-{self.counter}"
        staged.write_bytes(data)
        return staged

    def object_path(self, repo, key):
        return self.out(repo, "annex", "examinekey", "--format=${objectpath}", key)

    def local(self, repo, key, *, path=MAPPED):
        # Script-only observation callback: this does not confer publication
        # authority. The companion tests also exercise real qualified scopes.
        with BoundTree(repo) as tree:
            return publication_payload.verify(
                tree, stored_path=path, annex_key=key, qualified_object_path=self.object_path(repo, key),
                binding_digest="a" * 64, require_scope=lambda: None)

    def baseline(self, repo):
        with BoundTree(repo) as tree:
            return publication_tree.capture(tree, read_git=lambda *a: self.git(repo, *a).stdout,
                                            require_scope=lambda: None)

    def committed(self, repo, key, before, *, path=MAPPED):
        obj = self.object_path(repo, key)
        pointer = posixpath.relpath(obj, str(Path(path).parent)).encode()
        oid = hashlib.sha1(b"blob " + str(len(pointer)).encode() + b"\0" + pointer).hexdigest()
        entry = publication_tree.TreeEntry(path, "120000", oid)
        with BoundTree(repo) as tree:
            return publication_tree.verify(
                tree, read_git=lambda *a: self.git(repo, *a).stdout, require_scope=lambda: None,
                before=before, allowed_delta={path: entry}, stored_path=path, annex_key=key,
                qualified_object_path=obj, binding_digest="a" * 64, profile_digest="b" * 64)

    def import_object(self, repo, key, staged, *, ok=True):
        return self.git(repo, "annex", "setkey", "--", key, str(staged), ok=ok)

    def fromkey(self, repo, key, *, path=MAPPED, force=True, ok=True):
        return self.git(repo, "annex", "fromkey", *(("--force",) if force else ()), "--", key, path, ok=ok)

    def cases(self):
        self.tools()
        keys = {"SHA256": f"SHA256-s{len(CONTENT)}--{sha(CONTENT)}",
                "SHA256E": f"SHA256E-s{len(CONTENT)}--{sha(CONTENT)}.json"}
        results = {"completed": [], "existing_targets": [], "wrong_content": [], "interruptions": [],
                   "exclusive_pointer": [], "new_fill_force_add": []}
        self.details = results
        for backend, key in keys.items():
            for force, ignored in ((False, False), (True, False), (False, True), (True, True)):
                repo = self.init(f"complete-{backend}-{force}-{ignored}", ignored=ignored)
                before = self.baseline(repo)
                staged = self.stage()
                self.import_object(repo, key, staged)
                require(not staged.exists(), "setkey did not consume owned staged input")
                require((repo / self.object_path(repo, key)).read_bytes() == CONTENT, "object bytes changed")
                mapped = self.fromkey(repo, key, force=force, ok=False)
                record = {"backend": backend, "force": force, "ignored": ignored,
                          "fromkey_exit": mapped.returncode,
                          "stdout": mapped.stdout.decode(errors="replace"),
                          "stderr": mapped.stderr.decode(errors="replace")}
                if mapped.returncode != 0:
                    require(ignored and mapped.returncode == 1 and b"ignored" in mapped.stderr,
                            "unexpected fromkey failure")
                    # Native fromkey creates a pointer before its non-forced Git
                    # staging fails. This mutation must remain a durable intent,
                    # not be mistaken for a clean no-op or successful import.
                    require(self.baseline(repo) == before, "failed fromkey changed index/tree")
                    payload = self.local(repo, key)
                    self.git(repo, "--literal-pathspecs", "add", "-f", "--", MAPPED)
                    record["explicit_owned_pointer_stage_required"] = True
                else:
                    require(not ignored, "native ignored-path behavior changed")
                    payload = self.local(repo, key)
                    record["explicit_owned_pointer_stage_required"] = False
                self.git(repo, "commit", "-qm", "replica pointer")
                tree = self.committed(repo, key, before)
                record.update(payload=payload.record(), tree=tree.record())
                results["completed"].append(record)
                self.check(f"{backend}-ignored-{ignored}-force-{force}-native-result-and-independent-proof")

        # Existing path occupancy is independent from object identity. Do not
        # assume --force means overwrite permission (or successful no-op).
        for kind in ("regular", "tracked", "directory", "symlink", "dangling"):
            repo = self.init("occupied-" + kind)
            key = keys["SHA256E"]
            self.import_object(repo, key, self.stage())
            destination = repo / MAPPED
            destination.parent.mkdir(parents=True)
            sentinel = repo / "sentinel"
            sentinel.write_bytes(b"preserve unrelated bytes")
            if kind in {"regular", "tracked"}:
                destination.write_bytes(b"occupied unrelated bytes")
            elif kind == "directory":
                destination.mkdir()
            else:
                destination.symlink_to(sentinel if kind == "symlink" else repo / "absent")
            if kind == "tracked":
                self.git(repo, "-c", "filter.annex.clean=cat", "add", "-f", "--", MAPPED)
                self.git(repo, "commit", "-qm", "occupied fixture")
            before_stat = destination.lstat()
            before_link = os.readlink(destination) if destination.is_symlink() else None
            before_bytes = destination.read_bytes() if kind in {"regular", "tracked"} else None
            before_index = (repo / ".git/index").read_bytes() if (repo / ".git/index").exists() else None
            result = self.fromkey(repo, key, ok=False)
            preserved = (destination.lstat().st_ino == before_stat.st_ino
                         and (not destination.is_symlink() or os.readlink(destination) == before_link)
                         and (before_bytes is None or destination.read_bytes() == before_bytes))
            index_preserved = ((repo / ".git/index").read_bytes() if (repo / ".git/index").exists() else None) == before_index
            require(preserved and index_preserved and sentinel.read_bytes() == b"preserve unrelated bytes",
                    "fromkey overwrote an occupied path or index")
            results["existing_targets"].append({"kind": kind, "exit": result.returncode,
                                                "path_and_index_preserved": preserved and index_preserved})
            self.check(f"occupied-{kind}-preserved-by-native-fromkey")

        # Plumbing may trust the supplied key. Record native behavior, then demand
        # independent bytes/size rejection regardless of native exit status.
        for wrong in (b"x" * len(CONTENT), b"too short"):
            repo = self.init("wrong-" + str(len(wrong)))
            key = keys["SHA256E"]
            staged = self.stage(wrong)
            result = self.import_object(repo, key, staged, ok=False)
            if result.returncode == 0:
                self.fromkey(repo, key)
                try:
                    self.local(repo, key)
                except PublicationRefused as exc:
                    refusal = exc.code
                else:
                    raise AssertionError("wrong setkey bytes passed independent payload proof")
            else:
                refusal = "native-setkey-refused"
            results["wrong_content"].append({"actual_size": len(wrong), "setkey_exit": result.returncode,
                                              "staging_exists": staged.exists(),
                                              "object_exists": (repo / self.object_path(repo, key)).exists(),
                                              "stderr": result.stderr.decode(errors="replace"),
                                              "independent_result": refusal})
            require(result.returncode == 0 or not (repo / self.object_path(repo, key)).exists(),
                    "failed setkey left a supposedly available object")
            self.check(f"wrong-setkey-content-{len(wrong)}-not-valid-publication")

        repo = self.init("interrupt-setkey-before-fromkey")
        key = keys["SHA256E"]
        before = self.baseline(repo)
        self.import_object(repo, key, self.stage())
        require(self.baseline(repo) == before, "setkey changed the file tree/index")
        try:
            self.local(repo, key)
        except PublicationRefused as exc:
            results["interruptions"].append({"phase": "after-setkey", "refusal": exc.code})
        else:
            raise AssertionError("object-only import incorrectly supplied readable target proof")
        self.fromkey(repo, key)
        self.local(repo, key)
        try:
            self.committed(repo, key, before)
        except PublicationRefused as exc:
            results["interruptions"].append({"phase": "after-fromkey-before-commit", "refusal": exc.code})
        else:
            raise AssertionError("uncommitted target incorrectly supplied a tree proof")
        self.git(repo, "commit", "-qm", "resume exact interrupted mapping")
        self.committed(repo, key, before)
        self.check("object-only-and-uncommitted-pointer-refuse-until-resumed-completion")

        repo = self.init("interrupt-fromkey-before-setkey")
        before = self.baseline(repo)
        self.fromkey(repo, key)
        require((repo / MAPPED).is_symlink() and not (repo / MAPPED).exists(), "expected dangling pointer")
        self.git(repo, "commit", "-qm", "pointer without bytes")
        self.committed(repo, key, before)
        try:
            self.local(repo, key)
        except PublicationRefused as exc:
            results["interruptions"].append({"phase": "fromkey-without-setkey", "refusal": exc.code})
        else:
            raise AssertionError("committed dangling pointer incorrectly supplied payload proof")
        self.import_object(repo, key, self.stage())
        self.local(repo, key)
        self.check("force-allows-content-absent-pointer-but-independent-payload-proof-refuses")

        # Alternative avoids fromkey's ignored-path partial-failure branch:
        # exact verified object -> exclusive descriptor-relative symlink ->
        # literal, forced Git staging. No ordinary byte/filter import is involved.
        for backend, key in keys.items():
            repo = self.init("exclusive-pointer-" + backend, ignored=True)
            before = self.baseline(repo)
            self.import_object(repo, key, self.stage())
            obj = self.object_path(repo, key)
            target = repo / MAPPED
            target.parent.mkdir(parents=True)
            link = posixpath.relpath(obj, str(Path(MAPPED).parent))
            with BoundTree(repo) as tree:
                with tree.parent(MAPPED) as (parent_fd, leaf):
                    os.symlink(link, leaf, dir_fd=parent_fd)
                    try:
                        os.symlink("unrelated", leaf, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    else:
                        raise AssertionError("exclusive symlink creation replaced occupied target")
            payload = self.local(repo, key)
            require(self.baseline(repo) == before, "exclusive symlink changed Git tree/index")
            self.git(repo, "--literal-pathspecs", "add", "-f", "--", MAPPED)
            self.git(repo, "commit", "-qm", "exclusive replica pointer")
            proof = self.committed(repo, key, before)
            results["exclusive_pointer"].append({"backend": backend, "key": key,
                                                  "payload": payload.record(), "tree": proof.record()})
            self.check(f"{backend}-exclusive-descriptor-symlink-force-stage-preserves-exact-key")

        # New-Fill command must independently override the ignore check. Native
        # annex add's force flag does so; fromkey's force flag above does not.
        for target_path in (MAPPED, "ignored/model.json"):
            repo = self.init("new-fill-" + ("neutral" if target_path == MAPPED else "ordinary"))
            (repo / ".gitignore").write_text("ignored/\norg/repo/__modelark_payload_v1__/\n")
            self.git(repo, "add", "--", ".gitignore")
            self.git(repo, "commit", "-qm", "fixture root ignore policy")
            require(self.git(repo, "check-ignore", "--", target_path, ok=False).returncode == 0,
                    "root ignore negative control failed")
            before = self.baseline(repo)
            destination = repo / target_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(CONTENT)
            self.git(repo, "annex", "add", "--force", "--force-large", "--backend=SHA256", "--", target_path)
            key = keys["SHA256"]
            payload = self.local(repo, key, path=target_path)
            self.git(repo, "commit", "-qm", "forced new Fill payload")
            proof = self.committed(repo, key, before, path=target_path)
            results["new_fill_force_add"].append({"path": target_path, "key": key,
                                                   "payload": payload.record(), "tree": proof.record()})
            self.check("new-fill-force-add-root-ignored-" + target_path)

        require(self.source.read_bytes() == CONTENT, "original source was consumed or altered")
        self.check("original-source-unchanged-only-disposable-owned-staging-consumed")
        formats = {str(path.parent.parent.relative_to(self.root)):
                   self.out(path.parent.parent, "config", "annex.version")
                   for path in self.root.rglob(".git/config")}
        require(set(formats.values()) == {"8"}, "final repository format drift")
        results.update(final_repo_formats=formats, tools=TOOLS,
                       production_writer_qualified=False, native_fromkey_overwrite_authorized=False,
                       original_source_sha256=sha(self.source.read_bytes()))
        return results


def main():
    if not __debug__:
        raise RuntimeError("Qualification requires assertions enabled")
    probe = Replica()
    result = {"status": "running", "root": str(probe.root), "checks": probe.checks,
              "live_archive_or_catalog_access": False, "network_access": False}
    try:
        probe.cases()
        result["status"] = "passed"
    except Exception:
        result["status"] = "failed"
        result["failure"] = traceback.format_exc()
    result["details"] = probe.details
    result["source_sha256"] = {str(path.relative_to(PROJECT)): sha(path.read_bytes()) for path in (
        Path(__file__), Path(__file__).with_name("qualify_annex_sha256e.py"),
        Path(__file__).with_name("qualify_annex_publication.py"),
        PROJECT / "modelark/publication_payload.py", PROJECT / "modelark/publication_tree.py",
        PROJECT / "modelark/publication_policy.py")}
    output = probe.root / "replica-pointer-result.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "checks": len(probe.checks), "result": str(output)}))
    if result["status"] != "passed":
        print(result["failure"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
