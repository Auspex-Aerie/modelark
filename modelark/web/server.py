"""Thin HTTP layer: route /api/* to the api modules, serve /static/* from disk."""
from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import secrets
import signal
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from modelark.core import platform as osplat
from modelark.core import telemetry
from modelark import plan, wishlist
from modelark.web import (catalog_api, data, disk_api, fill_api, fill_worker,
                                 library_api, plan_api, selection_api, verify_api)

STATIC = Path(str(resources.files("modelark.web").joinpath("static")))
MAX_REQUEST_BODY = 64 * 1024
CSRF_HEADER = "X-ModelArk-CSRF"
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _is_loopback_name(host: str | None) -> bool:
    """Accept only literal loopback addresses (plus the conventional localhost name)."""
    if not host:
        return False
    host = host.lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authority(value: str | None, default_port: int = 80) -> tuple[str, int] | None:
    """Parse an HTTP authority without accepting credentials, paths, or malformed ports."""
    if not value or any(ch.isspace() for ch in value):
        return None
    try:
        parsed = urlsplit("//" + value)
        if (parsed.username is not None or parsed.password is not None or parsed.path
                or parsed.query or parsed.fragment):
            return None
        hostname = parsed.hostname
        port = parsed.port if parsed.port is not None else default_port
    except ValueError:
        return None
    if not _is_loopback_name(hostname):
        return None
    return hostname.lower(), port


