"""Native annex-8 pointer/object IO tests; only new pytest temporary repositories.

This does not qualify the unfinished production command runner or map publisher.
Test config isolation is explicit and never changes operator global Git config.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from modelark.publication_payload import verify
from modelark.publication_policy import PublicationRefused
from modelark.slice.linux import BoundTree


@pytest.fixture
def native(tmp_path):
    if shutil.which("git-annex") is None:
        pytest.skip("native git-annex unavailable")
    annex = Path(shutil.which("git-annex")).resolve()
    if hashlib.sha256(annex.read_bytes()).hexdigest() != "5de67e4fd40d011af9f99f563df80238b1d74c001c00cc1ed64227eb2e79ccc7":
        pytest.skip("this IO qualification requires pinned annex 8.20210223")
    # Necessary test isolation: no inherited Git index/worktree/object routing,
    # credential/global configuration, signing, executable hooks or custom filters.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    archive = tmp_path / "native"
    archive.mkdir()
    def git(*args):
        result = subprocess.run(["/usr/bin/git", "-C", str(archive), "-c", "user.name=Publication test",
                                 "-c", "user.email=test@invalid", "-c", "commit.gpgsign=false",
                                 "-c", "core.hooksPath=/dev/null", *args],
                                env=environment, capture_output=True, check=True)
        return result.stdout.decode().strip()
    git("init", "-q")
    git("annex", "init", "--version=8", "--quiet", "disposable-payload-test")
    git("config", "annex.backend", "SHA256")
    return archive, git


def test_actual_native_locked_and_unlocked_objects_and_missing_mapped_copy(native):
    archive, git = native
    data = b"tiny original metadata\r\n\xe9"
    stored = "org/repo/__modelark_payload_v1__/p-native.blob"
    path = archive / stored
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    git("annex", "add", "--force-large", "--", stored)
    key = git("annex", "lookupkey", "--", stored)
    # This native command supplies the real mixed-case hash-directory grammar.
    obj = git("annex", "examinekey", "--format=${objectpath}", key)
    def observe():
        with BoundTree(archive) as tree:
            return verify(tree, stored_path=stored, annex_key=key, qualified_object_path=obj,
                          binding_digest="a" * 64, require_scope=lambda: None)
    assert observe().representation == "locked"
    git("annex", "unlock", "--", stored)
    assert observe().representation == "unlocked"
    git("annex", "lock", "--", stored)
    assert observe().stored_sha256 == hashlib.sha256(data).hexdigest()
    path.unlink()
    assert (archive / obj).read_bytes() == data
    with pytest.raises(PublicationRefused, match="UNAVAILABLE"):
        observe()
    assert git("config", "annex.version") == "8"
