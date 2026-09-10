"""Disposable Stage-A codec round trips under one shared AS policy.

Run with the project's dev Python: -m scripts.qualify_codec_resources [--large].
No live catalog, archive, service, USB, network or host configuration is accessed.
The parent observes Linux host/visible ancestor cgroup headroom before each
sequential child. This is a sampled admission, not a system memory reservation.
The qualification report is evidence for this runtime and fixtures, not blanket
qualification of every codec input or a completed Slice delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import resource
import subprocess
import sys
import tempfile

from modelark.codec_resources import CodecMemoryPolicy, CodecResourceRefusal


def available_memory() -> dict:
    """Conservative visible Linux memory headroom; unknown accounting refuses."""
    try:
        return _available_memory()
    except (OSError, ValueError, OverflowError) as exc:
        raise CodecResourceRefusal("could not establish memory headroom") from exc


def _available_memory():
    host_available = None
    for line in Path("/proc/meminfo").read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == "MemAvailable:":
            if len(fields) != 3 or fields[2] != "kB":
                raise CodecResourceRefusal("unrecognized MemAvailable accounting")
            host_available = int(fields[1]) * 1024
            if host_available < 0:
                raise CodecResourceRefusal("negative host memory accounting")
    if host_available is None:
        raise CodecResourceRefusal("host available memory is unknown")
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()
              if " - cgroup2 " in line]
    if len(mounts) != 1 or mounts[0][3:5] != ["/", "/sys/fs/cgroup"]:
        raise CodecResourceRefusal("full cgroup2 hierarchy is not visible")
    groups = Path("/proc/self/cgroup").read_text().splitlines()
    if len(groups) != 1 or not groups[0].startswith("0::/"):
        raise CodecResourceRefusal("unrecognized unified cgroup membership")
    relative = groups[0][4:]
    if relative and any(part in {"", ".", ".."} for part in relative.split("/")):
        raise CodecResourceRefusal("invalid cgroup membership")
    root = Path("/sys/fs/cgroup")
    current = root / relative
    cgroup_headroom = {}
    while True:
        # Initial hierarchy roots have no memory.max; a visible namespace root
        # may have one and its limit must not be discarded.
        if current != root or (current / "memory.max").exists():
            maximum = (current / "memory.max").read_text().strip()
            used = int((current / "memory.current").read_text().strip())
            if used < 0 or maximum != "max" and int(maximum) < 0:
                raise CodecResourceRefusal("negative cgroup memory accounting")
            if maximum != "max":
                cgroup_headroom[str(current.relative_to(root))] = max(0, int(maximum) - used)
        if current == root:
            break
        current = current.parent
    return {"available_bytes": min([host_available, *cgroup_headroom.values()]),
            "observations": {"host_available_bytes": host_available,
                             "cgroup_headroom_bytes": cgroup_headroom}}


def _dump(path, record):
    with Path(path).open("x") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fixture(path, size):
    """A valid one-tensor BF16 safetensors file, generated without tensor APIs."""
    header_size = 256
    payload = size - 8 - header_size
    if payload <= 0 or payload % 2:
        raise ValueError("fixture size must accommodate an even BF16 payload")
    header = json.dumps({"weight": {"dtype": "BF16", "shape": [payload // 2],
                                    "data_offsets": [0, payload]}}).encode()
    if len(header) > header_size:
        raise ValueError("fixture header too large")
    random_bytes = random.Random(12345).randbytes(1 << 19)
    block = bytearray(1 << 20)
    block[::2], block[1::2] = random_bytes, b"\x3f" * (1 << 19)
    with path.open("xb") as handle:
        handle.write(header_size.to_bytes(8, "little"))
        handle.write(header.ljust(header_size, b" "))
        remaining = payload
        while remaining:
            piece = block[:min(remaining, len(block))]
            handle.write(piece)
            remaining -= len(piece)
    return _sha(path)


def _metrics():
    result = {"peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    for line in Path("/proc/self/status").read_text().splitlines():
        fields = line.split()
        if fields[0] in {"VmPeak:", "VmSize:", "VmRSS:"}:
            result[fields[0][:-1] + "_bytes"] = int(fields[1]) * 1024
    return result


def worker(request_path):
    request = json.loads(Path(request_path).read_text())
    policy = CodecMemoryPolicy.from_record(request["policy"])
    policy.install_in_worker()  # before native imports, same for every operation
    before = _metrics()
    if request["phase"] == "allocation-refusal":
        try:
            bytearray(policy.address_space_bytes + 1)
        except MemoryError:
            _dump(request["result"], {"passed": True, "allocation_refused": True,
                                      "policy": policy.to_record(), "metrics": _metrics()})
            return
        raise RuntimeError("allocation ceiling failed")
    from modelark import compress
    import zipnn

    imported = _metrics()
    source, encoded, restored = (Path(request[name]) for name in ("source", "encoded", "restored"))
    phase = request["phase"]
    if phase == "compress":
        compress.compress_file(source, encoded, codec=request["codec"], threads=1)
    elif phase == "canary":
        if not compress.canary_ok(encoded, request["sha256"]):
            raise RuntimeError("canary hash mismatch")
    elif phase == "restore":
        compress.decompress_file(encoded, restored)
        if restored.stat().st_size != source.stat().st_size or _sha(restored) != request["sha256"]:
            raise RuntimeError("restored original bytes mismatch")
    else:
        raise ValueError("unknown qualification phase")
    if _sha(source) != request["sha256"]:
        raise RuntimeError("source changed")
    with encoded.open("rb") as handle:
        magic = handle.read(5)
    if request["codec"] == "streamznn":
        if magic != b"SZNN\x01":
            raise RuntimeError("writer did not produce StreamZNN")
    elif request["codec"] == "zipnn-whole":
        if magic[:4] != b"ZN\x00\x05":
            raise RuntimeError("writer did not produce whole ZipNN 0.5")
    else:
        raise ValueError("unknown fixture codec")
    _dump(request["result"], {"passed": True, "phase": phase, "policy": policy.to_record(),
                              "metrics_before_import": before, "metrics_after_import": imported,
                              "metrics": _metrics(), "stored_bytes": encoded.stat().st_size,
                              "stored_sha256": _sha(encoded), "original_sha256": request["sha256"],
                              "magic_hex": magic.hex(), "python": sys.version,
                              "original_bytes": source.stat().st_size,
                              "codec": request["codec"],
                              "zipnn_version": importlib.metadata.version("zipnn"),
                              "torch_version": importlib.metadata.version("torch"),
                              "zipnn_module": str(Path(zipnn.__file__).name)})


def run(*, large=False, output_parent=None):
    policy = CodecMemoryPolicy(8 << 30, 2 << 30)
    policy.admit(available_memory()["available_bytes"])
    output = Path(tempfile.mkdtemp(prefix="modelark-codec-qualification-", dir=output_parent))
    report = {"status": "running", "scope": "synthetic-codec-resource-qualification-only",
              "policy": policy.to_record(), "results": [], "large": large,
              "production_adoption": False, "physical_USB_or_archive_test": False}
    checkout = Path(__file__).resolve().parent.parent
    report["implementation_sha256"] = {
        name: _sha(checkout / name) for name in (
            "modelark/codec_resources.py", "modelark/compress.py", "modelark/streamznn.py",
            "scripts/qualify_codec_resources.py")}
    print(f"Qualification directory: {output}", flush=True)
    index = 0

    def launch(request):
        nonlocal index
        sample = available_memory()
        policy.admit(sample["available_bytes"])
        index += 1
        result_path = output / f"{index:02d}-result.json"
        request_path = output / f"{index:02d}-request.json"
        request = {**request, "policy": policy.to_record(), "result": str(result_path)}
        _dump(request_path, request)
        # Native libraries may print: protocol is an exclusive result file, not stdout.
        with (output / f"{index:02d}-stdout.txt").open("x") as out, \
                (output / f"{index:02d}-stderr.txt").open("x") as err:
            proc = subprocess.Popen([sys.executable, "-m", "scripts.qualify_codec_resources",
                                     "--worker", str(request_path)], stdout=out, stderr=err,
                                    close_fds=True)
            try:
                code = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
        if code != 0 or not result_path.exists():
            raise RuntimeError(f"qualification child {index} failed with exit {code}; see {output}")
        result = json.loads(result_path.read_text())
        if result.get("passed") is not True or result.get("policy") != policy.to_record():
            raise RuntimeError("child did not certify requested policy")
        report["results"].append({"phase": request["phase"], "admission": sample, **result})
        print(f"passed {index}: {request['phase']} {request.get('codec', '')}", flush=True)

    try:
        launch({"phase": "allocation-refusal"})
        sizes = [99_630_640, 409_993_344] if large else [2 << 20]
        with tempfile.TemporaryDirectory(prefix="synthetic-", dir=output) as scratch:
            for size in sizes:
                for codec in ("zipnn-whole", "streamznn"):
                    case = Path(scratch) / f"{codec}-{size}"
                    case.mkdir()
                    source = case / "weights.safetensors"
                    digest = _fixture(source, size)
                    request = {"source": str(source), "encoded": str(case / "misleading.blob"),
                               "restored": str(case / "restored.safetensors"),
                               "codec": codec, "sha256": digest}
                    for phase in ("compress", "canary", "restore"):
                        launch({**request, "phase": phase})
        if any(_sha(checkout / name) != digest
               for name, digest in report["implementation_sha256"].items()):
            raise RuntimeError("implementation changed during qualification; rerun unchanged code")
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _dump(output / "result.json", report)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--large", action="store_true", help="qualify the two real failure sizes")
    parser.add_argument("--output-parent", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        print(run(large=args.large, output_parent=args.output_parent))


if __name__ == "__main__":
    main()
