#!/usr/bin/env python3
"""The harness's stand-ins for everything that would otherwise leave the machine.

One loopback HTTP server, three jobs:

  speech-to-text   POST /v1/audio/transcriptions   (voxtype's `[whisper] mode = "remote"`)
                   answers with the next scripted transcript and records what audio
                   it was sent (length, loudness), so a take that recorded silence
                   shows up as a failed assertion instead of a passing one.
  cleanup model    POST /v1/chat/completions       (streamed, what `PP_ORDER=local` calls)
                   POST /api/chat                  (Ollama's own, the non-streaming path)
                   `echo` mode cleans the transcript with a fixed, documented rule
                   (clean_text below); other modes fail the way a real provider does.
  target log       POST /_target                   events from the browser page
                   GET  /_page/cmd                 commands the page polls for

and a control surface the scenarios drive with curl:

  POST /_ctl     {"stt": {...}, "llm": {...}}      set modes, queue transcripts
  GET  /_state                                     everything recorded so far
  POST /_reset                                     forget the recordings, keep nothing
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

LOCK = threading.Lock()
PAGES_DIR = [""]
STATE: dict = {}


def reset() -> None:
    STATE.clear()
    STATE.update({
        "stt": {"queue": [], "default": "this is the default transcript from the fake speech to text",
                "status": 200, "delay_ms": 0, "sticky_fail": False},
        "llm": {"mode": "echo", "delay_ms": 0, "chunk_delay_ms": 0, "reply": None},
        "stt_log": [], "llm_log": [], "target": [], "page_cmds": [], "other": [],
    })


reset()

FILLERS = re.compile(r"\b(?:um+|uh+|er+|erm)\b[,.]?\s*", re.I)


def clean_text(raw: str) -> str:
    """What the fake cleanup model does to a transcript. Scenarios compute their
    expectation with the same rule (tests/e2e/lib/expect.sh mirrors it): drop
    filler sounds, trim, capitalise the first letter, end an unfinished
    sentence with a full stop. It never rewrites a word."""
    out = []
    for line in raw.split("\n"):
        t = FILLERS.sub("", line).strip()
        t = re.sub(r"\s{2,}", " ", t)
        if t:
            t = t[0].upper() + t[1:]
            if t[-1] not in ".?!:;)":
                t += "."
        out.append(t)
    return "\n".join(out)


def audio_stats(blob: bytes) -> dict:
    i = blob.find(b"RIFF")
    if i < 0:
        return {"wav": False, "bytes": len(blob)}
    try:
        with wave.open(BytesIO(blob[i:]), "rb") as w:
            n, rate, ch, width = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(n)
    except (wave.Error, EOFError):
        # A streamed WAV can carry a placeholder length; read what is there.
        hdr = blob[i:i + 44]
        if len(hdr) < 44:
            return {"wav": False, "bytes": len(blob)}
        ch = struct.unpack("<H", hdr[22:24])[0]
        rate = struct.unpack("<I", hdr[24:28])[0]
        width = struct.unpack("<H", hdr[34:36])[0] // 8
        frames = blob[i + 44:]
        n = len(frames) // max(1, width * ch)
    peak = rms = 0.0
    if width == 2 and frames:
        vals = struct.unpack("<%dh" % (len(frames) // 2), frames[: len(frames) // 2 * 2])
        peak = max(abs(v) for v in vals) / 32768.0
        rms = (sum(v * v for v in vals) / len(vals)) ** 0.5 / 32768.0
    elif width == 4 and frames:
        vals = struct.unpack("<%df" % (len(frames) // 4), frames[: len(frames) // 4 * 4])
        peak = max(abs(v) for v in vals)
        rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
    return {"wav": True, "seconds": round(n / float(rate or 1), 3), "rate": rate, "channels": ch,
            "peak": round(peak, 4), "rms": round(rms, 4), "bytes": len(blob)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet; the state log is the record
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code: int, doc, ctype="application/json") -> None:
        data = doc if isinstance(doc, (bytes, bytearray)) else json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET -------------------------------------------------------------
    def do_GET(self):
        if self.path.startswith("/_state"):
            with LOCK:
                return self._send(200, STATE)
        if self.path.startswith("/_health"):
            return self._send(200, {"ok": True})
        if self.path.startswith("/_page/cmd"):
            with LOCK:
                cmd = STATE["page_cmds"].pop(0) if STATE["page_cmds"] else None
            return self._send(200, {"cmd": cmd})
        if self.path.startswith("/_page/"):
            return self._page(self.path[len("/_page/"):])
        with LOCK:
            STATE["other"].append({"t": time.time(), "method": "GET", "path": self.path})
        self._send(404, {"error": "not here"})

    def _page(self, name: str) -> None:
        import os
        root = PAGES_DIR[0]
        path = os.path.join(root, os.path.basename(name or "index.html"))
        try:
            data = open(path, "rb").read()
        except OSError:
            return self._send(404, {"error": "no page"})
        self._send(200, data, "text/html; charset=utf-8")

    # ---- POST ------------------------------------------------------------
    def do_POST(self):
        body = self._body()
        path = self.path.split("?")[0]
        if path == "/_ctl":
            doc = json.loads(body or b"{}")
            with LOCK:
                for k in ("stt", "llm"):
                    if k in doc:
                        d = dict(doc[k])
                        if k == "stt" and "queue" in d:       # answers queue up; they do not replace each other
                            STATE["stt"]["queue"].extend(d.pop("queue"))
                        STATE[k].update(d)
                if "page_cmd" in doc:
                    STATE["page_cmds"].append(doc["page_cmd"])
            return self._send(200, {"ok": True})
        if path == "/_reset":
            with LOCK:
                reset()
            return self._send(200, {"ok": True})
        if path == "/_target":
            try:
                ev = json.loads(body or b"{}")
            except ValueError:
                ev = {"raw": body.decode("utf-8", "replace")}
            ev["t"] = time.time()
            with LOCK:
                STATE["target"].append(ev)
            return self._send(200, {"ok": True})
        if path.endswith("/audio/transcriptions"):
            return self._stt(body)
        if path.endswith("/chat/completions"):
            return self._llm(body, stream_default=True)
        if path.endswith("/api/chat"):
            return self._llm(body, stream_default=False)
        with LOCK:
            STATE["other"].append({"t": time.time(), "method": "POST", "path": self.path})
        self._send(404, {"error": "not here"})

    def _stt(self, body: bytes) -> None:
        stats = audio_stats(body)
        with LOCK:
            cfg = dict(STATE["stt"])
            item = STATE["stt"]["queue"].pop(0) if STATE["stt"]["queue"] else cfg["default"]
            # A queued answer is a string, or {"text", "delay_ms", "status"} for this request only.
            if isinstance(item, dict):
                text = item.get("text", cfg["default"])
                cfg["delay_ms"] = item.get("delay_ms", cfg["delay_ms"])
                cfg["status"] = item.get("status", cfg["status"])
            else:
                text = item
            STATE["stt_log"].append({"t": time.time(), "audio": stats, "answered": text if cfg["status"] == 200 else None,
                                     "status": cfg["status"], "delay_ms": cfg["delay_ms"]})
        if cfg["delay_ms"]:
            time.sleep(cfg["delay_ms"] / 1000.0)
        if cfg["status"] != 200:
            return self._send(cfg["status"], {"error": "fake speech to text is down"})
        self._send(200, {"text": text})

    def _llm(self, body: bytes, stream_default: bool) -> None:
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            req = {}
        msgs = req.get("messages") or []
        user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                user = m.get("content") or ""
                break
        # The user message is <previous>…</previous> / <app> / <screen> blocks followed by
        # <dictation>…</dictation>: only the last block is the transcript.
        m = re.findall(r"<dictation>(.*?)</dictation>", user, re.S)
        raw = (m[-1] if m else re.sub(r"</?dictation>", "", user)).strip()
        with LOCK:
            cfg = dict(STATE["llm"])
            STATE["llm_log"].append({"t": time.time(), "path": self.path, "stream": bool(req.get("stream", stream_default)),
                                     "raw": raw[:4000], "mode": cfg["mode"]})
        if cfg["delay_ms"]:
            time.sleep(cfg["delay_ms"] / 1000.0)
        mode = cfg["mode"]
        if mode == "fail500":
            return self._send(500, {"error": "fake provider is down"})
        if mode == "reset":
            self.connection.close()
            self.close_connection = True
            return
        if mode == "hang":
            time.sleep(60)
            return self._send(504, {"error": "too late"})
        text = cfg["reply"] if cfg.get("reply") is not None else clean_text(raw)
        stream = bool(req.get("stream", stream_default)) and self.path.endswith("/chat/completions")
        if not stream:
            return self._send(200, {"message": {"role": "assistant", "content": text},
                                    "choices": [{"message": {"role": "assistant", "content": text}}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(b: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
            self.wfile.flush()

        words = re.findall(r"\S+\s*|\n", text)
        step = max(1, len(words) // 12)
        for i in range(0, len(words), step):
            piece = "".join(words[i:i + step])
            chunk(("data: " + json.dumps({"choices": [{"delta": {"content": piece}, "finish_reason": None}]}) + "\n\n").encode())
            if cfg["chunk_delay_ms"]:
                time.sleep(cfg["chunk_delay_ms"] / 1000.0)
        chunk(("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}) + "\n\n").encode())
        chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-file", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--pages", default="")
    a = ap.parse_args()
    PAGES_DIR[0] = a.pages
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    with open(a.port_file, "w") as f:
        f.write(str(srv.server_address[1]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "clean":
        sys.stdout.write(clean_text(sys.argv[2]))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        assert clean_text("um this is a test") == "This is a test."
        assert clean_text("hello there.\nuh second line") == "Hello there.\nSecond line."
        print("ok")
        sys.exit(0)
    sys.exit(main())
