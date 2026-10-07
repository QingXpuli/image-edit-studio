"""Offline transport regression. Real child processes, loopback relay only."""
from __future__ import annotations

import base64
import contextlib
import gzip
import http.client
import importlib.util
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
import numpy as np

ROOT = Path(os.environ.get("MANTU_TEST_ROOT", str(Path(__file__).resolve().parents[2])))
sys.path.insert(0, str(ROOT / "compose" / "images"))
import image_transport as T

SECRET = "sk-fake-secret-never-real"
SIGNED = "signature=private-token"


def png_bytes(size=(32, 32)):
    b = io.BytesIO()
    Image.new("RGB", size, (80, 120, 40)).save(b, "PNG")
    return b.getvalue()


# Imported by both real child processes, before any app credential fallback.
STUB = '''import os, socket, sys, types, urllib.parse
m = types.ModuleType("winreg")
def denied(*a, **k):
    raise OSError("isolated registry")
m.OpenKey = denied
m.QueryValueEx = denied
m.HKEY_CURRENT_USER = 0
sys.modules["winreg"] = m
original = socket.socket.connect
def connect(self, address):
    if address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise OSError("offline guard")
    return original(self, address)
socket.socket.connect = connect
# curl is not Python: validate its argv in every child before spawning it.
import subprocess
run = subprocess.run
def safe_run(args, *a, **k):
    if isinstance(args, (tuple, list)) and args and "curl" in str(args[0]).lower():
        url = urllib.parse.urlsplit(str(args[-1]))
        if url.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise OSError("offline curl guard")
    return run(args, *a, **k)
subprocess.run = safe_run
'''


