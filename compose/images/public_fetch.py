"""Bounded unauthenticated downloads with per-hop public-IP pinning."""
from __future__ import annotations

import http.client
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit


class UnsafeDownload(ValueError):
    pass


def public_endpoints(url: str):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise UnsafeDownload("Only http/https downloads are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeDownload("URL credentials are not allowed")
        host = parsed.hostname
        if "%" in host:
            raise UnsafeDownload("Scoped addresses are not allowed")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        if isinstance(exc, UnsafeDownload):
            raise
        raise UnsafeDownload("Cannot resolve a public download address") from exc
    if not infos:
        raise UnsafeDownload("No download address was resolved")
    for family, kind, proto, _, sockaddr in infos:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise UnsafeDownload("Unrecognized download address") from exc
        mapped = getattr(address, "ipv4_mapped", None)
        if not address.is_global or address.is_multicast or (mapped and not mapped.is_global):
            raise UnsafeDownload("Private, loopback, link-local or reserved target blocked")
    return parsed, infos


def _connect_pinned(infos, timeout, source_address=None):
    last_error = None
    for family, kind, proto, _, sockaddr in infos[:8]:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError("No public endpoint is reachable")


def fetch_public_bytes(url: str, *, max_bytes=20 * 1024 * 1024,
                       timeout=30, max_redirects=4, content_types=("image/",)):
    for hop in range(max_redirects + 1):
        parsed, infos = public_endpoints(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        factory = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = factory(parsed.hostname, port, timeout=timeout)
        # Freeze the resolved endpoints for this connection; HTTPS still uses the original host for SNI/certificate checks.
        conn._create_connection = lambda _address, timeout=timeout, source_address=None: _connect_pinned(infos, timeout, source_address)
        try:
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            conn.request("GET", path, headers={"User-Agent": "image-edit/0.1", "Accept": "image/*,video/*;q=0.8"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or hop == max_redirects:
                    raise UnsafeDownload("Invalid or excessive redirects")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise UnsafeDownload(f"Download HTTP {response.status}")
            mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if not any(mime.startswith(t) for t in content_types):
                raise UnsafeDownload("Unexpected download content type")
            length = response.getheader("Content-Length")
            if length is not None and (int(length) < 0 or int(length) > max_bytes):
                raise UnsafeDownload("Download exceeds byte limit")
            data = response.read(max_bytes + 1)
            if not data or len(data) > max_bytes:
                raise UnsafeDownload("Empty or oversized download")
            return data, mime
        finally:
            conn.close()
    raise UnsafeDownload("Too many redirects")
