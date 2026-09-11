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
import os
from pathlib import Path
import random
import resource
import subprocess
import sys
import tempfile

from modelark.codec_resources import CodecMemoryPolicy, available_memory
from modelark.codec_process import isolated_command, initialize_worker, runtime_record, validate_runtime

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


def worker(request_path, parent_pid):
    request = json.loads(Path(request_path).read_text())
    policy = CodecMemoryPolicy.from_record(request["policy"])
    initialize_worker(policy, parent_pid)  # same guarded startup as the decoder
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
                              "worker": validate_runtime(runtime_record(), policy),
                              "zipnn_module": str(Path(zipnn.__file__).name)})


def _guarded_roundtrip(encoded, size, expected_hash, policy):
    """Exercise the actual bounded parent transport, not a buffered stand-in."""
    from modelark.codec_supervisor import guarded_zipnn_frame, IO_BYTES

    before = _metrics()
    digest = hashlib.sha256()
    total = maximum_piece = 0
    evidence = {}
    with encoded.open("rb") as source:
        with guarded_zipnn_frame(source, policy=policy,
                                  max_stored_bytes=encoded.stat().st_size,
                                  max_decoded_bytes=size, remaining_bytes=size,
                                  on_complete=evidence.update) as chunks:
            for piece in chunks:
                maximum_piece = max(maximum_piece, len(piece))
                total += len(piece)
                digest.update(piece)
        if source.read(1):
            raise RuntimeError("guarded whole-frame fixture has trailing content")
    if total != size or digest.hexdigest() != expected_hash or maximum_piece > IO_BYTES:
        raise RuntimeError("guarded round trip differs from original or IO ceiling")
    return {"passed": True, "phase": "guarded-reader", "codec": "zipnn-whole",
            "policy": policy.to_record(), "original_bytes": size,
            "original_sha256": digest.hexdigest(), "maximum_output_piece": maximum_piece,
            **evidence,
            "parent_metrics_before": before, "parent_metrics_after": _metrics()}


def run(*, large=False, output_parent=None, guarded_reader=False):
    policy = CodecMemoryPolicy(8 << 30, 2 << 30)
    policy.admit(available_memory()["available_bytes"])
    output = Path(tempfile.mkdtemp(prefix="modelark-codec-qualification-", dir=output_parent))
    report = {"status": "running", "scope": "synthetic-codec-resource-qualification-only",
              "policy": policy.to_record(), "results": [], "large": large,
              "guarded_reader": guarded_reader,
              "production_adoption": False, "physical_USB_or_archive_test": False}
    checkout = Path(__file__).resolve().parent.parent
    report["implementation_sha256"] = {
        name: _sha(checkout / name) for name in (
            "modelark/codec_resources.py", "modelark/compress.py", "modelark/streamznn.py",
            "modelark/codec_worker.py", "modelark/codec_supervisor.py",
            "modelark/codec_process.py",
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
            proc = subprocess.Popen(isolated_command("scripts.qualify_codec_resources",
                                     "--worker", request_path, "--parent-pid", os.getpid()), stdout=out, stderr=err,
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
        if large and guarded_reader:
            sizes.insert(0, 64 << 20)  # Default StreamZNN-sized native work unit too.
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
                    if guarded_reader and codec == "zipnn-whole":
                        result = _guarded_roundtrip(case / "misleading.blob", size, digest, policy)
                        report["results"].append(result)
                        print(f"passed guarded reader: {size} original bytes", flush=True)
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
    parser.add_argument("--guarded-reader", action="store_true",
                        help="also qualify bounded parent/worker whole-frame transport")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.parent_pid)
    else:
        print(run(large=args.large, output_parent=args.output_parent, guarded_reader=args.guarded_reader))


if __name__ == "__main__":
    main()
