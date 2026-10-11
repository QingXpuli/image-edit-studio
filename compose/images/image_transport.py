"""Single-submit transport and local-only result publication primitives."""
from __future__ import annotations

import base64
import binascii
import gzip
import http.client
import io
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

# Cloudflare on some relay hosts (sub.geiliapi.com) returns 1010 for
# Python-urllib's default User-Agent. Browser UA is enough to pass GET /models.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class CallResult:
    phase: str = "preflight"
    classification: str = "not_sent"
    acceptance: str = "not_sent"
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    received_bytes: int = 0
    submit_attempts: int = 0
    retryable_generation: bool = False
    elapsed_s: float = 0.0
    marker_job: str = "not_requested"
    marker_shared: str = "not_requested"
    text: str = field(default="", repr=False)

    def report(self):
        return {k: v for k, v in vars(self).items() if k != "text"}

    def legacy(self):
        status = self.http_status if self.http_status is not None else -1
        if self.classification == "response_complete":
            return status, self.text
        return status, json.dumps({"error": {"type": self.classification}})


class NoPostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Refuse *all* POST redirects: even a same-origin 307 would repeat POST.
        if req.get_method() == "POST":
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def atomic_bytes(path, raw, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_report(path, result):
    if path:
        atomic_bytes(path, (json.dumps(result.report(), indent=2) + "\n").encode())
        events = Path(str(path) + ".events.jsonl")
        with events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.report()) + "\n")
            f.flush()
            os.fsync(f.fileno())


def private_snapshot(path, text, *, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    atomic_bytes(path, text.encode("utf-8"), private=True)


def submission_marker(path, *, payload=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        if payload is None:
            raw = b'POST may have been submitted; never resubmit this job.\n'
        else:
            raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def post_once(req, *, timeout=900, marker=None, report_path=None, opener=None):
    result = CallResult()
    started = time.monotonic()
    response = None
    chunks = []
    try:
        if req.has_header("User-agent") or req.has_header("User-Agent"):
            pass
        else:
            req.add_header("User-Agent", BROWSER_UA)
        if not req.has_header("Accept"):
            req.add_header("Accept", "application/json")
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoPostRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        shared_marker = os.environ.get("ZIMAGE_SUBMISSION_MARKER", "").strip()
        if marker:
            result.marker_job = "requested"
            try:
                submission_marker(marker)
                result.marker_job = "created"
            except FileExistsError:
                result.marker_job = "existing"
                result.phase = "submission_guard"
                result.classification = "submission_already_marked"
                result.acceptance = "unknown"
                return result
            except OSError:
                result.marker_job = "error"
                result.phase = "submission_guard"
                result.classification = "local_error"
                return result
        if shared_marker:
            result.marker_shared = "requested"
            try:
                submission_marker(shared_marker, payload={
                    "state": "sending", "pid": os.getpid(),
                    "job_marker": str(marker) if marker else None,
                    "created_at": time.time(),
                })
                result.marker_shared = "created"
            except FileExistsError:
                result.marker_shared = "existing"
                result.phase = "submission_guard"
                result.classification = "shared_submission_already_marked"
                result.acceptance = "unknown"
                return result
            except OSError:
                result.marker_shared = "error"
                result.phase = "submission_guard"
                result.classification = "local_error"
                return result
        result.phase = "submit"
        write_report(report_path, result)
        result.acceptance = "unknown"
        result.submit_attempts = 1
        try:
            response = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        result.http_status = int(response.code)
        result.phase = "read_response"
        headers = response.headers
        ctype = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        # Never copy arbitrary relay headers into public diagnostics.
        result.content_type = ctype if ctype in (
            "application/json", "text/json", "text/plain", "text/html", "application/octet-stream") else "other"
        length = headers.get("Content-Length")
        if length is not None:
            result.content_length = int(length)
            if result.content_length < 0:
                raise ValueError("invalid length")
        while True:
            try:
                chunk = response.read(65536)
            except http.client.IncompleteRead as exc:
                result.received_bytes += len(exc.partial)
                raise
            if not chunk:
                break
            chunks.append(chunk)
            result.received_bytes += len(chunk)
        if result.content_length is not None and result.received_bytes != result.content_length:
            raise http.client.IncompleteRead(b"", result.content_length - result.received_bytes)
        raw = b"".join(chunks)
        encoding = headers.get("Content-Encoding", "identity").lower().strip()
        result.phase = "decode_response"
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
        elif encoding not in ("", "identity"):
            raise ValueError("unsupported encoding")
        result.text = raw.decode("utf-8")
        result.phase = "response"
        if result.http_status == 200:
            result.classification = "response_complete"
        else:
            result.classification = http_failure(result.http_status, result.text)
    except Exception as exc:
        if result.acceptance == "not_sent":
            result.classification = "local_error"
        elif isinstance(exc, (TimeoutError,)) or isinstance(getattr(exc, "reason", None), TimeoutError):
            result.classification = "timeout"
        elif result.phase == "read_response":
            result.classification = "response_interrupted"
        elif result.phase == "decode_response":
            result.classification = "invalid_response_encoding"
        else:
            result.classification = "transport_error"
    finally:
        if response is not None:
            response.close()
        result.elapsed_s = round(time.monotonic() - started, 3)
        write_report(report_path, result)
    return result


def http_failure(status, text):
    # Only classify known codes; never publish server messages or exception text.
    if "moderation" in text.lower() or "content_policy" in text.lower():
        return "moderation_blocked"
    if status == 401:
        return "auth_error"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "request_rejected"
    if 500 <= status < 600:
        return "server_error"
    return "http_error"


class ResultError(Exception):
    pass


def result_item(text):
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        raise ResultError("invalid_json") from None
    if not isinstance(obj, dict):
        raise ResultError("invalid_json_shape")
    if obj.get("error"):
        classification = "moderation_blocked" if "moderation" in json.dumps(obj["error"]).lower() else "response_error"
        raise ResultError(classification)
    data = obj.get("data")
    if not isinstance(data, list) or not data:
        raise ResultError("no_data")
    item = data[0]
    if not isinstance(item, dict):
        raise ResultError("no_image")
    if isinstance(item.get("b64_json"), str) and item["b64_json"]:
        return item
    if usable_url(item.get("url")):
        return item
    raise ResultError("no_image")


def usable_url(url):
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username and not parts.password
    except ValueError:
        return False


def publish_image(temp, out):
    temp, out = Path(temp), Path(out)
    try:
        with Image.open(temp) as im:
            im.verify()
        with Image.open(temp) as im:
            im.load()
    except Exception:
        raise ResultError("invalid_image") from None
    if out.exists():
        raise ResultError("output_exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    # No-clobber publication, including a destination created after the check.
    os.link(temp, out)
    temp.unlink()


def save_b64(value, out):
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ResultError("invalid_b64") from None
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        publish_image(tmp, out)
    finally:
        Path(tmp).unlink(missing_ok=True)
