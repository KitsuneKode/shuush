"""Exercise only disposable captures, fake desktop commands and a loopback provider."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from test_dictation_pass import ROOT, LIBEXEC, _load_module, _sandbox_env, _write_exec


@pytest.fixture
def box(tmp_path):
    commands = tmp_path / "bin"
    commands.mkdir()
    env = _sandbox_env(tmp_path, commands)
    fake = commands / "voxtype-fork"
    _write_exec(fake, (ROOT / "tests/fixtures/fake-voxtype").read_text() +
                "\n# Runtime protocol strings: VOXTYPE_TAKE_ID recovered.txt sensitive_field\n")
    env.update(SHUUSH_VOXTYPE_BIN=str(fake), FAKE_SCHEMA=str(ROOT / "tests/fixtures/schema-fork.json"))
    for name in ("wpctl", "notify-send", "wl-paste", "niri", "swaymsg", "qs", "kitten"):
        _write_exec(commands / name, "#!/bin/sh\nexit 1\n")
    _write_exec(commands / "hyprctl", '#!/bin/sh\n[ "$1" = activewindow ] && cat "$HOME/window"\n')
    _write_exec(commands / "wl-copy", '#!/bin/sh\nprintf "%s" "$*" > "$HOME/copied"; cat >> "$HOME/copied"\n')
    _write_exec(commands / "voxtype", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/commands"\n')
    (tmp_path / "home/window").write_text(json.dumps({"address": "0xa", "pid": 1, "class": "kitty"}))
    conf = tmp_path / "config/voxtype"
    (conf / "postprocess.env").write_text("PP_SCREEN_CONTEXT=0\nPP_KEEP_ON_CLIPBOARD=0\n")
    return env


def run(env, script, *args, text=""):
    return subprocess.run([str(script), *args], input=text, capture_output=True, text=True,
                          env=env, timeout=15, check=True)


@pytest.fixture
def cloud():
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"Test the cleanup provider."}}]}')

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def pp_copy(tmp_path):
    dest = tmp_path / "scripts"
    shutil.copytree(LIBEXEC, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


@pytest.mark.parametrize("failure", ["exit", "partial", "invalid", "no_verdict", "password", "marker", "unprobed_marker"])
def test_private_never_reaches_cloud_or_clipboard(tmp_path, box, cloud, failure):
    scripts = pp_copy(tmp_path)
    rt = tmp_path / "rt/voxtype"
    history = tmp_path / "state/voxtype/history.jsonl"
    url, calls = cloud
    config = tmp_path / "config/voxtype/postprocess.env"
    config.write_text(f"PP_SCREEN_CONTEXT=0\nPP_ORDER=groq\nPP_GROQ_PROXY={url}\nPP_KEEP_ON_CLIPBOARD=1\n")
    # Positive control: the same provider is reachable on this exact script and configuration.
    run(dict(box, PP_NO_HISTORY="1"), scripts / "voxtype-postprocess.sh", text="Test the cleanup provider.")
    assert len(calls) == 1
    calls.clear()
    if failure in ("marker", "unprobed_marker"):
        if failure == "unprobed_marker":
            box["SHUUSH_VOXTYPE_BIN"] = str(tmp_path / "unavailable")
        (rt / "sensitive_field").touch()
    elif failure == "password":
        (scripts / "voxtype_screen_ctx.py").write_text(
            f'import sys, pathlib\nassert "--sensitivity-only" in sys.argv\npathlib.Path({str(tmp_path / "probed")!r}).touch()\n'
            'print(\'{"password":true}\')')
    else:
        code = {"exit": "raise SystemExit(7)", "partial": 'print(\'{"private_why":""}\');raise SystemExit(7)',
                "invalid": 'print("{")', "no_verdict": 'print("{}")'}[failure]
        (scripts / "voxtype_pp_gate_run.py").write_text(
            f"import sys, pathlib\nassert sys.argv[1]=='prepare'\npathlib.Path({str(tmp_path / 'prepared')!r}).touch()\n" + code)
    if failure != "password":
        (scripts / "voxtype_screen_ctx.py").write_text("import json; print(json.dumps(dict(password=False)))")
    private = "  Raw private words stay here."
    result = run(box, scripts / "voxtype-postprocess.sh", text=private)
    if failure == "password":
        assert (tmp_path / "probed").exists()
    if failure not in ("password", "marker", "unprobed_marker"):
        assert (tmp_path / "prepared").exists()
    assert result.stdout == private
    assert calls == [] and (rt / "pp_private").exists()
    assert not history.exists() and not (tmp_path / "home/copied").exists()
    assert not (rt / "live_text").exists() and not (rt / "pp_final").exists()


@pytest.mark.parametrize("window,private", [("other", False), ("closed", False), ("other", True)])
def test_queued_cleanup_uses_original_target(tmp_path, box, window, private):
    rt = tmp_path / "rt/voxtype"
    win = tmp_path / "home/window"
    if private:
        (rt / "sensitive_field").touch()
    run(box, LIBEXEC / "voxtype-toggle.sh", "hold")
    first = (rt / "take_id").read_text()
    initial = rt / "takes" / first
    (initial / "take_screen_ctx.json").write_text('{"password":false}')
    (initial / "claimed").touch()  # daemon observed the start
    (rt / "sensitive_field").unlink(missing_ok=True)
    (rt / "state").write_text("transcribing")
    for marker in ("pid", "queue_pid"):
        (rt / marker).write_text(str(os.getpid()))
    win.write_text(json.dumps({"address": "0xb", "pid": 2, "class": "slack"}))
    run(box, LIBEXEC / "voxtype-toggle.sh", "hold")
    second = (rt / "take_id").read_text()
    assert first != second
    assert (initial / "target_profile").read_text() == "terminal"
    assert (rt / "takes" / second / "target_profile").read_text() == "chat"
    if window == "closed":
        win.write_text("{}")
    pp_env = dict(box, VOXTYPE_TAKE_ID=first, VOXTYPE_STREAM_OUTPUT="1")
    result = run(pp_env, LIBEXEC / "voxtype-postprocess.sh", text="Example")
    assert result.stdout == "" and not (tmp_path / "home/copied").exists()
    assert (initial / "disposition").read_text().strip() == ("private_not_inserted" if private else "recovered")
    recovered = initial / "recovered.txt"
    if private:
        assert not recovered.exists()
        assert not (tmp_path / "state/voxtype/history.jsonl").exists()
    else:
        assert recovered.read_text() == "Example"
        assert recovered.stat().st_mode & 0o777 == 0o600


def test_bring_back_and_studio_see_late_recovery(tmp_path, box, monkeypatch):
    studio = _load_module("studio_recovery", LIBEXEC / "voxtype-studio.py")
    rt = tmp_path / "rt/voxtype"
    folder = rt / "takes/123"
    folder.mkdir(parents=True)
    (rt / "take_id").write_text("123")
    (folder / "recovered.txt").write_text("words from the daemon")
    (folder / "disposition").write_text("recovered")
    for key, val in box.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(studio, "HISTORY", tmp_path / "state/voxtype/history.jsonl")
    monkeypatch.setattr(studio, "RT", rt)
    row, = studio.load_history()
    assert (row["take_id"], row["outcome"], row["clean"]) == ("123", "recovered", "words from the daemon")
    result = run(box, ROOT / "bin/shuush", "recover")
    assert result.stdout == "" and result.stderr == ""
    assert (tmp_path / "home/copied").read_text() == row["clean"]
    (folder / "pp_private").touch()
    assert studio.load_history() == []


def test_delivery_metrics_require_matching_receipts(tmp_path):
    from voxtype_delivery import OUTCOMES, receipt

    studio = _load_module("studio_outcomes", LIBEXEC / "voxtype-studio.py")
    rows = [{"via": "groq"}]
    for outcome in OUTCOMES:
        (tmp_path / "outcome.json").write_text(json.dumps({"take_id": "123", "outcome": outcome}))
        assert receipt(tmp_path, "456") == ""
        assert receipt(tmp_path, "123") == outcome
        rows.append({"outcome": outcome, "via": "passthrough"})
    assert studio.compute_insights(rows)["success_pct"] == pytest.approx(100 / 6)
    assert studio.compute_insights([{"via": "groq"}])["delivery_rate"] == "—"


def test_review_append_waits_for_studio_rewrite(tmp_path, box, monkeypatch):
    import voxtype_storage as storage

    studio = _load_module("studio_race", LIBEXEC / "voxtype-studio.py")
    hist = tmp_path / "state/voxtype/history.jsonl"
    original = {"ts": "one", "raw": "one"}
    hist.write_text(json.dumps(original) + "\n")
    writer = None
    write = storage.atomic_write

    def append_during_replace(path, content):
        nonlocal writer
        writer = subprocess.Popen([sys.executable, str(LIBEXEC / "voxtype-review.py")],
                                  env=dict(box, RV_RAW="two", RV_CLEAN="Two", RV_TS="two"))
        time.sleep(.15)
        assert writer.poll() is None
        write(path, content)

    monkeypatch.setattr(storage, "atomic_write", append_during_replace)
    assert studio.update_history_entry(hist, original, "One")
    assert writer.wait(timeout=5) == 0
    assert [row["clean"] for row in map(json.loads, hist.read_text().splitlines())] == ["One", "Two"]


def test_correction_writers_and_studio_undo(tmp_path, box, monkeypatch):
    import voxtype_learn as learn

    tsv = tmp_path / "config/voxtype/corrections.tsv"
    monkeypatch.setattr(learn, "TSV", tsv)
    undo = {}
    assert learn.remember("webar", "Waybar", "user", undo) is None
    code = 'import voxtype_learn as l; assert l.remember("hyperlane","Hyprland","user") is None'
    workers = [subprocess.Popen([sys.executable, "-c", code], cwd=LIBEXEC, env=box) for _ in range(14)]
    assert all(p.wait(timeout=8) == 0 for p in workers)
    learn.undo_corrections(undo)
    assert learn.load() == {"hyperlane": ["Hyprland", "user", 14]}


@pytest.mark.skipif(shutil.which("qs") is None, reason="Quickshell not installed")
@pytest.mark.parametrize("private,kind,take,check,label", [
    (True, "private_not_inserted", "11", False, "Private · not inserted"),
    (False, "delivered", "11", True, ""),
    (False, "delivered", "12", False, ""),
    (False, "clipboard_only", "11", False, "Clipboard only"),
    (False, "recovered", "11", False, "Target changed · saved in Studio"),
    ("late", "failed", "11", False, "Delivery failed"),
    (False, "cancelled", "11", False, "Cancelled"),
])
def test_hud_private_words_and_terminal_check(tmp_path, box, private, kind, take, check, label):
    rt = tmp_path / "rt/voxtype"
    (rt / "take_id").write_text("11")
    if private is True:
        (rt / "sensitive_field").touch()
    _write_exec(tmp_path / "bin/shuush", '#!/bin/sh\necho \'{"caps":{"sensitive_field":true,"take_identity":true}}\'')
    (tmp_path / "KitsuneHud.qml").symlink_to(ROOT / "osd/kitsune-hud/KitsuneHud.qml")
    # Exercise the actual component (including zero-length marker reads and exit animation)
    # in an offscreen Qt process with a private runtime and no surface, input, or audio.
    (tmp_path / "shell.qml").write_text("""
