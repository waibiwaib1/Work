"""Local Codex proxy. Retries 403/429/502/503 before any response body is sent.

Wait uses truncated binary exponential backoff. Containers call
http://host.docker.internal:8787/v1. This process forwards to the real gateway
and keeps one request in flight.
"""

from __future__ import annotations

import http.client
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

UPSTREAM = "https://seed-code.bytedance.com"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8787
PROXY_BASE = f"http://host.docker.internal:{LISTEN_PORT}/v1"
RETRY_STATUSES = {403, 429, 502, 503}
RETRY_BUDGET_SECONDS = 6 * 60
RETRY_LIMIT_SECONDS = 30 * 60
SLOT_SECONDS = 1.0
BACKOFF_EXPONENT_CAP = 6
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

_slot = threading.BoundedSemaphore(2)
_backoff_lock = threading.Lock()
_backoff_attempt = 0
_last_penalty = 0.0
QUIET_SECONDS = 60.0
_sides_lock = threading.Lock()
_sides: dict[tuple[str, str], dict] = {}
_retry_counts: dict[tuple[str, str], int] = {}
_retry_since: dict[tuple[str, str], float] = {}
_activity_path: Path | None = None


def proxy_base(instance_id: str, side: str) -> str:
    return f"http://host.docker.internal:{LISTEN_PORT}/s/{quote(instance_id, safe='')}/{side}/v1"


def split_route(path: str) -> tuple[str | None, str | None, str]:
    raw, sep, query = path.partition("?")
    parts = [unquote(part) for part in raw.split("/")]
    if len(parts) >= 5 and parts[1] == "s" and parts[3] in ("a", "b") and parts[4] == "v1":
        rest = "/" + "/".join(raw.split("/")[4:])
        if sep:
            rest += "?" + query
        return parts[2], parts[3], rest
    return None, None, path


def set_activity_path(path: Path) -> None:
    global _activity_path
    _activity_path = path


def clear_activity() -> None:
    with _sides_lock:
        _sides.clear()
        _retry_counts.clear()
        _retry_since.clear()
        _write_activity_locked()


def retry_limited(instance_id: str | None, side: str | None) -> bool:
    """True once this side has been retrying for RETRY_LIMIT_SECONDS.

    The clock starts at the first retry and keeps running across Codex reconnects.
    A non-retry response clears it.
    """
    if not instance_id or side not in ("a", "b"):
        return False
    now = time.monotonic()
    with _sides_lock:
        key = (instance_id, side)
        started = _retry_since.get(key)
        if started is None:
            _retry_since[key] = now
            return False
        return now - started >= RETRY_LIMIT_SECONDS


def clear_retry_streak(instance_id: str | None, side: str | None) -> None:
    if not instance_id or side not in ("a", "b"):
        return
    with _sides_lock:
        _retry_since.pop((instance_id, side), None)


def note_side(instance_id: str | None, side: str | None, phase: str | None, **extra: object) -> None:
    if not instance_id or side not in ("a", "b"):
        return
    with _sides_lock:
        key = (instance_id, side)
        if phase is None:
            _sides.pop(key, None)
        else:
            if phase == "retrying":
                _retry_counts[key] = _retry_counts.get(key, 0) + 1
            _sides[key] = {"phase": phase, "count": _retry_counts.get(key, 0), **extra}
        _write_activity_locked()


def _write_activity_locked() -> None:
    if _activity_path is None:
        return
    payload: dict[str, dict] = {}
    for (instance_id, side), info in _sides.items():
        payload.setdefault(instance_id, {})[side] = info
    _activity_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _activity_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(_activity_path)


def side_label(side: str | None) -> str:
    return side if side in ("a", "b") else "-"


def truncated_binary_backoff(attempt: int) -> float:
    """Wait in 2^(k-1) .. 2^k-1 slots, with k capped. Never zero."""
    exponent = min(max(attempt, 1), BACKOFF_EXPONENT_CAP)
    slots = random.randrange(2 ** (exponent - 1), 2**exponent)
    return slots * SLOT_SECONDS


def next_backoff() -> tuple[int, float]:
    global _backoff_attempt, _last_penalty
    with _backoff_lock:
        _backoff_attempt = min(_backoff_attempt + 1, BACKOFF_EXPONENT_CAP)
        _last_penalty = time.monotonic()
        attempt = _backoff_attempt
    return attempt, truncated_binary_backoff(attempt)


def ease_backoff() -> None:
    global _backoff_attempt, _last_penalty
    now = time.monotonic()
    with _backoff_lock:
        if _backoff_attempt == 0 or now - _last_penalty < QUIET_SECONDS:
            return
        _backoff_attempt -= 1
        _last_penalty = now


class ReuseServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_relay(
    upstream: str = UPSTREAM,
    host: str = LISTEN_HOST,
    port: int = LISTEN_PORT,
    slots: int = 2,
) -> ThreadingHTTPServer:
    global _slot, _backoff_attempt, _last_penalty
    _slot = threading.BoundedSemaphore(max(slots, 1))
    with _backoff_lock:
        _backoff_attempt = 0
        _last_penalty = 0.0
    clear_activity()
    handler = make_handler(upstream)
    server = ReuseServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="gsb-relay", daemon=True)
    thread.start()
    bound = server.server_address[1]
    print(f"relay http://{host}:{bound}/v1 -> {upstream}/v1 slots={max(slots, 1)}", flush=True)
    return server


def make_handler(upstream: str) -> type[BaseHTTPRequestHandler]:
    parsed = urlparse(upstream)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self._proxy()

        def do_POST(self) -> None:  # noqa: N802
            self._proxy()

        def do_PUT(self) -> None:  # noqa: N802
            self._proxy()

        def do_DELETE(self) -> None:  # noqa: N802
            self._proxy()

        def do_PATCH(self) -> None:  # noqa: N802
            self._proxy()

        def _proxy(self) -> None:
            if self.path.split("?", 1)[0] == "/healthz":
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            instance, side, upstream_path = split_route(self.path)
            self._instance = instance
            self._side = side
            self._upstream_path = upstream_path
            try:
                note_side(instance, side, "running")
                with _slot:
                    self._forward(body)
            finally:
                note_side(instance, side, None)

        def _forward(self, body: bytes) -> None:
            deadline = time.monotonic() + RETRY_BUDGET_SECONDS
            retries = 0
            while True:
                try:
                    conn, response = open_upstream(
                        parsed, self.command, self._upstream_path, self.headers.items(), body
                    )
                except (OSError, http.client.HTTPException):
                    if time.monotonic() >= deadline:
                        self.send_error(502, "upstream unavailable")
                        return
                    if retry_limited(self._instance, self._side):
                        print(
                            f"relay retry limit 502 {side_label(self._side)} after {RETRY_LIMIT_SECONDS // 60}m",
                            flush=True,
                        )
                        self.send_error(502, "upstream unavailable")
                        return
                    retries += 1
                    attempt, wait = next_backoff()
                    print(
                        f"relay upstream error {side_label(self._side)} retry in {wait:.0f}s attempt {attempt}",
                        flush=True,
                    )
                    note_side(self._instance, self._side, "retrying", wait=int(wait), attempt=attempt)
                    time.sleep(wait)
                    note_side(self._instance, self._side, "running")
                    continue
                try:
                    retryable = response.status in RETRY_STATUSES and time.monotonic() < deadline
                    if retryable and retry_limited(self._instance, self._side):
                        print(
                            f"relay retry limit {response.status} {side_label(self._side)} after {RETRY_LIMIT_SECONDS // 60}m",
                            flush=True,
                        )
                        self._send_upstream(response)
                        return
                    if retryable:
                        retries += 1
                        attempt, wait = next_backoff()
                        print(
                            f"relay {response.status} {side_label(self._side)} {self._upstream_path.split('?', 1)[0]} retry in {wait:.0f}s attempt {attempt}",
                            flush=True,
                        )
                        response.read()
                        note_side(
                            self._instance,
                            self._side,
                            "retrying",
                            wait=int(wait),
                            attempt=attempt,
                            status=response.status,
                        )
                        time.sleep(wait)
                        note_side(self._instance, self._side, "running")
                        continue
                    if response.status not in RETRY_STATUSES:
                        clear_retry_streak(self._instance, self._side)
                    if retries == 0:
                        ease_backoff()
                    self._send_upstream(response)
                    return
                finally:
                    conn.close()

        def _send_upstream(self, response: http.client.HTTPResponse) -> None:
            self.send_response(response.status, response.reason)
            has_length = False
            for key, value in response.getheaders():
                if key.lower() in HOP_HEADERS:
                    continue
                if key.lower() == "content-length":
                    has_length = True
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.close_connection = True
            if not has_length:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if self.command == "HEAD":
                return
            if has_length:
                remaining = int(response.getheader("Content-Length") or "0")
                while remaining:
                    chunk = response.read(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    self.wfile.write(chunk)
                return
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    return Handler


def open_upstream(
    parsed,
    method: str,
    path: str,
    headers,
    body: bytes,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(parsed.hostname, port, timeout=30)
    else:
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=30)
    forwarded = {"Host": parsed.hostname or ""}
    for key, value in headers:
        if key.lower() in HOP_HEADERS:
            continue
        forwarded[key] = value
    conn.request(method, path, body=body or None, headers=forwarded)
    if conn.sock is not None:
        conn.sock.settimeout(900)
    return conn, conn.getresponse()
