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
PLUGIN_MANIFEST = PLUGIN_ROOT / ".zcode-plugin" / "plugin.json"
UPDATE_REPO = "QingXpuli/image-edit-studio"


def plugin_version() -> str:
    try:
        data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        v = data.get("version")
        if isinstance(v, str) and v.strip():
            return v.strip()
    except (OSError, ValueError):
        pass
    return "0.0.0"


def _version_tuple(text: str) -> tuple[int, ...]:
    nums = []
    for part in re.split(r"[^\d]+", (text or "").lstrip("v")):
        if part.isdigit():
            nums.append(int(part))
    return tuple(nums) or (0,)

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
                    "version": plugin_version(),
                },
            )
            return
        if path == "/api/update":
            current = plugin_version()
            latest = current
            url = "https://github.com/%s/releases" % UPDATE_REPO
            notes = ""
            try:
                req = urllib.request.Request(
                    "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO,
                    headers={"User-Agent": "image-edit-canvas/" + current, "Accept": "application/vnd.github+json"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
                tag = str(data.get("tag_name") or "").strip()
                if tag:
                    latest = tag.lstrip("v")
                    url = str(data.get("html_url") or url)
                    notes = str(data.get("name") or "")
            except Exception as exc:
                log("update check failed: %s" % exc)
                self._json(200, {"ok": True, "current": current, "latest": None, "newer": False, "url": url, "error": "暂时查不到 GitHub Release"})
                return
            newer = _version_tuple(latest) > _version_tuple(current)
            self._json(200, {"ok": True, "current": current, "latest": latest, "newer": newer, "url": url, "name": notes})
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
        ctype = self.headers.get("Content-Type") or ""
        body = self._read_body()
        fields, files = parse_multipart(ctype, body)
        log("edit fields=%s files=%s" % (redact(fields), [(f["name"], f["filename"], len(f["data"])) for f in files]))

        local = load_local_config()
        base, key = resolve_credentials(fields)
        engine_id = (fields.get("engine") or "gpt").strip() or "gpt"
        engine = ENGINES.get(engine_id) or ENGINES["gpt"]
        model = (fields.get("model") or local.get("model") or engine.get("i2i_model") or "gpt-image-2").strip()
        prompt = (fields.get("prompt") or "").strip()
        size_text = (fields.get("size") or "1024x1024").strip()
        quality = (fields.get("quality") or "high").strip()
        resolution = (fields.get("resolution") or "").strip()
        pad_mode = (fields.get("pad_mode") or "fit").strip()
        mask_mode = (fields.get("mask_mode") or "paint_edit").strip()
        scope = (fields.get("scope") or "mask").strip()
        operation = (fields.get("operation") or "edit").strip()
        try:
            count = max(1, min(4, int(fields.get("count") or "1")))
        except ValueError:
            count = 1
        output_format = (fields.get("output_format") or "png").strip().lower()
        if output_format not in ("png", "jpeg", "webp"):
            output_format = "png"

        if not base or not key:
            self._json(400, {"ok": False, "error": "需要 Base URL 和 API Key（只在本机页面填写，不会写入插件）"})
            return
        if not prompt:
            self._json(400, {"ok": False, "error": "缺少提示词"})
            return

        contents = files_by_name(files, "content", "image", "images")
        masks = files_by_name(files, "mask", "masks")
        refs = files_by_name(files, "ref", "reference", "refs")
        if operation != "generate":
            # 文生图不需要内容图；改图才需要。
            if not contents:
                self._json(400, {"ok": False, "error": "请先选一张内容图"})
                return
            if not engine.get("multi_image") and (len(contents) + len(refs)) > 1:
                self._json(400, {"ok": False, "error": "当前引擎不接受多张参考图"})
                return

        invert = mask_mode in ("invert", "keep", "paint_keep", "reverse")
        use_mask = scope in ("mask", "masked", "inpaint") and bool(engine.get("mask")) and operation != "generate"
        target = parse_size(size_text)

        painted: list[Image.Image] = []
        extras: list[Image.Image] = []
        for i, item in enumerate(contents):
            try:
                im = open_image(item["data"])
            except Exception as e:
                self._json(400, {"ok": False, "error": "内容图无法读取: %s" % e})
                return
            mask_im = None
            if i < len(masks) and masks[i]["data"]:
                try:
                    mask_im = open_image(masks[i]["data"])
                except Exception as e:
                    self._json(400, {"ok": False, "error": "遮罩无法读取: %s" % e})
                    return
            im2, mask2 = resize_pair(im, mask_im, target, pad_mode)
            if use_mask and mask2 is not None:
                binary = mask_to_binary(mask2, invert=invert)
                if binary.getextrema()[1] == 0:
                    self._json(400, {"ok": False, "error": "遮罩是空的。请在图上涂抹要改的区域（或改用整图重画）。"})
                    return
                painted.append(paint_red(im2, mask2, invert=invert))
            else:
                extras.append(im2)

        if use_mask and not painted:
            self._json(400, {"ok": False, "error": "仅遮罩区模式需要一张遮罩。请在图上涂抹要改的区域。"})
            return

        ref_images: list[Image.Image] = []
        for item in refs:
            try:
                rim = open_image(item["data"])
            except Exception as e:
                self._json(400, {"ok": False, "error": "参考图无法读取: %s" % e})
                return
            rim2, _ = resize_pair(rim, None, target, pad_mode)
            ref_images.append(rim2)

        # 仅遮罩区：只发涂红图 + 参考图，不发干净原图（否则模型会把红区「恢复」回去）。
        # 整图：发原图（已缩放到目标尺寸）+ 参考图，不涂红。
        send_images: list[Image.Image] = []
        if use_mask:
            send_images.extend(painted)
            send_images.extend(ref_images)
        else:
            send_images.extend(extras)
            send_images.extend(ref_images)

        blobs: list[tuple[str, str, bytes, str]] = []
        total = 0
        for idx, im in enumerate(send_images):
            data, q = encode_jpeg(im)
            total += len(data)
            field = "image" if idx == 0 else "image[%d]" % idx
            blobs.append((field, "image%d.jpg" % idx, data, "image/jpeg"))
            log("feed %s jpeg q=%d bytes=%d size=%s" % (field, q, len(data), im.size))
        if total > 2_100_000:
            self._json(
                400,
                {
                    "ok": False,
                    "error": "投喂体积约 %.2f MiB，中转站大约 1.96 MiB 起可能拒收。请降低分辨率或少加参考图。"
                    % (total / 1024 / 1024),
                },
            )
            return

        final_prompt = prompt
        if use_mask:
            final_prompt = prompt.rstrip("。.") + "。" + RED_PROMPT

        up_fields: dict[str, str] = {
            "model": model,
            "prompt": final_prompt,
            "size": size_text,
            "n": str(count),
        }
        if engine.get("uses_quality") and quality and quality != "auto":
            up_fields["quality"] = quality
        if engine.get("uses_resolution") and resolution:
            up_fields["resolution"] = resolution
        if output_format != "png":
            up_fields["output_format"] = output_format

        payload, content_type = build_multipart(up_fields, blobs)
        url = join_url(base, "/images/edits" if operation != "generate" else "/images/generations")
        if operation == "generate":
            gen_fields = dict(up_fields)
            payload, content_type = build_multipart(gen_fields, [])
            content_type = "application/json"
            payload = json.dumps(gen_fields).encode("utf-8")

        status, parsed, text = http_json("POST", url, key, body=payload, content_type=content_type, timeout=UPSTREAM_TIMEOUT)
        if status != 200:
            log("edit upstream status=%s" % status)
        if status == 502:
            self._json(
                200,
                {
                    "ok": False,
                    "status": 502,
                    "retry": False,
                    "error": "上游返回 502，受理状态可能未知；请先核对任务或账单，不要直接重复生成。",
                    "upstream": parsed,
                },
            )
            return
        if status != 200:
            self._json(200, {"ok": False, "status": status, "error": parsed or text, "retry": False})
            return

        # Collect every returned image; upstreams answer with data[] for n>1.
        raw_items: list[tuple[str, str]] = []
        if isinstance(parsed, dict):
            data = parsed.get("data")
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        b = item.get("b64_json") or item.get("b64")
                        u = item.get("url")
                        if b:
                            raw_items.append(("b64", str(b)))
                        elif u:
                            raw_items.append(("url", str(u)))
            if not raw_items:
                b = parsed.get("b64_json") or parsed.get("b64")
                u = parsed.get("url")
                if b:
                    raw_items.append(("b64", str(b)))
                elif u:
                    raw_items.append(("url", str(u)))
        if not raw_items:
            log("edit upstream 200 without images")
            self._json(200, {"ok": False, "status": status, "error": "上游没有 b64_json 也没有可用 url", "upstream": parsed})
            return

        images: list[dict[str, str]] = []
        for kind, val in raw_items[:4]:
            b64 = val
            if kind == "url":
                try:
                    raw_img, _mime = fetch_public_bytes(val, timeout=60)
                    b64 = base64.b64encode(raw_img).decode("ascii")
                except Exception:
                    log("edit result download failed")
                    continue
            try:
                out_im = open_image(base64.b64decode(b64))
                dims = "%dx%d" % out_im.size
            except Exception:
                dims = ""
            images.append({"b64": b64, "size": dims})
        if not images:
            self._json(200, {"ok": False, "status": status, "error": "上游返回了图片但本机下载/解析失败", "upstream": parsed})
            return
        mime = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}[output_format]
        self._json(
            200,
            {
                "ok": True,
                "status": 200,
                "b64": images[0]["b64"],
                "images": images,
                "mime": mime,
                "requested_size": size_text,
                "actual_size": images[0]["size"],
                "feed_bytes": total,
                "note": "上游尺寸不严格，实际可能和请求不一致，不要做像素级对齐。" if images[0]["size"] and images[0]["size"] != size_text else "",
            },
        )

    def _api_save_defaults(self) -> None:
        # 只接受本机页面发来的请求：自定义头跨域带不了，防恶意网页 CSRF 覆盖配置。
        if (self.headers.get("X-Image-Edit-Local") or "") != "1":
            self._json(403, {"ok": False, "error": "拒绝：缺少本机页面标识头"})
            return
        body = self._json_body()
        cfg = {}
        base = normalize_base(str(body.get("base_url") or ""))
        key = str(body.get("api_key") or "").strip()
        model = str(body.get("model") or "").strip()
        if not key and USER_LOCAL.is_file():
            try:
                existing = json.loads(USER_LOCAL.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and base == normalize_base(str(existing.get("base_url") or "")):
                    key = str(existing.get("api_key") or "")
            except (OSError, ValueError):
                pass
        if base:
            cfg["base_url"] = base
        if key:
            cfg["api_key"] = key
        if model:
            cfg["model"] = model
        if not cfg:
            self._json(400, {"ok": False, "error": "没有可保存的字段"})
            return
        import tempfile
        USER_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".image-edit.", suffix=".tmp", dir=USER_LOCAL.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(cfg, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, USER_LOCAL)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        log("save-defaults updated %s (api_key length=%d)" % (", ".join(sorted(cfg)), len(key)))
        self._json(200, {"ok": True, "path": str(USER_LOCAL), "saved": sorted(cfg)})

    def _api_video(self) -> None:
        body = self._json_body()
        base, key = resolve_credentials(body)
        model = str(body.get("model") or "").strip()
        prompt = str(body.get("prompt") or "").strip()
        size = str(body.get("size") or "1280x720").strip()
        seconds = str(body.get("seconds") or "8").strip()

        if not base or not key:
            self._json(400, {"ok": False, "error": "需要 Base URL 和 API Key"})
            return
        if not model:
            self._json(400, {"ok": False, "error": "缺少视频模型名"})
            return
        if not prompt:
            self._json(400, {"ok": False, "error": "缺少提示词"})
            return

        job_id = secrets.token_hex(6)
        with VIDEO_JOBS_LOCK:
            VIDEO_JOBS[job_id] = {
                "status": "queued",
                "phase": "queued",
                "progress": None,
                "error": None,
                "file_url": None,
                "upstream_id": None,
            }
        log("video[%s] submit model=%s size=%s seconds=%s base=%s" % (job_id, model, size, seconds, base))
        threading.Thread(
            target=_video_worker,
            args=(job_id, base, key, model, prompt, size, seconds),
            daemon=True,
        ).start()
        self._json(200, {"ok": True, "job_id": job_id})


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
