"""Stage-A policy tests never lower the pytest process's resource limits."""
import json
import subprocess
import sys

import pytest

from modelark.codec_resources import CodecMemoryPolicy, CodecResourceRefusal


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1024", None])
def test_invalid_ceiling(value):
    with pytest.raises(ValueError):
        CodecMemoryPolicy(value, 0)


@pytest.mark.parametrize("value", [True, -1, 1.5, "1", None])
def test_invalid_reserve(value):
    with pytest.raises(ValueError):
        CodecMemoryPolicy(1024, value)


def test_admission_boundary_is_operation_independent():
    policy = CodecMemoryPolicy(1024, 128)
    policy.admit(1152)
    policy.admit(1153)
    with pytest.raises(CodecResourceRefusal):
        policy.admit(1151)
    with pytest.raises(ValueError):
        policy.admit(True)


def test_policy_roundtrip_and_closed_world():
    policy = CodecMemoryPolicy(4096, 1024)
    assert CodecMemoryPolicy.from_record(json.loads(json.dumps(policy.to_record()))) == policy
    record = policy.to_record()
    for changed in ({**record, "version": "future"}, {**record, "operation": "slice"},
                    {k: v for k, v in record.items() if k != "reserve_bytes"}):
        with pytest.raises(ValueError):
            CodecMemoryPolicy.from_record(changed)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worker guard")
def test_real_child_ceiling_denies_allocation_and_does_not_change_parent():
    import resource

    before = resource.getrlimit(resource.RLIMIT_AS)
    program = '''
import resource
from modelark.codec_resources import CodecMemoryPolicy
p = CodecMemoryPolicy(256 << 20, 0)
p.install_in_worker()
assert resource.getrlimit(resource.RLIMIT_AS) == (256 << 20, 256 << 20)
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
try:
    bytearray(512 << 20)
except MemoryError:
    print("allocation-refused")
else:
    raise AssertionError("guard did not enforce allocation ceiling")
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "allocation-refused"
    assert resource.getrlimit(resource.RLIMIT_AS) == before


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worker guard")
@pytest.mark.parametrize("hard_mib", [256, 1024])
def test_inherited_lower_limit_is_not_silently_relaxed(hard_mib):
    program = f'''
import resource
from modelark.codec_resources import CodecMemoryPolicy, CodecResourceRefusal
resource.setrlimit(resource.RLIMIT_AS, (256 << 20, {hard_mib} << 20))
try:
    CodecMemoryPolicy(512 << 20, 0).install_in_worker()
except CodecResourceRefusal:
    print("refused")
else:
    raise AssertionError("inherited ceiling was ignored")
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worker guard")
def test_native_abort_is_contained_without_core_dump(tmp_path):
    import signal

    program = '''
import os, resource
from modelark.codec_resources import CodecMemoryPolicy
CodecMemoryPolicy(256 << 20, 0).install_in_worker()
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
os.abort()
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True)
    assert result.returncode == -signal.SIGABRT
