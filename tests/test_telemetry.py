"""DEC-023 / #26: the ported logger writes to a rotating file + stdout, with Bayence-style context."""
from __future__ import annotations

import logging
from pathlib import Path

from modelark.core import telemetry


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def test_writes_to_file_with_context(tmp_path):
    logf = tmp_path / "modelark.log"
    telemetry.configure(level="INFO", file_path=logf, to_console=False)
    log = telemetry.get_logger("fetch", drive="drive-00")
    log.info("shard stored", repo="deepseek-ai/x", shard="3/8", ratio=0.67)
    text = _read(logf)
    assert "shard stored" in text
    assert 'repo="deepseek-ai/x"' in text and "shard=\"3/8\"" in text and "ratio=0.67" in text
    assert "modelark.fetch" in text          # reparented into the modelark namespace


def test_with_context_merges(tmp_path):
    logf = tmp_path / "m.log"
    telemetry.configure(level="INFO", file_path=logf, to_console=False)
    telemetry.get_logger("fill").with_context(run="primary").info("go", drive="drive-01")
    text = _read(logf)
    assert 'run="primary"' in text and 'drive="drive-01"' in text


def test_level_filters(tmp_path):
    logf = tmp_path / "lvl.log"
    telemetry.configure(level="WARNING", file_path=logf, to_console=False)
    log = telemetry.get_logger("x")
    log.info("hidden")
    log.warning("shown")
    text = _read(logf)
    assert "shown" in text and "hidden" not in text


def test_rotation(tmp_path):
    logf = tmp_path / "rot.log"
    telemetry.configure(level="INFO", file_path=logf, max_bytes=2000, backups=2, to_console=False)
    log = telemetry.get_logger("spam")
    for i in range(400):
        log.info("line padding padding padding padding", i=i)
    # rotation produced backups and kept the cap (backups=2 → .log + .log.1 + .log.2)
    rotated = sorted(tmp_path.glob("rot.log*"))
    assert logf in rotated and len(rotated) <= 3 and (tmp_path / "rot.log.1").exists()


def test_context_key_named_message_does_not_collide(tmp_path):
    # regression: a context kwarg named `message` (or any method-param name) must NOT raise — this is
    # exactly the fill_api "fill finished" line that crashed the worker on Stop (TypeError).
    logf = tmp_path / "c.log"
    telemetry.configure(level="INFO", file_path=logf, to_console=False)
    telemetry.get_logger("f").info("fill finished", ok=True, message="stopped by request")
    text = _read(logf)
    assert "fill finished" in text and 'message="stopped by request"' in text


def test_invalid_level_raises(tmp_path):
    try:
        telemetry.configure(level="NOPE", file_path=tmp_path / "x.log", to_console=False)
    except ValueError:
        return
    raise AssertionError("expected ValueError for a bad level")


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    logging.shutdown()
    print("all passed")
