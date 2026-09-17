"""Hop-1 verified-export unix socket (RFC-003 / DEC-156)."""
from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import time
from pathlib import Path
from unittest import mock

from modelark import export_provider, restore
from modelark.core import db


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    return con


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _catalog(con, repo: str, name: str, data: bytes, drive: str, stored: Path):
    digest = _sha(data)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(data)
    con.execute(
        "INSERT INTO models(repo_id, status, total_size_bytes) VALUES(?,?,?)",
        [repo, "archived", len(data)],
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) VALUES(?,?,?,?,?)",
        [repo, name, len(data), digest, "safetensors"],
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,compressed,annex_key,orig_sha256_provenance) VALUES(?,?,?,?,?,?,?,?,?)",
        [repo, name, stored.name, name, drive, digest, 0, f"SHA256E-s{len(data)}--{digest}.bin",
         "hub_confirmed"],
    )
    return digest


def _rpc(path: Path, request: dict) -> dict:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(10)
    conn.connect(str(path))
    conn.sendall((json.dumps(request) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    conn.close()
    return json.loads(buf.split(b"\n", 1)[0])


def test_capabilities_and_prepare_regular_files(tmp_path):
    con = _mem()
    repo = "org/model"
    payload = b"weights-bytes-for-export"
    archive = tmp_path / "drive" / "modelark"
    stored = archive / "org" / "model" / "model.safetensors"
    digest = _catalog(con, repo, "model.safetensors", payload, "drive-00", stored)
    sock = tmp_path / "export.sock"
    holds = tmp_path / "holds"
    provider = export_provider.ExportProvider(
        connect=lambda: con, socket_path=sock, holds_dir=holds, close_connections=False
    )
    with mock.patch.object(restore.register, "archive_path", return_value=archive):
        provider.start()
        try:
            cap = _rpc(sock, {"action": "modelark:capabilities"})
            assert cap["ok"] is True
            assert cap["data"]["purpose"] == ["verified-export"]
            arts = _rpc(sock, {"action": "modelark:artifacts", "q": "org"})
            assert arts["data"]["artifacts"][0]["id"] == repo
            assert arts["data"]["artifacts"][0]["availability"] == "ready"
            job = _rpc(sock, {"action": "modelark:prepare", "id": repo})
            assert job["ok"] is True
            job_id = job["data"]["id"]
            envelope = None
            for _ in range(50):
                snap = _rpc(sock, {"action": "modelark:job", "id": job_id})
                if snap["data"]["status"] in {"done", "error", "cancelled"}:
                    envelope = snap["data"].get("envelope")
                    break
                time.sleep(0.05)
            assert envelope, snap
            root = Path(envelope["hold"]["root"])
            restored = root / "model.safetensors"
            assert restored.read_bytes() == payload
            assert restored.stat().st_nlink == 1
            assert not restored.is_symlink()
            assert envelope["files"][0]["original_sha256"] == digest
            assert envelope["hold"]["holdUntil"] is None
            released = _rpc(sock, {"action": "modelark:release", "id": envelope["hold"]["id"]})
            assert released["ok"] is True
            assert not restored.exists()
        finally:
            provider.stop()


def test_needs_media_does_not_copy(tmp_path):
    con = _mem()
    repo = "org/cold"
    payload = b"shelved"
    archive = tmp_path / "drive" / "modelark"
    stored = archive / "org" / "cold" / "a.bin"
    _catalog(con, repo, "a.bin", payload, "drive-07", stored)
    sock = tmp_path / "export.sock"
    provider = export_provider.ExportProvider(
        connect=lambda: con, socket_path=sock, holds_dir=tmp_path / "holds",
        close_connections=False,
    )
    with mock.patch.object(restore.register, "archive_path", return_value=None):
        provider.start()
        try:
            job = _rpc(sock, {"action": "prepare", "id": repo})
            assert job["data"]["status"] == "needs-media"
            assert job["data"]["awaitingDrive"] == "drive-07"
            assert list((tmp_path / "holds").glob("*")) == []
        finally:
            provider.stop()
