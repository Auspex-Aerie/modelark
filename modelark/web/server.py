"""Thin HTTP layer: route /api/* to the api modules, serve /static/* from disk."""
from __future__ import annotations

import json
import mimetypes
import signal
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from modelark.core import platform as osplat
from modelark.core import telemetry
from modelark import plan, wishlist
from modelark.web import (catalog_api, data, disk_api, fill_api, fill_worker,
                                 library_api, plan_api, selection_api, verify_api)

STATIC = Path(str(resources.files("modelark.web").joinpath("static")))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, headers=None):
        payload = body if isinstance(body, bytes) else body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-response — harmless

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str), "application/json", code)

    def _static(self, name):
        path = (STATIC / name).resolve()
        if STATIC not in path.parents and path != STATIC or not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(path.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    def do_GET(self):
        u = urlparse(self.path)
        p = parse_qs(u.query)
        try:
            if u.path == "/":
                self._static("index.html")
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
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
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


def serve(port: int = 8077, open_browser: bool = True, resume: bool = False):
    telemetry.configure(**wishlist.logging_config())   # file + stdout; flushes per record (unlike print → journald)
    log = telemetry.get_logger("portal")
    data.conn()
    with data._lock:
        plan.bootstrap(data.conn())    # #33: ensure the `ark` plan exists + owns the fleet before serving
    n = data.build_cache()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
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
