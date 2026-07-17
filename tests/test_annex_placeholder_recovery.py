"""INC-019: verified staging safely repairs missing-content git-annex placeholders."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from modelark import fetch


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def _annex_repo(root: Path) -> None:
    root.mkdir()
    _run("git", "-C", str(root), "init", "-q")
    _run("git", "-C", str(root), "config", "user.name", "ModelArk Test")
    _run("git", "-C", str(root), "config", "user.email", "test@modelark.invalid")
    _run("git", "-C", str(root), "annex", "init", "placeholder-test", "-q")


def test_staging_is_private_stable_and_same_filesystem():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first = fetch._download_stage_dir(root, "org/model", annex=True)
        second = fetch._download_stage_dir(root, "org/model", annex=True)
        other = fetch._download_stage_dir(root, "org/other", annex=True)
        assert first == second and first != other
        assert first.is_relative_to(root / ".git" / "annex" / "tmp" / "modelark-downloads")


def test_verified_stage_replaces_only_proven_broken_annex_placeholder():
    if shutil.which("git-annex") is None:
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "archive"
        _annex_repo(root)
        relative = "org/model/README.md"
        target = root / relative
        target.parent.mkdir(parents=True)
        payload = b"verified model card\n"
        target.write_bytes(payload)
        _run("git", "-C", str(root), "annex", "add", relative)
        key = _run("git", "-C", str(root), "annex", "lookupkey", "--", relative).stdout.strip()
        _run("git", "-C", str(root), "commit", "-qam", "fixture")
        assert target.is_symlink() and target.exists()

        object_path = (target.parent / os.readlink(target)).resolve(strict=False)
        object_path.parent.chmod(0o755)
        object_path.unlink()
        assert target.is_symlink() and not target.exists()
        whereis = json.loads(_run(
            "git", "-C", str(root), "annex", "whereis", "--json", relative,
        ).stdout)
        assert whereis["key"] == key and whereis["whereis"], "stale location claim must remain"

        stage = fetch._download_stage_dir(root, "org/model", annex=True)
        staged = stage / relative
        staged.parent.mkdir(parents=True)
        staged.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        published = fetch._publish_staged(
            root, staged, target, digest, relative, annex=True,
        )
        assert published == target and target.is_file() and not target.is_symlink()
        assert target.read_bytes() == payload

        repaired_key = fetch._annex_add(root, target)
        assert repaired_key == key
        assert target.is_symlink() and target.exists() and target.read_bytes() == payload
        repaired = json.loads(_run(
            "git", "-C", str(root), "annex", "whereis", "--json", relative,
        ).stdout)
        assert any(item["here"] for item in repaired["whereis"]), repaired
        content = _run("git", "-C", str(root), "annex", "contentlocation", key).stdout.strip()
        assert (root / content).is_file(), "repaired key must have physical annex content"

        rogue = root / "org/model/untracked.bin"
        rogue.symlink_to(os.readlink(target))
        assert rogue.is_symlink() and rogue.exists(), "fixture imitates an annex-shaped link"
        (root / content).parent.chmod(0o755)
        (root / content).unlink()
        assert not rogue.exists()
        rogue_stage = stage / "untracked.bin"
        rogue_stage.write_bytes(payload)
        try:
            fetch._publish_staged(root, rogue_stage, rogue, digest, "untracked.bin", annex=True)
            raise AssertionError("an untracked annex-shaped link is not a proven placeholder")
        except fetch.TargetPathConflictError:
            pass
        assert rogue.is_symlink() and rogue_stage.read_bytes() == payload


def test_unproven_broken_symlink_and_conflicting_file_are_preserved():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stage = root / ".stage"
        stage.mkdir()
        payload = b"new verified bytes"
        digest = hashlib.sha256(payload).hexdigest()

        broken = root / "org/model/config.json"
        broken.parent.mkdir(parents=True)
        broken.symlink_to(root / "not-an-annex-object")
        staged = stage / "broken"
        staged.write_bytes(payload)
        try:
            fetch._publish_staged(root, staged, broken, digest, "config.json", annex=False)
            raise AssertionError("arbitrary broken symlink must fail closed")
        except fetch.TargetPathConflictError as exc:
            assert exc.code == "TARGET_PATH_CONFLICT"
        assert broken.is_symlink() and staged.read_bytes() == payload

        conflict = root / "org/model/README.md"
        conflict.write_bytes(b"existing different bytes")
        staged2 = stage / "conflict"
        staged2.write_bytes(payload)
        try:
            fetch._publish_staged(root, staged2, conflict, digest, "README.md", annex=False)
            raise AssertionError("different existing content must fail closed")
        except fetch.TargetPathConflictError:
            pass
        assert conflict.read_bytes() == b"existing different bytes"
        assert staged2.read_bytes() == payload


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
