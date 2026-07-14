#!/usr/bin/env python3
"""Collect the two sanitized empirical gates required before DEC-045 Phase 3.

This command never writes to the supplied catalog or model shard.  Temporary codec output and the
disposable concurrency clone live below ``--scratch-dir`` and are removed before exit.  Its JSON is
safe to attach to a review: repository ids, filenames, drive labels, and local paths are omitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from modelark import capacity, compress, plan, reconcile, streamznn


class GateEvidenceError(RuntimeError):
    """The supplied artifact cannot prove the requested Phase 3 gate."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safetensors_dtypes(path: Path) -> tuple[str, ...]:
    with path.open("rb") as handle:
        raw_size = handle.read(8)
        if len(raw_size) != 8:
            raise GateEvidenceError("representative shard has no safetensors header")
        header_size = int.from_bytes(raw_size, "little")
        if header_size <= 0 or header_size > 100 * 1024 * 1024:
            raise GateEvidenceError("representative shard has an invalid safetensors header size")
        try:
            header = json.loads(handle.read(header_size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateEvidenceError("representative shard has an invalid safetensors header") from exc
    dtypes = sorted({
        value.get("dtype")
        for name, value in header.items()
        if name != "__metadata__" and isinstance(value, dict) and value.get("dtype")
    })
    if "BF16" not in dtypes:
        raise GateEvidenceError("representative safetensors shard contains no BF16 tensor")
    return tuple(dtypes)


def measure_streamznn(
    shard: Path,
    scratch_dir: Path,
    *,
    chunk_bytes: int = streamznn.DEFAULT_CHUNK,
) -> dict:
    """Measure real filesystem high-water and byte identity for one BF16 safetensors shard."""
    shard = shard.expanduser().resolve(strict=True)
    if not shard.is_file():
        raise GateEvidenceError("representative shard must be a regular file")
    dtypes = _safetensors_dtypes(shard)
    raw_size = shard.stat().st_size
    cap = compress.codec_output_cap(
        raw_size,
        compress.CODEC_STREAM,
        stream_chunk_bytes=chunk_bytes,
    )
    scratch_dir = scratch_dir.expanduser().resolve(strict=True)
    free = shutil.disk_usage(scratch_dir).free
    if free < cap + (64 << 20):
        raise GateEvidenceError(
            f"scratch filesystem has {free} free bytes; needs at least {cap + (64 << 20)}"
        )

    source_sha = _sha256(shard)
    with tempfile.TemporaryDirectory(prefix="modelark-phase3-codec-", dir=scratch_dir) as tmp:
        root = Path(tmp)
        output = root / "representative.znn"
        stop = threading.Event()
        peak = [0]

        def sample_disk() -> None:
            while not stop.wait(0.01):
                total = 0
                for entry in root.iterdir():
                    try:
                        if entry.is_file():
                            total += entry.stat().st_size
                    except FileNotFoundError:  # atomic temp -> final rename raced the sample
                        pass
                peak[0] = max(peak[0], total)

        monitor = threading.Thread(target=sample_disk, name="phase3-codec-high-water", daemon=True)
        monitor.start()
        started = time.perf_counter()
        try:
            streamznn.compress_file(
                shard,
                output,
                dtype="bfloat16",
                chunk_bytes=chunk_bytes,
                max_output_bytes=cap,
            )
            duration = time.perf_counter() - started
            output_size = output.stat().st_size
            peak[0] = max(peak[0], output_size)
            roundtrip_ok = streamznn.verify_sha256(output, source_sha)
        finally:
            stop.set()
            monitor.join()

    if not roundtrip_ok:
        raise GateEvidenceError("StreamZNN canary did not reproduce the source shard hash")
    if peak[0] > cap:
        raise GateEvidenceError(f"observed filesystem high-water {peak[0]} exceeded cap {cap}")
    return {
        "gate": "real_bf16_streamznn_high_water",
        "passed": True,
        "input_bytes": raw_size,
        "tensor_dtypes": list(dtypes),
        "chunk_bytes": chunk_bytes,
        "output_bytes": output_size,
        "filesystem_high_water_bytes": peak[0],
        "enforced_cap_bytes": cap,
        "cap_headroom_bytes": cap - peak[0],
        "compression_ratio": round(output_size / raw_size, 6) if raw_size else 0,
        "duration_seconds": round(duration, 3),
        "roundtrip_sha256_verified": True,
    }


def _percentile_95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"{path.expanduser().resolve(strict=True).as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
        check_same_thread=False,
    )


def _active_plan(con: sqlite3.Connection) -> tuple[str, str]:
    active = plan.active(con)
    if active is None:
        raise GateEvidenceError("catalog copy has no active plan")
    return active["plan_id"], active["capacity_mode"]


def _run_shadow(con: sqlite3.Connection, plan_id: str, capacity_mode: str) -> tuple[dict, float]:
    started = time.perf_counter()
    report = reconcile.shadow_report(con, plan_id, capacity_mode=capacity_mode)
    return report, time.perf_counter() - started


def _run_executor_path(
    con: sqlite3.Connection,
    plan_id: str,
    capacity_mode: str,
) -> tuple[reconcile.ReconcileResult, capacity.CapacityPlan, float]:
    """Time only the graph and ledger that the Phase 3 executor will actually consume."""
    started = time.perf_counter()
    graph = reconcile.reconcile_plan(con, plan_id)
    ledger = capacity.plan_capacity(con, graph, capacity_mode=capacity_mode)
    return graph, ledger, time.perf_counter() - started


def measure_catalog_replay(
    catalog: Path,
    scratch_dir: Path,
    *,
    samples: int = 20,
) -> dict:
    """Replay a copied catalog read-only and prove a concurrent writer does not block it."""
    if samples < 2:
        raise GateEvidenceError("catalog replay requires at least two samples")
    catalog = catalog.expanduser().resolve(strict=True)
    scratch_dir = scratch_dir.expanduser().resolve(strict=True)
    source = _open_read_only(catalog)
    try:
        source.execute("PRAGMA query_only=ON")
        plan_id, capacity_mode = _active_plan(source)
        _run_executor_path(source, plan_id, capacity_mode)  # warm caches; excluded from p95
        timings = []
        for _ in range(samples):
            graph, ledger, elapsed = _run_executor_path(source, plan_id, capacity_mode)
            timings.append(elapsed)
        # Legacy normalization is a Phase 1/2 review seam, not part of the Phase 3 production loop.
        # Run it once for equivalence evidence, but never charge it to the executor latency budget.
        report, shadow_elapsed = _run_shadow(source, plan_id, capacity_mode)

        # Exercise the same graph while a writer owns a RESERVED lock, but only on an ephemeral
        # consistent backup.  The operator-supplied catalog remains OS-level read-only throughout.
        with tempfile.TemporaryDirectory(prefix="modelark-phase3-catalog-", dir=scratch_dir) as tmp:
            clone = Path(tmp) / "catalog.sqlite"
            writable = sqlite3.connect(clone, isolation_level=None)
            try:
                source.backup(writable)
                writable.execute("PRAGMA journal_mode=WAL")
                writable.execute("BEGIN IMMEDIATE")
                reader = _open_read_only(clone)
                try:
                    clone_plan_id, clone_capacity_mode = _active_plan(reader)
                    _run_executor_path(reader, clone_plan_id, clone_capacity_mode)
                finally:
                    reader.close()
                    writable.execute("ROLLBACK")
            finally:
                writable.close()

        graph_payload = graph.to_dict()
        diagnostics = Counter(item["severity"] for item in graph_payload["diagnostics"])
        p95_ms = 1000 * _percentile_95(timings)
        legacy_error = report["shadow"].get("legacy_error")
        return {
            "gate": "copied_catalog_release_host_replay",
            "passed": p95_ms <= 500,
            "catalog_bytes": catalog.stat().st_size,
            "selected_repositories": len(report["repos"]),
            "archived_rows": source.execute("SELECT count(*) FROM archived").fetchone()[0],
            "graph_hash": graph_payload["graph_hash"],
            "work_intents": len(graph.intents),
            "assigned_tasks": len(ledger.tasks),
            "capacity_feasible": ledger.feasible,
            "capacity_failures": len(ledger.failures),
            "diagnostics_by_severity": dict(sorted(diagnostics.items())),
            "legacy_comparison_available": legacy_error is None,
            "legacy_error_type": legacy_error.split(":", 1)[0] if legacy_error else None,
            "legacy_target_equivalent": report["placement_comparison"]["target_equivalent"],
            "samples": samples,
            "executor_path_p95_milliseconds": round(p95_ms, 3),
            "executor_path_maximum_milliseconds": round(1000 * max(timings), 3),
            "shadow_comparison_milliseconds": round(1000 * shadow_elapsed, 3),
            "release_host_budget_milliseconds": 500,
            "concurrent_writer_read_succeeded": True,
            "source_opened_uri_mode_ro": True,
        }
    finally:
        source.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    codec = sub.add_parser("streamznn", help="measure a representative real BF16 shard")
    codec.add_argument("shard", type=Path)
    codec.add_argument("--scratch-dir", type=Path, required=True)
    codec.add_argument("--chunk-bytes", type=int, default=streamznn.DEFAULT_CHUNK)
    catalog = sub.add_parser("catalog", help="replay an operator-approved copied SQLite catalog")
    catalog.add_argument("catalog_copy", type=Path)
    catalog.add_argument("--scratch-dir", type=Path, required=True)
    catalog.add_argument("--samples", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "streamznn":
            evidence = measure_streamznn(
                args.shard,
                args.scratch_dir,
                chunk_bytes=args.chunk_bytes,
            )
        else:
            evidence = measure_catalog_replay(
                args.catalog_copy,
                args.scratch_dir,
                samples=args.samples,
            )
    except (GateEvidenceError, sqlite3.DatabaseError) as exc:
        print(json.dumps({
            "gate": args.command,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, sort_keys=True))
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
