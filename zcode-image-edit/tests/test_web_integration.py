"""Real webpage-to-CLI integration with a loopback fake relay and temporary records."""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "compose/images"))
from image_cli_adapter import execute_image_request


def png(color=(50, 90, 130), size=(1024, 1024)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


class Relay(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.posts += 1
        self.server.paths.append(self.path)
        self.server.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if self.server.mode == "interrupt":
            raw = b'{"data": ['
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw) + 10))
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True
            return
        raw = json.dumps({"data": [{"b64_json": base64.b64encode(png()).decode()}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class WebIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.d = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        self.server.posts = 0
        self.server.paths = []
        self.server.bodies = []
        self.server.mode = "b64"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.environment = patch.dict(os.environ, {
            "ZIMAGE_WEB_ROOT": str(self.d / "web"), "ZIMAGE_JOBS_DIR": str(self.d / "jobs"),
            "ZIMAGE_CACHE_DIR": str(self.d / "cache"), "ZIMAGE_LEDGER_FILE": str(self.d / "ledger"),
            "ZIMAGE_PENDING_FILE": str(self.d / "pending"), "RELAY_DISABLE_REGISTRY": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    def request(self, prompt, **options):
        return execute_image_request(prompt=prompt, source=options.pop("source", png(size=(32, 32))),
                                     size="1024x1024", model="gpt-image-2", quality="low",
                                     base_url=self.base, api_key="fake-web-key", **options)

    def test_whole_two_requests_one_post(self):
        a = self.request("preserve identity")
        self.assertTrue(a["ok"], a)
        self.assertEqual(a["task_result"], "NEEDS_REVIEW")
        b = self.request("preserve identity")
        self.assertTrue(b["ok"], b)
        self.assertTrue(b["from_cache"])
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.server.paths, ["/v1/images/edits"])
        self.assertNotEqual(a["job_id"], b["job_id"])
        self.assertEqual(a["result_sha256"], b["result_sha256"])

    def test_generate_json_and_zero_post_dryrun(self):
        dry = self.request("blue tree", source=None, operation="generate", dry_run=True)
        self.assertTrue(dry["ok"], dry)
        self.assertEqual(self.server.posts, 0)
        live = self.request("blue tree", source=None, operation="generate")
        self.assertTrue(live["ok"], live)
        self.assertEqual(self.server.paths, ["/v1/images/generations"])
        fields = json.loads(self.server.bodies[0])
        self.assertEqual(fields["prompt"], "blue tree")
        self.assertEqual(str(fields["n"]), "1")

    def test_same_unknown_request_never_reposts(self):
        self.server.mode = "interrupt"
        a = self.request("unknown result")
        self.assertFalse(a["ok"])
        self.assertEqual(self.server.posts, 1)
        b = self.request("unknown result")
        self.assertFalse(b["ok"])
        self.assertEqual(self.server.posts, 1)
        self.assertFalse(b["retry"])

    def test_local_refs_reach_payload_and_need_semantic_review(self):
        source = Image.new("RGB", (32, 32), (50, 90, 130))
        sb = io.BytesIO(); source.save(sb, "PNG")
        mask = Image.new("RGBA", source.size, (255, 255, 255, 0))
        mask.paste((255, 255, 255, 255), (8, 8, 24, 24))
        mb = io.BytesIO(); mask.save(mb, "PNG")
        # Uniform fake output keeps protected pixels but deliberately does not fulfill edit intent.
        result = self.request("replace marker", source=sb.getvalue(), mask=mb.getvalue(), refs=[("style", png(size=(32, 32)))])
        body = self.server.bodies[0]
        self.assertIn(b'name="image[1]"', body)
        self.assertNotIn(b'name="image[2]"', body)
        self.assertNotEqual(result.get("task_result"), "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
