"""Local Codex proxy. Retries 403/429/502/503 before any response body is sent.

Wait uses truncated binary exponential backoff. A recovered request wakes other
retry sleepers for staggered probes. Containers call the per-task routes under
http://host.docker.internal:8787.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

UPSTREAM = os.getenv("GSB_UPSTREAM") or "https://seed-code.bytedance.com"
LISTEN_HOST = os.getenv("GSB_RELAY_LISTEN_HOST") or "0.0.0.0"
LISTEN_PORT = int(os.getenv("GSB_RELAY_PORT") or "8787")
CONTAINER_HOST = os.getenv("GSB_RELAY_CONTAINER_HOST") or "host.docker.internal"
PROXY_BASE = f"http://{CONTAINER_HOST}:{LISTEN_PORT}/v1"
RETRY_STATUSES = {403, 429, 502, 503}
RETRY_LIMIT_SECONDS = 30 * 60
SLOT_SECONDS = 1.0
BACKOFF_EXPONENT_CAP = 6
RECOVERY_JITTER_SECONDS = 5.0
RECOVERY_SIGNAL_COOLDOWN_SECONDS = 5.0
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

QUIET_SECONDS = 60.0
_slot = threading.BoundedSemaphore(2)
_sides_lock = threading.Lock()
_sides: dict[tuple[str, str], dict] = {}
_retry_counts: dict[tuple[str, str], int] = {}
_retry_since: dict[tuple[str, str], float] = {}
_retry_since_wall: dict[tuple[str, str], float] = {}
_activity_path: Path | None = None


def proxy_base(instance_id: str, side: str) -> str:
    return f"http://{CONTAINER_HOST}:{LISTEN_PORT}/s/{quote(instance_id, safe='')}/{side}/v1"


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
        _retry_since_wall.clear()
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
            _retry_since_wall[key] = time.time()
            return False
        return now - started >= RETRY_LIMIT_SECONDS


def clear_retry_streak(instance_id: str | None, side: str | None) -> None:
    if not instance_id or side not in ("a", "b"):
        return
    with _sides_lock:
        key = (instance_id, side)
        _retry_since.pop(key, None)
        _retry_since_wall.pop(key, None)
        if key in _sides:
            _sides[key].pop("retry_since", None)
            _write_activity_locked()


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
            retry_since = _retry_since_wall.get(key)
            _sides[key] = {
                "phase": phase,
                "count": _retry_counts.get(key, 0),
                "retry_since": retry_since,
                **extra,
            }
        _write_activity_locked()


def _write_activity_locked() -> None:
    if _activity_path is None:
        return
    payload: dict = {"retrylevel": _backoff.level()}
    for (instance_id, side), info in _sides.items():
        payload.setdefault(instance_id, {})[side] = info
    _activity_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _activity_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(_activity_path)


def side_label(side: str | None) -> str:
    return side if side in ("a", "b") else "-"


def request_label(instance_id: str | None, side: str | None) -> str:
    return f"{instance_id or '-'}:{side_label(side)}"


def relay_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[{timestamp}] {message}", flush=True)


def rate_limit_headers(response: http.client.HTTPResponse) -> str:
    """Return safe rate-limit response headers for diagnostics."""
    values = []
    for key, value in response.getheaders():
        name = key.lower()
        if name != "retry-after" and "ratelimit" not in name and "rate-limit" not in name:
            continue
        clean = " ".join(value.split())[:200]
        values.append(f"{key}={clean}")
    return " ".join(values)


def truncated_binary_backoff(attempt: int) -> float:
    """Wait in 2^(k-1) .. 2^k-1 slots, with k capped. Never zero."""
    exponent = min(max(attempt, 1), BACKOFF_EXPONENT_CAP)
    slots = random.randrange(2 ** (exponent - 1), 2**exponent)
    return slots * SLOT_SECONDS


class BackoffController:
    """Global congestion state shared by all relay requests."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.reset()

    def reset(self) -> None:
        with self._condition:
            self._attempt = 0
            self._last_penalty = 0.0
            self._generation = 0
            self._last_reward = 0.0
            self._penalized_generation = 0

    def level(self) -> int:
        with self._condition:
            return self._attempt

    def generation(self) -> int:
        with self._condition:
            return self._generation

    def join_latest(
        self,
        known_generation: int,
        probe_generation: int | None,
    ) -> tuple[int, int | None]:
        """Join a recovery wave that started while a request was in flight."""
        current_generation = self.generation()
        if current_generation != known_generation:
            return current_generation, current_generation
        return known_generation, probe_generation

    def _reset_if_stable(self, now: float) -> None:
        if self._attempt and now - self._last_penalty >= QUIET_SECONDS:
            self._attempt = 0

    def failure(self, probe_generation: int | None = None) -> tuple[int, float]:
        """Record a failure; one recovery probe wave raises the level only once."""
        with self._condition:
            now = time.monotonic()
            self._reset_if_stable(now)
            should_raise = True
            if probe_generation and probe_generation == self._generation:
                should_raise = self._penalized_generation != probe_generation
                self._penalized_generation = probe_generation
            if should_raise:
                self._attempt = min(self._attempt + 1, BACKOFF_EXPONENT_CAP)
            self._last_penalty = now
            attempt = self._attempt
        return attempt, truncated_binary_backoff(attempt)

    def recovery(self) -> tuple[int, int, bool]:
        """Wake all sleepers; reward at most once per cooldown interval."""
        now = time.monotonic()
        with self._condition:
            self._reset_if_stable(now)
            rewarded = now - self._last_reward >= RECOVERY_SIGNAL_COOLDOWN_SECONDS
            if rewarded:
                self._attempt = max(self._attempt - 1, 0)
                self._last_reward = now
            self._generation += 1
            generation = self._generation
            level = self._attempt
            self._condition.notify(1)
        return generation, level, rewarded

    def healthy(self) -> None:
        """Apply the stable-period reset on ordinary successful traffic."""
        with self._condition:
            self._reset_if_stable(time.monotonic())

    def wait(
        self,
        wait: float,
        known_generation: int,
        deadline: float,
    ) -> tuple[int, int | None, float, float]:
        """Wait for backoff or recovery, without crossing the retry deadline."""
        started = time.monotonic()
        timeout = min(wait, max(0.0, deadline - started))
        with self._condition:
            if self._generation == known_generation and timeout > 0:
                self._condition.wait_for(
                    lambda: self._generation != known_generation,
                    timeout=timeout,
                )
            current_generation = self._generation
        if current_generation == known_generation:
            return current_generation, None, time.monotonic() - started, 0.0
        jitter = min(
            random.uniform(0.0, RECOVERY_JITTER_SECONDS),
            max(0.0, deadline - time.monotonic()),
        )
        time.sleep(jitter)
        return current_generation, current_generation, time.monotonic() - started, jitter


