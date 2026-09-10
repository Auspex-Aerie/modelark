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
    def read(path, *args, **kwargs):
        if str(path) not in files:
            raise FileNotFoundError(str(path))
        return files[str(path)]

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "exists", lambda path: str(path) in files)
    return files


def test_ancestor_limit_and_memavailable_not_memfree(monkeypatch):
    _proc(monkeypatch)
    sample = q.available_memory()
    assert sample["available_bytes"] == 40000
    assert sample["observations"]["host_available_bytes"] == 200 * 1024
    assert sample["observations"]["cgroup_headroom_bytes"] == {"a": 40000}


def test_visible_root_limit_is_not_ignored(monkeypatch):
    _proc(monkeypatch, root_limit=True)
    assert q.available_memory()["available_bytes"] == 20000


@pytest.mark.parametrize("group", ["host", "host_available_bytes", "cgroup_headroom_bytes"])
def test_cgroup_names_cannot_replace_host_headroom(monkeypatch, group):
    files = _proc(monkeypatch)
    files["/proc/meminfo"] = "MemAvailable: 1 kB\n"
    files["/proc/self/cgroup"] = f"0::/{group}\n"
    files[f"/sys/fs/cgroup/{group}/memory.max"] = "200000"
    files[f"/sys/fs/cgroup/{group}/memory.current"] = "0"
    sample = q.available_memory()
    assert sample["available_bytes"] == 1024
    assert sample["observations"]["host_available_bytes"] == 1024
    assert sample["observations"]["cgroup_headroom_bytes"] == {group: 200000}
    with pytest.raises(CodecResourceRefusal):
        q.CodecMemoryPolicy(4096, 0).admit(sample["available_bytes"])


def test_overdrawn_cgroup_has_zero_headroom(monkeypatch):
    files = _proc(monkeypatch)
    files["/sys/fs/cgroup/a/memory.current"] = "100001"
    assert q.available_memory()["available_bytes"] == 0


@pytest.mark.parametrize("path,value", [
    ("/proc/meminfo", "MemFree: 1000000 kB\n"),
    ("/proc/self/cgroup", "2:memory:/a\n"),
    ("/proc/self/cgroup", "0::/../a\n"),
    ("/proc/self/cgroup", "0:://a\n"),
    ("/proc/self/cgroup", "0::/a/\n"),
    ("/proc/self/mountinfo", "1 2 0:3 /hidden /sys/fs/cgroup rw - cgroup2 cgroup rw\n"),
])
def test_unknown_headroom_refuses(monkeypatch, path, value):
    files = _proc(monkeypatch)
    files[path] = value
    with pytest.raises(CodecResourceRefusal):
        q.available_memory()


@pytest.mark.parametrize("value", [None, "unknown", "-1"])
@pytest.mark.parametrize("field", ["memory.max", "memory.current"])
def test_unreadable_or_invalid_ancestor_is_typed_refusal(monkeypatch, value, field):
    files = _proc(monkeypatch)
    path = f"/sys/fs/cgroup/a/{field}"
    if value is None:
        del files[path]
    else:
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
