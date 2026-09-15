"""Stage-1 integration qualification ONLY: native advisory tags in fresh fixtures.

No archive/catalog destination, network remote, live apply or cleanup. The pinned
profile is inherited from the accepted Stage-0 harness. This does NOT qualify the
unfinished production publisher/authority adapters. Run without Python -O.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
import time
import traceback

from qualify_annex_publication import ANNEX_REF, Protocol, Refusal, require, sha

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from modelark.publication_annotations import (  # noqa: E402
    check_annotation_merge, check_annotation_write, parse_annotations,
)
from modelark.publication_policy import PublicationRefused  # noqa: E402


class Annotations(Protocol):
    def __init__(self):
        super().__init__()
        self.admitted_tags = {}

    def admit_input(self, old, incoming, allowed, map_uuid, clock_limit):
        # This fixture allowlist is captured only after check_annotation_write
        # verified the native write against the sealed synthetic source baseline.
        # It is NOT a substitute for the production source authority/proof factory.
        for name, data in self.admitted_tags.items():
            require(incoming.get(name) == data, "source annotation proof changed")
            require(name.removesuffix(".log.met").rsplit("/", 1)[-1] in allowed,
                    "unselected annotation key")
        super().admit_input({k: v for k, v in old.items() if k not in self.admitted_tags},
                            {k: v for k, v in incoming.items() if k not in self.admitted_tags},
                            allowed, map_uuid, clock_limit)

    def metadata_delta(self, old, new, allowed, map_uuid, clock_limit=None):
        for name, source in self.admitted_tags.items():
            check_annotation_merge(baseline=old.get(name, b""), admitted_source=source,
                                   candidate=new.get(name, b""))
        return super().metadata_delta({k: v for k, v in old.items() if k not in self.admitted_tags},
                                      {k: v for k, v in new.items() if k not in self.admitted_tags},
                                      allowed, map_uuid, clock_limit)

    def native_tags(self, repo, key, values):
        args = ["annex", "metadata", f"--key={key}"]
        for field, value in values.items():
            args.extend(["-s", f"{field}={value}"])
        self.git(repo, *args)
        ref = self.flush_annex(repo)
        tree = self.metadata_tree(repo, ref)
        names = [name for name in tree if name.endswith(f"/{key}.log.met")]
        assert len(names) == 1
        return ref, names[0], tree[names[0]]

    def annotation_cases(self):
        self.tools()
        source = self.init("annotation-source")
        self.isolate(source)
        self.qualified(source)
        relative, key = self.add(source, ".gitignore", b"*.bin\n")
        self.git(source, "commit", "-qm", "synthetic selected payload")
        self.flush_annex(source)
        target = self.init("annotation-map")
        self.isolate(target)
        # Controlled initial registration, before publication baseline is sealed.
        self.git(target, "fetch", "--quiet", str(source),
                 f"{ANNEX_REF}:refs/remotes/registered/git-annex")
        self.flush_annex(target)
        # Existing annotation for another repository sharing this key. This is
        # metadata only: it must not create map payload presence or be overwritten
        # by an independently written source model tag during the native merge.
        old_ref, _, _ = self.native_tags(target, key, {"model": "other/owner"})
        old = self.metadata_tree(target, old_ref)
        before = b""
        native_logs = []
        # Exact current Fill behavior, replacement, repeated -s, omitted params,
        # and native encoding. Identical -s is not assumed a byte-identical no-op.
        cases = [
            {"model": "org/repo", "format": "safetensors", "quant": "none", "params": "7.5"},
            {"model": "org/other", "format": "safetensors", "quant": "none", "params": "8"},
            {"model": "org/other", "format": "safetensors", "quant": "none", "params": "8"},
            {"model": "org/space 🐺", "format": "gguf", "quant": "Q4_K_M"},
        ]
        for index, values in enumerate(cases):
            ref, name, after = self.native_tags(source, key, values)
            check_annotation_write(before=before, after=after, assignments=values,
                                   clock_ceiling=Decimal(str(time.time())))
            fields = json.loads(self.out(source, "annex", "metadata", f"--key={key}", "--json"))["fields"]
            effective = {}
            for (field, value), cell in parse_annotations(after).items():
                if cell.present:
                    effective.setdefault(field, []).append(value)
            assert {k: sorted(v) for k, v in effective.items()} == {
                k: sorted(v) for k, v in fields.items() if k != "lastchanged" and not k.endswith("-lastchanged")}
            native_logs.append(after.decode())
            self.check(f"native-tag-write-{index}-matches-planned-values-and-native-reader")
            before = after
        assert fields["params"] == ["8"], "omitted parameter was not preserved"
        self.admitted_tags = {name: after}
        source_uuid = self.out(source, "config", "annex.uuid")
        map_uuid = self.out(target, "config", "annex.uuid")
        location = self.out(source, "annex", "contentlocation", key)
        assert (source / location).read_bytes() == b"*.bin\n"
        assert (source / relative).read_bytes() == b"*.bin\n"
        allowed = {key: {source_uuid}}
        staged, candidate = self.stage("annotation-staging", target, source, ref, allowed)
        merged = self.metadata_tree(staged, candidate)
        self.metadata_delta(old, merged, allowed, map_uuid)
        actual = json.loads(self.out(staged, "annex", "metadata", f"--key={key}", "--json"))["fields"]
        expected_fields = {k: sorted(v) for k, v in fields.items()
                           if k != "lastchanged" and not k.endswith("-lastchanged")}
        expected_fields["model"] = sorted(expected_fields["model"] + ["other/owner"])
        assert {k: sorted(v) for k, v in actual.items()
                if k != "lastchanged" and not k.endswith("-lastchanged")} == expected_fields
        self.check("native-quarantined-staged-tag-merge-preserves-effective-advisory-fields")

        # A changed native record after the sealed source proof cannot reach merger.
        self.native_tags(source, key, {"model": "unexpected/overwrite"})
        changed_ref = self.out(source, "rev-parse", ANNEX_REF)
        count = self.native_merges
        self.refused("source-tag-change-after-proof-refuses-before-native-merge", lambda: self.stage(
            "annotation-bad-input", target, source, changed_ref, allowed))
        assert self.native_merges == count
        # Native unrelated same-key field must not become an accepted Fill tag write.
        _, _, restored = self.native_tags(source, key, cases[-1])
        _, _, hostile = self.native_tags(source, key, {"operator": "unplanned"})
        try:
            check_annotation_write(before=restored, after=hostile, assignments=cases[-1],
                                   clock_ceiling=Decimal(str(time.time())))
        except PublicationRefused:
            self.check("native-unplanned-same-key-field-refuses-source-write-proof")
        else:
            raise AssertionError("unplanned native tag was accepted")
        # All map operations above were staging only; map has neither tags nor bytes.
        assert self.out(target, "rev-parse", ANNEX_REF) == old_ref
        assert self.metadata_tree(target, old_ref) == old
        assert not list((target / ".git/annex/objects").rglob("SHA256-*"))
        self.check("real-fixture-map-remains-unchanged-and-contentless")
        self.details = {"native_logs": native_logs, "source_key": key, "tag_path": name,
                        "old_map_ref": old_ref, "candidate_ref": candidate,
                        "production_publisher_qualified": False}


def main():
    if not __debug__:
        raise RuntimeError("Qualification requires assertions enabled")
    probe = Annotations()
    result = {"status": "running", "root": str(probe.root), "checks": probe.checks,
              "live_archive_or_catalog_access": False, "stage_1_complete": False}
    try:
        probe.annotation_cases()
        result["details"] = probe.details
        result["final_repo_formats"] = {
            str(config.parent.parent.relative_to(probe.root)):
                probe.out(config.parent.parent, "config", "annex.version")
            for config in probe.root.rglob(".git/config")}
        assert set(result["final_repo_formats"].values()) == {"8"}
        result["status"] = "passed"
    except (Exception, Refusal):
        result["status"] = "failed"
        result["failure"] = traceback.format_exc()
    result["source_sha256"] = {str(path.relative_to(PROJECT)): sha(path.read_bytes()) for path in (
        Path(__file__).resolve(), PROJECT / "scripts/qualify_annex_publication.py",
        PROJECT / "scripts/qualify_annex_payload.py", PROJECT / "modelark/publication_annotations.py",
        PROJECT / "modelark/publication_policy.py")}
    output = probe.root / "annotation-result.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "checks": len(probe.checks), "result": str(output)}))
    if result["status"] != "passed":
        print(result["failure"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
