"""INC-030 Gate-1 contracts for explicit repair-drive resolver wiring.

Contracts only. Production is intentionally unchanged, so c01-c04 must be
behavior-specific red until an authorized Gate 2 wires ``register.archive_path``.
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

from modelark import cli, hash_repair, register
from modelark.core import db
from test_dec053_054_gate1_contracts import _h, _repair_status
from test_dec053_054_gate2_remediation import _migrated_clone


def _migrated_catalog(tmp_path: Path) -> Path:
    con = _migrated_clone(tmp_path)
    try:
        row = con.execute("PRAGMA database_list").fetchone()
        assert row and row[2]
        return Path(row[2])
    finally:
        con.close()


def _connect_catalog(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _run_cli(*, drive: str, epoch: int, fingerprint: str) -> None:
    cli.main([
        "repair-drive",
        "--drive", drive,
        "--identity-epoch", str(epoch),
        "--identity-fingerprint", fingerprint,
    ])


def _git_archive_with_file(tmp_path: Path) -> tuple[Path, bytes]:
    archive = tmp_path / "archive"
    archive.mkdir()
    subprocess.run(["git", "init"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "inc030@example.invalid"],
        cwd=archive, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "INC-030"],
        cwd=archive, check=True, capture_output=True,
    )
    repo_dir = archive / "org" / "m"
    repo_dir.mkdir(parents=True)
    content = b"inc030-tier2-archive-head-bytes"
    (repo_dir / "tier2.bin").write_bytes(content)
    subprocess.run(["git", "add", "-A"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "INC-030 tier-2 fixture"],
        cwd=archive, check=True, capture_output=True,
    )
    return archive, content


def test_c01_cli_repair_drive_passes_register_archive_path():
    """CLI passes the canonical accessor plus exact identity authority."""
    engine = mock.Mock(return_value={
        "status": "complete", "applied": 0, "unresolved": 0,
    })
    con = mock.Mock()
    with mock.patch.object(db, "connect", return_value=con), mock.patch.object(
        hash_repair, "run_explicit_drive_repair", engine,
    ):
        _run_cli(drive="d0", epoch=7, fingerprint=_h("f"))

    engine.assert_called_once()
    args, kwargs = engine.call_args
    assert args and args[0] is con
    label = args[1] if len(args) > 1 else kwargs.get("drive_label")
    assert label == "d0"
    assert kwargs["identity_epoch"] == 7
    assert kwargs["identity_fingerprint"] == _h("f")
    assert kwargs.get("archive_resolver") is register.archive_path
    con.close.assert_called_once_with()


def test_c02_cli_resolver_path_reaches_archive_head_tier2(tmp_path):
    """The installed command reaches real tier-2 evidence through its resolver."""
    catalog = _migrated_catalog(tmp_path)
    archive, content = _git_archive_with_file(tmp_path)
    seed = _connect_catalog(catalog)
    try:
        seed.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','tier2.bin',?,NULL,'aux')",
            [len(content)],
        )
        seed.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,"
            "orig_sha256_provenance) "
            "VALUES('org/m','tier2.bin','tier2.bin','tier2.bin','d0',"
            "NULL,?,?,0,NULL,NULL)",
            [len(content), len(content)],
        )
    finally:
        seed.close()

    resolver = mock.Mock(return_value=archive)
    with mock.patch.object(db, "connect", side_effect=lambda: _connect_catalog(catalog)), \
            mock.patch.object(register, "archive_path", resolver):
        _run_cli(drive="d0", epoch=1, fingerprint=_h("f"))

    check = _connect_catalog(catalog)
    try:
        row = check.execute(
            "SELECT orig_sha256,orig_sha256_provenance FROM archived "
            "WHERE repo_id='org/m' AND rfilename='tier2.bin' AND drive_label='d0'"
        ).fetchone()
        assert row == (hashlib.sha256(content).hexdigest(), "archive-head-blob")
        assert _repair_status(check, "d0", 1) == "complete"
    finally:
        check.close()
    resolver.assert_called_once()
    assert resolver.call_args.args[1] == "d0"


def test_c03_cli_resolver_none_active_drive_records_blocked_absent(tmp_path):
    """Active unmounted work is blocked, not mislabeled as needing refetch."""
    catalog = _migrated_catalog(tmp_path)
    seed = _connect_catalog(catalog)
    try:
        seed.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','absent.bin',17,NULL,'aux')"
        )
        seed.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,"
            "orig_sha256_provenance) "
            "VALUES('org/m','absent.bin','absent.bin','absent.bin','d-absent',"
            "NULL,17,17,1,NULL,NULL)"
        )
        assert seed.execute(
            "SELECT lifecycle FROM drives WHERE drive_label='d-absent'"
        ).fetchone() == ("active",)
    finally:
        seed.close()

    resolver = mock.Mock(return_value=None)
    with mock.patch.object(db, "connect", side_effect=lambda: _connect_catalog(catalog)), \
            mock.patch.object(register, "archive_path", resolver):
        _run_cli(drive="d-absent", epoch=1, fingerprint=_h("b"))

    check = _connect_catalog(catalog)
    try:
        state = check.execute(
            "SELECT status,detail FROM drive_hash_repair_state "
            "WHERE drive_label='d-absent' AND identity_epoch=1"
        ).fetchone()
        assert state == ("blocked_absent", "archive_unavailable")
        assert state[0] != "needs_refetch"
        assert check.execute(
            "SELECT orig_sha256 FROM archived WHERE drive_label='d-absent' "
            "AND rfilename='absent.bin'"
        ).fetchone() == (None,)
    finally:
        check.close()
    resolver.assert_called_once()
    assert resolver.call_args.args[1] == "d-absent"


def test_c04_cli_wrong_epoch_receives_resolver_but_halts_before_resolver_io(tmp_path):
    """Wiring does not move resolver I/O ahead of transactional identity refusal."""
    catalog = _migrated_catalog(tmp_path)
    before_con = _connect_catalog(catalog)
    try:
        before = list(before_con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "WHERE drive_label='d0' ORDER BY rfilename"
        ))
    finally:
        before_con.close()

    resolver = mock.Mock(side_effect=AssertionError(
        "wrong-epoch repair must halt before archive resolution"
    ))
    real_engine = hash_repair.run_explicit_drive_repair
    seen = {}

    def recording_engine(*args, **kwargs):
        seen["archive_resolver"] = kwargs.get("archive_resolver")
        return real_engine(*args, **kwargs)

    with mock.patch.object(db, "connect", side_effect=lambda: _connect_catalog(catalog)), \
            mock.patch.object(register, "archive_path", resolver), \
            mock.patch.object(
                hash_repair, "run_explicit_drive_repair", side_effect=recording_engine,
            ):
        _run_cli(drive="d0", epoch=99, fingerprint=_h("f"))

    check = _connect_catalog(catalog)
    try:
        assert _repair_status(check, "d0", 99) == "halted"
        detail = check.execute(
            "SELECT detail FROM drive_hash_repair_state "
            "WHERE drive_label='d0' AND identity_epoch=99"
        ).fetchone()[0]
        assert "epoch" in detail.lower() and "mismatch" in detail.lower()
        after = list(check.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "WHERE drive_label='d0' ORDER BY rfilename"
        ))
        assert after == before
    finally:
        check.close()
    resolver.assert_not_called()
    assert seen.get("archive_resolver") is resolver
