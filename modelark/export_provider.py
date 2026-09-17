"""Local verified-export provider (RFC-003 hop-1 / DEC-156).

JSON-line unix socket, Spark-shaped envelope: one request ``{action,...}\\n``
returns ``{ok:true,data}`` or ``{ok:false,error}``. Not the operator portal.
Holds are regular files until the caller releases them. No ModelArk expiry.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from modelark import archive_manifest, register, restore
from modelark.core import db

SCHEMA = 1
PURPOSE = "verified-export"
MAX_REQUEST_BYTES = 1_000_000
SOCKET_NAME = "export-provider.sock"


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "modelark-export.sock"
    return db.STATE_DIR / SOCKET_NAME


def default_holds_dir() -> Path:
    return db.STATE_DIR / "export-holds"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _action(request: dict) -> str:
    raw = request.get("action")
    if not isinstance(raw, str) or not raw:
        raise ValueError("Request needs an action.")
    return raw.split(":", 1)[-1] if raw.startswith("modelark:") else raw


def _artifact_id(request: dict) -> str:
    value = request.get("id") or request.get("artifactId")
    if not isinstance(value, str) or not value.strip() or "/" not in value or ".." in value:
        raise ValueError("Choose an artifact id like org/model.")
    return value.strip()


def _planned(con, repo_id: str) -> list[str]:
    try:
        return [
            item.rfilename
            for item in archive_manifest.manifest_for_repo(
                con, repo_id, archive_manifest.recovery_policy()
            )
        ]
    except archive_manifest.ArchivePolicyError:
        copies = restore._rows(con, repo_id)
        return sorted(copies)


def _copy_rows(con, repo_id: str, rfilename: str) -> list[dict]:
    rows = []
    for values in con.execute(
        "SELECT a.drive_label, a.orig_sha256, a.orig_sha256_provenance, a.verified_at, "
        "a.annex_key, a.compressed, d.fs_uuid "
        "FROM archived a LEFT JOIN drives d ON d.drive_label = a.drive_label "
        "WHERE a.repo_id=? AND a.rfilename=? ORDER BY a.drive_label",
        [repo_id, rfilename],
    ).fetchall():
        rows.append({
            "drive": values[0],
            "orig_sha256": values[1],
            "provenance": values[2],
            "verified_at": values[3],
            "annex_key": values[4],
            "compressed": bool(values[5]),
            "fs_uuid": values[6],
            "attached": register.archive_path(con, values[0]) is not None,
        })
    return rows


def _plan(con, repo_id: str) -> dict:
    copies = restore._rows(con, repo_id)
    if not copies:
        return {
            "status": "missing",
            "planned": [],
            "visits": [],
            "missing": [repo_id],
            "needs": [],
            "assigned": {},
        }
    planned = _planned(con, repo_id)
    assigned: dict[str, dict] = {}
    visits: list[str] = []
    missing: list[str] = []
    needs: list[str] = []
    for rfilename in planned:
        rows = copies.get(rfilename) or []
        attached = [row for row in rows if register.archive_path(con, row["drive_label"]) is not None]
        if attached:
            chosen = attached[0]
            assigned[rfilename] = chosen
            if chosen["drive_label"] not in visits:
                visits.append(chosen["drive_label"])
        elif rows:
            if rows[0]["drive_label"] not in needs:
                needs.append(rows[0]["drive_label"])
        else:
            missing.append(rfilename)
    if missing:
        status = "missing"
    elif needs:
        status = "needs-media"
    else:
        status = "ready"
    visit_plan = []
    for drive in visits:
        bytes_on = 0
        for rf, row in assigned.items():
            if row["drive_label"] != drive:
                continue
            size = con.execute(
                "SELECT size_bytes FROM files WHERE repo_id=? AND rfilename=?",
                [repo_id, rf],
            ).fetchone()
            bytes_on += int((size[0] if size else 0) or 0)
        visit_plan.append({
            "drive": drive,
            "bytes": bytes_on,
            "state": "pending",
        })
    for drive in needs:
        visit_plan.append({"drive": drive, "bytes": 0, "state": "pending"})
    return {
        "status": status,
        "planned": planned,
        "visits": visits,
        "missing": missing,
        "needs": needs,
        "assigned": assigned,
        "visitPlan": visit_plan,
    }


def _file_envelope(con, repo_id: str, rfilename: str, original_sha256: str | None) -> dict:
    copies = []
    vendor = None
    annex_key = None
    representation = "raw"
    size = 0
    row = con.execute(
        "SELECT size_bytes, sha256 FROM files WHERE repo_id=? AND rfilename=?",
        [repo_id, rfilename],
    ).fetchone()
    if row:
        size = int(row[0] or 0)
        vendor = row[1]
    for item in _copy_rows(con, repo_id, rfilename):
        annex_key = annex_key or item["annex_key"]
        if item["compressed"]:
            representation = "streamznn"
        copies.append({
            "drive": item["drive"],
            "fs_uuid": item["fs_uuid"],
            "observed_at": item["verified_at"],
            "class": "attached" if item["attached"] else "shelved",
            "presence": "physical-verify" if item["verified_at"] else "copy-record",
        })
    authority = None
    if vendor or repo_id:
        authority = {
            "kind": "hf-revision",
            "repo": repo_id,
            "revision": None,
            "vendor_sha256": vendor,
            "provenance": "hub_confirmed" if vendor else None,
        }
        authority["revision"] = None
    return {
        "path": rfilename,
        "size": size,
        "original_sha256": original_sha256 or vendor,
        "authority": authority,
        "stored": {"representation": representation, "annex_key": annex_key},
        "copies": copies,
    }


def _snapshot(job: dict) -> dict:
    out = {
        "id": job["id"],
        "status": job["status"],
        "progress": job.get("progress", 0),
        "bytesDone": job.get("bytesDone", 0),
        "bytesTotal": job.get("bytesTotal", 0),
        "etaTotalSec": job.get("etaTotalSec"),
        "etaNextSwapSec": job.get("etaNextSwapSec"),
        "awaitingDrive": job.get("awaitingDrive"),
        "visitPlan": job.get("visitPlan") or [],
    }
    if job.get("error"):
        out["error"] = job["error"]
    if job.get("envelope"):
        out["envelope"] = job["envelope"]
    return out


def _materialize_hold(con, repo_id: str, plan: dict, tree: Path, should_stop) -> dict:
    restored = []
    for drive in plan["visits"]:
        if should_stop():
            raise restore.RestoreError("cancelled")
        for rfilename, row in plan["assigned"].items():
            if row["drive_label"] != drive:
                continue
            if should_stop():
                raise restore.RestoreError("cancelled")
            archive = register.archive_path(con, drive)
            if archive is None:
                raise restore.RestoreError(f"{drive} detached during prepare")
            stored_rel = restore._stored_relative(row)
            repo_rel = restore._safe_relative(repo_id, description="repository id")
            stored = Path(archive) / Path(*repo_rel.parts) / Path(*stored_rel.parts)
            source, _retrieved, detail = restore._annex_content(
                Path(archive), row, stored, may_mutate=restore._may_mutate(con, drive)
            )
            if source is None:
                raise restore.RestoreError(f"{rfilename}: {detail}")
            expected = restore._expected_hash(row)
            if expected is None:
                raise restore.RestoreError(f"{rfilename}: no original-byte sha256")
            dest_rel = restore._safe_relative(rfilename, description="Hugging Face file path")
            restore._materialize(source, tree / Path(*dest_rel.parts), row, expected)
            restored.append({"path": rfilename, "sha256": expected, "drive": drive})
    return restored


class ExportProvider:
    def __init__(self, *, connect, socket_path: Path, holds_dir: Path, close_connections: bool = True):
        self._connect = connect
        self._close_connections = close_connections
        self.socket_path = Path(socket_path)
        self.holds_dir = Path(holds_dir)
        self.holds_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._holds: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def _db(self):
        return self._connect()

    def _done(self, con) -> None:
        if self._close_connections:
            con.close()

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict) or isinstance(request, list):
            raise ValueError("Request must be an object.")
        action = _action(request)
        if action == "capabilities":
            return {
                "schema": SCHEMA,
                "purpose": [PURPOSE],
                "delivery": ["prepared-files"],
                "holdUntil": "caller-optional",
            }
        con = self._db()
        try:
            if action == "artifacts":
                return self._artifacts(con, request)
            if action == "availability":
                return self._availability(con, request)
            if action == "prepare":
                return self._prepare(con, request)
            if action == "job":
                job_id = request.get("id")
                with self._lock:
                    job = self._jobs.get(job_id)
                if not job:
                    raise ValueError("Unknown job.")
                return _snapshot(job)
            if action == "cancel":
                return self._cancel(request)
            if action == "release":
                return self._release(request)
            raise ValueError(f"Unknown action: {action}")
        finally:
            self._done(con)

    def _artifacts(self, con, request: dict) -> dict:
        q = (request.get("q") or "").strip().lower()
        rows = con.execute(
            "SELECT DISTINCT a.repo_id, m.total_size_bytes "
            "FROM archived a LEFT JOIN models m ON m.repo_id=a.repo_id "
            "ORDER BY a.repo_id"
        ).fetchall()
        items = []
        for repo_id, size in rows:
            if q and q not in repo_id.lower():
                continue
            plan = _plan(con, repo_id)
            items.append({
                "id": repo_id,
                "revision": None,
                "sizeBytes": size,
                "availability": plan["status"],
            })
        return {"artifacts": items[:200]}

    def _availability(self, con, request: dict) -> dict:
        ids = request.get("ids") or []
        if not isinstance(ids, list) or len(ids) > 64:
            raise ValueError("ids must be a list of at most 64 artifact ids.")
        results = []
        for raw in ids:
            if not isinstance(raw, str):
                continue
            plan = _plan(con, raw)
            results.append({
                "id": raw,
                "status": plan["status"],
                "visitPlan": plan["visitPlan"],
                "awaitingDrive": plan["needs"][0] if plan["needs"] else None,
            })
        return {"results": results}

    def _prepare(self, con, request: dict) -> dict:
        repo_id = _artifact_id(request)
        hold_until = request.get("holdUntil")
        if hold_until is not None and not isinstance(hold_until, str):
            raise ValueError("holdUntil must be an ISO timestamp or null.")
        plan = _plan(con, repo_id)
        job_id = "job_" + uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "repo_id": repo_id,
            "status": "queued",
            "progress": 0,
            "bytesDone": 0,
            "bytesTotal": sum(v["bytes"] for v in plan["visitPlan"]),
            "etaTotalSec": None,
            "etaNextSwapSec": None,
            "awaitingDrive": None,
            "visitPlan": plan["visitPlan"],
            "holdUntil": hold_until,
            "cancel": threading.Event(),
        }
        if plan["status"] == "missing":
            job["status"] = "error"
            job["error"] = "No archived files for this artifact."
        elif plan["status"] == "needs-media":
            job["status"] = "needs-media"
            job["awaitingDrive"] = plan["needs"][0]
        else:
            job["status"] = "preparing"
            job["visitPlan"] = [
                {**row, "state": "current" if i == 0 else "pending"}
                for i, row in enumerate(plan["visitPlan"])
            ]
        with self._lock:
            self._jobs[job_id] = job
        if job["status"] == "preparing":
            threading.Thread(
                target=self._run_prepare, args=(job_id, plan), daemon=True, name="modelark-export"
            ).start()
        return _snapshot(job)

    def _run_prepare(self, job_id: str, plan: dict) -> None:
        with self._lock:
            job = self._jobs[job_id]
        repo_id = job["repo_id"]
        hold_id = "hold_" + uuid.uuid4().hex[:16]
        root = self.holds_dir / hold_id
        tree = root / "tree"
        stage = root / ".stage"
        con = self._db()
        try:
            stage.mkdir(parents=True)
            restored = _materialize_hold(
                con, repo_id, plan, stage, job["cancel"].is_set
            )
            if job["cancel"].is_set():
                raise restore.RestoreError("cancelled")
            os.replace(stage, tree)
            files = [
                _file_envelope(con, repo_id, item["path"], item["sha256"])
                for item in restored
            ]
            envelope = {
                "schema": SCHEMA,
                "purpose": PURPOSE,
                "artifact": {
                    "id": repo_id,
                    "revision": None,
                    "manifest_digest": None,
                },
                "files": files,
                "hold": {
                    "id": hold_id,
                    "granted_at": _now(),
                    "root": str(tree.resolve()),
                    "holdUntil": job.get("holdUntil"),
                },
            }
            with self._lock:
                job["status"] = "done"
                job["progress"] = 1
                job["envelope"] = envelope
                job["visitPlan"] = [{**row, "state": "done"} for row in job["visitPlan"]]
                self._holds[hold_id] = {"id": hold_id, "root": tree, "job_id": job_id}
        except restore.RestoreError as exc:
            shutil.rmtree(root, ignore_errors=True)
            with self._lock:
                job["status"] = "cancelled" if job["cancel"].is_set() else "error"
                job["error"] = str(exc)
        except Exception as exc:
            shutil.rmtree(root, ignore_errors=True)
            with self._lock:
                job["status"] = "error"
                job["error"] = str(exc)
        finally:
            self._done(con)

    def _cancel(self, request: dict) -> dict:
        job_id = request.get("id")
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError("Unknown job.")
            job["cancel"].set()
            if job["status"] in {"queued", "needs-media"}:
                job["status"] = "cancelled"
        return _snapshot(job)

    def _release(self, request: dict) -> dict:
        hold_id = request.get("id")
        with self._lock:
            hold = self._holds.pop(hold_id, None)
        if not hold:
            raise ValueError("Unknown hold.")
        shutil.rmtree(hold["root"].parent, ignore_errors=True)
        return {"released": hold_id}

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="modelark-export-sock")
        self._thread.start()

    def _loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(30)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST_BYTES:
                    raise ValueError("Request too large.")
            if b"\n" not in buf:
                raise ValueError("Incomplete request.")
            request = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            data = self.handle(request)
            payload = json.dumps({"ok": True, "data": data}, default=str) + "\n"
        except Exception as exc:
            payload = json.dumps({"ok": False, "error": str(exc)[:4000]}) + "\n"
        try:
            conn.sendall(payload.encode("utf-8"))
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def start_background(*, connect=None, socket_path: Path | None = None, holds_dir: Path | None = None) -> ExportProvider:
    provider = ExportProvider(
        connect=connect or db.connect,
        socket_path=socket_path or default_socket_path(),
        holds_dir=holds_dir or default_holds_dir(),
    )
    provider.start()
    return provider
