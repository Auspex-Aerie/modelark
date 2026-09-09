"""Qualify an actual pre-fix wheel's reader floor using disposable catalogs only.

No application CLI, singleton, portal, archive mount, or live catalog is used.
The wheel's complete package payload is checked against the supplied git commit;
each reader then runs in a fresh isolated Python process importing that wheel.
This is the reader-floor gate, not the full two-client lock matrix (slice 5).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _verify_payload(wheel, repository, commit):
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], cwd=repository,
        capture_output=True, text=True, check=True).stdout.strip()
    archive = subprocess.run(
        ["git", "archive", revision], cwd=repository, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source, zipfile.ZipFile(wheel) as package:
        checked = []
        for name in package.namelist():
            if name.endswith("/") or not name.startswith(("modelark/", "scripts/")):
                continue
            original = source.extractfile(name)
            if original is None or original.read() != package.read(name):
                raise ValueError(f"Wheel payload does not match {revision}: {name}")
            checked.append(name)
        required = {"modelark/core/db.py", "modelark/core/schema.sql", "modelark/slice/catalog.py"}
        if not required.issubset(checked):
            raise ValueError("Wheel is missing reader-floor package files")
    return revision, len(checked)


def _tree_bytes(root):
    return {str(path.relative_to(root)): _sha(path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file()}


def _logical_catalog(path):
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0], tuple(con.iterdump())
    finally:
        con.close()


def _worker(extracted, case, reader, version):
    # -I excludes the working directory and PYTHONPATH. Only the explicit wheel
    # extraction is placed ahead of runtime dependencies; validate every import.
    sys.path.insert(0, str(extracted))
    from modelark.core import db
    from modelark.slice import catalog, domain

    origins = {name: str(Path(module.__file__).resolve()) for name, module in (
        ("core_db", db), ("slice_catalog", catalog), ("slice_domain", domain))}
    for origin in origins.values():
        Path(origin).relative_to(extracted)
    if db._SCHEMA_VERSION != 7:
        raise ValueError("Qualification requires the actual pre-fix v7 wheel")
    case.mkdir()  # Refuse reuse/overwrite, including accidental live directories.
    data = case / "data"
    data.mkdir()
    path = data / "catalog.sqlite"
    sentinel = case / "synthetic-source.bin"
    sentinel.write_bytes(b"reader-floor synthetic source; never archive bytes\n")
    con = sqlite3.connect(path)
    try:
        con.executescript(db.SCHEMA_PATH.read_text())
        con.execute("INSERT INTO models(repo_id) VALUES('qualification/tiny')")
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('qualification/tiny','model.safetensors',1,?,'safetensors')", ["a" * 64])
        con.execute(f"PRAGMA user_version={version}")
        con.commit()
    finally:
        con.close()
    db.configure(data_dir=data, state_dir=case / "state")
    before_bytes = _tree_bytes(case)
    before_logical = _logical_catalog(path)
    source_digest = _sha(sentinel.read_bytes())
    outcome = "accepted"
    refusal = None
    try:
        if reader == "slice":
            snapshot = catalog.read_catalog(path, domain.SliceSpec(
                repo_ids=("qualification/tiny",), destination_id="synthetic-folder",
                destination_root="qualification"))
            if snapshot.schema_version != 7 or len(snapshot.files) != 1:
                raise AssertionError("Old Slice did not read the valid v7 fixture")
        else:
            opened = db.connect(read_only=reader == "core-ro")
            try:
                if opened.execute("SELECT repo_id FROM models").fetchall() != [("qualification/tiny",)]:
                    raise AssertionError("Old core reader did not read the valid fixture")
            finally:
                opened.close()
    except domain.SliceRefusal as exc:
        if reader != "slice" or version != 8 or exc.code != "CATALOG_VERSION_UNSUPPORTED":
            raise
        outcome, refusal = "rejected", exc.code
    except RuntimeError as exc:
        if reader == "slice" or version != 8 or "newer than this ModelArk build (v7)" not in str(exc):
            raise
        outcome, refusal = "rejected", str(exc)
    expected = "rejected" if version == 8 else "accepted"
    if outcome != expected:
        raise AssertionError(f"{reader} v{version}: expected {expected}, observed {outcome}")
    after_bytes = _tree_bytes(case)
    if _logical_catalog(path) != before_logical:
        raise AssertionError("Reader changed catalog schema, rows, or user_version")
    if version == 8 and after_bytes != before_bytes:
        raise AssertionError("Rejected reader changed catalog/source bytes or created sidecar files")
    if _sha(sentinel.read_bytes()) != source_digest:
        raise AssertionError("Reader changed synthetic source bytes")
    print(json.dumps({
        "reader": reader, "physical_catalog_version": version, "outcome": outcome,
        "refusal": refusal, "logical_catalog_unchanged": True,
        "entire_fixture_byte_identical": after_bytes == before_bytes,
        "synthetic_source_unchanged": True, "imported_from": origins,
    }))


def qualify(old_wheel, source_commit, repository):
    wheel = Path(old_wheel).resolve(strict=True)
    revision, payload_files = _verify_payload(wheel, repository, source_commit)
    root = Path(tempfile.mkdtemp(prefix="modelark-reader-floor-qualification-"))
    extracted = root / "old-wheel"
    extracted.mkdir()
    with zipfile.ZipFile(wheel) as package:
        for name in package.namelist():
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError("Unsafe wheel member path")
        package.extractall(extracted)
    checks = []
    for version in (7, 8):
        for reader in ("core-ro", "core-rw", "slice"):
            command = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker",
                       str(extracted), str(root / f"{reader}-v{version}"), reader, str(version)]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"{reader} v{version} failed:\n{result.stdout}\n{result.stderr}")
            checks.append(json.loads(result.stdout))
    report = {
        "status": "passed", "old_wheel": str(wheel), "wheel_sha256": _sha(wheel.read_bytes()),
        "source_commit": revision, "source_payload_files_verified": payload_files,
        "checks": checks, "retained_directory": str(root),
        "physical_USB_or_archive_test": False, "two_client_lock_matrix": False,
    }
    (root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-wheel", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--worker", nargs=4, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        extracted, case, reader, version = args.worker
        _worker(Path(extracted).resolve(), Path(case).resolve(), reader, int(version))
        return
    if args.old_wheel is None or args.source_commit is None:
        parser.error("--old-wheel and --source-commit are required")
    print(json.dumps(qualify(args.old_wheel, args.source_commit, args.repository), indent=2))


if __name__ == "__main__":
    main()
