"""Stage-0 ONLY: pinned-tool and publication protocol probes in fresh /tmp fixtures.

No destination arguments, network remotes, archive/catalog access or cleanup. The
validators below are executable specifications, NOT production authority guards.
Run with the development Python, without -O. Fixtures and failures are retained.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import traceback

from qualify_annex_payload import ATTRS, ORIGINAL, PREFIX, Experiment, sha


TOOLS = {
    "/usr/bin/git": "5a39a7909c023f92a84b77b49e6b008f3f152b833135b96d73ac7c403314a88a",
    "/usr/bin/git-annex": "5de67e4fd40d011af9f99f563df80238b1d74c001c00cc1ed64227eb2e79ccc7",
    "/usr/bin/dash": "4f291296e89b784cd35479fca606f228126e3641f5bcaee68dee36583d7c9483",
}
FILTERS = {
    "filter.annex.clean": "git-annex smudge --clean -- %f",
    "filter.annex.smudge": "git-annex smudge -- %f",
}
ANNEX_REF = "refs/heads/git-annex"


class Refusal(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Protocol(Experiment):
    def __init__(self):
        super().__init__()
        self.profile = {}
        self.details = {}
        self.native_merges = 0

    def tools(self, search_path=None):
        # Inspect bytes/resolution before invoking annex, including its filter helper.
        for path, digest in TOOLS.items():
            require(sha(Path(path).read_bytes()) == digest, f"unqualified binary: {path}")
        for name in ("git", "git-annex"):
            resolved = shutil.which(name, path=search_path or self.env.get("PATH", ""))
            require(resolved and Path(resolved).resolve() == Path("/usr/bin") / name,
                    f"helper resolution: {name}")
        require(Path("/bin/sh").resolve() == Path("/usr/bin/dash"), "shell resolution")

    def qualified(self, repo, overrides=()):
        self.tools()
        require(self.out(repo, *overrides, "config", "annex.version") == "8", "format drift")
        raw = self.git(repo, *overrides, "config", "--null", "--show-origin", "--show-scope",
                       "--get-regexp", r"^filter\.annex\.", ok=False).stdout
        cells = raw.split(b"\0")[:-1]
        require(len(cells) % 3 == 0, "config framing")
        observed = {}
        for offset in range(0, len(cells), 3):
            scope, origin, entry = [x.decode() for x in cells[offset:offset + 3]]
            key, value = entry.split("\n", 1)
            require(scope == "local" and origin == "file:.git/config", "filter origin")
            require(key not in observed, "ambiguous duplicate filter")
            observed[key] = value
        require(observed == FILTERS, "unqualified annex filter command")
        attribute_data = (repo / ".git/info/attributes").read_bytes()
        expected = (f"{PREFIX}** filter=annex -text -ident !working-tree-encoding !eol "
                    "annex.largefiles=anything annex.backend=SHA256\n").encode()
        require(attribute_data == expected, "attribute policy drift")
        probe = PREFIX + "p-" + "0" * 64 + ".blob"
        values = self.git(repo, "check-attr", "-z", "filter", "text", "ident",
                          "working-tree-encoding", "eol", "annex.largefiles", "annex.backend",
                          "--", probe).stdout.split(b"\0")[:-1]
        attrs = {values[i + 1].decode(): values[i + 2].decode()
                 for i in range(0, len(values), 3)}
        require(attrs == {"filter": "annex", "text": "unset", "ident": "unset",
                          "working-tree-encoding": "unspecified", "eol": "unspecified",
                          "annex.largefiles": "anything", "annex.backend": "SHA256"},
                "effective attribute drift")
        return {"tools": TOOLS, "filters": observed, "origins": "local:file:.git/config",
                "attributes_sha256": sha(attribute_data), "effective_attributes": attrs,
                "repository_format": 8}

    def refused(self, name, callback):
        try:
            callback()
        except Refusal as exc:
            self.check(name, refusal=str(exc))
        else:
            raise AssertionError(f"negative case accepted: {name}")

    def profile_cases(self):
        repo = self.init("profile")
        self.isolate(repo)
        self.profile = self.qualified(repo)
        config_seal = self.config_seal(repo)
        index = self.out(repo, "write-tree")
        head = self.out(repo, "rev-parse", "HEAD")
        marker = self.root / "SUSPICIOUS-COMMAND-RAN"
        bad = f"touch {marker}"
        for key in ("clean", "smudge", "process", "required"):
            self.git(repo, "config", f"filter.annex.{key}", "false" if key == "required" else bad)
            self.refused(f"profile-refuses-{key}-substitution-before-execution",
                         lambda: self.qualified(repo))
            self.git(repo, "config", "--unset-all", f"filter.annex.{key}")
            if f"filter.annex.{key}" in FILTERS:
                self.git(repo, "config", f"filter.annex.{key}", FILTERS[f"filter.annex.{key}"])
        include = self.root / "included-config"
        include.write_text('[filter "annex"]\n\tclean = ' + FILTERS["filter.annex.clean"] + "\n")
        self.git(repo, "config", "include.path", str(include))
        self.refused("profile-refuses-included-filter-even-with-same-command",
                     lambda: self.qualified(repo))
        self.git(repo, "config", "--unset", "include.path")
        self.refused("profile-refuses-command-override-origin",
                     lambda: self.qualified(repo, ("-c", "filter.annex.clean=" + FILTERS["filter.annex.clean"])))
        substitute = self.root / "substitute-bin"
        substitute.mkdir()
        fake = substitute / "git-annex"
        fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
        fake.chmod(0o700)
        self.refused("profile-refuses-helper-path-substitution-before-execution",
                     lambda: self.tools(str(substitute) + os.pathsep + self.env.get("PATH", "")))
        self.git(repo, "config", "annex.version", "9")
        self.refused("profile-refuses-format-nine-before-annex-invocation", lambda: self.qualified(repo))
        self.git(repo, "config", "annex.version", "8")
        with (repo / ".git/info/attributes").open("a") as stream:
            stream.write(f"{PREFIX}** filter=qualification\n")
        self.refused("profile-refuses-attribute-drift", lambda: self.qualified(repo))
        self.isolate(repo)
        assert self.qualified(repo) == self.profile
        assert self.config_seal(repo) == config_seal
        self.git(repo, "config", "annex.alwayscommit", "false")
        self.refused("profile-seal-refuses-other-effective-config-drift", lambda: require(
            self.config_seal(repo) == config_seal, "full config/origin seal drift"))
        self.git(repo, "config", "--unset", "annex.alwayscommit")
        assert self.config_seal(repo) == config_seal
        self.profile["fixture_config_seal"] = config_seal
        assert not marker.exists()
        assert self.out(repo, "write-tree") == index and self.out(repo, "rev-parse", "HEAD") == head
        assert (repo / ORIGINAL).read_bytes() == ATTRS
        self.check("negative-profile-cases-preserve-payload-head-index-and-never-execute-marker")

    def config_seal(self, repo):
        # Seal all values AND origins without printing credentials from global Git
        # config. Per-repo seal equality is not cross-repo profile equivalence.
        raw = self.git(repo, "config", "--null", "--show-origin", "--show-scope", "--list").stdout
        cells = raw.split(b"\0")[:-1]
        require(len(cells) % 3 == 0, "config-list framing")
        records = sorted(tuple(cell.decode() for cell in cells[i:i + 3])
                         for i in range(0, len(cells), 3))
        return sha(canonical(records))

    def ownership(self, repo, manifest, registry):
        # Synthetic sealed registry supplied by the fixture, not a Git ref's self-claim.
        require(sha(canonical(manifest)) == registry["digest"], "manifest digest")
        for name in ("library", "operation", "profile"):
            require(manifest[name] == registry[name], f"manifest {name}")
        require(manifest["version"] == 1 and manifest["files"], "manifest version/files")
        seen = set()
        require(not (repo / PREFIX).exists(), "pre-existing unowned namespace")
        for entry in manifest["files"]:
            logical = entry["original"]
            require(logical and not logical.startswith("/") and
                    all(p not in ("", ".", "..", ".git") for p in logical.split("/")), "logical grammar")
            expected = PREFIX + "p-" + sha(logical.encode()) + ".blob"
            require(entry["stored"] == expected and expected not in seen, "mapping identity/uniqueness")
            seen.add(expected)
            require(entry["key"] == f"SHA256-s{entry['size']}--{entry['sha256']}", "key binding")
            original = repo / "org/repo" / logical
            require(original.is_file() and not original.is_symlink(), "original regular file")
            data = original.read_bytes()
            require(len(data) == entry["size"] and sha(data) == entry["sha256"], "old byte divergence")
            require(self.git(repo, "cat-file", "blob", f"HEAD:org/repo/{logical}").stdout == data,
                    "HEAD/worktree divergence")
        return {"operation": registry["operation"], "manifest_digest": registry["digest"],
                "representation_receipt_only": True, "catalog_rows_created": 0}

    def returning_cases(self):
        source = self.init("convergence-source")
        returning = self.root / "convergence-returning"
        self.git(self.root, "clone", "--no-local", "--quiet", str(source), str(returning))
        self.git(returning, "annex", "init", "--version=8", "--quiet", "convergence-returning")
        self.isolate(source)
        self.isolate(returning)
        path, key = self.add(source, ".gitattributes", ATTRS)
        entry = {"original": ".gitattributes", "stored": path, "key": key,
                 "sha256": sha(ATTRS), "size": len(ATTRS)}
        manifest = {"version": 1, "library": "synthetic-library", "operation": "synthetic-operation",
                    "profile": sha(canonical(self.profile)), "files": [entry]}
        registry = {k: manifest[k] for k in ("library", "operation", "profile")}
        registry["digest"] = sha(canonical(manifest))
        receipt = self.ownership(returning, manifest, registry)
        assert receipt["catalog_rows_created"] == 0
        self.check("unclaimed-returning-copy-validates-without-inventing-catalog-rows")
        for field in ("library", "operation", "profile", "digest"):
            wrong = dict(registry, **{field: "wrong"})
            self.refused(f"ownership-refuses-wrong-{field}", lambda: self.ownership(returning, manifest, wrong))
        for field, value in (("key", "SHA256-s0--" + "0" * 64), ("stored", "../escape"),
                             ("original", "../.gitattributes")):
            bad = copy.deepcopy(manifest)
            bad["files"][0][field] = value
            sealed_bad = dict(registry, digest=sha(canonical(bad)))
            self.refused(f"ownership-refuses-invalid-{field}-despite-matching-digest",
                         lambda: self.ownership(returning, bad, sealed_bad))
        duplicate = copy.deepcopy(manifest)
        duplicate["files"] *= 2
        self.refused("ownership-refuses-duplicate-mapping", lambda: self.ownership(
            returning, duplicate, dict(registry, digest=sha(canonical(duplicate)))))
        conflicting = copy.deepcopy(manifest)
        other = dict(entry, sha256=sha(b"different version"), size=len(b"different version"))
        other["key"] = f"SHA256-s{other['size']}--{other['sha256']}"
        conflicting["files"].append(other)
        self.refused("ownership-refuses-conflicting-versions-of-one-logical-file", lambda: self.ownership(
            returning, conflicting, dict(registry, digest=sha(canonical(conflicting)))))
        namespace = returning / PREFIX
        namespace.mkdir()
        self.refused("ownership-refuses-pre-existing-unowned-namespace",
                     lambda: self.ownership(returning, manifest, registry))
        namespace.rmdir()  # Only the empty directory just made by this negative fixture.
        (returning / ORIGINAL).write_bytes(b"operator edit\n")
        self.refused("ownership-refuses-divergent-returning-bytes",
                     lambda: self.ownership(returning, manifest, registry))
        assert (returning / ORIGINAL).read_bytes() == b"operator edit\n"
        (returning / ORIGINAL).write_bytes(ATTRS)
        assert self.ownership(returning, manifest, registry) == receipt
        backup = self.root / "returning-original.backup"
        backup.write_bytes((returning / ORIGINAL).read_bytes())
        with backup.open("rb") as stream:
            os.fsync(stream.fileno())
        self.add(returning, ".gitattributes", backup.read_bytes())
        self.git(returning, "commit", "-qm", "fixture local preservation")
        preserved = self.out(returning, "rev-parse", "HEAD")
        self.git(source, "update-index", "--force-remove", "--", ORIGINAL)
        (source / ORIGINAL).unlink()
        self.git(source, "commit", "-qm", "fixture retirement candidate")
        self.git(returning, "fetch", "--quiet", "origin", "HEAD:refs/modelark/retirement")
        new = self.out(returning, "rev-parse", "refs/modelark/retirement")
        changes = self.git(returning, "diff-tree", "--no-commit-id", "--name-only", "-z", "-r",
                           preserved, new).stdout
        assert changes == ORIGINAL.encode() + b"\0"
        # The same clone has already preserved/annexed its old bytes. Only now retire.
        assert (returning / ORIGINAL).read_bytes() == backup.read_bytes() == ATTRS
        self.git(returning, "update-ref", "HEAD", new, preserved)
        self.git(returning, "read-tree", "--reset", "-u", new)
        assert not (returning / ORIGINAL).exists()
        assert (returning / path).read_bytes() == backup.read_bytes()
        assert self.out(returning, "write-tree") == self.out(returning, "rev-parse", f"{new}^{{tree}}")
        self.pointer(returning, entry, "HEAD")
        self.consumers(returning, entry)
        self.git(returning, "add", "--", path)
        self.git(returning, "commit", "-qm", "fixture unlocked pointer")
        self.pointer(returning, entry, "HEAD")
        self.check("same-returning-clone-preserves-converts-retires-and-reads-original-through-consumers")

    def parse_pointer(self, mode, data, entry, object_path):
        key = entry["key"]
        require(re.fullmatch(r"SHA256-s[0-9]+--[0-9a-f]{64}", key), "pointer key grammar")
        require(re.fullmatch(r"\.git/annex/objects/[A-Za-z0-9]{2}/[A-Za-z0-9]{2}/" +
                             re.escape(key) + "/" + re.escape(key), object_path), "qualified object path")
        if mode == "120000":
            expected = os.path.relpath(object_path, str(PurePosixPath(entry["stored"]).parent)).encode()
            require(data == expected, "locked pointer exact path/key/hashdir")
        else:
            require(mode == "100644" and data == f"/annex/objects/{key}\n".encode(),
                    "unlocked pointer grammar/key")

    def pointer(self, repo, entry, ref, *, require_content=True):
        record = self.git(repo, "ls-tree", "-z", ref, "--", entry["stored"]).stdout
        meta, name = record[:-1].split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        require(name.decode() == entry["stored"] and kind == "blob", "tree entry")
        data = self.git(repo, "cat-file", "blob", oid).stdout
        # Native examinekey computes the canonical hashdir without requiring bytes.
        # This version uses mixed-case hashdirs for object paths, not hashdirlower.
        object_path = self.out(repo, "annex", "examinekey", "--format=${objectpath}", entry["key"])
        self.parse_pointer(mode, data, entry, object_path)
        if require_content:
            content = (repo / object_path).read_bytes()
            require(len(content) == entry["size"] and sha(content) == entry["sha256"], "object bytes")
        self.check(f"committed-pointer-full-grammar-{mode}", pointer=data.decode())
        return mode, data, object_path

    def pointer_negatives(self):
        repo = self.init("pointer-negatives")
        self.isolate(repo)
        path, key = self.add(repo, ".gitattributes", ATTRS)
        entry = {"stored": path, "key": key, "size": len(ATTRS), "sha256": sha(ATTRS)}
        self.git(repo, "commit", "-qm", "fixture pointer negative baseline")
        mode, data, target = self.pointer(repo, entry, "HEAD")
        for name, bad_mode, bad_data in (
            ("key", mode, data.replace(key.encode(), b"SHA256-s0--" + b"0" * 64)),
            ("suffix", mode, data + b"extra"),
            ("absolute", mode, b"/" + target.encode()),
            ("hashdir", mode, data.replace(b"objects/", b"objects/wrong/")),
            ("traversal", mode, b"../" + data),
            ("raw-Git-payload", "100644", ATTRS),
            ("executable-mode", "100755", f"/annex/objects/{key}\n".encode()),
        ):
            self.refused("pointer-refuses-" + name,
                         lambda: self.parse_pointer(bad_mode, bad_data, entry, target))

    def flush_annex(self, repo):
        # Annex's own merger, confined to a disposable staging repo. No transport,
        # content transfer or file-tree commit is permitted by these explicit flags.
        self.native_merges += 1
        self.git(repo, "annex", "sync", "--only-annex", "--no-content", "--no-pull",
                 "--no-push", "--no-commit")
        return self.out(repo, "rev-parse", ANNEX_REF)

    def metadata_tree(self, repo, ref):
        records = self.git(repo, "ls-tree", "-rz", ref).stdout.split(b"\0")[:-1]
        result = {}
        for record in records:
            meta, name = record.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            require(mode == "100644" and kind == "blob", "metadata entry grammar")
            result[name.decode()] = self.git(repo, "cat-file", "blob", oid).stdout
        return result

    def locations(self, data):
        # Pinned 8.20210223 subset: unknown formats/ambiguous records are refused.
        records = {}
        require(data.endswith(b"\n"), "location log framing")
        for line in data.splitlines():
            match = re.fullmatch(rb"([0-9]+(?:\.[0-9]+)?)s ([01]) "
                                 rb"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", line)
            require(match is not None, "unknown location grammar")
            timestamp, status, identity = [x.decode() for x in match.groups()]
            clock = Decimal(timestamp)
            history = records.setdefault(identity, {})
            require(clock not in history or history[clock] == int(status), "conflicting equal-clock records")
            history[clock] = int(status)
        return records

    def record_delta(self, before, after, allowed_uuids, map_uuid, clock_limit):
        changes = {}
        for identity, history in after.items():
            previous = before.get(identity, {})
            additions = {clock: status for clock, status in history.items()
                         if clock not in previous or previous[clock] != status}
            if not additions:
                continue
            require(identity != map_uuid, "new MAP UUID presence claim")
            require(identity in allowed_uuids, "unproven/unknown drive UUID")
            require(all(status == 1 for status in additions.values()), "drop claim")
            require(all(clock <= clock_limit for clock in additions), "unqualified future clock")
            require(not previous or min(additions) > max(previous), "nonadvancing/conflicting clock")
            require(history[max(history)] == 1, "effective location not present")
            changes[identity] = [str(clock) for clock in sorted(additions)]
        return changes

    def metadata_delta(self, old, new, allowed, map_uuid, clock_limit=None):
        clock_limit = clock_limit if clock_limit is not None else Decimal(str(time.time()))
        changes = {}
        for name in sorted(old.keys() | new.keys()):
            if old.get(name) == new.get(name):
                continue
            match = re.fullmatch(r"[0-9a-f]{3}/[0-9a-f]{3}/(SHA256-s[0-9]+--[0-9a-f]{64})\.log", name)
            require(match is not None, f"unrelated metadata change: {name}")
            key = match[1]
            require(key in allowed, "unselected metadata key")
            require(name in new, "deleted location log")
            before = self.locations(old[name]) if name in old else {}
            after = self.locations(new[name])
            for identity, history in before.items():
                require(all(after.get(identity, {}).get(clock) == status for clock, status in history.items()),
                        "old location history changed")
            additions = self.record_delta(before, after, allowed[key], map_uuid, clock_limit)
            if additions:
                changes[key] = additions
        return changes

    def admit_input(self, old, incoming, allowed, map_uuid, clock_limit):
        # Git-only quarantine inspection BEFORE an annex-recognized ref exists.
        # No speculative native merge/reader of imported config/remote metadata.
        for name, data in incoming.items():
            if old.get(name) == data:
                continue
            match = re.fullmatch(r"[0-9a-f]{3}/[0-9a-f]{3}/(SHA256-s[0-9]+--[0-9a-f]{64})\.log", name)
            if match is None:
                # Registered UUID descriptions can be a strict byte-line subset
                # of the map baseline; only the native merger constructs the union.
                require(name == "uuid.log" and name in old and
                        set(data.splitlines()) <= set(old[name].splitlines()),
                        f"unadmitted input metadata: {name}")
                continue
            key = match[1]
            before = self.locations(old[name]) if name in old else {}
            after = self.locations(data)
            # Source snapshots may omit old map history; the final merged candidate
            # must preserve it. New records alone need selected physical authority.
            self.record_delta(before, after, allowed.get(key, set()), map_uuid, clock_limit)

    def stage(self, name, map_repo, source, source_annex, allowed):
        repo = self.root / name
        self.git(self.root, "clone", "--no-local", "--no-checkout", "--quiet", str(map_repo), str(repo))
        self.git(repo, "remote", "remove", "origin")
        # Staging borrows map UUID for reading existing metadata; no init or new
        # description/location records, and no annex content or real-map writes.
        for key, value in {"annex.uuid": self.out(map_repo, "config", "annex.uuid"),
                           "annex.version": "8", **FILTERS}.items():
            self.git(repo, "config", key, value)
        self.isolate(repo)
        self.qualified(repo)
        old = self.out(map_repo, "rev-parse", ANNEX_REF)
        self.git(repo, "update-ref", ANNEX_REF, old)
        self.git(repo, "fetch", "--quiet", str(source), f"{source_annex}:refs/modelark/quarantine-annex")
        self.admit_input(self.metadata_tree(repo, old), self.metadata_tree(repo, source_annex), allowed,
                         self.out(map_repo, "config", "annex.uuid"), Decimal(str(time.time())))
        self.git(repo, "update-ref", "refs/remotes/sealed/git-annex", source_annex)
        head = self.out(repo, "rev-parse", "HEAD")
        new = self.flush_annex(repo)
        assert self.out(repo, "rev-parse", "HEAD") == head
        assert not list((repo / ".git/annex/objects").rglob("SHA256-*"))
        return repo, new

    def metadata_cases(self):
        source = self.init("metadata-source")
        self.isolate(source)
        _, unrelated_key = self.add(source, ".unrelated", b"prior unrelated content\n")
        self.git(source, "commit", "-qm", "fixture unrelated baseline")
        self.flush_annex(source)
        target = self.init("metadata-map")
        self.isolate(target)
        # Independent map history; controlled fixture registration introduces the
        # known source UUID/old records BEFORE capturing the publication baseline.
        self.git(target, "fetch", "--quiet", str(source),
                 "HEAD:refs/modelark/registered-tree", f"{ANNEX_REF}:refs/remotes/registered/git-annex")
        self.flush_annex(target)
        self.git(target, "read-tree", "--reset", "-u", "refs/modelark/registered-tree")
        tree = self.out(target, "write-tree")
        independent = self.out(target, "commit-tree", tree, data=b"independent map fixture root\n")
        self.git(target, "update-ref", "HEAD", independent)
        assert self.git(target, "merge-base", "HEAD", "refs/modelark/registered-tree", ok=False).returncode
        old_annex = self.flush_annex(target)
        old_head = self.out(target, "rev-parse", "HEAD")
        old_index = self.out(target, "write-tree")
        old_metadata = self.metadata_tree(target, old_annex)
        map_uuid = self.out(target, "config", "annex.uuid")
        source_uuid = self.out(source, "config", "annex.uuid")
        clock_start = Decimal(str(time.time()))
        path, key = self.add(source, ".gitattributes", ATTRS)
        source_annex = self.flush_annex(source)
        # Physical original/key/object checks precede the metadata allowlist.
        location = self.out(source, "annex", "contentlocation", key)
        assert (source / location).read_bytes() == ATTRS
        allowed = {key: {source_uuid}}
        self.git(source, "update-index", "--force-remove", "--", ORIGINAL)
        (source / ORIGINAL).unlink()
        self.git(source, "commit", "-qm", "fixture map file-tree candidate")
        new_head = self.out(source, "rev-parse", "HEAD")
        staged, candidate = self.stage("metadata-staging", target, source, source_annex, allowed)
        metadata = self.metadata_tree(staged, candidate)
        delta = self.metadata_delta(old_metadata, metadata, allowed, map_uuid)
        assert set(delta) == {key} and set(delta[key]) == {source_uuid}
        whereis = json.loads(self.out(staged, "annex", "whereis", "--json", f"--key={key}"))
        assert {record["uuid"] for record in whereis["whereis"]} == {source_uuid}
        assert self.out(staged, "rev-parse", ANNEX_REF) == candidate
        assert self.out(target, "rev-parse", ANNEX_REF) == old_annex
        assert self.out(target, "rev-parse", "HEAD") == old_head
        assert self.out(target, "write-tree") == old_index
        self.check("native-staged-annex-merge-validates-selected-drive-UUID-and-preserves-real-map",
                   changed_keys=list(delta), effective_UUIDs=[source_uuid])
        # Validate *actual native annex changes*, not just our parser's own examples.
        cases = [
            ("map-presence", ("setpresentkey", key, map_uuid, "1")),
            ("unknown-UUID", ("setpresentkey", key, "00000000-0000-4000-8000-000000000001", "1")),
            ("unselected-key", ("setpresentkey", unrelated_key, map_uuid, "1")),
            ("drop", ("setpresentkey", key, source_uuid, "0")),
            ("description", ("describe", source_uuid, "unexpected description")),
            ("trust", ("untrust", source_uuid)),
            ("config", ("config", "--set", "annex.dotfiles", "true")),
        ]
        for name, command in cases:
            bad_stage, _ = self.stage("bad-metadata-" + name, target, staged, candidate, allowed)
            self.git(bad_stage, "annex", *command)
            bad_ref = self.flush_annex(bad_stage)
            bad_tree = self.metadata_tree(bad_stage, bad_ref)
            self.refused("metadata-refuses-native-" + name,
                         lambda: self.metadata_delta(old_metadata, bad_tree, allowed, map_uuid))
            calls = self.native_merges
            self.refused("quarantine-refuses-native-" + name + "-before-annex-merge",
                         lambda: self.stage("quarantined-" + name, target, bad_stage, bad_ref, allowed))
            assert self.native_merges == calls
        log = next(name for name in metadata if name.endswith(f"/{key}.log"))
        for name, modified in (
            ("unknown-grammar", b"future-format v2\n"),
            ("dominating-drop-clock", metadata[log] + f"9999999999s 0 {source_uuid}\n".encode()),
            ("future-presence-clock", metadata[log] + f"9999999999s 1 {source_uuid}\n".encode()),
            ("equal-clock-conflict", metadata[log] + metadata[log].replace(b" 1 ", b" 0 ")),
        ):
            bad = dict(metadata, **{log: modified})
            self.refused("metadata-refuses-" + name,
                         lambda: self.metadata_delta(old_metadata, bad, allowed, map_uuid))
        repeated, repeated_ref = self.stage("metadata-idempotent", target, staged, candidate, allowed)
        repeated_tree = self.metadata_tree(repeated, repeated_ref)
        assert self.metadata_delta(metadata, repeated_tree, allowed, map_uuid) == {}
        assert repeated_tree == metadata
        require(all(clock_start <= Decimal(clock) <= Decimal(str(time.time()))
                    for uuids in delta.values() for clocks in uuids.values() for clock in clocks),
                "native clock window")
        self.check("native-repeat-merge-is-exact-metadata-noop-with-original-location-clocks")
        self.details["metadata"] = {"old_annex": old_annex, "source_annex": source_annex,
                                    "candidate_annex": candidate, "delta": delta,
                                    "native_location_log": metadata[log].decode(),
                                    "map_UUID": map_uuid, "source_UUID": source_uuid,
                                    "merge_command": "git annex sync --only-annex --no-content --no-pull --no-push --no-commit"}
        self.map_replay(target, staged, source, old_head, new_head, old_annex, candidate,
                        old_index, path, key, delta)

    def existing_location_cases(self):
        for prior in ("unknown", "absent", "present"):
            source = self.init("existing-source-" + prior)
            self.isolate(source)
            _, key = self.add(source, ".gitattributes", ATTRS)
            self.git(source, "commit", "-qm", "fixture existing content key")
            self.flush_annex(source)
            destination = self.root / ("existing-copy-" + prior)
            self.git(self.root, "clone", "--no-local", "--quiet", str(source), str(destination))
            self.git(destination, "annex", "init", "--version=8", "--quiet", "existing-copy")
            self.isolate(destination)
            identity = self.out(destination, "config", "annex.uuid")
            self.git(source, "remote", "add", "copy", str(destination))
            self.git(source, "annex", "describe", "copy", "existing-copy")
            if prior == "absent":
                self.git(source, "annex", "setpresentkey", key, identity, "0")
            if prior == "present":
                self.git(source, "annex", "copy", f"--key={key}", "--to=copy")
            self.flush_annex(source)
            target = self.root / ("existing-map-" + prior)
            self.git(self.root, "clone", "--no-local", "--no-checkout", "--quiet", str(source), str(target))
            self.git(target, "annex", "init", "--version=8", "--quiet", "existing-map")
            self.isolate(target)
            old_ref = self.flush_annex(target)
            old = self.metadata_tree(target, old_ref)
            if prior == "present":
                # Renew a physically true native record, not a handcrafted clock.
                location = self.out(destination, "annex", "contentlocation", key)
                assert (destination / location).read_bytes() == ATTRS
                self.git(source, "annex", "setpresentkey", key, identity, "1")
            else:
                self.git(source, "annex", "copy", f"--key={key}", "--to=copy")
            location = self.out(destination, "annex", "contentlocation", key)
            assert (destination / location).read_bytes() == ATTRS
            source_ref = self.flush_annex(source)
            allowed = {key: {identity}}
            staged, candidate = self.stage("existing-staged-" + prior, target, source, source_ref, allowed)
            new = self.metadata_tree(staged, candidate)
            delta = self.metadata_delta(old, new, allowed, self.out(target, "config", "annex.uuid"))
            if prior == "present":
                # The installed client keeps an already-true setpresentkey as an
                # exact no-op, not a new clock. Qualify what it actually emits.
                assert delta == {} and old == new
            else:
                assert set(delta) == {key} and set(delta[key]) == {identity}
            result = json.loads(self.out(staged, "annex", "whereis", "--json", f"--key={key}"))
            effective = {record["uuid"] for record in result["whereis"]}
            assert effective == {identity, self.out(source, "config", "annex.uuid")}
            assert self.out(target, "rev-parse", ANNEX_REF) == old_ref
            log = next(name for name in new if name.endswith(f"/{key}.log"))
            self.check("native-existing-key-" + prior + "-to-present" + ("-noop" if prior == "present" else ""),
                       old_log=old[log].decode(), candidate_log=new[log].decode(),
                       effective_UUIDs=sorted(effective))

    def map_replay(self, repo, staged, source, old_head, new_head, old_annex, candidate,
                   old_index, path, key, delta):
        # Fetch sealed objects into non-authoritative refs only. The map's live HEAD,
        # annex ref and index have not changed while evaluating the candidate.
        self.git(repo, "fetch", "--quiet", str(source), f"{new_head}:refs/modelark/candidate-tree")
        self.git(repo, "fetch", "--quiet", str(staged), f"{candidate}:refs/modelark/candidate-annex")
        head_ref = self.out(repo, "symbolic-ref", "HEAD")
        old_paths = self.file_tree(repo, old_head)
        new_paths = self.file_tree(repo, new_head)
        require(self.working_paths(repo) == old_paths, "initial worktree binding")
        require(set(old_paths) ^ set(new_paths) == {ORIGINAL, path}, "exact path delta")
        require(all(old_paths[p] == new_paths[p] for p in old_paths.keys() & new_paths.keys()),
                "unrelated tree change")
        intent = {"old_head": old_head, "new_head": new_head, "old_annex": old_annex,
                  "new_annex": candidate, "old_index": old_index, "delta_sha256": sha(canonical(delta)),
                  "old_paths": old_paths, "new_paths": new_paths,
                  "expected_final_index": self.out(repo, "rev-parse", f"{new_head}^{{tree}}"),
                  "operation": "synthetic-map-operation", "decoded_delta": delta,
                  "allowed_UUIDs": sorted(delta[key]), "key": key,
                  "profile": self.qualified(repo), "config_seal": self.config_seal(repo),
                  "source_file_ref": new_head, "source_annex_ref": self.out(source, "rev-parse", ANNEX_REF)}
        intent_path = self.root / "map-intent.json"
        self.durable_json(intent_path, intent)
        transaction = (f"start\nupdate {head_ref} {new_head} {old_head}\n"
                       f"update {ANNEX_REF} {candidate} {old_annex}\nprepare\ncommit\n").encode()
        stale = transaction.replace(f"{candidate} {old_annex}".encode(), f"{candidate} {'0' * 40}".encode())
        failed = self.git(repo, "update-ref", "--stdin", data=stale, ok=False)
        assert failed.returncode and self.out(repo, "rev-parse", "HEAD") == old_head
        assert self.out(repo, "rev-parse", ANNEX_REF) == old_annex
        self.check("stale-annex-CAS-refuses-entire-file-and-metadata-ref-group")
        self.git(repo, "update-ref", "--stdin", data=transaction)
        index_bytes = (repo / ".git/index").read_bytes()
        lock = repo / ".git/index.lock"
        with lock.open("xb") as stream:
            stream.write(b"synthetic competing index owner\n")
        failed = self.git(repo, "read-tree", "--reset", "-u", new_head, ok=False)
        assert failed.returncode
        # write-tree itself may require index.lock to update the cache-tree.
        # Observe raw index bytes while the lock is held, without another writer.
        assert (repo / ".git/index").read_bytes() == index_bytes
        assert (repo / ORIGINAL).read_bytes() == ATTRS
        assert json.loads(intent_path.read_bytes()) == intent
        self.check("index-lock-after-atomic-ref-publication-retains-old-tree-and-pending-intent")
        lock.unlink()  # Exact synthetic lock created above, not another process's lock.
        require(self.out(repo, "rev-parse", "HEAD") == new_head and
                self.out(repo, "rev-parse", ANNEX_REF) == candidate, "replay refs")
        # Ref advancement does not allow overwriting an unrelated operator edit.
        (repo / ORIGINAL).write_bytes(b"external change\n")
        self.refused("replay-refuses-external-old-path-edit", lambda: require(
            (repo / ORIGINAL).read_bytes() == ATTRS, "replay old-path changed"))
        assert (repo / ORIGINAL).read_bytes() == b"external change\n"
        (repo / ORIGINAL).write_bytes(ATTRS)
        require(self.out(repo, "write-tree") == old_index, "replay index changed")
        # Native read-tree writes worktree paths before atomically replacing index:
        # allow old index + partial old/new worktree, never a partial/mixed index.
        self.git(repo, "update-index", "--force-remove", "--", ORIGINAL)
        self.refused("replay-refuses-mixed-index", lambda: self.replay_paths(repo, old_paths, new_paths))
        self.git(repo, "read-tree", old_head)
        (repo / ORIGINAL).unlink()
        self.replay_paths(repo, old_paths, new_paths)
        extra = repo / "unexpected-file"
        extra.write_bytes(b"unrelated operator bytes\n")
        self.refused("replay-refuses-unrelated-extra-in-partial-tree",
                     lambda: self.replay_paths(repo, old_paths, new_paths))
        assert extra.read_bytes() == b"unrelated operator bytes\n"
        extra.unlink()  # Only this synthetic negative fixture, after checking preservation.
        extra.mkdir()
        self.refused("replay-refuses-empty-unrelated-directory",
                     lambda: self.replay_paths(repo, old_paths, new_paths))
        extra.rmdir()
        self.replay_paths(repo, old_paths, new_paths)
        self.git(repo, "read-tree", new_head)
        self.refused("replay-refuses-new-index-with-incomplete-worktree",
                     lambda: self.replay_paths(repo, old_paths, new_paths))
        self.git(repo, "read-tree", old_head)
        self.git(repo, "read-tree", "--reset", "-u", new_head)
        expected_tree = self.out(repo, "rev-parse", f"{new_head}^{{tree}}")
        require(self.out(repo, "write-tree") == expected_tree, "final index")
        require(not (repo / ORIGINAL).exists() and (repo / path).is_symlink(), "final worktree")
        require(self.working_paths(repo) == new_paths, "complete final path binding")
        self.check("old-index-with-partial-worktree-converges-without-changing-unrelated-paths")
        assert self.out(repo, "annex", "lookupkey", "--", path) == key
        assert not self.out(repo, "annex", "contentlocation", key, ok=False)
        whereis = json.loads(self.out(repo, "annex", "whereis", "--json", f"--key={key}"))
        assert {record["uuid"] for record in whereis["whereis"]} == set(delta[key])
        assert self.out(repo, "rev-parse", ANNEX_REF) == candidate
        entry = {"stored": path, "key": key, "size": len(ATTRS), "sha256": sha(ATTRS)}
        self.pointer(repo, entry, "HEAD", require_content=False)
        receipt = {**intent, "final_index": expected_tree, "status": "fixture-complete"}
        receipt_path = self.root / "map-receipt.json"
        receipt_path.mkdir()  # Deterministic receipt-write failure after final tree.
        try:
            self.durable_json(receipt_path, receipt)
        except FileExistsError:
            pass
        else:
            raise AssertionError("receipt failure injection did not fail")
        assert not receipt_path.is_file() and intent_path.is_file()
        receipt_path.rmdir()
        # Reopen durable intent and acknowledge intended-new state without ref/annex
        # merge replay. This models process loss; it is not a kill/power-loss test.
        assert json.loads(intent_path.read_bytes()) == intent
        assert self.out(repo, "rev-parse", "HEAD") == new_head
        assert self.out(repo, "rev-parse", ANNEX_REF) == candidate
        assert self.out(repo, "write-tree") == expected_tree
        assert self.working_paths(repo) == new_paths
        require(self.qualified(repo) == intent["profile"] and self.config_seal(repo) == intent["config_seal"],
                "recovery profile changed")
        require(self.out(source, "rev-parse", "HEAD") == intent["source_file_ref"] and
                self.out(source, "rev-parse", ANNEX_REF) == intent["source_annex_ref"], "source drift")
        self.pointer(repo, entry, "HEAD", require_content=False)
        # Reread annex's effective interpretation on replay, not only before the
        # failed receipt. A new receipt cannot reuse an earlier whereis observation.
        locations = json.loads(self.out(repo, "annex", "whereis", "--json", f"--key={key}"))
        require({record["uuid"] for record in locations["whereis"]} == set(intent["allowed_UUIDs"]),
                "replayed location evidence")
        require(self.out(repo, "rev-parse", ANNEX_REF) == intent["new_annex"], "reader changed annex ref")
        require(self.out(repo, "write-tree") == intent["expected_final_index"], "recovery final index")
        self.durable_json(receipt_path, receipt)
        assert json.loads(receipt_path.read_bytes()) == receipt
        self.check("receipt-failure-and-verified-new-state-replay-without-map-payload-copy")

    def file_tree(self, repo, ref):
        result = {}
        for record in self.git(repo, "ls-tree", "-rz", ref).stdout.split(b"\0")[:-1]:
            meta, path = record.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            require(kind == "blob" and mode in ("120000", "100644"), "file-tree grammar")
            data = self.git(repo, "cat-file", "blob", oid).stdout
            result[path.decode()] = {"mode": mode, "sha256": sha(data)}
        return result

    def working_paths(self, repo):
        result = {}
        for parent, directories, names in os.walk(repo, followlinks=False):
            if Path(parent) == repo:
                directories.remove(".git")
            for name in names + [name for name in directories if (Path(parent) / name).is_symlink()]:
                path = Path(parent) / name
                if path.is_symlink():
                    mode, data = "120000", os.readlink(path).encode()
                else:
                    require(path.is_file(), "unexpected nonregular file")
                    mode, data = "100644", path.read_bytes()
                result[str(path.relative_to(repo))] = {"mode": mode, "sha256": sha(data)}
        return result

    def directory_paths(self, repo):
        result = set()
        for parent, directories, _ in os.walk(repo, followlinks=False):
            if Path(parent) == repo:
                directories.remove(".git")
            for name in directories:
                path = Path(parent) / name
                if not path.is_symlink():
                    result.add(str(path.relative_to(repo)))
        return result

    def replay_paths(self, repo, old, new):
        observed = self.working_paths(repo)
        index = {}
        for record in self.git(repo, "ls-files", "--stage", "-z").stdout.split(b"\0")[:-1]:
            meta, path = record.split(b"\t", 1)
            mode, oid, stage = meta.decode().split()
            require(stage == "0", "unmerged index")
            data = self.git(repo, "cat-file", "blob", oid).stdout
            index[path.decode()] = {"mode": mode, "sha256": sha(data)}
        directories = {str(parent) for path in old.keys() | new.keys()
                       for parent in PurePosixPath(path).parents if str(parent) != "."}
        require(self.directory_paths(repo) <= directories, "unrelated directory")
        require(index == old or index == new, "mixed index is not a native publication state")
        if index == new:
            require(observed == new, "new index requires complete new worktree")
        else:
            for path in old.keys() | new.keys() | observed.keys():
                require(path in old or path in new, "unrelated path")
                require(observed.get(path) in (old.get(path), new.get(path)), "path outside sealed old/new")

    def durable_json(self, path, value):
        with path.open("xb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def run(self):
        error = None
        try:
            self.tools()
            self.profile_cases()
            self.returning_cases()
            self.pointer_negatives()
            self.metadata_cases()
            self.existing_location_cases()
        except Exception:
            error = traceback.format_exc()
        result = {"status": "failed" if error else "passed-bounded-protocol-probes", "error": error,
                  "checks": self.checks, "profile": self.profile, "details": self.details,
                  "retained_directory": str(self.root), "stage_0_complete": False,
                  "physical_USB_or_archive_test": False,
                  "not_proven": ["production authority/profile implementation", "production crash recovery",
                                 "complete deployment helper closure", "full process-kill durability matrix",
                                 "live migration"]}
        (self.root / "protocol-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 1 if error else 0


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Assertions required: do not use -O")
    raise SystemExit(Protocol().run())
