"""MCP stdio handshake, configuration and process-ownership regressions."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("canvas_mcp", PLUGIN / "mcp/image_edit_mcp.py")
canvas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canvas)


class ConfigurationTests(unittest.TestCase):
    def test_single_mcp_definition_and_config(self):
        manifest = json.loads((PLUGIN / ".zcode-plugin/plugin.json").read_text())
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        mcp = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["image-edit"]
        self.assertEqual(mcp["command"], "${user_config.python}")
        self.assertEqual(mcp["env"]["IMAGE_EDIT_PORT"], "${user_config.port}")
        self.assertEqual(manifest["userConfig"]["python"]["default"], "python")
        skill = PLUGIN / "skills/image-edit-canvas/SKILL.md"
        self.assertTrue(skill.is_file())
        self.assertIn("name: image-edit-canvas", skill.read_text(encoding="utf-8"))
        self.assertTrue((PLUGIN / "commands/image-edit-canvas.md").is_file())
        self.assertFalse((PLUGIN / "skills/image-edit").exists())

    def test_invalid_port(self):
        for port in (-1, 65536):
            with self.assertRaises(ValueError):
                canvas._port(port)
        with patch.dict(os.environ, {"IMAGE_EDIT_PORT": "not-a-number"}):
            with self.assertRaises(ValueError):
                canvas._port()

    def test_unrelated_health_not_accepted(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n): return b'{"ok":true,"pid":100,"port":8000}'
        with patch.object(canvas._PROBE_OPENER, "open", return_value=Response()):
            self.assertFalse(canvas._probe(8000)["ok"])

    def test_stop_does_not_touch_unverified_pid(self):
        st = {"running": True, "pid": 100, "pid_alive": True, "probe": {"body": {"pid": 101}}}
        with patch.object(canvas, "status_payload", return_value=st), patch.object(canvas._PROBE_OPENER, "open") as stop:
            result = json.loads(canvas.image_edit_stop())
            self.assertFalse(result["ok"])
            self.assertFalse(result["stopped"])
            stop.assert_not_called()

    def test_live_unhealthy_record_does_not_spawn_again(self):
        with patch.object(canvas, "status_payload", return_value={"running": False, "pid_alive": True, "port": 8000}), patch.object(canvas, "_spawn") as spawn:
            result = json.loads(canvas.image_edit_ensure(8000))
            self.assertFalse(result["ok"])
            spawn.assert_not_called()

    def test_actual_server_start_reuse_and_stop(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"IMAGE_EDIT_STATE_DIR": tmp, "IMAGE_EDIT_PORT": str(port)}), patch.object(canvas, "PID_FILE", Path(tmp) / ".image-edit.pid"), patch.object(canvas, "LOG_FILE", Path(tmp) / "server.log"):
                try:
                    started = json.loads(canvas.image_edit_ensure(port))
                    self.assertTrue(started["ok"], started)
                    reused = json.loads(canvas.image_edit_ensure(port))
                    self.assertTrue(reused["already"])
                    self.assertEqual(reused["probe"]["body"]["pid"], started["service_pid"])
                    stopped = json.loads(canvas.image_edit_stop())
                    self.assertTrue(stopped["ok"], stopped)
                    self.assertTrue(stopped["stopped"])
                finally:
                    canvas.image_edit_stop()
                    deadline = time.monotonic() + 3
                    while canvas.PID_FILE.exists() and time.monotonic() < deadline:
                        time.sleep(0.1)

    def test_stdio_initialize_and_list_tools(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ)
        for name in list(env):
            if any(word in name for word in ("API_KEY", "TOKEN", "SECRET")):
                env.pop(name)
        env.update(IMAGE_EDIT_PLUGIN_ROOT=str(PLUGIN), IMAGE_EDIT_PORT=str(port))
        params = StdioServerParameters(command=sys.executable,
                                       args=["-B", str(PLUGIN / "mcp/image_edit_mcp.py")], env=env)
        async def check():
            with anyio.fail_after(20):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        result = await session.initialize()
                        self.assertEqual(result.serverInfo.name, "image-edit")
                        tools = await session.list_tools()
                        self.assertEqual({t.name for t in tools.tools}, {"image_edit_status", "image_edit_ensure", "image_edit_open", "image_edit_stop"})
                        status = await session.call_tool("image_edit_status", {})
                        self.assertFalse(status.isError)
        anyio.run(check)


if __name__ == "__main__":
    unittest.main(verbosity=2)
