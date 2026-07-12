"""#14: self-describing annex — per-key model/format/quant/params metadata (fetch._annex_metadata) so a
shelved drive announces WHAT it holds and the fleet is queryable. Real git-annex round-trip; skips
cleanly if git-annex is absent."""
from __future__ import annotations

import subprocess
from pathlib import Path

from modelark import fetch


def _has_annex() -> bool:
    return subprocess.run(["git", "annex", "version"], capture_output=True).returncode == 0


def test_annex_metadata_roundtrip(tmp_path):
    if not _has_annex():
        print("  (git-annex absent — skipped)")
        return
    repo = tmp_path / "d"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "annex", "init", "drive-00 · test · primary", "-q"], check=True)

    (repo / "model.safetensors").write_bytes(b"weights")
    key = fetch._annex_add(repo, repo / "model.safetensors")
    assert key, "annex add should return a key"
    fetch._annex_metadata(repo, key, "deepseek-ai/DeepSeek-R1", 32.0, "safetensors", "bf16")
    out = subprocess.run(["git", "-C", str(repo), "annex", "metadata", f"--key={key}"],
                         capture_output=True, text=True).stdout
    for field in ("model=deepseek-ai/DeepSeek-R1", "format=safetensors", "quant=bf16", "params=32.0"):
        assert field in out, (field, out)

    # a NULL quant → 'none'; None params → omitted (aux companion files)
    (repo / "config.json").write_bytes(b"{}")
    k2 = fetch._annex_add(repo, repo / "config.json")
    fetch._annex_metadata(repo, k2, "deepseek-ai/DeepSeek-R1", None, "aux", None)
    out2 = subprocess.run(["git", "-C", str(repo), "annex", "metadata", f"--key={k2}"],
                          capture_output=True, text=True).stdout
    assert "quant=none" in out2 and "format=aux" in out2 and "params=" not in out2, out2


if __name__ == "__main__":
    import tempfile
    test_annex_metadata_roundtrip(Path(tempfile.mkdtemp()))
    print("ok  test_annex_metadata_roundtrip")
    print("all passed")
