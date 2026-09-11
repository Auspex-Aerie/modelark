"""Explicit startup boundary shared by codec and qualification children.

Only trusted stdlib/project bootstrap code runs before the guards. Dependency
site hooks run afterwards, including virtualenv discovery on Python 3.10/3.12.
This is not an OS sandbox or protection against malicious installed code.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import sys


class WorkerInitializationError(RuntimeError):
    """The required child startup/lifetime contract could not be established."""


def isolated_command(module, *args):
    """Pin project imports and suppress automatic site initialization."""
    root = str(Path(__file__).resolve().parent.parent)
    bootstrap = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                 "import runpy; runpy.run_module(sys.argv.pop(1), run_name='__main__')")
    return [sys.executable, "-I", "-S", "-c", bootstrap, root, module, *map(str, args)]


def bind_parent(parent_pid):
    """Kill on spawning-thread death; check the race before installing prctl."""
    if sys.platform != "linux" or type(parent_pid) is not int or parent_pid <= 0:
        raise WorkerInitializationError("codec parent-death guard requires Linux and parent PID")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise WorkerInitializationError("could not install codec parent-death guard")
    if os.getppid() != parent_pid:
        raise WorkerInitializationError("codec parent exited before worker initialization")


def initialize_worker(policy, parent_pid):
    """Fresh -I -S child only: lifetime, memory, then dependency initialization."""
    if not sys.flags.isolated or not sys.flags.no_site:
        raise WorkerInitializationError("codec worker requires isolated no-site startup")
    bind_parent(parent_pid)
    policy.install_in_worker()
    # -S prevents even `import site` from running hooks automatically. Explicit
    # main() restores virtualenv paths before 3.14 as well as normal site hooks,
    # but only after the irreversible memory guard and parent binding exist.
    import site
    site.main()


def runtime_record():
    """Actual child observations, not binary attestation or another process's versions."""
    import importlib.metadata
    import resource
    import torch
    import zipnn

    parent_signal = ctypes.c_int()
    if ctypes.CDLL(None, use_errno=True).prctl(2, ctypes.byref(parent_signal), 0, 0, 0) != 0:
        raise WorkerInitializationError("could not observe parent-death guard")
    return {
        "version": "modelark.codec-runtime.v1",
        "startup": "isolated-no-site-guard-then-site.v1",
        "python": sys.version, "executable": sys.executable,
        "prefix": sys.prefix, "base_prefix": sys.base_prefix,
        "address_space_bytes": list(resource.getrlimit(resource.RLIMIT_AS)),
        "parent_death_signal": parent_signal.value,
        "zipnn": {"distribution_version": importlib.metadata.version("zipnn"),
                  "module": str(Path(zipnn.__file__).resolve())},
        "torch": {"distribution_version": importlib.metadata.version("torch"),
                  "module": str(Path(torch.__file__).resolve())},
    }


def validate_runtime(record, policy):
    """Closed-world, bounded identity and observed-guard record for this session."""
    if type(record) is not dict or set(record) != {
            "version", "startup", "python", "executable", "prefix", "base_prefix",
            "address_space_bytes", "parent_death_signal", "zipnn", "torch"}:
        raise ValueError("invalid worker runtime fields")
    if (record["version"] != "modelark.codec-runtime.v1"
            or record["startup"] != "isolated-no-site-guard-then-site.v1"):
        raise ValueError("unknown worker runtime version")
    for name in ("python", "executable", "prefix", "base_prefix"):
        value = record[name]
        if type(value) is not str or not 0 < len(value) <= 1024:
            raise ValueError("invalid worker runtime identity")
        if name != "python" and not os.path.isabs(value):
            raise ValueError("worker runtime path must be absolute")
    limits = record["address_space_bytes"]
    if (type(limits) is not list or len(limits) != 2
            or any(type(limit) is not int or limit != policy.address_space_bytes for limit in limits)
            or type(record["parent_death_signal"]) is not int
            or record["parent_death_signal"] != signal.SIGKILL):
        raise ValueError("worker guard evidence differs from request")
    for name in ("zipnn", "torch"):
        identity = record[name]
        if type(identity) is not dict or set(identity) != {"distribution_version", "module"}:
            raise ValueError("invalid worker dependency fields")
        if any(type(value) is not str or not 0 < len(value) <= 1024 for value in identity.values()):
            raise ValueError("invalid worker dependency identity")
        if not os.path.isabs(identity["module"]):
            raise ValueError("worker dependency path must be absolute")
    return record