_backoff = BackoffController()


class ReuseServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_relay(
    upstream: str = UPSTREAM,
    host: str = LISTEN_HOST,
    port: int = LISTEN_PORT,
    slots: int = 2,
) -> ThreadingHTTPServer:
    global _slot
    _slot = threading.BoundedSemaphore(max(slots, 1))
    _backoff.reset()
    clear_activity()
    handler = make_handler(upstream)
    server = ReuseServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="gsb-relay", daemon=True)
    thread.start()
    bound = server.server_address[1]
    relay_log(f"relay http://{host}:{bound}/v1 -> {upstream}/v1 slots={max(slots, 1)}")
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
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                note_side(instance, side, None)

        def _forward(self, body: bytes) -> None:
            # Codex request retries are disabled, so the relay owns the full
            # retry window for this single request.
            deadline: float | None = None
            retries = 0
            known_generation = _backoff.generation()
            probe_generation = None
            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self.send_error(502, "upstream unavailable")
                    return
                request_timeout = 30.0 if deadline is None else min(30.0, deadline - now)
                try:
                    conn, response = open_upstream(
                        parsed,
                        self.command,
                        self._upstream_path,
                        self.headers.items(),
                        body,
                        request_timeout,
                    )
                except (OSError, http.client.HTTPException) as exc:
                    now = time.monotonic()
                    if deadline is None:
                        deadline = now + RETRY_LIMIT_SECONDS
                    known_generation, probe_generation = _backoff.join_latest(
                        known_generation,
                        probe_generation,
                    )
                    if now >= deadline:
                        self.send_error(502, "upstream unavailable")
                        return
                    if retry_limited(self._instance, self._side):
                        relay_log(
                            f"relay retry limit 502 {request_label(self._instance, self._side)}"
                            f" after {RETRY_LIMIT_SECONDS // 60}m"
                        )
                        self.send_error(502, "upstream unavailable")
                        return
                    retries += 1
                    attempt, wait = _backoff.failure(probe_generation)
                    detail = " ".join(str(exc).split())[:200]
                    relay_log(
                        f"relay upstream error {request_label(self._instance, self._side)}"
                        f" {type(exc).__name__}"
                        + (f": {detail}" if detail else "")
                        + f" retry in {wait:.0f}s attempt {attempt}"
                    )
                    known_generation, probe_generation = self._wait_to_retry(
                        wait,
                        attempt,
                        known_generation,
                        deadline,
                    )
                    if time.monotonic() >= deadline:
                        self.send_error(502, "upstream unavailable")
                        return
                    continue
                try:
                    known_generation, probe_generation = _backoff.join_latest(
                        known_generation,
                        probe_generation,
                    )
                    retryable = response.status in RETRY_STATUSES
                    if retryable and deadline is None:
                        deadline = time.monotonic() + RETRY_LIMIT_SECONDS
                    if retryable and (
                        time.monotonic() >= deadline
                        or retry_limited(self._instance, self._side)
                    ):
                        limits = rate_limit_headers(response)
                        relay_log(
                            f"relay retry limit {response.status} {request_label(self._instance, self._side)}"
                            f" after {RETRY_LIMIT_SECONDS // 60}m"
                            + (f" {limits}" if limits else ""),
                        )
                        self._send_upstream_status(response)
                        return
                    if retryable:
                        retries += 1
                        attempt, wait = _backoff.failure(probe_generation)
                        limits = rate_limit_headers(response)
                        relay_log(
                            f"relay {response.status} {request_label(self._instance, self._side)}"
                            f" {self._upstream_path.split('?', 1)[0]}"
                            f" retry in {wait:.0f}s attempt {attempt}"
                            + (f" {limits}" if limits else ""),
                        )
                        conn.close()
                        known_generation, probe_generation = self._wait_to_retry(
                            wait,
                            attempt,
                            known_generation,
                            deadline,
                            status=response.status,
                        )
                        if time.monotonic() >= deadline:
                            self._send_upstream_status(response)
                            return
                        continue
                    if response.status not in RETRY_STATUSES:
                        clear_retry_streak(self._instance, self._side)
                    if 200 <= response.status < 300:
                        if retries:
                            generation, level, rewarded = _backoff.recovery()
                            relay_log(
                                f"relay recovery wake one from {request_label(self._instance, self._side)}"
                                f" generation {generation} backoff {level} rewarded={rewarded}"
                            )
                        else:
                            _backoff.healthy()
                    self._send_upstream(response)
                    return
                finally:
                    conn.close()

        def _wait_to_retry(
            self,
            wait: float,
            attempt: int,
            known_generation: int,
            deadline: float,
            status: int | None = None,
        ) -> tuple[int, int | None]:
            extra = {"status": status} if status is not None else {}
            note_side(
                self._instance,
                self._side,
                "retrying",
                wait=int(min(wait, max(0.0, deadline - time.monotonic()))),
                attempt=attempt,
                **extra,
            )
            known_generation, probe_generation, total_wait, jitter = _backoff.wait(
                wait,
                known_generation,
                deadline,
            )
            if probe_generation is not None:
                relay_log(
                    f"relay recovery probe {request_label(self._instance, self._side)}"
                    f" total_wait={total_wait:.1f}s jitter={jitter:.1f}s"
                    f" generation {probe_generation}"
                )
            note_side(self._instance, self._side, "running")
            return known_generation, probe_generation

        def _send_upstream_status(self, response: http.client.HTTPResponse) -> None:
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in HOP_HEADERS or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()

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
            read_chunk = getattr(response, "read1", response.read)
            if has_length:
                remaining = int(response.getheader("Content-Length") or "0")
                while remaining:
                    chunk = read_chunk(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    self.wfile.write(chunk)
                return
            while True:
                chunk = read_chunk(8192)
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
    timeout: float = 30.0,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
        )
    else:
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
    forwarded = {"Host": parsed.hostname or ""}
    for key, value in headers:
        if key.lower() in HOP_HEADERS:
            continue
        forwarded[key] = value
    try:
        conn.request(method, path, body=body or None, headers=forwarded)
        response = conn.getresponse()
    except Exception:
        conn.close()
        raise
    if conn.sock is not None:
        conn.sock.settimeout(900)
    return conn, response
