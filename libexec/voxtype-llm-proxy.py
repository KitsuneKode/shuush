#!/usr/bin/env python3
"""voxtype-llm-proxy: hold one warm HTTPS connection to Groq for cleanup calls.

Every cleanup used to be a fresh `curl`, which pays DNS + TCP + TLS on each
dictation. On a slow network that measured 0.4-2.1s for TLS alone (2026-10-10),
which is more than the model call itself. This proxy keeps a persistent
connection, and voxtype-live-osd hits /warm when recording starts, so the
handshake happens while the user is still talking.

    POST http://127.0.0.1:11435/openai/v1/chat/completions   forwarded as-is
    GET  http://127.0.0.1:11435/warm                         open/refresh the connection

A request with "stream": true is relayed as it arrives (server-sent events),
so the cleanup can start typing on the first sentence; anything else is
buffered and answered with a Content-Length, as before.

The API key is read from ~/.config/voxtype/secrets.env and added here, so it
never appears in a process command line. Every GROQ_API_KEY* entry is used:
on a 429 (free tier: ~8K tokens/min per account) the request is retried with
the next key over the same warm connection, and that key stays current until
it is limited too. Keys from separate accounts have separate quotas (verified
2026-10-12); two keys from one account would share one.
"""
import http.client
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "api.groq.com"
LISTEN = ("127.0.0.1", 11435)
IDLE_REFRESH_S = 45      # reconnect proactively if idle longer than this on /warm


def api_keys() -> list[str]:
    conf = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxtype"
    secrets = conf / "secrets.env"
    if not secrets.is_file():
        return []
    try:
        text = secrets.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return re.findall(r'^GROQ_API_KEY\d*\s*=\s*"?([^"\n]+)"?', text, re.M)


class Upstream:
    """One keep-alive HTTPS connection, serialized by a lock (dictation is serial)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conn: http.client.HTTPSConnection | None = None
        self.last_used = 0.0
        self.key_index = 0       # sticky: stay on the key that last worked

    def _connect(self) -> None:
        if self.conn:
            self.conn.close()
        self.conn = http.client.HTTPSConnection(HOST, timeout=10)
        self.conn.connect()

    def request(self, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        keys = api_keys()
        if not keys:
            return 503, b'{"error":"no_api_key"}'
        with self.lock:
            for _ in keys:
                status, data = self._send(method, path, body, keys[self.key_index % len(keys)])
                if status != 429:
                    return status, data
                self.key_index = (self.key_index + 1) % len(keys)
            return status, data

    def _open(self, method: str, path: str, body: bytes | None, key: str) -> http.client.HTTPResponse:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "User-Agent": "voxtype-llm-proxy/1.0", "Connection": "keep-alive"}
        for attempt in (1, 2):   # one retry: the server may have closed an idle connection
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request(method, path, body=body, headers=headers)
                return self.conn.getresponse()
            except (http.client.HTTPException, OSError):
                self.conn = None
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def _send(self, method: str, path: str, body: bytes | None, key: str) -> tuple[int, bytes]:
        try:
            resp = self._open(method, path, body, key)
            data = resp.read()
        except (http.client.HTTPException, OSError):
            self.conn = None
            raise
        self.last_used = time.monotonic()
        return resp.status, data

    def stream(self, path: str, body: bytes, start, write) -> None:
        """Relay a streamed completion: start(status, content_type) once,
        then write(bytes) per piece as Groq sends it."""
        keys = api_keys()
        if not keys:
            start(503, "application/json")
            write(b'{"error":"no_api_key"}')
            return
        with self.lock:
            for n in range(len(keys)):
                resp = self._open("POST", path, body, keys[self.key_index % len(keys)])
                if resp.status == 429 and n + 1 < len(keys):
                    resp.read()
                    self.key_index = (self.key_index + 1) % len(keys)
                    continue
                start(resp.status, resp.getheader("Content-Type", "text/event-stream"))
                try:
                    while True:
                        piece = resp.read1(65536)
                        if not piece:
                            break
                        write(piece)
                except (http.client.HTTPException, OSError):
                    # Caller went away (cancel, timeout) or upstream broke
                    # mid-body: the connection is half-read, never reuse it.
                    self.conn = None
                    raise
                self.last_used = time.monotonic()
                return

    def warm(self) -> None:
        idle = time.monotonic() - self.last_used
        if self.conn is None or idle > IDLE_REFRESH_S:
            self.request("GET", "/openai/v1/models", None)


UP = Upstream()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):   # quiet; failures are visible in the caller's timings
        pass

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/warm":
            return self._reply(404, b"{}")
        try:
            UP.warm()
            self._reply(200, b'{"warm":true}')
        except Exception as e:  # noqa: BLE001 - report, never crash the server
            self._reply(502, f'{{"error":"{type(e).__name__}"}}'.encode())

    def do_POST(self) -> None:
        if not self.path.startswith("/openai/v1/"):
            return self._reply(404, b"{}")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            streaming = json.loads(body or b"{}").get("stream") is True
        except ValueError:
            streaming = False
        if streaming:
            return self._relay(body)
        try:
            status, data = UP.request("POST", self.path, body)
            self._reply(status, data)
        except Exception as e:  # noqa: BLE001
            self._reply(502, f'{{"error":"{type(e).__name__}"}}'.encode())

    def _relay(self, body: bytes) -> None:
        started = False

        def start(status: int, content_type: str) -> None:
            nonlocal started
            started = True
            # HTTP/1.0 response without a length: the body ends when this
            # connection closes, so each piece can be written as it comes.
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def write(piece: bytes) -> None:
            self.wfile.write(piece)
            self.wfile.flush()

        try:
            UP.stream(self.path, body, start, write)
        except Exception as e:  # noqa: BLE001
            if not started:
                self._reply(502, f'{{"error":"{type(e).__name__}"}}'.encode())


if __name__ == "__main__":
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
