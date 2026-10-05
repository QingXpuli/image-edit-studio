"""Security regressions using deterministic stubs and an isolated loopback server."""
from __future__ import annotations

import http.client
import io
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import mask_edit_app as app
import public_fetch as downloads

SENTINEL = "TEST_CREDENTIAL_NOT_REAL"


class DownloadTests(unittest.TestCase):
    def addr(self, host, port, **kw):
        ip = host if host in ("127.0.0.1", "169.254.169.254") else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def test_block_non_public_without_connect(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1", "0.0.0.0"):
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            with patch.object(downloads.socket, "getaddrinfo", return_value=[(family, socket.SOCK_STREAM, 6, "", (ip, 80))]), patch.object(downloads.socket, "socket") as connect:
                with self.assertRaises(downloads.UnsafeDownload):
                    downloads.fetch_public_bytes("http://private.invalid/image.png")
                connect.assert_not_called()

    def test_empty_dns_and_invalid_urls(self):
        with patch.object(downloads.socket, "getaddrinfo", return_value=[]):
            with self.assertRaises(downloads.UnsafeDownload):
                downloads.public_endpoints("https://empty.invalid/")
        for url in ("file:///etc/passwd", "http://user:pass@public.invalid/", "http://[fe80::1%25eth0]/"):
            with self.assertRaises(downloads.UnsafeDownload):
                downloads.public_endpoints(url)

    def test_pin_and_redirect_validation(self):
        sockets = []
        connections = []
        responses = [(200, {"Content-Type": "image/png"}, b"png")]
        class Sock:
            def __init__(self, *a): sockets.append(self); self.target = None
            def settimeout(self, t): pass
            def connect(self, address): self.target = address
            def close(self): pass
        class Response:
            def __init__(self, status, headers, raw): self.status, self.headers, self.raw = status, headers, raw
            def getheader(self, name, default=None): return self.headers.get(name, default)
            def read(self, n): return self.raw[:n]
        class Connection:
            def __init__(self, host, port, **kw): self.host, self.port = host, port; connections.append(self)
            def request(self, method, path, headers):
                self.headers = headers
                self.sock = self._create_connection((self.host, self.port), timeout=1)
            def getresponse(self): return Response(*responses.pop(0))
            def close(self): pass
        with patch.object(downloads.socket, "getaddrinfo", side_effect=self.addr) as dns, patch.object(downloads.socket, "socket", Sock), patch.object(downloads.http.client, "HTTPSConnection", Connection):
            data, mime = downloads.fetch_public_bytes("https://public.invalid/image.png")
            self.assertEqual((data, mime), (b"png", "image/png"))
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(sockets[0].target[0], "93.184.216.34")
            self.assertEqual(connections[0].host, "public.invalid")
            self.assertNotIn("Authorization", connections[0].headers)
            responses.append((302, {"Location": "http://127.0.0.1/secret"}, b""))
            with self.assertRaises(downloads.UnsafeDownload):
                downloads.fetch_public_bytes("https://public.invalid/image.png")
            self.assertEqual(len(connections), 2)

    def test_size_type_and_redirect_limit(self):
        class Response:
            status = 200
            headers = {"Content-Type": "image/png", "Content-Length": "1000"}
            def getheader(self, name, default=None): return self.headers.get(name, default)
            def read(self, n): return b"x" * n
        response = Response()
        class Connection:
            def __init__(self, *a, **kw): pass
            def request(self, *a, **kw): pass
            def getresponse(self): return response
            def close(self): pass
        with patch.object(downloads.socket, "getaddrinfo", side_effect=self.addr), patch.object(downloads.http.client, "HTTPConnection", Connection):
            with self.assertRaises(downloads.UnsafeDownload):
                downloads.fetch_public_bytes("http://public.invalid/img", max_bytes=10)
            response.headers = {"Content-Type": "text/html"}
            with self.assertRaises(downloads.UnsafeDownload):
                downloads.fetch_public_bytes("http://public.invalid/img")
            response.status = 302
            response.headers = {"Location": "/again"}
            with self.assertRaises(downloads.UnsafeDownload):
                downloads.fetch_public_bytes("http://public.invalid/img", max_redirects=1)


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.paths = patch.multiple(app, USER_LOCAL=Path(cls.tmp.name) / "settings.json",
                                   LOG_FILE=Path(cls.tmp.name) / "server.log", PLUGIN_ROOT=Path(cls.tmp.name))
        cls.paths.start()
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.paths.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
        finally:
            conn.close()

    def test_defaults_never_returns_key(self):
        with patch.dict(app.os.environ, {"IMAGE_EDIT_API_KEY": SENTINEL, "IMAGE_EDIT_BASE_URL": "https://relay.invalid"}, clear=True):
            status, body = self.request("GET", "/api/defaults")
            self.assertEqual(status, 200)
            self.assertTrue(body["has_key"])
            self.assertNotIn("api_key", body)
            self.assertNotIn(SENTINEL, json.dumps(body))

    def test_guard_get_post_and_null_origin(self):
        for headers in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}, {"Origin": "null"}):
            status, _ = self.request("GET", "/api/defaults", headers=headers)
            self.assertEqual(status, 403)
            with patch.object(app, "http_json") as upstream:
                status, _ = self.request("POST", "/api/test", b"{}", headers={**headers, "Content-Type": "application/json"})
                self.assertEqual(status, 403)
                upstream.assert_not_called()
        status, _ = self.request("GET", "/health", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_config_sources_cannot_mix_different_bases(self):
        app.USER_LOCAL.write_text(json.dumps({"base_url": "https://new.invalid/v1"}))
        lower = app.PLUGIN_ROOT / "local.json"
        lower.write_text(json.dumps({"base_url": "https://old.invalid/v1", "api_key": SENTINEL}))
        try:
            with patch.dict(app.os.environ, {"IMAGE_EDIT_BASE_URL": "https://env.invalid/v1", "IMAGE_EDIT_API_KEY": "ENV_NOT_REAL"}, clear=True):
                cfg = app.load_local_config()
                self.assertEqual(cfg["base_url"], "https://new.invalid/v1")
                self.assertEqual(cfg["api_key"], "")
        finally:
            app.USER_LOCAL.unlink(missing_ok=True)
            lower.unlink(missing_ok=True)

    def test_signed_download_url_not_logged(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, "PNG")
        url_path = "/api/fetch-url?url=https%3A%2F%2Fpublic.invalid%2Fimage%3Ftoken%3DSIGNED_TEST_NOT_REAL"
        for data, expected in ((buffer.getvalue(), 200), (b"broken", 502)):
            with patch.object(app, "fetch_public_bytes", return_value=(data, "image/png")):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
                try:
                    conn.request("GET", url_path)
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, expected)
                finally:
                    conn.close()
        self.assertNotIn("SIGNED_TEST_NOT_REAL", app.LOG_FILE.read_text(encoding="utf-8"))

    def test_server_key_bound_to_base(self):
        config = {"api_key": SENTINEL, "base_url": "https://relay.invalid/v1", "model": "example"}
        with patch.object(app, "load_local_config", return_value=config), patch.object(app, "http_json", return_value=(200, {"data": []}, "")) as upstream:
            for path in ("/api/test", "/api/models"):
                status, body = self.request("POST", path, json.dumps({"base_url": "https://relay.invalid"}), {"Content-Type": "application/json"})
                self.assertEqual(status, 200)
                self.assertEqual(upstream.call_args.args[2], SENTINEL)
                upstream.reset_mock()
                status, _ = self.request("POST", path, json.dumps({"base_url": "https://other.invalid"}), {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                upstream.assert_not_called()

    def test_save_preserves_local_key_but_not_env(self):
        app.USER_LOCAL.write_text(json.dumps({"base_url": "https://relay.invalid/v1", "api_key": SENTINEL}))
        headers = {"Content-Type": "application/json", "X-Image-Edit-Local": "1"}
        try:
            status, body = self.request("POST", "/api/save-defaults", json.dumps({"base_url": "https://relay.invalid", "api_key": "", "model": "test"}), headers)
            self.assertEqual(status, 200)
            self.assertNotIn(SENTINEL, json.dumps(body))
            self.assertEqual(json.loads(app.USER_LOCAL.read_text())["api_key"], SENTINEL)
            status, _ = self.request("POST", "/api/save-defaults", json.dumps({"base_url": "https://other.invalid"}), headers)
            self.assertEqual(status, 200)
            self.assertNotIn("api_key", json.loads(app.USER_LOCAL.read_text()))
        finally:
            app.USER_LOCAL.unlink(missing_ok=True)

    def test_proxy_blocks_loopback_and_decodes_images(self):
        original_dns = downloads.socket.getaddrinfo
        def dns(host, port, *args, **kwargs):
            if host == "local.invalid":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
            return original_dns(host, port, *args, **kwargs)
        with patch.object(downloads.socket, "getaddrinfo", side_effect=dns):
            status, _ = self.request("GET", "/api/fetch-url?url=http%3A%2F%2Flocal.invalid%2Fsecret")
            self.assertEqual(status, 400)
        with patch.object(app, "fetch_public_bytes", return_value=(b"bad image", "image/png")):
            status, _ = self.request("GET", "/api/fetch-url?url=https%3A%2F%2Fpublic.invalid%2Fimage")
            self.assertEqual(status, 502)


class TransportTests(unittest.TestCase):
    def test_authenticated_redirect_is_blocked(self):
        request = app.urllib.request.Request("https://relay.invalid/v1/models", headers={"Authorization": SENTINEL})
        with self.assertRaises(urllib.error.HTTPError):
            app._SameOriginRedirect().redirect_request(request, None, 302, "Found", {}, "https://other.invalid/image")

    def test_post_unknown_result_not_retried(self):
        for failure in (urllib.error.URLError("timed out"), urllib.error.HTTPError("https://relay.invalid", 503, "failed", {}, io.BytesIO(b"{}"))):
            with patch.object(app._OPENER, "open", side_effect=failure) as upstream:
                app.http_json("POST", "https://relay.invalid/v1/images/edits", SENTINEL, b"{}")
                self.assertEqual(upstream.call_count, 1)

    def test_video_does_not_fallback_on_uncertain_acceptance(self):
        for status in (0, 401, 429, 500, 503):
            with patch.object(app, "http_json", return_value=(status, {}, "")) as upstream, patch.object(app, "video_job_update"), patch.object(app, "video_job_log"):
                app._video_worker("test", "https://relay.invalid/v1", SENTINEL, "test", "test", "640x480", "4")
                self.assertEqual(upstream.call_count, 1)

    def test_external_video_download_has_no_bearer(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "VIDEO_DIR", Path(tmp)), patch.object(app, "fetch_public_bytes", return_value=(b"video", "video/mp4")) as fetch, patch.object(app, "http_bytes") as auth, patch.object(app, "video_job_update"), patch.object(app, "video_job_log"):
            app._video_download("test", "https://relay.invalid", SENTINEL, "", {"url": "https://public.invalid/video.mp4"})
            auth.assert_not_called()
            self.assertNotIn(SENTINEL, str(fetch.call_args))


if __name__ == "__main__":
    original_connect = socket.socket.connect
    def loopback_only(self, address):
        if address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("External network forbidden in regression tests")
        return original_connect(self, address)
    with patch.object(socket.socket, "connect", loopback_only):
        unittest.main(verbosity=2)
