"""Canonical capacity-mode CLI and the one-release legacy aliases."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "modelark", "--data-dir", str(data_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_canonical_capacity_mode_cli(tmp_path):
    created = _run(
        tmp_path, "plan", "create", "--id", "aware",
        "--capacity-mode", "compression_aware",
    )
    assert created.returncode == 0, created.stderr
    assert "capacity mode=compression_aware" in created.stdout
    assert "deprecated" not in created.stderr

    changed = _run(tmp_path, "plan", "capacity-mode", "guaranteed", "--plan", "aware")
    assert changed.returncode == 0, changed.stderr
    assert "capacity mode → guaranteed" in changed.stdout


def test_legacy_cli_aliases_map_and_print_deprecation(tmp_path):
    created = _run(
        tmp_path, "plan", "create", "--id", "legacy", "--provisioning", "compressed",
    )
    assert created.returncode == 0, created.stderr
    assert "capacity mode=compression_aware" in created.stdout
    assert "deprecated" in created.stderr

    changed = _run(tmp_path, "plan", "provisioning", "uncompressed", "--plan", "legacy")
    assert changed.returncode == 0, changed.stderr
    assert "capacity mode → guaranteed" in changed.stdout
    assert "deprecated" in changed.stderr


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))
            print(f"ok  {name}")
    print("all passed")