import QtQuick
import Quickshell
import Quickshell.Io
ShellRoot {
    KitsuneHud { id: hud; width: 800; height: 600 }
    FileView { id: outcome; path: Quickshell.env("XDG_RUNTIME_DIR") + "/voxtype/outcome.json"; printErrors: false }
    FileView { id: marker; path: Quickshell.env("XDG_RUNTIME_DIR") + "/voxtype/sensitive_field"; printErrors: false }
    Timer {
        interval: 250; running: true; repeat: true; property int phase: 0
        onTriggered: {
            if (phase === 0) {
                hud.daemonState = "recording";
                hud.shownText = "synthetic preview"; hud.wordsOk = true;
            } else if (phase === 1) {
                if (Quickshell.env("LATE") === "1") marker.setText("1");
            } else if (phase === 2) {
                console.log("WORDS=" + hud.hasText);
                outcome.setText(JSON.stringify({take_id: Quickshell.env("RESULT_ID"), at_ms: Date.now(),
                    outcome: Quickshell.env("RESULT_KIND")}));
                hud.daemonState = "idle";
            } else {
                console.log("RECEIPT=" + JSON.stringify([hud.confirmed, hud.terminalLabel]));
                Qt.quit();
            }
            phase++;
        }
    }
}
""")
    (rt / "pp_result").write_text(f"{int(time.time() * 1000)} groq 11")
    env = dict(box, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", RESULT_ID=take, RESULT_KIND=kind, LATE="1" if private == "late" else "0")
    res = subprocess.run([shutil.which("qs"), "-p", str(tmp_path / "shell.qml")], env=env,
                         capture_output=True, text=True, timeout=8)
    log = res.stdout + res.stderr
    assert "WORDS=" + str(not private).lower() in log, log
    assert "RECEIPT=" + json.dumps([check, label], ensure_ascii=False, separators=(",", ":")) in log, log


def test_same_target_returns_text_and_studio_records_daemon_result(tmp_path, box):
    rt = tmp_path / "rt/voxtype"
    run(box, LIBEXEC / "voxtype-toggle.sh", "hold")
    take = (rt / "take_id").read_text()
    (rt / "takes" / take / "take_screen_ctx.json").write_text('{"password":false}')
    result = run(dict(box, VOXTYPE_TAKE_ID=take), LIBEXEC / "voxtype-postprocess.sh", text="Hello")
    assert result.stdout == "Hello"
    (rt / "outcome.json").write_text(json.dumps({"take_id": take, "outcome": "clipboard_only"}))
    hist = tmp_path / "state/voxtype/history.jsonl"
    end = time.monotonic() + 4
    while not hist.exists() and time.monotonic() < end:
        time.sleep(.02)
    row = json.loads(hist.read_text())
    assert (row["take_id"], row["mode"], row["outcome"]) == (take, "terminal", "clipboard_only")