class Response:
    def __init__(self, raw=b'{}', status=200, headers=None, error=None):
        self.raw = io.BytesIO(raw)
        self.code = status
        self.headers = headers or {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
        self.error = error
        self.once = False

    def read(self, amount):
        if self.error and self.once:
            raise self.error
        self.once = True
        return self.raw.read(min(amount, 7) if self.error else amount)

    def close(self):
        pass


class TransportUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="transport-unit-")
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.req = urllib.request.Request("https://offline.invalid/images/edits", data=b"x", method="POST")
        self.req.add_header("Authorization", "Bearer " + SECRET)

    def call(self, response=None, error=None):
        opener = Mock()
        opener.open.side_effect = error
        if not error:
            opener.open.return_value = response
        r = T.post_once(self.req, opener=opener, marker=self.d / "submit", report_path=self.d / "report.json")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(r.submit_attempts, 1)
        self.assertFalse(r.retryable_generation)
        self.assertEqual(r.acceptance, "unknown")
        public = (self.d / "report.json").read_text() + (self.d / "report.json.events.jsonl").read_text()
        self.assertNotIn(SECRET, public)
        self.assertNotIn(SIGNED, public)
        return r

    def test_opener_construction_failure_not_sent_zero(self):
        with patch.object(T.urllib.request, "build_opener", side_effect=ValueError("local " + SECRET)):
            result = T.post_once(self.req, marker=self.d / "submit")
        self.assertEqual(result.submit_attempts, 0)
        self.assertEqual(result.acceptance, "not_sent")
        self.assertEqual(result.classification, "local_error")
        self.assertFalse((self.d / "submit").exists())

    def test_ssl_eof(self):
        r = self.call(error=ssl.SSLEOFError(8, SECRET + SIGNED))
        self.assertEqual(r.classification, "transport_error")
        self.assertEqual(r.phase, "submit")

    def test_timeout(self):
        self.assertEqual(self.call(error=TimeoutError(SECRET)).classification, "timeout")

    def test_incomplete_partial_bytes(self):
        r = self.call(Response(b"1234567", error=http.client.IncompleteRead(b"890", 20)))
        self.assertEqual(r.received_bytes, 10)
        self.assertEqual(r.classification, "response_interrupted")

    def test_read_error(self):
        r = self.call(Response(b"1234567", error=ssl.SSLEOFError(8, SECRET)))
        self.assertEqual(r.received_bytes, 7)
        self.assertEqual(r.http_status, 200)
        self.assertEqual(r.phase, "read_response")

    def test_declared_length_mismatch(self):
        r = self.call(Response(b'{}', headers={"Content-Length": "20"}))
        self.assertEqual(r.received_bytes, 2)
        self.assertEqual(r.classification, "response_interrupted")

    def test_http_statuses_and_error_body_secret(self):
        for status, classification in ((400, "request_rejected"), (401, "auth_error"),
                                       (429, "rate_limited"), (500, "server_error"), (502, "server_error")):
            with self.subTest(status=status):
                (self.d / "submit").unlink(missing_ok=True)
                exc = urllib.error.HTTPError(self.req.full_url, status, SECRET,
                        {"Content-Length": str(len(SECRET)), "Content-Type": "application/json"}, io.BytesIO(SECRET.encode()))
                r = self.call(error=exc)
                self.assertEqual(r.http_status, status)
                self.assertEqual(r.classification, classification)
                self.assertNotIn(SECRET, r.legacy()[1])

    def test_error_read_interruption_preserves_status(self):
        for status in (400, 401):
            with self.subTest(status=status):
                (self.d / "submit").unlink(missing_ok=True)
                exc = urllib.error.HTTPError(self.req.full_url, status, SECRET, {},
                        Response(b"1234567", error=http.client.IncompleteRead(b"abc", 10)))
                r = self.call(error=exc)
                self.assertEqual(r.http_status, status)
                self.assertEqual(r.classification, "response_interrupted")
                self.assertEqual(r.received_bytes, 10)

    def test_gzip_and_deflate_complete(self):
        raw = json.dumps({"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}).encode()
        for encoding, compressed in (("gzip", gzip.compress(raw)), ("deflate", zlib.compress(raw))):
            with self.subTest(encoding=encoding):
                (self.d / "submit").unlink(missing_ok=True)
                r = self.call(Response(compressed, headers={"Content-Length": str(len(compressed)), "Content-Encoding": encoding}))
                self.assertEqual(r.received_bytes, len(compressed))
                self.assertEqual(r.text, raw.decode())
                self.assertEqual(r.classification, "response_complete")

    def test_truncated_gzip(self):
        raw = gzip.compress(b'{"data": []}')[:-5]
        r = self.call(Response(raw, headers={"Content-Encoding": "gzip", "Content-Length": str(len(raw))}))
        self.assertEqual(r.classification, "invalid_response_encoding")

    def test_json_shape_no_data_no_image_moderation(self):
        for text, classification in (('{"data":', "invalid_json"), ('[]', "invalid_json_shape"),
                                      ('{}', "no_data"), ('{"data": []}', "no_data"),
                                      ('{"data": [{}]}', "no_image"),
                                      ('{"error": "moderation ' + SECRET + '"}', "moderation_blocked")):
            with self.subTest(classification=classification):
                with self.assertRaisesRegex(T.ResultError, classification):
                    T.result_item(text)

    def test_b64_invalid_and_truncated_image_preserves_output(self):
        out = self.d / "old.png"
        out.write_bytes(b"old-user-bytes")
        for value, classification in (("%%%", "invalid_b64"),
                                     (base64.b64encode(png_bytes()[:45]).decode(), "invalid_image"),
                                     (base64.b64encode(png_bytes()).decode(), "output_exists")):
            with self.subTest(classification=classification):
                with self.assertRaisesRegex(T.ResultError, classification):
                    T.save_b64(value, out)
                self.assertEqual(out.read_bytes(), b"old-user-bytes")
        self.assertFalse(list(self.d.glob(".image-*")))

    def test_b64_atomic_success(self):
        out = self.d / "new.png"
        T.save_b64(base64.b64encode(png_bytes()).decode(), out)
        with Image.open(out) as im:
            im.load()
            self.assertEqual(im.size, (32, 32))

    def test_marker_local_failure_not_sent_zero(self):
        opener = Mock()
        with patch.object(T, "submission_marker", side_effect=OSError("marker unavailable")):
            result = T.post_once(self.req, opener=opener, marker=self.d / "submit")
        opener.open.assert_not_called()
        self.assertEqual(result.submit_attempts, 0)
        self.assertEqual(result.acceptance, "not_sent")
        self.assertEqual(result.classification, "local_error")

    def test_marker_prevents_second_post(self):
        first = self.call(Response())
        opener = Mock()
        second = T.post_once(self.req, opener=opener, marker=self.d / "submit")
        self.assertEqual(opener.open.call_count, 0)
        self.assertEqual(second.submit_attempts, 0)
        self.assertEqual(second.acceptance, "unknown")
        self.assertEqual(second.classification, "submission_already_marked")

    def test_redirect_refuses_post_all_origins(self):
        handler = T.NoPostRedirect()
        for code in (301, 302, 303, 307, 308):
            for url in (self.req.full_url, "https://other.invalid/steal"):
                self.assertIsNone(handler.redirect_request(self.req, None, code, "", {}, url))


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="transport-compat-")
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        app = types.ModuleType("mask_edit_app")
        app.build_multipart = Mock(return_value=b"payload")
        self.app = app
        env = {"RELAY_API_KEY": SECRET, "RELAY_API_KEY_HD": "fake-hd-key",
               "RELAY_BASE_URL": "http://127.0.0.1:1/v1", "RELAY_MODEL": "fake"}
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"mask_edit_app": app}):
            spec = importlib.util.spec_from_file_location("offline_gen", ROOT / "compose" / "images" / "gen.py")
            self.gen = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.gen)
        with patch.dict(sys.modules, {"gen": self.gen}):
            spec = importlib.util.spec_from_file_location("offline_round", ROOT / "style-distill" / "round_lib" / "run_round.py")
            self.rr = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.rr)
        self.rr.LEDGER = self.d / "ledger.jsonl"
        self.rr.PENDING = self.d / "pending.private.json"

    def test_legacy_tuple_and_explicit_three_single_call(self):
        for status in (-1, 502, 429):
            with self.subTest(status=status), patch.object(self.gen, "call", return_value=(status, "safe")) as call:
                self.assertEqual(self.rr.post_with_retry({}, {}, attempts=3), (status, "safe"))
                self.assertEqual(call.call_count, 1)
        result = T.CallResult(classification="response_complete", http_status=200, text='{"data": []}')
        with patch.object(self.gen, "call_structured", return_value=result):
            self.assertEqual(self.gen.call({}, {}), (200, '{"data": []}'))

    def test_quality_keys_and_structured_preflight_no_network(self):
        self.assertEqual(self.gen.key_for("low"), SECRET)
        self.assertEqual(self.gen.key_for("medium"), "fake-hd-key")
        self.assertEqual(self.gen.key_for("high"), "fake-hd-key")
        opener = Mock()
        with patch.object(self.gen, "KEY", ""):
            result = self.gen.call_structured({}, {}, opener=opener)
        self.assertEqual(result.classification, "missing_config")
        self.assertEqual(result.acceptance, "not_sent")
        self.assertEqual(result.submit_attempts, 0)
        opener.open.assert_not_called()
        self.app.build_multipart.side_effect = ValueError(SECRET)
        result = self.gen.call_structured({}, {}, opener=opener)
        self.assertEqual(result.classification, "local_error")
        self.assertEqual(result.acceptance, "not_sent")
        opener.open.assert_not_called()

    def test_structured_malformed_json_and_image_reports(self):
        for text, classification, phase in (('{"data":', "invalid_json", "parse_response"),
                                             ('{}', "no_data", "parse_response"),
                                             ('{"data": [{}]}', "no_image", "parse_response"),
                                             ('{"data": [{"b64_json": "%%%"}]}', "invalid_b64", "decode_image"),
                                             ('{"data": [{"b64_json": "aGVsbG8="}]}', "invalid_image", "decode_image")):
            with self.subTest(classification=classification):
                result = T.CallResult(acceptance="unknown", submit_attempts=1, http_status=200, text=text)
                path = self.d / "report.json"
                self.assertEqual(self.rr.receive_result(result, self.d / "out.png", path), 1)
                report = json.loads(path.read_text())
                self.assertEqual(report["classification"], classification)
                self.assertEqual(report["phase"], phase)
                self.assertEqual(report["acceptance"], "unknown")
                self.assertFalse((self.d / "out.png").exists())

    def test_downloader_three_total_budget_no_nested_retry(self):
        calls = []
        def failed(args, **kw):
            calls.append((args, kw))
            return types.SimpleNamespace(returncode=1)
        with patch.object(self.rr.shutil, "which", return_value="fake-curl"), patch.object(self.rr.subprocess, "run", side_effect=failed):
            with self.assertRaisesRegex(T.ResultError, "download_failed"):
                self.rr.download("https://offline.invalid/image?" + SIGNED, self.d / "out.png", attempts=8, timeout=600)
        self.assertEqual(len(calls), 3)
        for args, kw in calls:
            self.assertNotIn("--retry", args)
            self.assertEqual(args[1], "-q")
            self.assertLessEqual(float(args[args.index("--max-time") + 1]), 300)
            self.assertLessEqual(kw["timeout"], 300)
        self.assertFalse(list(self.d.glob(".download-*")))

    def test_snapshot_scrubs_key_and_missing_recovery(self):
        snapshot = self.d / "response.private.json"
        T.private_snapshot(snapshot, json.dumps({"data": [{"url": "https://offline.invalid/image?" + SIGNED}], "echo": SECRET}), secrets=(SECRET,))
        self.assertNotIn(SECRET, snapshot.read_text())
        self.assertIn(SIGNED, snapshot.read_text())
        report = self.d / "recovery.json"
        self.assertEqual(self.rr.recover_result(self.d / "missing", self.d / "out.png", report), 1)
        value = json.loads(report.read_text())
        self.assertEqual(value["classification"], "cannot_recover_unknown")
        self.assertEqual(value["acceptance"], "unknown")
        self.assertEqual(value["submit_attempts"], 0)


