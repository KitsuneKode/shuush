#!/usr/bin/env python3
"""Sandbox tests for Step 0 (per-take timing + latency stats) and Step 2 (PP_DELIVERY=shuush).

Uses only isolated temp dirs, fake voxtype/hyprctl/wtype/wl-copy/wl-paste scripts,
and loopback HTTP servers. Never touches live config, systemd, audio, or desktop.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = ROOT / "libexec"
FIX = ROOT / "tests" / "fixtures"
FAKE_VOX = FIX / "fake-voxtype"
STOCK = str(FIX / "schema-stock.json")
FORK = str(FIX / "schema-fork.json")

sys.path.insert(0, str(LIBEXEC))
sys.path.insert(0, str(ROOT / "tests"))

from test_dictation_pass import _load_module, _sandbox_env, _write_exec  # noqa: E402
import shuush_timing  # noqa: E402


def make_env(tmp_path: Path, schema: str = STOCK, **extra: str) -> tuple[dict[str, str], dict[str, Path]]:
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    env = _sandbox_env(tmp_path, bins)
    fake_vt = bins / "voxtype-daemon"
    marker = "\n# Runtime protocol strings: VOXTYPE_TAKE_ID recovered.txt sensitive_field\n" if schema == FORK else "\n"
    _write_exec(fake_vt, FAKE_VOX.read_text(encoding="utf-8") + marker)
    env.update(
        SHUUSH_VOXTYPE_BIN=str(fake_vt),
        FAKE_SCHEMA=schema,
        SHUUSH_DELIVER_SOCK=str(tmp_path / "rt/shuush/deliver.sock"),
    )

    home = tmp_path / "home"
    win_file = home / "window"
    clip_file = home / "clipboard"
    clip_types_file = home / "clipboard_types"
    events_log = home / "driver_events.jsonl"
    vt_calls = home / "voxtype_calls.log"
    notif_log = home / "notifs.log"

    win_file.write_text(json.dumps({"address": "0x111", "pid": 42, "class": "kitty", "title": "zsh"}))
    clip_types_file.write_text("text/plain;charset=utf-8\n")

    for name in ("wpctl", "niri", "swaymsg", "qs", "kitten", "ydotool", "dotool"):
        _write_exec(bins / name, "#!/bin/sh\nexit 1\n")

    _write_exec(
        bins / "notify-send",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{notif_log}"\n',
    )
    _write_exec(
        bins / "hyprctl",
        f'#!/bin/sh\n[ "$1" = activewindow ] && cat "{win_file}"\n',
    )
    _write_exec(
        bins / "voxtype",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{vt_calls}"\n',
    )
    _write_exec(
        bins / "wtype",
        f"""#!{sys.executable}
import json, sys, time
with open({str(events_log)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"tool": "wtype", "args": sys.argv[1:], "t": time.monotonic()}}) + "\\n")
""",
    )
    _write_exec(
        bins / "wl-copy",
        f"""#!{sys.executable}
import json, pathlib, sys, time
args = sys.argv[1:]
clip = pathlib.Path({str(clip_file)!r})
types = pathlib.Path({str(clip_types_file)!r})
if "--clear" in args:
    clip.unlink(missing_ok=True)
    types.write_text("")
    val = ""
elif "--" in args:
    idx = args.index("--")
    val = " ".join(args[idx + 1:])
    clip.write_text(val, encoding="utf-8")
    types.write_text("text/plain;charset=utf-8\\n")
else:
    data = sys.stdin.buffer.read()
    clip.write_bytes(data)
    val = data.decode("utf-8", errors="replace")
    types.write_text("text/plain;charset=utf-8\\n")
with open({str(events_log)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"tool": "wl-copy", "args": args, "text": val, "t": time.monotonic()}}) + "\\n")
""",
    )
    _write_exec(
        bins / "wl-paste",
        f"""#!{sys.executable}
import pathlib, sys
args = sys.argv[1:]
clip = pathlib.Path({str(clip_file)!r})
types = pathlib.Path({str(clip_types_file)!r})
if "--primary" in args:
    sys.exit(1)
if "--list-types" in args:
    if not clip.exists() and not (types.exists() and types.read_text().strip()):
        sys.exit(1)
    sys.stdout.write(types.read_text())
    sys.exit(0)
if not clip.exists():
    sys.exit(1)