class Handler(BaseHTTPRequestHandler):
    server_version = "ModelArk"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, headers=None):
        payload = body if isinstance(body, bytes) else body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            for k, v in _SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-response — harmless

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str), "application/json", code,
                   {"Cache-Control": "no-store"})

    def _request_authority(self) -> tuple[str, int] | None:
        authority = _authority(self.headers.get("Host"))
        if authority is None or authority[1] != self.server.server_port:
            return None
        return authority

    def _trusted_request(self) -> bool:
        if self._request_authority() is not None:
            return True
        self._json({"error": "untrusted Host header"}, 403)
        return False

    def _trusted_origin(self) -> bool:
        request_authority = self._request_authority()
        origin = self.headers.get("Origin")
        if request_authority is None or not origin:
            return False
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return False
        try:
            origin_port = parsed.port if parsed.port is not None else 80
        except ValueError:
            return False
        origin_host = parsed.hostname.lower() if parsed.hostname else None
        return (origin_host, origin_port) == request_authority

    def _index(self):
        """Render the per-process CSRF capability into a same-origin-only document."""
        page = (STATIC / "index.html").read_text()
        token = self.server.csrf_token
        meta = f'<meta name="modelark-csrf-token" content="{token}">'
        page = page.replace("<head>", "<head>\n" + meta, 1)
        self._send(page, "text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    def _static(self, name):
        path = (STATIC / name).resolve()
        if STATIC not in path.parents and path != STATIC or not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(path.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    def do_GET(self):
        if not self._trusted_request():
            return
        u = urlparse(self.path)
        p = parse_qs(u.query)
        try:
            if u.path == "/":
                self._index()
            elif u.path.startswith("/static/"):
                self._static(u.path[len("/static/"):])
            elif u.path == "/api/facets":
                self._json(catalog_api.facets())
            elif u.path == "/api/models":
                self._json(catalog_api.models(p))
            elif u.path == "/api/selection":
                self._json(selection_api.summary())
            elif u.path == "/api/disk":
                self._json(disk_api.disk())
            elif u.path == "/api/meta":
                self._json({"os": osplat.OS_LABEL, "smart_supported": osplat.SMART_SUPPORTED})
            elif u.path == "/api/library":
                self._json(library_api.library())
            elif u.path == "/api/library/plan":
                self._json(library_api.plan())
            elif u.path == "/api/library/queue":
                self._json(library_api.queue())
            elif u.path == "/api/library/queue-state":
                self._json(library_api.queue_state())
            elif u.path == "/api/plan":
                self._json(plan_api.overview())
            elif u.path == "/api/plan/totals":
                self._json(plan_api.totals())
            elif u.path == "/api/plan/cart":
                self._json(plan_api.cart())
            elif u.path == "/api/verify/suspects":
                self._json(verify_api.suspects())
            elif u.path == "/api/fill/status":
                self._json(fill_api.status())
            elif u.path == "/api/fill/last-terminal":
                self._json(fill_api.last_terminal())
            elif u.path == "/api/export":
                self._send(json.dumps(selection_api.export_ids(), indent=2),
                           "application/json", 200,
                           {"Content-Disposition": "attachment; filename=modelark-selection.json"})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        if not self._trusted_request():
            return
        if not self._trusted_origin():
            self._json({"error": "missing or untrusted Origin header"}, 403)
            return
        supplied_token = self.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(supplied_token, self.server.csrf_token):
            self._json({"error": "missing or invalid CSRF token"}, 403)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json({"error": "Content-Type must be application/json"}, 415)
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json({"error": "chunked request bodies are not supported"}, 400)
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._json({"error": "Content-Length is required"}, 411)
            return
        if length > MAX_REQUEST_BODY:
            self.close_connection = True
            self._json({"error": f"request body exceeds {MAX_REQUEST_BODY} bytes"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"error": "malformed JSON body"}, 400)
            return
        if not isinstance(body, dict):
            self._json({"error": "JSON body must be an object"}, 400)
            return
        u = urlparse(self.path)
        try:
            if u.path == "/api/selection":
                self._json(selection_api.toggle(body["id"], bool(body["on"])))
            elif u.path == "/api/selection/bulk":
                self._json(selection_api.bulk(body["ids"], bool(body["on"])))
            elif u.path == "/api/selection/clear":
                self._json(selection_api.clear())
            elif u.path == "/api/selection/finalize":
                self._json(selection_api.finalize())
            elif u.path == "/api/selection/oversize":
                self._json(selection_api.oversize(body))
            elif u.path == "/api/fill/start":
                self._json(fill_api.start(body))
            elif u.path == "/api/fill/stop":
                self._json(fill_api.stop(body))
            elif u.path == "/api/fill/confirm-drive":
                self._json(fill_api.confirm_drive(body))
            elif u.path == "/api/fill/ack-terminal":
                self._json(fill_api.ack_terminal(body))
            elif u.path == "/api/plan/select":
                self._json(plan_api.select(body))
            elif u.path == "/api/plan/create":
                self._json(plan_api.create(body))
            elif u.path == "/api/plan/provisioning":
                self._json(plan_api.set_provisioning(body))
            elif u.path == "/api/verify/run":
                self._json(verify_api.run(body))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def serve(port: int = 8077, open_browser: bool = True, resume: bool = False,
          host: str = "127.0.0.1"):
    if not _is_loopback_name(host):
        raise ValueError("the operator portal has no remote authentication; refusing a non-loopback bind")
    telemetry.configure(**wishlist.logging_config())   # file + stdout; flushes per record (unlike print → journald)
    log = telemetry.get_logger("portal")
    data.conn()
    with data._lock:
        plan.bootstrap(data.conn())    # #33: ensure the `ark` plan exists + owns the fleet before serving
    n = data.build_cache()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.csrf_token = secrets.token_urlsafe(32)
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{httpd.server_port}/"
    log.info("portal ready", url=url, models=n, resume=resume)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    if resume:   # DEC-023 resume-on-boot: for the supervised systemd service, pick the fill back up unattended
        r = fill_api.start({})   # plans in the worker thread (non-blocking); worker finishes 'done' if nothing to do
        if r["ok"]:
            log.info("auto-resume: fill worker started — continuing at the next unfilled shard")
        else:
            log.warning("auto-resume skipped", reason=r["error"])   # already running; drive-absent surfaces as worker 'error'
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stopping — closing catalog cleanly")
        httpd.shutdown()
        fill_worker.shutdown()   # ask the fill worker to stop at its next file boundary (daemon: dies with us)
        try:
            with data._lock:     # don't close the connection out from under a brief worker write
                data.conn().close()  # flush WAL / checkpoint so the DB never opens dirty
        except Exception:
            pass
