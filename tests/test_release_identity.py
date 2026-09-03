"""Release surfaces must identify one package/runtime artifact line (DEC-093)."""
from __future__ import annotations

import re
from pathlib import Path

import modelark


ROOT = Path(__file__).resolve().parents[1]
CURRENT_RELEASE = "0.3.2"


def _project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', project, re.MULTILINE)
    assert match is not None, "[project].version is missing"
    return match.group(1)


def test_release_identity_surfaces_agree():
    expected = _project_version()
    assert expected == CURRENT_RELEASE
    assert modelark.__version__ == expected
    assert f"## {expected} - " in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"ModelArk {expected}" in (ROOT / "README.md").read_text(encoding="utf-8")
    release = ROOT / "docs" / "releases" / f"v{expected}.md"
    assert release.is_file()
    assert f"ModelArk v{expected}" in release.read_text(encoding="utf-8")
