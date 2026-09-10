"""Actual old/new wheel exclusion matrix on disposable copied catalogs and bytes.

No CLI, portal, downloads, live catalogs, or devices. The only lock-policy test
seam forces physical acquisition nonblocking so contention is an observable
refusal. Real wheel workflow/identity code still chooses every physical key.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import zipfile


LABEL = "qualification-drive"
CAPACITY = 10**12


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog_files(path):
    return {item.name: _digest(item) for item in path.parent.iterdir()
            if item.is_file() and item.name.startswith(path.name)}


def _origins(extracted):
    origins = {}
    for name, module in tuple(sys.modules.items()):
        if name == "modelark" or name.startswith("modelark."):
            path = getattr(module, "__file__", None)
            if path:
                resolved = Path(path).resolve()
                resolved.relative_to(extracted)
                origins[name] = str(resolved)
    if not origins:
        raise AssertionError("no wheel imports verified")
    return origins


def _fingerprint(serial):
    from modelark.capacity_evidence import identity_fingerprint_v1
    return identity_fingerprint_v1(fs_uuid="qualification-fs", annex_uuid="qualification-annex",
                                   serial=serial, filesystem_capacity_bytes=CAPACITY)


def _client_context(args):
    case = Path(args["case"])
    return {"pid": os.getpid(), "catalog": str(case / "data" / "catalog.sqlite"),
            "state_directory": str(case / "state"), "physical_lock_namespace": args["locks"]}


def _prepare(case):
    from modelark.core import db
    from modelark import drive_bootstrap as bs, plan, proposal, register

    archive = case / "archive" / "qualification" / "tiny"
    archive.mkdir(parents=True)
    payload = b"disposable serial identity qualification source\n"
    (archive / "model.safetensors").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    con = db.connect()
    proof = json.dumps({"v": 1, "fs_uuid": "qualification-fs", "annex_uuid": "qualification-annex",
                        "serial": None}, sort_keys=True, separators=(",", ":"))
    con.execute(
        "INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,capacity_bytes,free_bytes,"
        "identity_epoch,write_generation,filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES(?,'qualification-fs','qualification-annex','SERIAL',?,?,1,1,?,?,'dedicated_local')",
        [LABEL, CAPACITY, CAPACITY, CAPACITY, _fingerprint(None)])
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES(?,1,1,'qualification')", [LABEL])
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
        "VALUES(?,1,1,?,?,?,'dedicated_local',?,?,'test')",
        [LABEL, CAPACITY, CAPACITY, _fingerprint(None), proof, proof])
    for repo in ("qualification/tiny", "qualification/pending"):
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) "
                    "VALUES(?,'model.safetensors',?,?,'safetensors','bf16')", [repo, len(payload), digest])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_relpath,orig_bytes,stored_bytes,"
        "orig_sha256,orig_sha256_provenance,compressed) VALUES('qualification/tiny','model.safetensors',"
        "?,'model.safetensors',?,?,?,'ingestion_computed',0)", [LABEL, len(payload), len(payload), digest])
    con.execute("INSERT INTO replicas(repo_id,rfilename,drive_label,present) "
                "VALUES('qualification/tiny','model.safetensors',?,1)", [LABEL])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('qualification/pending','test')")
    plan.create(con, "ark", name="Qualification")
    plan.add_drive(con, "ark", LABEL)
    plan.set_active(con, "ark")
    register.archive_path = lambda *args: None  # Offline clean evidence for draft creation; no device resolution.
    proposal.create_draft(con, plan_id="ark")
    con.close()
    legacy = case / "legacy.sqlite"
    shutil.copy2(db.DB_PATH, legacy)
    con = db.connect()
    # The actual repair runs against real synthetic files, with only hardware
    # observation injected. Its inventory, backup, rehearsal and commit are real.
    bs._live_evidence = lambda *args: bs._LiveEvidence(
        str(case / "archive"), "qualification-fs", "qualification-annex", "SERIAL",
        CAPACITY, CAPACITY, 4096, _fingerprint("SERIAL"), True)
    before = _digest(archive / "model.safetensors")
    repair = bs.repair_serial_identity(
        con, LABEL, expected_binding=bs.inspect_serial_identity(con, LABEL)["binding"],
        now="test", writers_stopped=True, blocking=False)
    assert repair["status"] == "repaired"
    assert con.execute("PRAGMA user_version").fetchone() == (8,)
    con.close()
    assert _digest(archive / "model.safetensors") == before
    shutil.copy2(db.DB_PATH, case / "repaired.sqlite")
    # A v7 canonical historical copy exercises actual old code with the other
    # key. This is synthetic reader-version setup, not a catalog downgrade tool.
    shutil.copy2(db.DB_PATH, case / "canonical-v7.sqlite")
    canonical = sqlite3.connect(case / "canonical-v7.sqlite")
    canonical.execute("PRAGMA user_version=7")
    canonical.close()
    return {"repair": repair, "synthetic_source_sha256": before,
            "catalogs": {name: str(case / name) for name in
                         ("legacy.sqlite", "repaired.sqlite", "canonical-v7.sqlite")}}


def _worker(args):
    extracted, case = Path(args["wheel"]), Path(args["case"])
    sys.path.insert(0, str(extracted))
    from modelark.core import db
    from modelark import drive_fence, execution_service

    db.configure(data_dir=case / "data", state_dir=case / "state")
    drive_fence._LOCK_DIR = Path(args["locks"])
    operation = args["operation"]
    if operation == "prepare":
        return {"outcome": "prepared", **_prepare(case), "origins": _origins(extracted)}
    if operation == "fd-child":
        os.write(args["ready_fd"], b"ready")
        os.close(args["ready_fd"])
        if sys.stdin.readline().strip() != "release":
            raise RuntimeError("child release command missing")
        for fd in args["fds"]:
            os.close(fd)
        return {"outcome": "child_released", "origins": _origins(extracted)}
    path = case / "data" / "catalog.sqlite"
    before = _digest(path)
    if operation == "old-slice":
        from modelark.slice import catalog, domain
        files_before = _catalog_files(path)
        try:
            catalog.read_catalog(path, domain.SliceSpec(("qualification/tiny",), "disposable", "root"))
        except domain.SliceRefusal as exc:
            assert exc.code == "CATALOG_VERSION_UNSUPPORTED"
            assert _digest(path) == before
            files_after = _catalog_files(path)
            return {"outcome": "rejected", "code": exc.code, "catalog_byte_identical": True,
                    "entire_catalog_bundle_byte_identical": files_after == files_before,
                    "sqlite_sidecars_created": sorted(set(files_after) - set(files_before)),
                    "source_opened": False, "origins": _origins(extracted)}
        raise AssertionError("actual old Slice accepted repaired v8")
    con = db.connect()
    # Writable connect may install its normal idempotent schema objects. The
    # contention invariant starts after that open, not before normal open setup.
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = _digest(path)
    logical_before = tuple(con.iterdump())
    physical_acquire = drive_fence._acquire

    def nonblocking(path, blocking, **kwargs):
        return physical_acquire(path, False, **kwargs)

    drive_fence._acquire = nonblocking
    io_called = []

    def no_io(*args, **kwargs):
        io_called.append(True)
        raise AssertionError("contended consumer crossed physical fence into IO/admission")

    try:
        if operation in {"hold", "try-hold", "child-parent"}:
            services = execution_service.production_services(con, catalog_path=path, state_dir=case / "state")
            with services.drive_fences.hold_all_sorted([LABEL]) as handles:
                if operation == "try-hold":
                    return {"outcome": "acquired", "count": len(handles), "origins": _origins(extracted)}
                if operation == "hold":
                    print(json.dumps({"outcome": "holding", "count": len(handles),
                                      "origins": _origins(extracted), "client": _client_context(args)}), flush=True)
                    if sys.stdin.readline().strip() != "release":
                        raise RuntimeError("holder release command missing")
                else:
                    read_fd, write_fd = os.pipe()
                    fds = [handle.fileno() for handle in handles]
                    child_args = {**args, "operation": "fd-child", "fds": fds, "ready_fd": write_fd}
                    child = subprocess.Popen(_command(child_args), stdin=sys.stdin,
                                             pass_fds=(*fds, write_fd))
                    os.close(write_fd)
                    try:
                        assert os.read(read_fd, 5) == b"ready"
                    finally:
                        os.close(read_fd)
            return {"outcome": "parent_released" if operation == "child-parent" else "released",
                    "child_pid": child.pid if operation == "child-parent" else None,
                    "origins": _origins(extracted)}
        if operation == "source":
            from modelark.slice.sources import FencedSources
            row = con.execute("SELECT fs_uuid,annex_uuid,serial,filesystem_capacity_bytes,identity_epoch,"
                              "identity_fingerprint FROM drives WHERE drive_label=?", [LABEL]).fetchone()
            drive = SimpleNamespace(**dict(zip(("fs_uuid", "annex_uuid", "serial", "filesystem_capacity_bytes",
                                               "identity_epoch", "identity_fingerprint"), row)), drive_label=LABEL)
            candidate = SimpleNamespace(drive=drive, copy=SimpleNamespace(repo_id="qualification/tiny"))
            with FencedSources(path, SimpleNamespace(open=no_io)).open(candidate):
                no_io()
        elif operation == "writer":
            from modelark.drive_mutation import drive_mutation
            with drive_mutation(con, [LABEL], "qualification", observe=no_io, reconcile=no_io,
                                now="test", blocking=False):
                no_io()
        elif operation == "approval":
            from modelark import proposal
            pid = con.execute("SELECT proposal_id FROM placement_proposals WHERE lifecycle='draft'").fetchone()[0]
            proposal.approve(con, pid, services=SimpleNamespace(observe_exact_capacity=no_io))
        elif operation == "repair":
            from modelark import drive_bootstrap as bs
            bs._live_evidence = no_io
            bs.repair_serial_identity(con, LABEL,
                expected_binding=bs.inspect_serial_identity(con, LABEL)["binding"], now="test",
                writers_stopped=True, blocking=False)
        else:
            raise ValueError(operation)
        raise AssertionError("contended operation unexpectedly completed")
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(exc, drive_fence.FenceUnavailable):
            code = "DRIVE_FENCE_UNAVAILABLE"
        assert code in {"DRIVE_FENCE_UNAVAILABLE", "SOURCE_BUSY"}, repr(exc)
        assert not io_called
        assert tuple(con.iterdump()) == logical_before
        con.close()
        assert _digest(path) == before
        return {"outcome": "contended", "code": code, "catalog_byte_identical": True,
                "source_opened": False, "origins": _origins(extracted)}
    finally:
        con.close()


def _command(args):
    return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker", json.dumps(args)]


def _run(args):
    result = subprocess.run(_command(args), capture_output=True, text=True, cwd=args["case"])
    if result.returncode:
        raise RuntimeError(f"{args['operation']} failed:\n{result.stdout}\n{result.stderr}")
    return json.loads(result.stdout)


@contextmanager
def _holder(args):
    process = subprocess.Popen(_command(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, cwd=args["case"])
    try:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(process.stderr.read())
        event = json.loads(line)
        assert event["outcome"] in {"holding", "parent_released"}, event
        if args["operation"] == "child-parent":
            assert process.wait() == 0  # The inherited child, not its parent, now retains the locks.
        yield event
    finally:
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.write("release\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass
        stdout, stderr = process.communicate()
        if process.returncode or stderr:
            raise RuntimeError(f"holder cleanup failed: {stdout}\n{stderr}")
        released = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        expected = "child_released" if args["operation"] == "child-parent" else "released"
        if not released or released[-1]["outcome"] != expected:
            raise RuntimeError("holder did not confirm descriptor release")
        event["release_confirmed"] = True


def qualify(old_wheel, old_commit, updated_wheel, updated_commit, repository):
    sys.dont_write_bytecode = True
    helper_spec = importlib.util.spec_from_file_location(
        "reader_floor_qualification", Path(__file__).with_name("qualify_catalog_reader_floor.py"))
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    root = Path(tempfile.mkdtemp(prefix="modelark-serial-clients-"))
    wheels = {}
    for name, path, commit in (("old", old_wheel, old_commit), ("updated", updated_wheel, updated_commit)):
        path = Path(path).resolve(strict=True)
        revision, count = helper._verify_payload(path, repository, commit)
        extracted = root / (name + "-wheel")
        with zipfile.ZipFile(path) as package:
            if any(PurePosixPath(member).is_absolute() or ".." in PurePosixPath(member).parts
                   for member in package.namelist()):
                raise ValueError("unsafe wheel member")
            package.extractall(extracted)
        wheels[name] = {"path": str(path), "commit": revision, "sha256": _digest(path),
                        "payload_files_verified": count, "extracted": str(extracted)}
    locks = root / "physical-locks"
    seed = root / "seed"
    seed.mkdir()
    prepared = _run({"wheel": wheels["updated"]["extracted"], "case": str(seed),
                     "locks": str(locks), "operation": "prepare"})
    sequence = 0

    def client(version, operation, catalog="legacy.sqlite"):
        nonlocal sequence
        sequence += 1
        case = root / f"client-{sequence:02d}-{version}-{operation}"
        (case / "data").mkdir(parents=True)
        shutil.copy2(seed / catalog, case / "data" / "catalog.sqlite")
        return {"wheel": wheels[version]["extracted"], "case": str(case),
                "locks": str(locks), "operation": operation}

    checks = []
    old_holder = client("old", "hold")
    with _holder(old_holder) as held:
        assert held["count"] == 1
        for operation in ("source", "writer", "approval", "repair"):
            result = _run(client("updated", operation))
            assert result["outcome"] == "contended"
            assert result["client"]["catalog"] != held["client"]["catalog"]
            assert result["client"]["state_directory"] != held["client"]["state_directory"]
            checks.append({"cell": "old-null-vs-updated-" + operation, "holder": held, "consumer": result})
    with _holder(client("updated", "hold")) as held:
        assert held["count"] == 2
        for catalog in ("legacy.sqlite", "canonical-v7.sqlite"):
            result = _run(client("old", "try-hold", catalog))
            assert result["outcome"] == "contended"
            checks.append({"cell": "updated-dual-vs-old-" + catalog, "holder": held, "consumer": result})
    with _holder(client("updated", "child-parent")) as held:
        for catalog in ("legacy.sqlite", "canonical-v7.sqlite"):
            result = _run(client("old", "try-hold", catalog))
            assert result["outcome"] == "contended"
            checks.append({"cell": "surviving-child-vs-old-" + catalog, "holder": held, "consumer": result})
    for catalog in ("legacy.sqlite", "canonical-v7.sqlite"):
        result = _run(client("old", "try-hold", catalog))
        assert result["outcome"] == "acquired"
        checks.append({"cell": "after-child-release-" + catalog, "consumer": result})
    # Two old clients: v8 reader-floor refusal does not repair or revoke an old
    # physical-key holder still using a separate unrepaired v7 catalog copy.
    with _holder(client("old", "hold")) as held:
        result = _run(client("old", "old-slice", "repaired.sqlite"))
        assert result["outcome"] == "rejected"
        checks.append({"cell": "two-old-v7-fill-lock-vs-v8-slice-reader", "holder": held, "consumer": result})
    sentinel = seed / "archive" / "qualification" / "tiny" / "model.safetensors"
    assert _digest(sentinel) == prepared["synthetic_source_sha256"]
    report = {"status": "passed", "wheels": wheels, "checks": checks,
              "preparation": prepared, "retained_directory": str(root),
              "synthetic_source_unchanged": True, "physical_USB_or_archive_test": False,
              "limits": ["same-host flock namespace only; distinct disposable catalog/state paths",
                         "actual old Fill production lock adapter, not a full old Fill worker or download",
                         "nonblocking acquisition test seam only; real workflow code derives every lock key",
                         "contended-operation byte baseline is after normal writable catalog-open setup",
                         "canonical v7 is a synthetic old-key fixture, not a supported downgrade of repaired v8",
                         "repair hardware observation injected; synthetic source inventory and catalog repair are real",
                         "old SQLite read-only open may create WAL/SHM sidecars; main catalog and source remain unchanged",
                         "old Slice catalog refusal is before source-use construction, not positive projection"]}
    (root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-wheel", type=Path)
    parser.add_argument("--old-commit")
    parser.add_argument("--updated-wheel", type=Path)
    parser.add_argument("--updated-commit")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker_args = json.loads(args.worker)
        print(json.dumps({**_worker(worker_args), "client": _client_context(worker_args)}), flush=True)
        return
    if not all((args.old_wheel, args.old_commit, args.updated_wheel, args.updated_commit)):
        parser.error("both wheel paths and source commits are required")
    print(json.dumps(qualify(args.old_wheel, args.old_commit, args.updated_wheel,
                             args.updated_commit, args.repository), indent=2))


if __name__ == "__main__":
    main()
