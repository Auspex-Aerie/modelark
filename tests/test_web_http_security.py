"""HTTP trust-boundary checks for the loopback operator portal."""
from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from unittest import mock

from modelark.web import plan_api, selection_api, server


@contextmanager
def _portal():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    httpd.csrf_token = "test-csrf-token"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _request(httpd, method, path, body=None, headers=None):
    con = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
    try:
        con.request(method, path, body=body, headers=headers or {})
        response = con.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        con.close()


def _mutation_headers(httpd, **extra):
    headers = {
        "Content-Type": "application/json",
        "Origin": f"http://127.0.0.1:{httpd.server_port}",
        server.CSRF_HEADER: httpd.csrf_token,
    }
    headers.update(extra)
    return headers


def test_index_injects_token_and_security_headers():
    with _portal() as httpd:
        status, headers, body = _request(httpd, "GET", "/")
    assert status == 200
    assert b'<meta name="modelark-csrf-token" content="test-csrf-token">' in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cache-Control"] == "no-store"


def test_csrf_injection_accepts_head_attributes_and_fails_closed():
    page = server._inject_csrf_meta('<html><head lang="en"><title>x</title></head></html>',
                                    'token-with-"quotes"')
    assert '<head lang="en">\n<meta name="modelark-csrf-token"' in page
    assert 'content="token-with-&quot;quotes&quot;"' in page
    try:
        server._inject_csrf_meta("<html><body>missing head</body></html>", "token")
        raise AssertionError("a missing head tag should fail closed")
    except RuntimeError as exc:
        assert "missing an opening head tag" in str(exc)


def test_host_header_rejects_dns_rebinding():
    with _portal() as httpd:
        status, _, body = _request(httpd, "GET", "/", headers={"Host": "attacker.example"})
        malformed, _, _ = _request(
            httpd, "GET", "/", headers={"Host": f"127.0.0.1:{httpd.server_port}?attacker"})
    assert status == 403 and b"untrusted Host" in body
    assert malformed == 403


def test_mutation_requires_same_origin_and_csrf_token():
    with _portal() as httpd:
        headers = _mutation_headers(httpd, Origin="https://attacker.example")
        status, _, _ = _request(httpd, "POST", "/api/selection/clear", "{}", headers)
        assert status == 403

        headers = _mutation_headers(httpd)
        del headers[server.CSRF_HEADER]
        status, _, body = _request(httpd, "POST", "/api/selection/clear", "{}", headers)
        assert status == 403 and b"CSRF" in body


def test_mutation_requires_json_object_and_well_formed_body():
    with _portal() as httpd:
        headers = _mutation_headers(httpd, **{"Content-Type": "text/plain"})
        status, _, _ = _request(httpd, "POST", "/api/selection/clear", "{}", headers)
        assert status == 415

        headers = _mutation_headers(httpd)
        status, _, _ = _request(httpd, "POST", "/api/selection/clear", "{", headers)
        assert status == 400
        status, _, body = _request(httpd, "POST", "/api/selection/clear", "[]", headers)
        assert status == 400 and b"must be an object" in body


def test_mutation_rejects_oversized_body_without_dispatch():
    with _portal() as httpd, mock.patch.object(selection_api, "clear") as clear:
        headers = _mutation_headers(httpd, **{"Content-Length": str(server.MAX_REQUEST_BODY + 1)})
        status, _, body = _request(httpd, "POST", "/api/selection/clear", b"", headers)
    assert status == 413 and b"exceeds" in body
    clear.assert_not_called()


def test_valid_mutation_dispatches():
    with _portal() as httpd, mock.patch.object(
            selection_api, "clear", return_value={"ok": True}) as clear:
        status, headers, body = _request(
            httpd, "POST", "/api/selection/clear", json.dumps({}), _mutation_headers(httpd))
    assert status == 200 and json.loads(body) == {"ok": True}
    assert headers["Referrer-Policy"] == "no-referrer"
    clear.assert_called_once_with()


def test_capacity_mode_route_and_deprecated_alias_dispatch():
    with _portal() as httpd, \
         mock.patch.object(plan_api, "set_capacity_mode", return_value={"ok": True}) as canonical, \
         mock.patch.object(plan_api, "set_provisioning", return_value={"ok": True}) as legacy:
        body = json.dumps({"plan_id": "ark", "capacity_mode": "guaranteed"})
        status, _, _ = _request(
            httpd, "POST", "/api/plan/capacity-mode", body, _mutation_headers(httpd)
        )
        legacy_body = json.dumps({"plan_id": "ark", "mode": "uncompressed"})
        old_status, _, _ = _request(
            httpd, "POST", "/api/plan/provisioning", legacy_body, _mutation_headers(httpd)
        )
    assert status == 200 and old_status == 200
    canonical.assert_called_once_with({"plan_id": "ark", "capacity_mode": "guaranteed"})
    legacy.assert_called_once_with({"plan_id": "ark", "mode": "uncompressed"})


def test_non_loopback_bind_is_refused_before_startup():
    try:
        server.serve(host="0.0.0.0", open_browser=False)
        raise AssertionError("non-loopback bind should be refused")
    except ValueError as exc:
        assert "non-loopback" in str(exc)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