sys.stdout.buffer.write(clip.read_bytes())
""",
    )

    conf = tmp_path / "config/voxtype"
    (conf / "config.toml").write_text(
        'engine = "whisper"\n\n[output]\nmode = "type"\n\n[output.post_process]\n'
        f'command = "{LIBEXEC / "voxtype-postprocess.sh"}"\n'
        "timeout_ms = 20000\ntrim = false\nfallback_on_empty = false\n",
        encoding="utf-8",
    )
    (conf / "postprocess.env").write_text(
        "PP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\nPP_ORDER=\n",
        encoding="utf-8",
    )
    env.update(extra)
    paths = {
        "rt": tmp_path / "rt/voxtype",
        "conf": conf,
        "state": tmp_path / "state/voxtype",
        "win": win_file,
        "clip": clip_file,
        "clip_types": clip_types_file,
        "events": events_log,
        "vt_calls": vt_calls,
        "notifs": notif_log,
    }
    return env, paths


class DeliverWorker:
    def __init__(self, env: dict[str, str]):
        self.env = env
        self.sock = Path(env["SHUUSH_DELIVER_SOCK"])
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "DeliverWorker":
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(LIBEXEC / "shuush_deliver.py"), "--socket", str(self.sock), "serve"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            r = subprocess.run(
                [sys.executable, "-I", str(LIBEXEC / "shuush_deliver.py"), "--socket", str(self.sock), "ping"],
                env=self.env,
                capture_output=True,
                timeout=2,
            )
            if r.returncode == 0:
                return self
            time.sleep(0.03)
        raise RuntimeError("shuush_deliver worker failed to start")

    def __exit__(self, *_exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)


def read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wait_until(pred, timeout: float = 8.0, step: float = 0.03) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError("timed out waiting for condition")


def run_cmd(env: dict[str, str], *args: str, text: str = "", check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), input=text, capture_output=True, text=True, env=env, timeout=20, check=check)


# ── Step 0: per-take timing & `shuush stats latency` ─────────────────────────


def test_step0_per_take_timing_and_stats_latency_daemon_mode(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=FORK)
    (p["conf"] / "postprocess.env").write_text(
        "PP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\nPP_RELEASE_TAIL_MS=40\nPP_ORDER=\n"
    )
    # 1. Start and release take via toggle script (records key_release and daemon_stop).
    run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
    take_id = (p["rt"] / "take_id").read_text().strip()
    take_dir = p["rt"] / "takes" / take_id
    (take_dir / "take_screen_ctx.json").write_text('{"password":false}')
    # Pretend key was held > 300ms so release stops instead of switching to hands-free.
    (p["rt"] / "hold_start").write_text(str(int(time.time() * 1000) - 800))
    run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "release")

    # 2. Fake daemon finishes ASR and invokes voxtype-postprocess.sh (default daemon mode).
    res = run_cmd(
        {**env, "VOXTYPE_TAKE_ID": take_id},
        str(LIBEXEC / "voxtype-postprocess.sh"),
        text="Hello",
    )
    assert res.stdout == "Hello"

    # 3. Fake daemon completes delivery and writes outcome.json.
    time.sleep(0.03)
    (p["rt"] / "outcome.json").write_text(json.dumps({"take_id": take_id, "outcome": "delivered"}))

    hist = p["state"] / "history.jsonl"
    wait_until(lambda: hist.is_file() and len(hist.read_text().splitlines()) >= 1)

    # Verify per-take timing.json has all 7 monotonic timestamps in non-decreasing order.
    timing_doc = json.loads((take_dir / "timing.json").read_text())
    ts = timing_doc["timestamps_ms"]
    for ev in shuush_timing.EVENTS:
        assert ev in ts, f"missing event {ev} in {ts}"
    ordered = [ts[ev] for ev in shuush_timing.EVENTS]
    assert ordered == sorted(ordered), f"timestamps out of order: {ts}"
    assert timing_doc["durations_ms"]["release_tail_ms"] >= 20
    assert timing_doc["app_family"] == "terminal"

    # Verify history.jsonl row carries backward-compatible timing fields.
    row = json.loads(hist.read_text().splitlines()[0])
    assert row["take_id"] == take_id
    assert row["outcome"] == "delivered"
    assert row["delivery_mode"] == "daemon"
    assert row["delivery_method"] == "daemon"
    assert row["app_class"] == "kitty"
    assert row["app_family"] == "terminal"
    assert "release_to_done_ms" in row["timing"]
    assert "stop_to_done_ms" in row["timing"]

    # Add mixed rows (legacy + paste + stream_type + browser/editor) and run `shuush stats latency`.
    extra_rows = [
        {"ts": "2026-11-01T10:00:00+05:30", "mode": "default", "via": "groq", "pp_ms": 500, "llm_ms": 380, "raw": "legacy", "clean": "Legacy short."},
        {"ts": "2026-11-28T11:00:00+05:30", "mode": "default", "via": "groq", "pp_ms": 620, "llm_ms": 480, "raw": "x", "clean": "a" * 340,
         "delivery_mode": "shuush", "delivery_method": "paste", "app_class": "zen-browser", "app_family": "browser",
         "timing": {"stop_to_done_ms": 910, "cleanup_ms": 620, "delivery_ms": 45, "asr_ms": 245}},
        {"ts": "2026-11-28T11:05:00+05:30", "mode": "terminal", "via": "groq", "pp_ms": 410, "llm_ms": 300, "raw": "y", "clean": "b" * 120,
         "delivery_mode": "shuush", "delivery_method": "stream_type", "app_class": "code", "app_family": "editor",
         "timing": {"stop_to_done_ms": 700, "cleanup_ms": 410, "cleanup_ttfb_ms": 180, "delivery_ms": 95, "asr_ms": 195}},
    ]
    with hist.open("a", encoding="utf-8") as f:
        for r in extra_rows:
            f.write(json.dumps(r) + "\n")

    txt = run_cmd(env, str(ROOT / "bin/shuush"), "stats", "latency").stdout
    assert "By delivery method" in txt and "By text length band" in txt and "By app family" in txt
    assert "paste" in txt and "stream_type" in txt and "legacy (unrecorded)" in txt
    assert "short (<80c)" in txt and "medium (80-299c)" in txt and "long (>=300c)" in txt
    assert "terminal" in txt and "browser" in txt and "editor" in txt

    js = json.loads(run_cmd(env, str(ROOT / "bin/shuush"), "stats", "latency", "--json").stdout)
    assert js["total"] == 4 and js["with_stage_timing"] == 3 and js["legacy_rows"] == 1
    assert js["by_delivery_method"]["paste"]["metrics"]["delivery_ms"]["p50"] == 45
    assert js["by_app_family"]["editor"]["metrics"]["cleanup_ttfb_ms"]["p50"] == 180
    assert js["by_length_band"]["long (>=300c)"]["count"] == 1


# ── Step 2: default OFF, handoff, graceful degradation, doctor ───────────────


def test_default_delivery_off_and_graceful_degradation(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)
    # Default: PP_DELIVERY is unset -> daemon mode.
    doc = json.loads(run_cmd(env, str(ROOT / "bin/shuush"), "doctor", "--json").stdout)
    assert doc["delivery"]["mode"] == "daemon" and doc["delivery"]["effective"] == "daemon"

    # Enable PP_DELIVERY=shuush, but worker NOT running -> degrades cleanly to inline daemon stdout.
    (p["conf"] / "postprocess.env").write_text("PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\nPP_ORDER=\n")
    doc_down = json.loads(run_cmd(env, str(ROOT / "bin/shuush"), "doctor", "--json").stdout)
    assert doc_down["delivery"]["mode"] == "shuush"
    assert doc_down["delivery"]["worker_active"] is False
    assert doc_down["delivery"]["effective"] == "daemon"

    run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
    take1 = (p["rt"] / "current_capture").read_text().strip()
    (p["rt"] / "takes" / take1 / "take_screen_ctx.json").write_text('{"password":false}')
    # Stock toggle in shuush mode MUST NOT pass --profile (profiles hardcode fallback_on_empty = true).
    assert "--profile" not in p["vt_calls"].read_text()

    res_fallback = run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="Fallback")
    assert res_fallback.stdout == "Fallback"

    # With worker running, but config.toml missing fallback_on_empty = false -> refuses handoff & degrades inline!
    (p["conf"] / "config.toml").write_text('[output.post_process]\ncommand = "x"\nfallback_on_empty = true\n')
    with DeliverWorker(env):
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        take2 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / take2 / "take_screen_ctx.json").write_text('{"password":false}')
        res_guard = run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="Guarded")
        assert res_guard.stdout == "Guarded"
        assert read_events(p["events"]) == []

    # Fix config.toml -> handoff succeeds on BOTH stock and fork daemons, doctor shows effective=shuush.
    for schema in (STOCK, FORK):
        sub = tmp_path / Path(schema).stem
        sub.mkdir()
        e2, p2 = make_env(sub, schema=schema)
        (p2["conf"] / "postprocess.env").write_text("PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\nPP_ORDER=\n")
        with DeliverWorker(e2):
            d_on = json.loads(run_cmd(e2, str(ROOT / "bin/shuush"), "doctor", "--json").stdout)
            assert d_on["delivery"]["mode"] == "shuush"
            assert d_on["delivery"]["effective"] == "shuush"
            assert d_on["delivery"]["worker_active"] is True
            txt = run_cmd({**e2, "NO_COLOR": "1"}, str(ROOT / "bin/shuush"), "doctor").stdout
            assert "Delivery: configured=shuush, active=shuush" in txt

            run_cmd(e2, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            tid = (p2["rt"] / "current_capture").read_text().strip()
            (p2["rt"] / "takes" / tid / "take_screen_ctx.json").write_text('{"password":false}')
            r = run_cmd(e2, str(LIBEXEC / "voxtype-postprocess.sh"), text="Delivered")
            assert r.stdout == "", "handed-off take must print empty stdout so daemon goes idle immediately"
            wait_until(lambda: len(read_events(p2["events"])) == 1)
            assert read_events(p2["events"])[0]["args"] == ["--", "Delivered"]


# ── Step 2: strict take ordering with overlapping takes ──────────────────────


def test_overlapping_takes_deliver_in_strict_take_order(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)

    class SlowServer(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
            # Slow response for take 1 (~350ms) so take 2 (short, 0ms LLM) finishes cleanup way earlier.
            time.sleep(0.35)
            self.send_response(200)
            self.end_headers()
            body = {"choices": [{"message": {"content": "First slow take completed."}}]}
            self.wfile.write(json.dumps(body).encode("utf-8"))

        def log_message(self, *_):
            pass

    srv = HTTPServer(("127.0.0.1", 0), SlowServer)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        (p["conf"] / "postprocess.env").write_text(
            f"PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\n"
            f"PP_ORDER=groq\nPP_GROQ_PROXY={url}\nPP_STREAM_TYPING=0\n"
        )
        with DeliverWorker(env):
            # Take 1: multi-word -> hits slow LLM (350ms).
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            t1 = (p["rt"] / "current_capture").read_text().strip()
            (p["rt"] / "takes" / t1 / "take_screen_ctx.json").write_text('{"password":false}')
            r1 = run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="um first slow take completed")
            assert r1.stdout == ""

            # Take 2 starts immediately while take 1 is still in LLM cleanup! Single word -> instant (short).
            time.sleep(0.05)
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            t2 = (p["rt"] / "current_capture").read_text().strip()
            assert int(t2) > int(t1)
            (p["rt"] / "takes" / t2 / "take_screen_ctx.json").write_text('{"password":false}')
            r2 = run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="Second")
            assert r2.stdout == ""

            # Wait for both to deliver.
            wait_until(lambda: len(read_events(p["events"])) == 2)
            typed = [e["args"] for e in read_events(p["events"]) if e["tool"] == "wtype"]
            assert typed == [["--", "First slow take completed."], ["--", "Second"]]

            # Both takes have timing.json and history rows in order.
            hist = p["state"] / "history.jsonl"
            wait_until(lambda: hist.is_file() and len(hist.read_text().splitlines()) == 2)
            rows = [json.loads(l) for l in hist.read_text().splitlines()]
            assert [r["take_id"] for r in rows] == [t1, t2]
            assert all(r["delivery_mode"] == "shuush" and r["delivery_method"] == "type" for r in rows)
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


# ── Step 2: short type vs long paste + clipboard policy ──────────────────────


def test_short_typed_long_pasted_and_clipboard_policy(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)
    (p["conf"] / "postprocess.env").write_text(
        "PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\n"
        "PP_PASTE_OVER_CHARS=300\nPP_RESTORE_CLIPBOARD_DELAY_MS=20\nPP_ORDER=\n"
    )
    with DeliverWorker(env):
        # 1. Short text (<= 300 chars) -> typed directly, clipboard untouched.
        p["clip"].write_text("user copied value")
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        t1 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / t1 / "take_screen_ctx.json").write_text('{"password":false}')
        run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="Short")
        wait_until(lambda: len(read_events(p["events"])) == 1)
        evs = read_events(p["events"])
        assert evs[0]["tool"] == "wtype" and evs[0]["args"] == ["--", "Short"]
        assert p["clip"].read_text() == "user copied value"

        # 2. Long text (> 300 chars) in terminal -> pasted with ctrl+shift+v, then clipboard restored!
        p["events"].unlink(missing_ok=True)
        long_text = "Word " + "word " * 69  # 350 chars > 300
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        t2 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / t2 / "take_screen_ctx.json").write_text('{"password":false}')
        run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text=long_text)
        wait_until(lambda: len(read_events(p["events"])) >= 3)
        evs2 = read_events(p["events"])
        assert evs2[0]["tool"] == "wl-copy" and evs2[0]["text"] == long_text.strip()
        assert evs2[1]["tool"] == "wtype" and evs2[1]["args"] == ["-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]
        assert evs2[2]["tool"] == "wl-copy" and p["clip"].read_text() == "user copied value"

        # 3. Long text with password-manager clipboard hint & PP_KEEP_ON_CLIPBOARD=0 -> NEVER touches clipboard, types instead!
        p["events"].unlink(missing_ok=True)
        p["clip"].write_text("s3cr3t-pass")
        p["clip_types"].write_text("x-kde-passwordManagerHint\ntext/plain;charset=utf-8\n")
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        t3 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / t3 / "take_screen_ctx.json").write_text('{"password":false}')
        run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text=long_text)
        wait_until(lambda: len(read_events(p["events"])) >= 1)
        evs3 = read_events(p["events"])
        assert all(e["tool"] != "wl-copy" for e in evs3)
        assert evs3[0]["tool"] == "wtype" and evs3[0]["args"] == ["--", long_text.strip()]
        assert p["clip"].read_text() == "s3cr3t-pass"

        # 4. Custom threshold + PP_KEEP_ON_CLIPBOARD=1 in browser -> ctrl+v paste and keeps text on clipboard.
        p["events"].unlink(missing_ok=True)
        p["clip_types"].write_text("text/plain;charset=utf-8\n")
        p["win"].write_text(json.dumps({"address": "0x222", "pid": 99, "class": "zen-browser", "title": "Docs"}))
        (p["conf"] / "postprocess.env").write_text(
            "PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=1\n"
            "PP_PASTE_OVER_CHARS=20\nPP_ORDER=\n"
        )
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        t4 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / t4 / "take_screen_ctx.json").write_text('{"password":false}')
        medium = "This exceeds twenty characters easily."
        run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text=medium)
        wait_until(lambda: len(read_events(p["events"])) >= 2)
        evs4 = read_events(p["events"])
        assert evs4[0]["tool"] == "wl-copy" and evs4[0]["text"] == medium
        assert evs4[1]["tool"] == "wtype" and evs4[1]["args"] == ["-M", "ctrl", "-k", "v", "-m", "ctrl"]
        assert p["clip"].read_text() == medium


# ── Step 2: streaming cleanup typed as it arrives (on stock voxtype!) ────────


def test_streaming_cleanup_types_incrementally_on_stock(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)
    sent_second_at: list[float] = []

    class StreamServer(BaseHTTPRequestHandler):
        def do_POST(self):
            _ = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send_delta(txt: str, fin: str | None = None):
                chunk = {"choices": [{"delta": {"content": txt}, "finish_reason": fin}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()

            # voxtype_pp_stream lags by 1 sentence, so emit 3 sentences with a pause before the 3rd.
            send_delta("First sentence arrives now. ")
            send_delta("Second sentence follows quickly. ")
            time.sleep(0.25)
            sent_second_at.append(time.monotonic())
            send_delta("Third sentence finishes the take.")
            send_delta("", "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *_):
            pass

    srv = HTTPServer(("127.0.0.1", 0), StreamServer)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        (p["conf"] / "postprocess.env").write_text(
            f"PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\n"
            f"PP_ORDER=groq\nPP_GROQ_PROXY={url}\n"
            f"PP_STREAM_TYPING=1\nPP_STREAM_MIN_WORDS=5\nPP_PASTE_OVER_CHARS=300\n"
        )
        raw = "first sentence arrives now second sentence follows quickly third sentence finishes the take"
        with DeliverWorker(env):
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            tid = (p["rt"] / "current_capture").read_text().strip()
            (p["rt"] / "takes" / tid / "take_screen_ctx.json").write_text('{"password":false}')
            r = run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text=raw)
            assert r.stdout == ""

            hist = p["state"] / "history.jsonl"
            wait_until(lambda: hist.is_file() and len(hist.read_text().splitlines()) == 1)
            evs = [e for e in read_events(p["events"]) if e["tool"] == "wtype"]
            assert len(evs) >= 2, f"expected incremental streamed wtype chunks, got {evs}"
            # First chunk MUST have been typed BEFORE the 3rd sentence was even sent!
            assert evs[0]["t"] < sent_second_at[0], "first sentence was not typed before stream finished"
            full_typed = "".join(e["args"][-1] for e in evs)
            assert "First sentence arrives now." in full_typed and "Third sentence finishes the take." in full_typed

            row = json.loads(hist.read_text().splitlines()[0])
            assert row["delivery_method"] == "stream_type"
            assert row["timing"]["cleanup_first_byte_ms"] < row["timing"]["cleanup_last_byte_ms"]
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


# ── Step 2: target loss -> recovered + `shuush recover` + Studio on stock ────


@pytest.mark.parametrize("loss_kind", ["switched", "closed"])
def test_target_loss_saves_recovered_and_works_on_stock(tmp_path: Path, loss_kind: str, monkeypatch) -> None:
    env, p = make_env(tmp_path, schema=STOCK)

    class DelayedServer(BaseHTTPRequestHandler):
        def do_POST(self):
            _ = self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(0.25)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"Saved for recovery."}}]}')

        def log_message(self, *_):
            pass

    srv = HTTPServer(("127.0.0.1", 0), DelayedServer)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        (p["conf"] / "postprocess.env").write_text(
            f"PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=1\n"
            f"PP_ORDER=groq\nPP_GROQ_PROXY={url}\nPP_STREAM_TYPING=0\n"
        )
        with DeliverWorker(env):
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            tid = (p["rt"] / "current_capture").read_text().strip()
            take_dir = p["rt"] / "takes" / tid
            (take_dir / "take_screen_ctx.json").write_text('{"password":false}')
            r = run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="um saved for recovery")
            assert r.stdout == ""

            # Focus changes or target window closes while cleanup is running!
            if loss_kind == "switched":
                p["win"].write_text(json.dumps({"address": "0x999", "pid": 77, "class": "slack"}))
            else:
                p["win"].write_text("{}")

            wait_until(lambda: (take_dir / "disposition").is_file())
            assert (take_dir / "disposition").read_text().strip() == "recovered"
            rec_path = take_dir / "recovered.txt"
            assert rec_path.read_text() == "Saved for recovery."
            assert stat.S_IMODE(rec_path.stat().st_mode) == 0o600
            assert read_events(p["events"]) == [], "must not type or copy to clipboard on target loss"

            # `shuush recover` copies it even on stock voxtype!
            run_cmd(env, str(ROOT / "bin/shuush"), "recover")
            assert p["clip"].read_text() == "Saved for recovery."

            # Studio `load_history` sees recovered take even on stock voxtype!
            studio = _load_module("studio_stock_rec", LIBEXEC / "voxtype-studio.py")
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            monkeypatch.setattr(studio, "HISTORY", p["state"] / "history.jsonl")
            monkeypatch.setattr(studio, "RT", p["rt"])
            rows = studio.load_history()
            assert any(r["take_id"] == tid and r["outcome"] == "recovered" and r["clean"] == "Saved for recovery." for r in rows)
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


# ── Step 2: private takes NEVER via clipboard (even > 300 chars) ─────────────


def test_private_take_never_uses_clipboard_even_when_long(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)
    llm_calls: list[bytes] = []

    class CloudSpy(BaseHTTPRequestHandler):
        def do_POST(self):
            llm_calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"Should never be called."}}]}')

        def log_message(self, *_):
            pass

    srv = HTTPServer(("127.0.0.1", 0), CloudSpy)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        (p["conf"] / "postprocess.env").write_text(
            f"PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=1\n"
            f"PP_PASTE_OVER_CHARS=50\nPP_ORDER=groq\nPP_GROQ_PROXY={url}\n"
        )
        # KeePassXC window -> private take!
        p["win"].write_text(json.dumps({"address": "0x555", "pid": 55, "class": "org.keepassxc.KeePassXC", "title": "Vault.kdbx"}))
        long_secret = "correct horse battery staple " * 15  # > 400 chars (way over threshold 50)
        with DeliverWorker(env):
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            tid = (p["rt"] / "current_capture").read_text().strip()
            take_dir = p["rt"] / "takes" / tid
            (take_dir / "take_screen_ctx.json").write_text('{"password":false}')
            r = run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text=long_secret)
            assert r.stdout == ""

            wait_until(lambda: (take_dir / "disposition").is_file())
            assert (take_dir / "disposition").read_text().strip() == "delivered"
            evs = read_events(p["events"])
            assert len(evs) == 1 and evs[0]["tool"] == "wtype" and evs[0]["args"] == ["--", long_secret]
            assert llm_calls == [], "private take must never reach LLM"
            assert not p["clip"].exists(), "private take must never touch clipboard"
            assert not (p["state"] / "history.jsonl").exists(), "private take must never write history"
            assert not (p["rt"] / "pp_final").exists()

            # Private take + target changed -> private_not_inserted, no typing, no clipboard, no recovered.txt!
            p["events"].unlink(missing_ok=True)
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            tid2 = (p["rt"] / "current_capture").read_text().strip()
            take_dir2 = p["rt"] / "takes" / tid2
            (take_dir2 / "take_screen_ctx.json").write_text('{"password":true}')
            p["win"].write_text(json.dumps({"address": "0x888", "pid": 88, "class": "kitty"}))
            run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="another secret")
            wait_until(lambda: (take_dir2 / "disposition").is_file())
            assert (take_dir2 / "disposition").read_text().strip() == "private_not_inserted"
            assert read_events(p["events"]) == []
            assert not (take_dir2 / "recovered.txt").exists()
            assert not (p["state"] / "history.jsonl").exists()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


# ── Step 2: empty-string interplay (wire/unwire + Shuush submit/append) ──────


def test_wire_empty_string_keys_and_unwire_restores_byte_for_byte(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)
    original = (
        '# user config\nengine = "whisper"\n\n'
        '[output]\nmode = "type"\nauto_submit = true\nappend_text = " "\n\n'
        '[output.post_process]\ntimeout_ms = 30000\n\n'
        '[text]\nsmart_auto_submit = true\n\n'
        '[osd]\nstyle = "default"\n'
    )
    cfg = p["conf"] / "config.toml"
    cfg.write_text(original, encoding="utf-8")
    (p["conf"] / "postprocess.env").write_text("PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\n")

    # `shuush wire` automatically sees PP_DELIVERY=shuush from postprocess.env!
    res = run_cmd(env, str(ROOT / "bin/shuush"), "wire")
    assert "delivery=shuush" in res.stdout
    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["output"]["post_process"]["fallback_on_empty"] is False
    assert parsed["output"]["auto_submit"] is False
    assert parsed["text"]["smart_auto_submit"] is False
    assert "append_text" not in parsed["output"]

    # Manifest records old values for all three keys, and active values migrated to postprocess.env!
    man = json.loads((tmp_path / "config/shuush/wired.json").read_text())
    assert man["keys"]["output.auto_submit"] == {"old": True, "new": False, "via": "voxtype"}
    assert man["keys"]["text.smart_auto_submit"] == {"old": True, "new": False, "via": "voxtype"}
    assert man["keys"]["output.append_text"] == {"old": " ", "new": None, "via": "voxtype"}

    pp_env_text = (p["conf"] / "postprocess.env").read_text()
    assert "PP_AUTO_SUBMIT=1" in pp_env_text
    assert "PP_SMART_AUTO_SUBMIT=1" in pp_env_text
    assert 'PP_APPEND_TEXT=" "' in pp_env_text

    # Switching back to daemon delivery via `shuush wire --delivery daemon` restores the 3 keys!
    run_cmd(env, str(ROOT / "bin/shuush"), "wire", "--delivery", "daemon")
    back_daemon = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert back_daemon["output"]["auto_submit"] is True
    assert back_daemon["text"]["smart_auto_submit"] is True
    assert back_daemon["output"]["append_text"] == " "
    assert back_daemon["output"]["post_process"]["command"].endswith("voxtype-postprocess.sh")

    # Switching back to shuush and then `shuush unwire` restores config.toml byte-for-byte!
    run_cmd(env, str(ROOT / "bin/shuush"), "wire", "--delivery", "shuush")
    run_cmd(env, str(ROOT / "bin/shuush"), "unwire")
    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "config/shuush/wired.json").exists()


def test_shuush_delivery_handles_append_text_auto_submit_and_smart_submit(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)
    (p["conf"] / "postprocess.env").write_text(
        'PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\n'
        'PP_SMART_AUTO_SUBMIT=1\nPP_APPEND_TEXT=" "\nPP_ORDER=\n'
    )
    with DeliverWorker(env):
        # 1. Trailing "submit" triggers Return and strips "submit", plus appends space.
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        t1 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / t1 / "take_screen_ctx.json").write_text('{"password":false}')
        run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="Run tests submit")
        wait_until(lambda: len(read_events(p["events"])) == 2)
        evs = read_events(p["events"])
        assert evs[0]["args"] == ["--", "Run tests "]
        assert evs[1]["args"] == ["-k", "Return"]

        # 2. Without trailing "submit", no Return, only append_text.
        p["events"].unlink(missing_ok=True)
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        t2 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / t2 / "take_screen_ctx.json").write_text('{"password":false}')
        run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="Hello world")
        wait_until(lambda: (p["rt"] / "takes" / t2 / "disposition").is_file())
        evs2 = read_events(p["events"])
        assert len(evs2) == 1 and evs2[0]["args"] == ["--", "Hello world "]

        # 3. PP_AUTO_SUBMIT=1 always sends Return after delivered text.
        p["events"].unlink(missing_ok=True)
        (p["conf"] / "postprocess.env").write_text(
            "PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\n"
            "PP_AUTO_SUBMIT=1\nPP_ORDER=\n"
        )
        run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
        t3 = (p["rt"] / "current_capture").read_text().strip()
        (p["rt"] / "takes" / t3 / "take_screen_ctx.json").write_text('{"password":false}')
        run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="Done")
        wait_until(lambda: len(read_events(p["events"])) == 2)
        evs3 = read_events(p["events"])
        assert evs3[0]["args"] == ["--", "Done"]
        assert evs3[1]["args"] == ["-k", "Return"]


def test_cancel_and_skip_in_shuush_delivery_mode(tmp_path: Path) -> None:
    env, p = make_env(tmp_path, schema=STOCK)

    class VerySlowServer(BaseHTTPRequestHandler):
        def do_POST(self):
            _ = self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(2.5)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"Too late."}}]}')

        def log_message(self, *_):
            pass

    srv = HTTPServer(("127.0.0.1", 0), VerySlowServer)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        (p["conf"] / "postprocess.env").write_text(
            f"PP_DELIVERY=shuush\nPP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\n"
            f"PP_ORDER=groq\nPP_GROQ_PROXY={url}\nPP_STREAM_TYPING=0\n"
        )
        with DeliverWorker(env):
            # 1. Skip active slow cleanup -> raw transcript delivered immediately!
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            t1 = (p["rt"] / "current_capture").read_text().strip()
            (p["rt"] / "takes" / t1 / "take_screen_ctx.json").write_text('{"password":false}')
            run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="raw words delivered on skip")
            time.sleep(0.15)
            run_cmd(env, str(ROOT / "bin/shuush"), "skip")
            wait_until(lambda: (p["rt"] / "takes" / t1 / "disposition").is_file(), timeout=3.0)
            assert (p["rt"] / "takes" / t1 / "disposition").read_text().strip() == "delivered"
            evs = read_events(p["events"])
            assert evs[-1]["args"] == ["--", "raw words delivered on skip"]

            # 2. Cancel active slow cleanup via `shuush cancel now` (even while voxtype state is idle!).
            p["events"].unlink(missing_ok=True)
            (p["rt"] / "state").write_text("idle")
            run_cmd(env, str(LIBEXEC / "voxtype-toggle.sh"), "hold")
            t2 = (p["rt"] / "current_capture").read_text().strip()
            (p["rt"] / "takes" / t2 / "take_screen_ctx.json").write_text('{"password":false}')
            run_cmd(env, str(LIBEXEC / "voxtype-postprocess.sh"), text="these words should be cancelled")
            time.sleep(0.15)
            run_cmd(env, str(ROOT / "bin/shuush"), "cancel", "now")
            wait_until(lambda: (p["rt"] / "takes" / t2 / "disposition").is_file(), timeout=3.0)
            assert (p["rt"] / "takes" / t2 / "disposition").read_text().strip() == "cancelled"
            assert read_events(p["events"]) == []
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)
