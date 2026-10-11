#!/usr/bin/env python3
"""Local image-edit service: paint-to-mask canvas backend + OpenAI-compatible /images/edits.

Bind 127.0.0.1 only. No auth. Do not expose to the network.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageOps
from public_fetch import UnsafeDownload, fetch_public_bytes

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PLUGIN_ROOT = ROOT.parent
STATE_ROOT = Path(os.environ.get("IMAGE_EDIT_STATE_DIR") or PLUGIN_ROOT)
PID_FILE = STATE_ROOT / ".image-edit.pid"
LOG_FILE = STATE_ROOT / "server.log"
USER_LOCAL = Path.home() / ".zcode" / "image-edit.local.json"

RED = (255, 0, 0)
MAX_UPSTREAM_BYTES = 1_900_000
JPEG_QUALITY = 88
UPSTREAM_TIMEOUT = 180

ENGINES: dict[str, dict[str, Any]] = {
    "gpt": {
        "label": "GPT Image（gpt-image-2 / 2.5）",
        "base": "",
        "key": "",
        "t2i_model": "gpt-image-2",
        "i2i_model": "gpt-image-2",
        "uses_quality": True,
        "uses_resolution": False,
        "edits_b64": True,
        "multi_image": True,
        "mask": True,
        "sizes": ["1024x1024", "1024x1536", "1536x1024"],
        "qualities": ["high", "medium", "low", "auto"],
    },
    "grok": {
        "label": "Grok Image（文生图）",
        "base": "",
        "key": "",
        "t2i_model": "grok-2-image",
        "i2i_model": "grok-2-image",
        "uses_quality": False,
        "uses_resolution": False,
        "edits_b64": True,
        "multi_image": False,
        "mask": False,
        "sizes": ["1024x1024"],
        "qualities": [],
    },
}

RED_PROMPT = (
    "红色区域只是编辑标记，不是画面内容，结果里不要出现红色色块。"
    "只改红色标记的这一块，其余像素必须保持原样。"
)


def normalize_base(base: str) -> str:
    """Accept base with or without /v1 and always produce the /v1 form."""
    base = (base or "").strip().rstrip("/")
    if base:
        parsed = urlparse(base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("Base URL 必须是无凭据、无查询参数的 http/https 地址")
        if not base.endswith("/v1"):
            base += "/v1"
    return base


def load_local_config() -> dict[str, str]:
    """Read Base URL / API Key from this machine only. Never from the plugin package."""
    sources: list[dict[str, str]] = []
    for path in (USER_LOCAL, PLUGIN_ROOT / "local.json"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            sources.append({k: data[k].strip() for k in ("base_url", "api_key", "model")
                            if isinstance(data.get(k), str) and data[k].strip()})
    env_base = (os.environ.get("IMAGE_EDIT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
    env_key = next((os.environ[name].strip() for name in
                    ("IMAGE_EDIT_API_KEY", "GEILI_SUB2API_KEY", "OPENAI_API_KEY")
                    if os.environ.get(name, "").strip()), "")
    sources.append({"base_url": env_base, "api_key": env_key})
    base = next((normalize_base(s["base_url"]) for s in sources if s.get("base_url")), "")
    # Never merge a credential with a base from a different configuration source.
    key = next((s["api_key"] for s in sources if s.get("api_key") and s.get("base_url")
                and normalize_base(s["base_url"]) == base), "")
    model = next((s["model"] for s in sources if s.get("model")), "gpt-image-2")
    return {"base_url": base, "api_key": key, "model": model}


def resolve_credentials(fields: dict[str, Any]) -> tuple[str, str]:
    local = load_local_config()
    base = normalize_base(str(fields.get("base_url") or "") or local.get("base_url", ""))
    key = str(fields.get("api_key") or "").strip()
    if not key and base and base == local.get("base_url"):
        key = local.get("api_key", "")
    return base, key


def log(msg: str) -> None:
    line = msg.rstrip() + "\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    sys.stderr.write(line)


def write_pid(port: int) -> None:
    PID_FILE.write_text(f"{os.getpid()}\n{port}\n", encoding="utf-8")


def clear_pid() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except OSError:
        pass


def parse_size(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", text or "")
    if not m:
        return 1024, 1024
    return int(m.group(1)), int(m.group(2))


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[dict[str, Any]]]:
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "", re.I)
    if not m:
        raise ValueError("multipart 缺少 boundary")
    boundary = (m.group(1) or m.group(2)).strip().encode("ascii", "ignore")
    delim = b"--" + boundary
    fields: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    for raw in body.split(delim):
        if not raw or raw.startswith(b"--"):
            continue
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        elif raw.startswith(b"\n"):
            raw = raw[1:]
        header_blob, sep, data = raw.partition(b"\r\n\r\n")
        if not sep:
            header_blob, sep, data = raw.partition(b"\n\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        elif data.endswith(b"\n"):
            data = data[:-1]
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n" if b"\r\n" in header_blob else b"\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin1").lower()] = v.decode("latin1").strip()
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]+)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', disp)
        if fn_m is not None:
            files.append(
                {
                    "name": name,
                    "filename": fn_m.group(1) or "blob",
                    "content_type": headers.get("content-type", "application/octet-stream"),
                    "data": data,
                }
            )
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def open_image(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def mask_channel(mask: Image.Image) -> Image.Image:
    if mask.mode in ("RGBA", "LA"):
        return mask.split()[-1]
    if mask.mode == "P":
        return mask.convert("RGBA").split()[-1]
    return mask.convert("L")


def resize_pair(
    img: Image.Image,
    mask: Image.Image | None,
    size: tuple[int, int],
    pad_mode: str,
) -> tuple[Image.Image, Image.Image | None]:
    tw, th = size
    pad_mode = (pad_mode or "fit").lower()
    img_rgb = img.convert("RGB")
    mask_l = mask_channel(mask) if mask is not None else None

    if pad_mode == "stretch":
        out = img_rgb.resize((tw, th), Image.Resampling.LANCZOS)
        out_m = mask_l.resize((tw, th), Image.Resampling.NEAREST) if mask_l else None
        return out, out_m

    iw, ih = img_rgb.size
    if pad_mode == "fill":
        scale = max(tw / iw, th / ih)
        nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
        resized = img_rgb.resize((nw, nh), Image.Resampling.LANCZOS)
        left, top = (nw - tw) // 2, (nh - th) // 2
        out = resized.crop((left, top, left + tw, top + th))
        if mask_l is not None:
            rm = mask_l.resize((nw, nh), Image.Resampling.NEAREST)
            out_m = rm.crop((left, top, left + tw, top + th))
        else:
            out_m = None
        return out, out_m

    scale = min(tw / iw, th / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    resized = img_rgb.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (tw, th), (0, 0, 0))
    ox, oy = (tw - nw) // 2, (th - nh) // 2
    canvas.paste(resized, (ox, oy))
    out_m = None
    if mask_l is not None:
        rm = mask_l.resize((nw, nh), Image.Resampling.NEAREST)
        mc = Image.new("L", (tw, th), 0)
        mc.paste(rm, (ox, oy))
        out_m = mc
    return canvas, out_m


def mask_to_binary(mask: Image.Image, invert: bool) -> Image.Image:
    """Painted pixels are red-on-transparent in the browser. Use alpha when present.

    Do not convert red RGB to L: 255,0,0 becomes ~76 and would miss the threshold.
    """
    if mask.mode in ("RGBA", "LA"):
        m = mask.split()[-1]
    elif mask.mode == "P":
        m = mask.convert("RGBA").split()[-1]
    else:
        m = mask.convert("L")
    m = m.point(lambda p: 255 if p >= 128 else 0)
    if invert:
        m = ImageOps.invert(m)
    return m


def paint_red(img: Image.Image, mask: Image.Image, invert: bool) -> Image.Image:
    base = img.convert("RGB")
    m = mask_to_binary(mask, invert)
    if m.size != base.size:
        m = m.resize(base.size, Image.Resampling.NEAREST)
    red = Image.new("RGB", base.size, RED)
    return Image.composite(red, base, m)


def encode_jpeg(img: Image.Image, max_bytes: int = MAX_UPSTREAM_BYTES) -> tuple[bytes, int]:
    rgb = img.convert("RGB")
    quality = JPEG_QUALITY
    data = b""
    while quality >= 45:
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data, quality
        quality -= 8
    return data, quality


def files_by_name(files: list[dict[str, Any]], *names: str) -> list[dict[str, Any]]:
    want = {n.lower() for n in names}
    return [f for f in files if f["name"].lower() in want]


def build_multipart(fields: dict[str, str], blobs: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "----ImageEdit" + secrets.token_hex(8)
    chunks: list[bytes] = []
    for k, v in fields.items():
        if v is None or v == "":
            continue
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        chunks.append(str(v).encode("utf-8") + b"\r\n")
    for field, filename, data, ctype in blobs:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        chunks.append(data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def join_url(base: str, path: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        raise ValueError("缺少 Base URL")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


_ORIG_GETADDRINFO = socket.getaddrinfo


def _ipv4_first_addrinfo(*args: Any, **kwargs: Any):
    """Prefer IPv4. Cloudflare IPv6 on this machine sometimes stalls the TLS handshake."""
    res = _ORIG_GETADDRINFO(*args, **kwargs)
    v4 = [x for x in res if x[0] == socket.AF_INET]
    return (v4 + [x for x in res if x[0] != socket.AF_INET]) if v4 else res


socket.getaddrinfo = _ipv4_first_addrinfo  # type: ignore[assignment]

def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(req.full_url) != _origin(newurl):
            raise urllib.error.HTTPError(newurl, 403, "Cross-origin authenticated redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameOriginRedirect())


def _transient(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "timed out",
            "timeout",
            "handshake",
            "temporarily unavailable",
            "connection reset",
            "eof occurred",
            "10054",
            "10060",
        )
    )


def http_json(method: str, url: str, api_key: str, body: bytes | None = None, content_type: str | None = None, timeout: int = 30) -> tuple[int, Any, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "image-edit/0.1",
    }
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    last_exc: BaseException | None = None
    attempts = 3 if method.upper() == "GET" else 1
    for i in range(attempts):
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(text) if text else {}, text
                except json.JSONDecodeError:
                    return resp.status, {"raw": text}, text
        except urllib.error.HTTPError as e:
            raw = e.read()
            text = raw.decode("utf-8", "replace")
            try:
                parsed = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = {"raw": text}
            if e.code in (429, 502, 503) and i + 1 < attempts:
                time.sleep(1.2 * (i + 1))
                continue
            return e.code, parsed, text
        except Exception as e:
            last_exc = e
            if i + 1 < attempts and _transient(e):
                time.sleep(1.0 * (i + 1))
                continue
            return 0, {"error": str(e)}, str(e)
    return 0, {"error": str(last_exc)}, str(last_exc)


def http_bytes(url: str, api_key: str, timeout: int = 300) -> tuple[int, bytes, str]:
    """GET binary content (e.g. finished video) with auth. Returns (status, body, content_type)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "image-edit/0.1",
    }
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")
    except Exception as e:
        return 0, b"", str(e)


