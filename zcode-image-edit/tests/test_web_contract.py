"""Offline web delegation and coordinate regressions; no real image service."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "compose" / "images"))
import image_cli_adapter as adapter
import mask_edit_app as app
import board_app as board


def png(size=(32, 32), *, alpha=False):
    buffer = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", size, (255, 255, 255, 255) if alpha else (50, 80, 120)).save(buffer, "PNG")
    return buffer.getvalue()


class WebContract(unittest.TestCase):
    def test_crop_mask_same_coordinates(self):
        source = Image.new("RGB", (200, 100))
        mask = Image.new("RGBA", source.size, (255, 255, 255, 0))
        mask.paste((255, 255, 255, 255), (70, 0, 90, 100))
        _, painted = app.make_visual_mask(source, mask, 100, 100, "crop", False)
        xs = np.where(painted.any(axis=0))[0]
        self.assertEqual((int(xs.min()), int(xs.max())), (20, 39))

    def test_pad_mask_and_inversion(self):
        source = Image.new("RGB", (200, 100))
        mask = Image.new("RGBA", source.size, (255, 255, 255, 255))
        _, painted = app.make_visual_mask(source, mask, 100, 100, "pad", False)
        self.assertEqual(int(painted.sum()), 5000)
        _, inverse = app.make_visual_mask(source, mask, 100, 100, "pad", True)
        self.assertEqual(int(inverse.sum()), 0)

    def test_adapter_uses_cli_and_credential_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            seen = []
            def fake(command, **kwargs):
                seen.append((command, kwargs))
                out = Path(command[command.index("--out") + 1])
                out.write_bytes(png())
                job = command[command.index("--job-id") + 1]
                jd = temp / "jobs" / job
                jd.mkdir(parents=True)
                (jd / "manifest.json").write_text(json.dumps({"status": "NEEDS_REVIEW", "semantic_validation": {"status": "PENDING"}}))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.dict(os.environ, {"ZIMAGE_WEB_ROOT": str(temp / "web"), "ZIMAGE_JOBS_DIR": str(temp / "jobs")}):
                result = adapter.execute_image_request(prompt="change", source=png(), refs=[("style", png())],
                    size="1024x1024", base_url="http://127.0.0.1:1/v1", api_key="fake-secret", runner=fake)
            self.assertTrue(result["ok"])
            cmd, args = seen[0]
            self.assertEqual(Path(cmd[2]), ROOT / "zcode-image-edit/bin/zimage.py")
            self.assertIn("--task-file", cmd)
            self.assertEqual(cmd.count("--ref"), 1)
            self.assertNotIn("fake-secret", " ".join(cmd))
            self.assertEqual(args["env"]["RELAY_API_KEY"], "fake-secret")
            self.assertEqual(result["semantic_validation"]["status"], "PENDING")
            task = json.loads(Path(cmd[cmd.index("--task-file") + 1]).read_text(encoding="utf-8"))
            self.assertTrue(task["side_effect_policy"]["generation_authorized"])
            self.assertEqual([r["role"] for r in task["input_roles"]], ["identity", "style"])

    def test_adapter_preflight_never_calls_runner(self):
        calls = []
        result = adapter.execute_image_request(prompt="change", source=png(), refs=[png()],
            model="grok-imagine-edit", base_url="http://127.0.0.1:1/v1", api_key="fake", runner=lambda *a, **k: calls.append(a))
        self.assertFalse(result["ok"])
        self.assertFalse(calls)

    def test_adapter_dryrun_not_authorized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ZIMAGE_WEB_ROOT": tmp, "ZIMAGE_JOBS_DIR": tmp + "/jobs"}):
            def fake(cmd, **kwargs):
                task = json.loads(Path(cmd[cmd.index("--task-file") + 1]).read_text(encoding="utf-8"))
                self.assertFalse(task["side_effect_policy"]["generation_authorized"])
                self.assertIn("--dry-run", cmd)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            result = adapter.execute_image_request(prompt="new", source=None, operation="generate", dry_run=True,
                base_url="http://127.0.0.1:1/v1", api_key="fake", runner=fake)
            self.assertTrue(result["ok"])
            self.assertNotIn("b64", result)

    def test_adapter_timeout_unknown_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ZIMAGE_WEB_ROOT": tmp}):
            def timedout(*a, **k):
                raise adapter.subprocess.TimeoutExpired("python", 1200)
            result = adapter.execute_image_request(prompt="new", source=None, operation="generate",
                base_url="http://127.0.0.1:1/v1", api_key="fake", runner=timedout)
            self.assertFalse(result["retry"])
            self.assertEqual(result["acceptance"], "unknown")

    def test_board_prompts_unique_cache_default_and_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            target = temp / "source.png"
            target.write_bytes(png())
            state = {"objects": [{"id": "a", "type": "image", "path": str(target)}], "identityId": "a", "prompt": "new", "size": "1024x1024"}
            with patch.object(board, "BOARD_DIR", temp / "board"), patch.object(board, "ALLOWED_ROOTS", [temp]):
                a, error = board.build_edit_cmd(state, dry_run=False, job_id="first", out=temp / "a.png")
                b, error2 = board.build_edit_cmd(state, dry_run=False, job_id="second", out=temp / "b.png")
            self.assertEqual((error, error2), ("", ""))
            self.assertNotIn("--no-cache", a)
            self.assertNotEqual(a[a.index("--prompt-file") + 1], b[b.index("--prompt-file") + 1])
            self.assertIn("--task-file", a)

    def test_no_image_sender_in_web_handlers(self):
        import ast
        paths = [(ROOT / "compose/images/mask_edit_app.py", "api_edit"),
                 (ROOT / "zcode-plugins/image-edit/server/mask_edit_app.py", "_api_edit")]
        for path, name in paths:
            tree = ast.parse(path.read_bytes())
            handler = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = {n.func.id for n in ast.walk(handler) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            self.assertIn("execute_image_request", calls)
            self.assertNotIn("http_json", calls)
            self.assertNotIn("relay_post", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
