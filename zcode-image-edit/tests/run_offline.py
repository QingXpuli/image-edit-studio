"""Run regression scripts with dummy credentials, loopback-only and temp-only writes."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = r'''
import os, pathlib, socket, sys, types, urllib.parse
sys.dont_write_bytecode = True
registry = types.ModuleType("winreg")
def deny_registry(*args, **kwargs):
    raise OSError("offline test: registry disabled")
registry.OpenKey = registry.QueryValueEx = deny_registry
registry.HKEY_CURRENT_USER = 0
sys.modules["winreg"] = registry
_temp = pathlib.Path(os.environ["ZIMAGE_TEST_TEMP"]).resolve()
_original_connect = socket.socket.connect
def connect(self, address):
    if address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise PermissionError("offline test: external network denied")
    return _original_connect(self, address)
socket.socket.connect = connect
_original_dns = socket.getaddrinfo
def dns(host, *args, **kwargs):
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise PermissionError("offline test: external DNS denied")
    return _original_dns(host, *args, **kwargs)
socket.getaddrinfo = dns
def audit(event, args):
    if event.startswith("winreg."):
        raise PermissionError("offline test: registry disabled")
    if event == "open":
        value, mode, flags = args
        if isinstance(value, (str, bytes, os.PathLike)):
            path = pathlib.Path(os.fsdecode(value)).resolve()
            writing = isinstance(mode, str) and any(x in mode for x in "wax+") or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            if writing and path != pathlib.Path(os.devnull).resolve() and not path.is_relative_to(_temp):
                raise PermissionError("offline test: write outside temp denied")
    if event in ("os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.link"):
        paths = args[:2] if event in ("os.rename", "os.link") else args[:1]
        for value in paths:
            if not pathlib.Path(value).resolve().is_relative_to(_temp):
                raise PermissionError("offline test: mutation outside temp denied")
    if event in ("os.system", "os.exec", "os.posix_spawn"):
        raise PermissionError("offline test: unmanaged native process denied")
    if event == "subprocess.Popen":
        argv = args[1]
        if isinstance(argv, str):
            import shlex
            argv = [value.strip('"') for value in shlex.split(argv, posix=False)]
        executable = pathlib.Path(str(args[0] or argv[0])).name.lower()
        if executable in ("curl", "curl.exe"):
            target = urllib.parse.urlsplit(str(argv[-1]))
            if target.hostname not in ("127.0.0.1", "::1", "localhost"):
                raise PermissionError("offline test: external curl denied")
        elif not executable.startswith("python"):
            raise PermissionError("offline test: native child denied")
sys.addaudithook(audit)
'''


def run_test(path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="mantu-offline-") as temporary:
        temp = Path(temporary)
        (temp / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
        env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "PATH", "COMSPEC") if name in os.environ}
        env.update(PYTHONPATH=str(temp), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   HOME=str(temp), USERPROFILE=str(temp), TMP=str(temp), TEMP=str(temp),
                   ZIMAGE_TEST_TEMP=str(temp), ZIMAGE_ROOT=str(ROOT),
                   RELAY_API_KEY="offline-dummy", RELAY_API_KEY_HD="offline-dummy",
                   RELAY_BASE_URL="http://127.0.0.1:1/v1", RELAY_MODEL="gpt-image-2",
                   IMAGE_EDIT_BASE_URL="http://127.0.0.1:1/v1", IMAGE_EDIT_API_KEY="offline-dummy",
                   RELAY_DISABLE_REGISTRY="1", ZIMAGE_JOBS_DIR=str(temp / "jobs"),
                   ZIMAGE_CACHE_DIR=str(temp / "cache"), ZIMAGE_WEB_ROOT=str(temp / "web"),
                   ZIMAGE_LEDGER_FILE=str(temp / "ledger.jsonl"), ZIMAGE_PENDING_FILE=str(temp / "pending.json"),
                   IMAGE_EDIT_STATE_DIR=str(temp / "plugin-state"))
        proof = subprocess.run([sys.executable, "-B", "-c", "import socket; socket.getaddrinfo('offline.invalid',443)"],
                               env=env, cwd=temp, capture_output=True, timeout=20)
        if proof.returncode == 0 or b"external DNS denied" not in proof.stderr:
            return {"path": str(path), "exit_code": 1, "error": "isolation failed"}
        try:
            result = subprocess.run([sys.executable, "-B", str(path)], env=env, cwd=ROOT,
                                    capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            return {"path": str(path), "exit_code": 1, "error": "test timeout"}
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace")
        print(stdout, end="")
        print(stderr, end="", file=sys.stderr)
        return {"path": str(path.relative_to(ROOT)), "exit_code": result.returncode,
                "stdout": stdout, "stderr": stderr, "isolation": "dummy credentials; loopback only; temp writes only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="+")
    parser.add_argument("--report")
    args = parser.parse_args()
    results = [run_test((ROOT / name).resolve()) for name in args.tests]
    if args.report:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"tests": len(results), "failed": sum(x["exit_code"] != 0 for x in results)}, ensure_ascii=False))
    return int(any(x["exit_code"] != 0 for x in results))


if __name__ == "__main__":
    raise SystemExit(main())
