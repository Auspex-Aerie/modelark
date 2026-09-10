"""Headroom sampling and fixture checks without invoking large native jobs."""
import json
from pathlib import Path

import pytest

from modelark.codec_resources import CodecResourceRefusal
from scripts import qualify_codec_resources as q


def _proc(monkeypatch, *, root_limit=False):
    files = {
        "/proc/meminfo": "MemFree: 1 kB\nMemAvailable: 200 kB\n",
        "/proc/self/mountinfo": "1 2 0:3 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/proc/self/cgroup": "0::/a/b\n",
        "/sys/fs/cgroup/a/b/memory.max": "max",
        "/sys/fs/cgroup/a/b/memory.current": "20000",
        "/sys/fs/cgroup/a/memory.max": "100000",
        "/sys/fs/cgroup/a/memory.current": "60000",
    }
    if root_limit:
        files["/sys/fs/cgroup/memory.max"] = "50000"
        files["/sys/fs/cgroup/memory.current"] = "30000"
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: files[str(path)])
    monkeypatch.setattr(Path, "exists", lambda path: str(path) in files)
    return files


def test_ancestor_limit_and_memavailable_not_memfree(monkeypatch):
    _proc(monkeypatch)
    sample = q.available_memory()
    assert sample["available_bytes"] == 40000
    assert sample["observations"]["host"] == 200 * 1024


def test_visible_root_limit_is_not_ignored(monkeypatch):
    _proc(monkeypatch, root_limit=True)
    assert q.available_memory()["available_bytes"] == 20000


def test_overdrawn_cgroup_has_zero_headroom(monkeypatch):
    files = _proc(monkeypatch)
    files["/sys/fs/cgroup/a/memory.current"] = "100001"
    assert q.available_memory()["available_bytes"] == 0


@pytest.mark.parametrize("path,value", [
    ("/proc/meminfo", "MemFree: 1000000 kB\n"),
    ("/proc/self/cgroup", "2:memory:/a\n"),
    ("/proc/self/cgroup", "0::/../a\n"),
    ("/proc/self/mountinfo", "1 2 0:3 /hidden /sys/fs/cgroup rw - cgroup2 cgroup rw\n"),
])
def test_unknown_headroom_refuses(monkeypatch, path, value):
    files = _proc(monkeypatch)
    files[path] = value
    with pytest.raises(CodecResourceRefusal):
        q.available_memory()


def test_synthetic_fixture_is_real_safetensors_layout(tmp_path):
    source = tmp_path / "test.safetensors"
    digest = q._fixture(source, 4096)
    data = source.read_bytes()
    length = int.from_bytes(data[:8], "little")
    header = json.loads(data[8:8 + length])
    assert header == {"weight": {"dtype": "BF16", "shape": [1916], "data_offsets": [0, 3832]}}
    assert len(data) == 4096
    assert digest == q._sha(source)
    with pytest.raises(FileExistsError):
        q._fixture(source, 4096)
