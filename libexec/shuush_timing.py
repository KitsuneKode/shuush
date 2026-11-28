#!/usr/bin/env python3
"""Per-take monotonic timing recorder and summarizer for Shuush.

Records monotonic timestamps (CLOCK_MONOTONIC, milliseconds) into a take's
directory ($XDG_RUNTIME_DIR/voxtype/takes/<take_id>/timing.jsonl) and
summarizes stage timestamps and durations into timing.json and history.jsonl.

Events tracked per take:
  - key_release          push-to-talk release or stop hotkey press (toggle script)
  - daemon_stop          command sent to voxtype record stop/toggle (after release tail)
  - transcript_ready     final transcript arrived at post-process stdin
  - cleanup_first_byte   first byte produced by cleanup (TTFB for streaming)
  - cleanup_last_byte    last byte produced by cleanup
  - delivery_start       delivery handed to the output driver (or daemon stdout)
  - delivery_done        delivery completed (driver finished or receipt received)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

EVENTS = (
    "key_release",
    "daemon_stop",
    "transcript_ready",
    "cleanup_first_byte",
    "cleanup_last_byte",
    "delivery_start",
    "delivery_done",
)

TERMINAL_CLASSES = {
    "ghostty", "kitty", "alacritty", "foot", "wezterm", "konsole", "xterm", "neovide",
    "st", "urxvt", "terminator", "tilix", "rio", "contour", "warp",
}
EDITOR_PREFIXES = (
    "code", "cursor", "zed", "dev.zed", "com.t3tools.t3code", "devin-desktop",
    "jetbrains-", "obsidian", "sublime", "emacs", "helix", "nvim", "windsurf",
)
BROWSER_PREFIXES = (
    "zen", "firefox", "librewolf", "chromium", "google-chrome", "brave",
    "vivaldi", "microsoft-edge", "thorium", "epiphany", "qutebrowser", "floorp",
)
CHAT_PREFIXES = (
    "zapzap", "slack", "discord", "vesktop", "webcord", "telegram", "whatsapp",
    "signal", "beeper", "ferdium", "franz", "element", "cinny", "armcord",
)

LENGTH_BANDS = (
    ("short (<80c)", 0, 80),
    ("medium (80-299c)", 80, 300),
    ("long (>=300c)", 300, None),
)


def mono_ms() -> int:
    """Monotonic milliseconds from CLOCK_MONOTONIC, shared across processes."""
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1_000_000


def classify_app_family(app_class: str = "", mode: str = "") -> str:
    """Normalize a window class into a coarse app family for latency breakdowns."""
    cls = (app_class or "").strip().lower()
    if cls:
        if cls in TERMINAL_CLASSES or "terminal" in cls:
            return "terminal"
        if any(cls == p or cls.startswith(p) for p in EDITOR_PREFIXES):
            return "editor"
        if any(cls == p or cls.startswith(p) for p in BROWSER_PREFIXES):
            return "browser"
        if any(cls == p or cls.startswith(p) for p in CHAT_PREFIXES):
            return "chat"
        return "other"
    return "unknown"


def length_band(chars: int) -> str:
    n = max(0, int(chars))
    for label, lo, hi in LENGTH_BANDS:
        if n >= lo and (hi is None or n < hi):
            return label
    return LENGTH_BANDS[-1][0]


def _write_line(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def record_events(
    take_dir: Path | str,
    events: list[str],
    *,
    ts_ms: int | None = None,
    meta: dict | None = None,
    take_id: str | None = None,
) -> int:
    """Append one or more monotonic timestamp events to <take_dir>/timing.jsonl."""
    d = Path(take_dir)
    if not d.is_dir():
        return ts_ms if ts_ms is not None else mono_ms()
    t = mono_ms() if ts_ms is None else int(ts_ms)
    tid = take_id or (d.name if re.fullmatch(r"[0-9]+", d.name) else "")
    jpath = d / "timing.jsonl"
    for ev in events:
        rec: dict = {"take_id": tid, "event": ev, "mono_ms": t}
        if meta:
            rec["meta"] = meta
        _write_line(jpath, rec)
    if not events and meta:
        _write_line(jpath, {"take_id": tid, "event": "meta", "mono_ms": t, "meta": meta})
    return t


def read_events(take_dir: Path | str) -> tuple[dict[str, int], dict]:
    """Read timing.jsonl from take_dir into (timestamps_ms, merged_meta)."""
    d = Path(take_dir)
    jpath = d / "timing.jsonl"
    ts: dict[str, int] = {}
    meta: dict = {}
    if not jpath.is_file():
        return ts, meta
    try:
        lines = jpath.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ts, meta
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ev = rec.get("event")
        val = rec.get("mono_ms")
        if isinstance(ev, str) and ev in EVENTS and isinstance(val, (int, float)):
            # Keep the first timestamp for start-like events and the latest for end-like events.
            if ev in ("key_release", "transcript_ready", "cleanup_first_byte", "delivery_start"):
                ts.setdefault(ev, int(val))
            else:
                ts[ev] = int(val)
        m = rec.get("meta")
        if isinstance(m, dict):
            meta.update(m)
    return ts, meta


def compute_durations(ts: dict[str, int]) -> dict[str, int]:
    """Derive non-negative stage durations (ms) from monotonic timestamps."""
    pairs = (
        ("release_tail_ms", "key_release", "daemon_stop"),
        ("asr_ms", "daemon_stop", "transcript_ready"),
        ("cleanup_ttfb_ms", "transcript_ready", "cleanup_first_byte"),
        ("cleanup_ms", "transcript_ready", "cleanup_last_byte"),
        ("cleanup_stream_ms", "cleanup_first_byte", "cleanup_last_byte"),
        ("queue_wait_ms", "cleanup_last_byte", "delivery_start"),
        ("delivery_ms", "delivery_start", "delivery_done"),
        ("stop_to_first_ms", "daemon_stop", "cleanup_first_byte"),
        ("stop_to_done_ms", "daemon_stop", "delivery_done"),
        ("release_to_done_ms", "key_release", "delivery_done"),
        ("transcript_to_done_ms", "transcript_ready", "delivery_done"),
    )
    out: dict[str, int] = {}
    for name, a, b in pairs:
        if a in ts and b in ts:
            delta = ts[b] - ts[a]
            if 0 <= delta <= 3_600_000:
                out[name] = int(delta)
    return out


def summarize(
    take_dir: Path | str | None,
    *,
    take_id: str = "",
    delivery_mode: str = "",
    delivery_method: str = "",
    app_class: str = "",
    mode: str = "",
    text_chars: int | None = None,
    extra_ts: dict[str, int] | None = None,
    write_file: bool = True,
) -> dict:
    """Build (and optionally persist) timing.json for a take."""
    ts: dict[str, int] = {}
    meta: dict = {}
    d = Path(take_dir) if take_dir else None
    if d and d.is_dir():
        ts, meta = read_events(d)
        if not take_id and re.fullmatch(r"[0-9]+", d.name):
            take_id = d.name
    if extra_ts:
        for k, v in extra_ts.items():
            if k in EVENTS and isinstance(v, (int, float)):
                ts[k] = int(v)
    d_mode = delivery_mode or str(meta.get("delivery_mode") or "")
    d_method = delivery_method or str(meta.get("delivery_method") or "")
    a_class = app_class or str(meta.get("app_class") or "")
    a_family = classify_app_family(a_class, mode=mode)
    durations = compute_durations(ts)
    doc: dict = {
        "take_id": take_id,
        "timestamps_ms": ts,
        "durations_ms": durations,
    }
    if d_mode:
        doc["delivery_mode"] = d_mode
    if d_method:
        doc["delivery_method"] = d_method
    if a_class:
        doc["app_class"] = a_class
    doc["app_family"] = a_family
    if text_chars is not None:
        doc["text_chars"] = int(text_chars)
        doc["length_band"] = length_band(int(text_chars))
    if write_file and d and d.is_dir():
        out_path = d / "timing.json"
        tmp = d / f"timing.json.tmp.{os.getpid()}"
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, out_path)
        except OSError:
            pass
    return doc


def compact_for_history(summary: dict) -> dict:
    """Compact timing object suitable for embedding in history.jsonl rows."""
    out: dict = {}
    for k, v in (summary.get("timestamps_ms") or {}).items():
        out[f"{k}_ms"] = v
    for k, v in (summary.get("durations_ms") or {}).items():
        out[k] = v
    return out


def _parse_kv(items: list[str]) -> dict:
    out: dict = {}
    for item in items:
        if "=" in item:
            k, v = item.split("=", 1)
            out[k] = v
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="shuush-timing")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("now", help="print monotonic ms")

    p_mark = sub.add_parser("mark", help="append monotonic event(s) to take_dir/timing.jsonl")
    p_mark.add_argument("take_dir")
    p_mark.add_argument("events", nargs="*")
    p_mark.add_argument("--ts", type=int, default=None)
    p_mark.add_argument("--take-id", default="")
    p_mark.add_argument("--kv", action="append", default=[])

    p_sum = sub.add_parser("summarize", help="summarize take_dir/timing.jsonl to timing.json")
    p_sum.add_argument("take_dir")
    p_sum.add_argument("--take-id", default="")
    p_sum.add_argument("--delivery-mode", default="")
    p_sum.add_argument("--delivery-method", default="")
    p_sum.add_argument("--app-class", default="")
    p_sum.add_argument("--mode", default="")
    p_sum.add_argument("--chars", type=int, default=None)
    p_sum.add_argument("--no-write", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "now":
        print(mono_ms())
        return 0
    if a.cmd == "mark":
        valid = [ev for ev in a.events if ev in EVENTS]
        meta = _parse_kv(a.kv)
        t = record_events(a.take_dir, valid, ts_ms=a.ts, meta=meta or None, take_id=a.take_id or None)
        print(t)
        return 0
    if a.cmd == "summarize":
        doc = summarize(
            a.take_dir,
            take_id=a.take_id,
            delivery_mode=a.delivery_mode,
            delivery_method=a.delivery_method,
            app_class=a.app_class,
            mode=a.mode,
            text_chars=a.chars,
            write_file=not a.no_write,
        )
        print(json.dumps(doc, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