class Relay(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        s = self.server
        s.posts += 1
        s.auth.append(self.headers.get("Authorization"))
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        s.last_body = body
        s.b64_requested = b'b64_json' in body
        if s.mode == "interrupt":
            raw = b'{"data": ['
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw) + 100))
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True
            return
        if s.mode == "b64":
            data = {"data": [{"b64_json": base64.b64encode(s.image).decode()}]}
        else:
            data = {"data": [{"url": f"http://127.0.0.1:{s.server_port}/image?{SIGNED}"}]}
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        s = self.server
        s.gets += 1
        raw = b"broken" if s.fail_get else s.image
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class RealChildren(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="transport-children-")
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        (self.d / "sitecustomize.py").write_text(STUB, encoding="utf-8")
        self.source = self.d / "source.png"
        self.source.write_bytes(png_bytes())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        self.server.posts = self.server.gets = 0
        self.server.auth = []
        self.server.mode = "b64"
        self.server.fail_get = False
        self.server.image = png_bytes((1024, 1024))
        self.server.last_body = b""
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("RELAY_", "ZIMAGE_", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))}
        self.env.update(PYTHONPATH=str(self.d), RELAY_API_KEY=SECRET, RELAY_API_KEY_HD="hd-fake",
                        RELAY_BASE_URL=f"http://127.0.0.1:{self.server.server_port}/v1", RELAY_MODEL="fake-model",
                        ZIMAGE_ROOT=str(ROOT), ZIMAGE_JOBS_DIR=str(self.d / "jobs"),
                        ZIMAGE_CACHE_DIR=str(self.d / "cache"), ZIMAGE_LEDGER_FILE=str(self.d / "ledger.jsonl"),
                        ZIMAGE_PENDING_FILE=str(self.d / "pending.private.json"))
        self.job_number = 0

    def run_cli(self, args, env=None):
        r = subprocess.run([sys.executable, str(ROOT / "zcode-image-edit" / "bin" / "zimage.py"), "edit", *args],
                           env=env or self.env, cwd=self.d, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=45)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertNotIn(SIGNED, r.stdout + r.stderr)
        return r

    def edit(self, extra=(), prompt="offline test", env=None):
        self.job_number += 1
        job = "job-" + str(self.job_number)
        out = self.d / (job + ".png")
        r = self.run_cli(["--image", str(self.source), "--whole", "--prompt", prompt,
                          "--size", "1024x1024", "--out", str(out), "--job-id", job, *extra], env)
        mf = json.loads((self.d / "jobs" / job / "manifest.json").read_text(encoding="utf-8"))
        return r, out, mf, self.d / "jobs" / job

    def public_safe(self, jd):
        paths = [*jd.glob("*transport*.json"), *jd.glob("*.events.jsonl"), jd / "manifest.json"]
        if (self.d / "ledger.jsonl").exists():
            paths.append(self.d / "ledger.jsonl")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(SECRET, text)
            self.assertNotIn(SIGNED, text)

    def test_interrupt_one_post_unknown_and_cannot_recover(self):
        self.server.mode = "interrupt"
        r, out, mf, jd = self.edit()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(mf["transport"]["http_status"], 200)
        self.assertEqual(mf["transport"]["received_bytes"], len(b'{"data": ['))
        self.assertEqual(mf["failure_type"], "response_interrupted")
        self.assertEqual(mf["acceptance"], "unknown")
        self.assertEqual(mf["phase"], "read_response")
        self.assertEqual(mf["submit_attempts"], 1)
        self.assertFalse(out.exists())
        self.assertTrue((jd / "result.tmp.png.submission").exists())
        self.public_safe(jd)
        rr = self.run_cli(["--recover-from", str(jd), "--out", str(out)])
        self.assertNotEqual(rr.returncode, 0)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.server.gets, 0)
        self.assertIn("cannot_recover_unknown", rr.stdout)

    def test_input_fidelity_passthrough_and_ledger_host(self):
        # 2026-10-07 第一批：--input-fidelity 透传到 multipart payload；ledger 记录 host。
        self.server.mode = "b64"
        r, out, mf, jd = self.edit(extra=["--input-fidelity", "low"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.server.posts, 1)
        self.assertTrue(self.server.last_body.count(b'name="input_fidelity"') >= 1)
        self.assertGreater(self.server.last_body.find(b'low'), 0)
        self.assertTrue(out.exists())
        ledger_line = (self.d / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
        rec = json.loads(ledger_line)
        self.assertTrue(str(rec.get("host", "")).startswith("127.0.0.1"))
        self.assertEqual(rec.get("input_fidelity"), "low")
        self.public_safe(jd)

    def test_b64_success_then_cache_and_local_snapshot_recovery(self):
        r, out, mf, jd = self.edit()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.server.gets, 0)
        self.assertTrue(out.exists())
        self.assertEqual(mf["state"], "saved")
        self.assertEqual(mf["status"], "NEEDS_REVIEW")
        self.assertEqual(mf["submit_attempts"], 1)
        self.assertTrue(self.server.b64_requested)
        self.assertEqual(self.server.auth, ["Bearer " + SECRET])
        r2, out2, mf2, _ = self.edit()
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(mf2["submit_attempts"], 0)
        self.assertTrue(mf2["from_cache"])
        restored = self.d / "restored.png"
        rr = self.run_cli(["--recover-from", str(jd), "--out", str(restored)])
        self.assertEqual(rr.returncode, 0, rr.stdout + rr.stderr)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.server.gets, 0)
        self.assertEqual(restored.read_bytes(), out.read_bytes())
        self.public_safe(jd)

    def test_recovery_continues_outer_mask_acceptance(self):
        mask = self.d / "mask.png"
        m = Image.new("RGBA", (32, 32), (255, 255, 255, 0))
        m.paste((255, 255, 255, 255), (8, 8, 20, 20))
        m.save(mask)
        # Protected pixels differ, so both initial and recovered artifacts must fail.
        b = io.BytesIO()
        Image.new("RGB", (1024, 1024), (230, 20, 50)).save(b, "PNG")
        self.server.image = b.getvalue()
        out = self.d / "old.png"
        out.write_bytes(b"old-protected-output")
        r = self.run_cli(["--image", str(self.source), "--mask-file", str(mask),
                          "--prompt", "local mask test", "--size", "1024x1024", "--out", str(out),
                          "--job-id", "masked", "--force"])
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        jd = self.d / "jobs" / "masked"
        rr = self.run_cli(["--recover-from", str(jd), "--out", str(out), "--force"])
        self.assertEqual(rr.returncode, 4, rr.stdout + rr.stderr)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.server.gets, 0)
        self.assertEqual(out.read_bytes(), b"old-protected-output")
        mf = json.loads((jd / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(mf["failure_type"], "protected_area_changed")

    def test_url_success(self):
        self.server.mode = "url"
        r, out, mf, jd = self.edit()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.server.posts, self.server.gets), (1, 1))
        self.assertEqual(mf["status"], "NEEDS_REVIEW")
        self.public_safe(jd)
        self.assertIn(SIGNED, (jd / "response.private.json").read_text())
        self.assertIn(SIGNED, (self.d / "pending.private.json").read_text())

    def test_failed_get_recovery_no_new_post_and_old_output_intact(self):
        self.server.mode = "url"
        self.server.fail_get = True
        # Force permits replacement only after success, never truncates old output.
        old = self.d / "job-1.png"
        old.write_bytes(b"user-old-output")
        r, out, mf, jd = self.edit(extra=("--force",))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.server.posts, self.server.gets), (1, 3))
        self.assertEqual(old.read_bytes(), b"user-old-output")
        self.assertEqual(mf["phase"], "download")
        self.assertEqual(mf["acceptance"], "unknown")
        self.server.fail_get = False
        rr = self.run_cli(["--recover-from", str(jd), "--out", str(old), "--force"])
        self.assertEqual(rr.returncode, 0, rr.stdout + rr.stderr)
        self.assertEqual((self.server.posts, self.server.gets), (1, 4))
        updated = json.loads((jd / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["status"], "NEEDS_REVIEW")
        self.assertEqual(updated["submit_attempts"], 0)
        self.public_safe(jd)

    def test_dryrun_zero_and_missing_config_registry_isolation(self):
        r, out, mf, jd = self.edit(extra=("--dry-run",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.server.posts, 0)
        self.assertEqual(mf["submit_attempts"], 0)
        self.assertEqual(mf["acceptance"], "not_sent")
        self.assertFalse(out.exists())
        self.assertFalse(list(jd.glob("*.submission")))
        env = dict(self.env)
        env.pop("RELAY_API_KEY")
        env.pop("RELAY_API_KEY_HD")
        r2, out2, mf2, jd2 = self.edit(env=env, prompt="missing config")
        self.assertNotEqual(r2.returncode, 0)
        self.assertEqual(self.server.posts, 0)
        self.assertEqual(mf2["failure_type"], "missing_config")
        self.assertEqual(mf2["submit_attempts"], 0)
        self.assertEqual(mf2["acceptance"], "not_sent")
        self.assertFalse(list(jd2.glob("*.submission")))

    def test_round_no_download_url_result_and_guard(self):
        self.server.mode = "url"
        prompt = self.d / "prompt.txt"
        prompt.write_text("offline", encoding="utf-8")
        out = self.d / "round.png"
        report = self.d / "round-report.json"
        snapshot = self.d / "round.private.json"
        command = [sys.executable, str(ROOT / "style-distill" / "round_lib" / "run_round.py"),
                   "--content", str(self.source), "--prompt", str(prompt), "--out", str(out),
                   "--size", "32x32", "--url-result", "--no-download", "--report", str(report),
                   "--snapshot", str(snapshot)]
        r = subprocess.run(command, env=self.env, cwd=self.d, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.server.posts, self.server.gets), (1, 0))
        self.assertFalse(self.server.b64_requested)
        self.assertEqual(json.loads(report.read_text())["classification"], "url_pending")
        again = subprocess.run(command, env=self.env, cwd=self.d, capture_output=True, timeout=30)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(self.server.posts, 1)
        self.assertNotIn(SIGNED.encode(), r.stdout + r.stderr)
        recover = [sys.executable, str(ROOT / "style-distill" / "round_lib" / "run_round.py"),
                   "--recover-from", str(snapshot), "--out", str(out), "--report", str(report)]
        rr = subprocess.run(recover, env=self.env, cwd=self.d, capture_output=True, timeout=30)
        self.assertEqual(rr.returncode, 0, rr.stdout + rr.stderr)
        self.assertEqual((self.server.posts, self.server.gets), (1, 1))
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