def _find_video_url(obj: Any) -> str | None:
    """Best-effort: locate a downloadable video URL anywhere in an upstream JSON response."""
    if isinstance(obj, str):
        return obj if obj.startswith(("http://", "https://")) else None
    if isinstance(obj, dict):
        for k in ("video_url", "mp4", "download_url", "content_url", "url"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                if "video" in v.lower() or "mp4" in v.lower() or k == "url":
                    return v
        for v in obj.values():
            r = _find_video_url(v)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = _find_video_url(v)
            if r:
                return r
    return None


# --- video jobs -------------------------------------------------------------

VIDEO_DIR = STATE_ROOT / "outputs"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_JOBS: dict[str, dict[str, Any]] = {}
VIDEO_JOBS_LOCK = threading.Lock()
VIDEO_POLL_INTERVAL = 4.0
VIDEO_POLL_DEADLINE = 600.0
_FILE_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def video_job_update(job_id: str, **kw: Any) -> None:
    with VIDEO_JOBS_LOCK:
        VIDEO_JOBS[job_id].update(kw)


def video_job_log(job_id: str, msg: str) -> None:
    log("video[%s] %s" % (job_id, msg))


def _video_download(job_id: str, base: str, key: str, upstream_id: str, parsed: Any) -> None:
    """Fetch the finished video: try /videos/{id}/content first, then any URL in the payload."""
    data, ctype, st = b"", "", 0
    if upstream_id:
        url = join_url(base, "/videos/%s/content" % upstream_id)
        st, data, ctype = http_bytes(url, key, timeout=300)
    if not (st == 200 and data and ("video" in ctype or data[:16].find(b"ftyp") >= 4)):
        video_job_log(job_id, "content endpoint status=%s ctype=%s len=%d" % (st, ctype, len(data)))
        url2 = _find_video_url(parsed)
        if not url2:
            video_job_update(job_id, status="failed", error="任务完成但拿不到视频文件（/content 失败且响应里没有可下载的 URL）")
            return
        data, ctype = fetch_public_bytes(url2, max_bytes=100 * 1024 * 1024,
                                         timeout=300, content_types=("video/", "application/octet-stream"))
    name = "%s.mp4" % job_id
    (VIDEO_DIR / name).write_bytes(data)
    video_job_log(job_id, "saved %s bytes=%d ctype=%s" % (name, len(data), ctype))
    video_job_update(job_id, status="completed", progress=100, file_url="/files/" + name)


def _video_worker(job_id: str, base: str, key: str, model: str, prompt: str, size: str, seconds: str) -> None:
    try:
        video_job_update(job_id, phase="submit", status="running")
        body = json.dumps({"model": model, "prompt": prompt, "seconds": seconds, "size": size}).encode("utf-8")
        status, parsed, text = http_json(
            "POST", join_url(base, "/videos"), key, body=body, content_type="application/json", timeout=60
        )
        video_job_log(job_id, "submit /videos status=%s" % status)

        if status not in (200, 201, 202) and status not in (404, 405):
            video_job_update(job_id, status="failed", error="提交失败或受理状态未知 HTTP %s；未自动重发" % status)
            return
        if status in (404, 405):
            # Fallback only when the server explicitly rejects this endpoint.
            video_job_update(job_id, phase="submit_sync")
            status2, parsed2, _ = http_json(
                "POST", join_url(base, "/videos/generations"), key, body=body, content_type="application/json", timeout=300
            )
            video_job_log(job_id, "submit /videos/generations status=%s" % status2)
            if status2 not in (200, 201, 202):
                video_job_update(
                    job_id,
                    status="failed",
                    error="提交失败 HTTP %s：%s（HTTP %s：%s）"
                    % (status, json.dumps(parsed, ensure_ascii=False)[:220], status2, json.dumps(parsed2, ensure_ascii=False)[:220]),
                )
                return
            video_job_update(job_id, phase="download")
            _video_download(job_id, base, key, "", parsed2)
            return

        upstream_id = None
        if isinstance(parsed, dict):
            for k in ("id", "job_id", "task_id"):
                if isinstance(parsed.get(k), str) and parsed[k]:
                    upstream_id = parsed[k]
                    break
        direct = _find_video_url(parsed) if isinstance(parsed, dict) else None
        if direct and not upstream_id:
            video_job_update(job_id, phase="download")
            _video_download(job_id, base, key, "", parsed)
            return
        if not upstream_id:
            video_job_update(job_id, status="failed", error="上游响应里没有任务 id，也没有可用的视频 URL")
            return

        video_job_update(job_id, phase="poll", upstream_id=upstream_id)
        deadline = time.time() + VIDEO_POLL_DEADLINE
        fails = 0
        while time.time() < deadline:
            time.sleep(VIDEO_POLL_INTERVAL)
            st, pj, _ = http_json("GET", join_url(base, "/videos/%s" % upstream_id), key, timeout=30)
            if st != 200:
                fails += 1
                video_job_log(job_id, "poll status=%s (fails=%d)" % (st, fails))
                if fails >= 5:
                    video_job_update(job_id, status="failed", error="轮询任务状态连续失败 HTTP %s" % st)
                    return
                continue
            fails = 0
            s = str(pj.get("status") or pj.get("state") or "").lower() if isinstance(pj, dict) else ""
            prog = pj.get("progress") if isinstance(pj, dict) else None
            video_job_update(job_id, status="running", progress=prog if isinstance(prog, (int, float)) else None)
            if s in ("completed", "succeeded", "success", "done"):
                video_job_update(job_id, phase="download")
                _video_download(job_id, base, key, upstream_id, pj)
                return
            if s in ("failed", "error", "cancelled", "canceled"):
                err = pj.get("error") if isinstance(pj, dict) else None
                video_job_update(job_id, status="failed", error="上游任务失败：%s" % (json.dumps(err, ensure_ascii=False)[:300] if err else s))
                return
        video_job_update(job_id, status="failed", error="超时：%.0f 秒内任务没有完成" % VIDEO_POLL_DEADLINE)
    except Exception as e:
        video_job_log(job_id, "worker error %s\n%s" % (e, traceback.format_exc()))
        video_job_update(job_id, status="failed", error=str(e))


def redact(fields: dict[str, str]) -> dict[str, str]:
    out = dict(fields)
    for k in list(out):
        if "key" in k.lower() or "secret" in k.lower() or "token" in k.lower():
            out[k] = "***"
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "image-edit/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        code = args[1] if len(args) > 1 else "-"
        log("%s - %s %s status=%s" % (self.address_string(), self.command, urlparse(self.path).path, code))

    def _send(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or "0")
        if n < 0 or n > 80 * 1024 * 1024:
            raise ValueError("请求体过大")
        return self.rfile.read(n) if n else b""

    def _guard(self) -> bool:
        """Same-origin fence for every request.

        Host 必须是环回地址：DNS rebinding 会把恶意页面的 Host 变成攻击者域名，
        在这里直接拒绝。带 Origin 的请求（跨站 fetch 一定带）必须同源，
        浏览器同源 GET 不带 Origin，不受影响。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json(403, {"ok": False, "error": "Host 校验失败：仅接受 127.0.0.1 访问"})
            return False
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self._json(403, {"ok": False, "error": "Origin 校验失败：跨站请求已拒绝"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            index = STATIC / "index.html"
            if not index.exists():
                self._send(500, b"index.html missing", "text/plain")
                return
            self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/health" or path == "/status":
            self._json(200, {"ok": True, "running": True, "service": "image-edit-canvas",
                             "pid": os.getpid(), "port": self.server.server_address[1]})
            return
        if path == "/api/engines":
            public = {}
            for k, v in ENGINES.items():
                item = dict(v)
                item.pop("key", None)
                public[k] = item
            self._json(200, {"ok": True, "engines": public})
            return
        if path == "/api/defaults":
            cfg = load_local_config()
            # 安全红线：api_key 只留在服务端（环境变量/local.json），绝不下发到页面。
            # 页面需要知道的是"有没有配好"，不是 key 本身。
            self._json(
                200,
                {
                    "ok": True,
                    "base_url": cfg.get("base_url") or "",
                    "has_key": bool(cfg.get("api_key")),
                    "model": cfg.get("model") or "gpt-image-2",
                    "source": str(USER_LOCAL) if USER_LOCAL.is_file() else "env",
                },
            )
            return
        if path == "/api/video/status":
            qs = urlparse(self.path).query
            job = ""
            for kv in qs.split("&"):
                if kv.startswith("job="):
                    job = _FILE_SAFE.sub("", kv[4:].replace("%", ""))
                    break
            with VIDEO_JOBS_LOCK:
                job = VIDEO_JOBS.get(job)
            if not job:
                self._json(404, {"ok": False, "error": "任务不存在"})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "status": job.get("status"),
                    "phase": job.get("phase"),
                    "progress": job.get("progress"),
                    "error": job.get("error"),
                    "file_url": job.get("file_url"),
                },
            )
            return
        if path == "/api/fetch-url":
            # 拖拽/粘贴场景兜底：QQ 等应用只给图片 URL 且跨域被拦时，由本机服务代为下载。
            qs = parse_qs(parsed.query)
            url = (qs.get("url", [""])[0] or "").strip()
            if not url.startswith(("http://", "https://")):
                self._json(400, {"ok": False, "error": "仅支持 http/https 图片地址"})
                return
            try:
                data, ctype = fetch_public_bytes(url)
                with Image.open(io.BytesIO(data)) as downloaded:
                    downloaded.load()
                self._send(200, data, ctype)
            except UnsafeDownload as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception:
                log("fetch-url download failed")
                self._json(502, {"ok": False, "error": "图片下载或解码失败"})
            return
        if path.startswith("/files/"):
            name = _FILE_SAFE.sub("", path[len("/files/"):])
            if not name:
                self._send(404, b"not found", "text/plain")
                return
            target = (VIDEO_DIR / name).resolve()
            try:
                target.relative_to(VIDEO_DIR.resolve())
            except ValueError:
                self._send(404, b"not found", "text/plain")
                return
            if not target.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = "video/mp4" if target.suffix == ".mp4" else "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)
            return
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            target = (STATIC / rel).resolve()
            try:
                target.relative_to(STATIC.resolve())
            except ValueError:
                self._send(404, b"not found", "text/plain")
                return
            if not target.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = "application/octet-stream"
            if target.suffix == ".js":
                ctype = "text/javascript; charset=utf-8"
            elif target.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            elif target.suffix == ".html":
                ctype = "text/html; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/stop":
                self._json(200, {"ok": True, "stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/test":
                self._api_test()
                return
            if path == "/api/models":
                self._api_models()
                return
            if path == "/api/edit":
                self._api_edit()
                return
            if path == "/api/video":
                self._api_video()
                return
            if path == "/api/save-defaults":
                self._api_save_defaults()
                return
            self._send(404, b"not found", "text/plain")
        except SystemExit:
            raise
        except Exception as e:
            log("handler error: %s\n%s" % (e, traceback.format_exc()))
            self._json(500, {"ok": False, "error": str(e)})

    def _json_body(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError("JSON 无法解析: %s" % e) from e
        if not isinstance(obj, dict):
            raise ValueError("JSON 必须是对象")
        return obj

    def _api_test(self) -> None:
        body = self._json_body()
        base, key = resolve_credentials(body)
        if not base or not key:
            self._json(400, {"ok": False, "error": "需要 base_url 和 api_key"})
            return
        status, parsed, _text = http_json("GET", join_url(base, "/models"), key, timeout=20)
        if status == 200:
            data = parsed.get("data") if isinstance(parsed, dict) else None
            n = len(data) if isinstance(data, list) else 0
            self._json(200, {"ok": True, "status": status, "model_count": n})
            return
        self._json(200, {"ok": False, "status": status, "error": parsed})

    def _api_models(self) -> None:
        body = self._json_body()
        base, key = resolve_credentials(body)
        if not base or not key:
            self._json(400, {"ok": False, "error": "需要 base_url 和 api_key"})
            return
        status, parsed, _text = http_json("GET", join_url(base, "/models"), key, timeout=20)
        ids = []
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
            for item in parsed["data"]:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
        self._json(200, {"ok": status == 200, "status": status, "models": ids, "raw_error": None if status == 200 else parsed})

    def _api_edit(self) -> None:
        fields, files = parse_multipart(self.headers.get("Content-Type") or "", self._read_body())
        local = load_local_config()
        base, key = resolve_credentials(fields)
        contents = files_by_name(files, "content", "image", "images")
        masks = files_by_name(files, "mask", "masks")
        references = files_by_name(files, "ref", "reference", "refs")
        operation = "generate" if fields.get("operation") == "generate" else "edit"
        if len(contents) > 1:
            self._json(400, {"ok": False, "error": "一次改图使用一张身份图；其他图片请放参考槽。"})
            return
        try:
            count = int(fields.get("count") or "1")
            if not 1 <= count <= 4:
                raise ValueError("count")
        except ValueError:
            self._json(400, {"ok": False, "error": "生成数量须为 1–4。"})
            return
        workspace = Path(os.environ.get("ZIMAGE_ROOT") or Path(__file__).resolve().parents[3])
        adapter_path = workspace / "compose" / "images"
        if not (workspace / "zcode-image-edit" / "bin" / "zimage.py").is_file():
            self._json(400, {"ok": False, "error": "请配置 ZIMAGE_ROOT 为包含 zimage.py 的项目目录。"})
            return
        sys.path.insert(0, str(adapter_path))
        from image_cli_adapter import execute_image_request
        engine = ENGINES.get(fields.get("engine") or "gpt", ENGINES["gpt"])
        whole = fields.get("scope") not in ("mask", "masked", "inpaint") or operation == "generate"
        if not whole and not engine["mask"]:
            self._json(400, {"ok": False, "error": "该引擎不支持遮罩。"})
            return
        if not whole and not masks:
            self._json(400, {"ok": False, "error": "局部编辑需要遮罩。"})
            return
        output_format = fields.get("output_format") or "png"
        request_sample = str(fields.get("request_id") or "").strip() if count > 1 else ""
        if count > 1 and (not request_sample or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", request_sample)):
            self._json(400, {"ok": False, "error": "多张采样需要稳定请求编号，避免未知受理后重复扣费。"})
            return
        requested_quality = fields.get("quality") or "low"
        if requested_quality == "auto":
            short = min(parse_size(fields.get("size") or "1024x1024"))
            requested_quality = "low" if short <= 1024 else ("medium" if short <= 2048 else "high")
        images, jobs = [], []
        result = {}
        for index in range(count):
            result = execute_image_request(
                prompt=fields.get("prompt") or "", source=contents[0]["data"] if contents else None,
                refs=[("reference", item["data"]) for item in references],
                mask=masks[0]["data"] if masks and not whole else None,
                protect=fields.get("mask_mode") in ("invert", "keep", "paint_keep", "reverse"),
                size=fields.get("size") or "1024x1024", quality=requested_quality,
                model=fields.get("model") or local.get("model") or engine["i2i_model"],
                base_url=base, api_key=key, pad="crop" if fields.get("pad_mode") == "crop" else "pad",
                operation=operation, input_fidelity=fields.get("input_fidelity") or "",
                output_format=output_format, sample_id=f"{request_sample}-{index}" if request_sample else "",
            )
            jobs.append(result.get("job_id"))
            if not result.get("ok"):
                result["images"] = images
                result["job_ids"] = jobs
                self._json(200, result)
                return
            images.extend(result.get("images") or [])
        result["images"] = images
        result["job_ids"] = jobs
        self._json(200, result)

    def _api_save_defaults(self) -> None:
        self._json(409, {
            "ok": False,
            "error": "本项目凭据只从环境变量读取；现有本机配置保留只读，不再由页面写入。",
        })


def main() -> None:
    parser = argparse.ArgumentParser(description="image-edit local canvas server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("IMAGE_EDIT_PORT") or "8000"))
    args = parser.parse_args()
    host = "127.0.0.1"
    httpd = ThreadingHTTPServer((host, args.port), Handler)
    write_pid(args.port)
    atexit.register(clear_pid)
    log("listening on http://%s:%d/" % (host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        clear_pid()


if __name__ == "__main__":
    main()
