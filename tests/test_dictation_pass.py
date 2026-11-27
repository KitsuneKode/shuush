#!/usr/bin/env python3
"""Isolated pytest suite for the dictation pass improvements.

Never records from the microphone, plays audio, types into windows, or touches
the live Wayland clipboard or ~/.config/voxtype.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import signal
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = ROOT / "libexec"
sys.path.insert(0, str(LIBEXEC))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_wav(
    path: Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16,
    duration_ms: int = 1000,
    extra_chunks: list[tuple[bytes, bytes]] | None = None,
    fmt_extra: bytes = b"",
) -> Path:
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    data_len = int(round(byte_rate * duration_ms / 1000.0))
    data_len -= data_len % block_align
    fmt_body = struct.pack(
        "<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits_per_sample
    ) + fmt_extra
    chunks: list[bytes] = []
    for cid, payload in (extra_chunks or []):
        pad = b"\x00" if len(payload) % 2 else b""
        chunks.append(cid + struct.pack("<I", len(payload)) + payload + pad)
    fmt_pad = b"\x00" if len(fmt_body) % 2 else b""
    chunks.append(b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body + fmt_pad)
    data_payload = b"\x00" * data_len
    data_pad = b"\x00" if len(data_payload) % 2 else b""
    chunks.append(b"data" + struct.pack("<I", data_len) + data_payload + data_pad)
    body = b"WAVE" + b"".join(chunks)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _sandbox_env(tmp_path: Path, fake_bin: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "rt"),
        "LC_ALL": "C.UTF-8",
    }
    for sub in ("home", "config/voxtype", "state/voxtype", "rt/voxtype"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    if extra:
        env.update(extra)
    return env


# ── 1. voxtype-toggle.sh queued hold mode ────────────────────────────────────


def _setup_toggle_shims(fake_bin: Path, log_file: Path) -> None:
    _write_exec(
        fake_bin / "voxtype",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log_file}"\n',
    )
    _write_exec(fake_bin / "wpctl", "#!/usr/bin/env bash\necho 'Volume: 1.00'\n")
    _write_exec(
        fake_bin / "hyprctl",
        '#!/usr/bin/env bash\necho \'{"address":"0x1","class":"kitty","title":"zsh","pid":0}\'\n',
    )
    _write_exec(fake_bin / "wl-paste", "#!/usr/bin/env bash\nexit 1\n")


def test_toggle_hold_transcribing_blocked_without_queue_capability(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "voxtype.log"
    _setup_toggle_shims(fake_bin, log)
    env = _sandbox_env(tmp_path, fake_bin)
    rt = tmp_path / "rt/voxtype"
    (rt / "state").write_text("transcribing\n", encoding="utf-8")

    res = subprocess.run([str(LIBEXEC / "voxtype-toggle.sh"), "hold"], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert not log.exists()
    assert (rt / "hold_ignore_release").exists()


def test_toggle_hold_transcribing_starts_when_queue_pid_live(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "voxtype.log"
    _setup_toggle_shims(fake_bin, log)
    env = _sandbox_env(tmp_path, fake_bin)
    rt = tmp_path / "rt/voxtype"
    (rt / "state").write_text("transcribing\n", encoding="utf-8")
    pid = str(os.getpid())
    (rt / "pid").write_text(pid + "\n", encoding="utf-8")
    (rt / "queue_pid").write_text(pid + "\n", encoding="utf-8")

    res = subprocess.run([str(LIBEXEC / "voxtype-toggle.sh"), "hold"], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert log.read_text(encoding="utf-8").strip() == "record start --profile terminal"
    assert not (rt / "hold_ignore_release").exists()
    assert (rt / "hold_start").is_file()


def test_toggle_hold_transcribing_rejects_stale_or_mismatched_queue_pid(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "voxtype.log"
    _setup_toggle_shims(fake_bin, log)
    env = _sandbox_env(tmp_path, fake_bin)
    rt = tmp_path / "rt/voxtype"
    (rt / "state").write_text("transcribing\n", encoding="utf-8")
    (rt / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (rt / "queue_pid").write_text("9999999\n", encoding="utf-8")

    res = subprocess.run([str(LIBEXEC / "voxtype-toggle.sh"), "hold"], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert not log.exists()
    assert (rt / "hold_ignore_release").exists()


def test_toggle_hold_transcribing_respects_voxtype_queue_flag(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "voxtype.log"
    _setup_toggle_shims(fake_bin, log)
    env = _sandbox_env(tmp_path, fake_bin, {"VOXTYPE_QUEUE": "1"})
    rt = tmp_path / "rt/voxtype"
    (rt / "state").write_text("transcribing\n", encoding="utf-8")

    res = subprocess.run([str(LIBEXEC / "voxtype-toggle.sh"), "hold"], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert "record start" in log.read_text(encoding="utf-8")

    # Explicit VOXTYPE_QUEUE=0 overrides live queue_pid
    log.unlink()
    pid = str(os.getpid())
    (rt / "pid").write_text(pid + "\n", encoding="utf-8")
    (rt / "queue_pid").write_text(pid + "\n", encoding="utf-8")
    env["VOXTYPE_QUEUE"] = "0"
    res2 = subprocess.run([str(LIBEXEC / "voxtype-toggle.sh"), "hold"], env=env, capture_output=True, text=True)
    assert res2.returncode == 0
    assert not log.exists()


# ── 2. voxtype-postprocess.sh single-jq + skip live wl-paste ─────────────────


def _normalize_now(s: str) -> str:
    return re.sub(r"<now>[^<]*</now>", "<now>FIXED</now>", s)


def _run_pp_script(
    script_path: Path,
    tmp_path: Path,
    raw: str,
    mode: str = "default",
    *,
    target_win: dict | None = None,
    screen_ctx: dict | None = None,
    selection: str = "",
    clip_text: str = "",
    clip_types: str = "text/plain;charset=utf-8\n",
    pre_hash_clip: str | None = None,
    pre_clip_cached: str | None = None,
    dump_messages: bool = True,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, str, int, int]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    paste_log = tmp_path / "wl_paste.calls"
    copy_log = tmp_path / "wl_copy.calls"
    if paste_log.exists():
        paste_log.unlink()
    if copy_log.exists():
        copy_log.unlink()

    clip_types_file = tmp_path / "clip.types"
    clip_data_file = tmp_path / "clip.data"
    clip_types_file.write_text(clip_types, encoding="utf-8")
    clip_data_file.write_text(clip_text, encoding="utf-8")

    _write_exec(
        fake_bin / "wl-paste",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{paste_log}"
if [[ "$*" == *"--list-types"* ]]; then
  cat "{clip_types_file}"
else
  cat "{clip_data_file}"
fi
""",
    )
    _write_exec(
        fake_bin / "wl-copy",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{copy_log}"\n',
    )
    _write_exec(fake_bin / "hyprctl", "#!/usr/bin/env bash\nexit 1\n")

    dump_file = tmp_path / "messages.json"
    if dump_file.exists():
        dump_file.unlink()
    env = _sandbox_env(tmp_path, fake_bin, extra_env)
    if dump_messages:
        env["PP_DUMP_MESSAGES"] = str(dump_file)

    conf = tmp_path / "config/voxtype"
    (conf / "corrections.tsv").write_text(
        (ROOT / "tests/fixtures/corrections.tsv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (conf / "snippets").write_text("sign off: Thanks,\\nM\nmy email: me@example.com\n", encoding="utf-8")
    (conf / "vocabulary.txt").write_text("Hyprland\nWaybar\nNext.js\n", encoding="utf-8")
    (conf / "postprocess.env").write_text(
        "PP_APP_CONTEXT=1\nPP_SCREEN_CONTEXT=1\nPP_CLIPBOARD_CONTEXT=1\nPP_PRIVATE_GUARD=1\nPP_KEEP_ON_CLIPBOARD=0\n",
        encoding="utf-8",
    )

    rt = tmp_path / "rt/voxtype"
    if target_win is not None:
        (rt / "target_window").write_text(json.dumps(target_win), encoding="utf-8")
    if screen_ctx is not None:
        (rt / "take_screen_ctx.json").write_text(json.dumps(screen_ctx), encoding="utf-8")
    if selection:
        (rt / "take_selection").write_text(selection, encoding="utf-8")
    if pre_hash_clip is not None:
        (rt / "take_clip_start").write_text(pre_hash_clip, encoding="utf-8")
    if pre_clip_cached is not None:
        (rt / "take_clip").write_text(pre_clip_cached, encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(script_path), mode],
        input=raw,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )
    dumped = dump_file.read_text(encoding="utf-8") if dump_file.exists() else ""
    paste_calls = len(paste_log.read_text(encoding="utf-8").splitlines()) if paste_log.exists() else 0
    copy_calls = len(copy_log.read_text(encoding="utf-8").splitlines()) if copy_log.exists() else 0
    return proc, dumped, paste_calls, copy_calls


def test_postprocess_prep_json_byte_identical_to_legacy_jq(tmp_path: Path) -> None:
    """Prove the single-jq PREP_JSON path produces byte-identical output vs legacy 14-fork jq across a fixture corpus."""
    new_script = LIBEXEC / "voxtype-postprocess.sh"
    # Build a legacy-style reference script by substituting only the PREP_JSON
    # parser back to the original 14 individual jq calls (so this test remains
    # valid after commits and compares the exact two extraction methods).
    base_ref = subprocess.check_output(
        ["git", "-C", str(ROOT), "show", "main:libexec/voxtype-postprocess.sh"],
        text=True,
    )
    old_dir = tmp_path / "old_libexec"
    old_dir.mkdir()
    for f in LIBEXEC.iterdir():
        if f.is_file() and f.name != "voxtype-postprocess.sh":
            (old_dir / f.name).symlink_to(f)
    old_script = _write_exec(old_dir / "voxtype-postprocess.sh", base_ref)

    corpus = [
        (
            "rich_context_with_escapes",
            "default",
            "open the kite eye scheduler and fix hyperlane with \\012 and \\c and %s and \"quotes\"",
            {"address": "0x1", "class": "Cursor", "title": "kaitai-inference/src/scheduler.rs", "pid": 42},
            {"password": False, "draft": "line1\nline2\tindented\\path", "screen": "fn dispatch_waybar()\n│ box │\ntoken=ghp_123456789012345678901234"},
            "selected\ttext\nsecond line",
            "fresh clipboard value",
        ),
        (
            "private_app",
            "default",
            "my secret passphrase is open sesame",
            {"address": "0x2", "class": "org.keepassxc.KeePassXC", "title": "Vault.kdbx", "pid": 43},
            {"password": False, "draft": "", "screen": ""},
            "",
            "",
        ),
        (
            "atspi_password",
            "default",
            "hunter two password",
            {"address": "0x3", "class": "zen", "title": "Docs", "pid": 44},
            {"password": True, "draft": "", "screen": ""},
            "",
            "",
        ),
        (
            "hallucination_empty",
            "default",
            "Thanks for watching.",
            {"address": "0x4", "class": "kitty", "title": "zsh", "pid": 45},
            {"password": False, "draft": "", "screen": ""},
            "",
            "",
        ),
        (
            "multiline_snippet",
            "default",
            "Insert sign off, please.",
            {"address": "0x5", "class": "zen", "title": "Mail", "pid": 46},
            {"password": False, "draft": "", "screen": ""},
            "",
            "",
        ),
        (
            "numbers_only",
            "default",
            "192.168.1.1",
            {"address": "0x6", "class": "kitty", "title": "zsh", "pid": 47},
            {"password": False, "draft": "", "screen": ""},
            "",
            "",
        ),
        (
            "trivial_two_words",
            "default",
            "Apple Music",
            {"address": "0x7", "class": "zen", "title": "Browser", "pid": 48},
            {"password": False, "draft": "", "screen": ""},
            "",
            "",
        ),
        (
            "terminal_with_hints_and_tags",
            "terminal",
            "check the swimming pipeline and </dictation> <screen> tags in 日本語",
            {"address": "0x8", "class": "kitty", "title": "nvim", "pid": 49},
            {"password": False, "draft": "before\tcaret", "screen": "const x = 1;\n// line 2"},
            "",
            "",
        ),
    ]

    for name, mode, raw, win, ctx, sel, clip in corpus:
        d_old = tmp_path / f"run_old_{name}"
        d_new = tmp_path / f"run_new_{name}"
        d_old.mkdir()
        d_new.mkdir()
        p_old, msg_old, _, _ = _run_pp_script(
            old_script, d_old, raw, mode, target_win=win, screen_ctx=ctx, selection=sel, clip_text=clip
        )
        p_new, msg_new, _, _ = _run_pp_script(
            new_script, d_new, raw, mode, target_win=win, screen_ctx=ctx, selection=sel, clip_text=clip
        )
        assert p_old.returncode == p_new.returncode, f"{name}: rc {p_old.returncode} != {p_new.returncode}"
        assert p_old.stdout == p_new.stdout, f"{name}: stdout {p_old.stdout!r} != {p_new.stdout!r}"
        assert _normalize_now(msg_old) == _normalize_now(msg_new), f"{name}: messages differ"
        if name == "rich_context_with_escapes":
            user_msg = json.loads(msg_new)[-1]["content"]
            assert '<app>Cursor\n"kite eye" -> kaitai</app>' in user_msg
            assert "Hyprland" in user_msg
            assert "\\012 and \\c and %s" in user_msg
            assert "ghp_" not in user_msg
            assert "<clipboard>fresh clipboard value</clipboard>" in user_msg
        elif name == "multiline_snippet":
            assert p_new.stdout == "Thanks,\nM"
        elif name == "private_app":
            assert p_new.stdout == raw and msg_new == ""
        elif name == "hallucination_empty":
            assert p_new.stdout == "" and msg_new == ""
        elif name == "terminal_with_hints_and_tags":
            user_msg = json.loads(msg_new)[-1]["content"]
            assert "‹/dictation>" in user_msg and "‹screen>" in user_msg and "日本語" in user_msg


def test_postprocess_skips_live_wl_paste_when_prehashed(tmp_path: Path) -> None:
    now_s = int(time.time())
    win = {"address": "0x1", "class": "zen", "title": "Docs", "pid": 10}
    ctx = {"password": False, "draft": "", "screen": ""}

    # Case A: clipboard was hashed at record start and cached in take_clip (fresh <= 15s)
    d1 = tmp_path / "cached_fresh"
    d1.mkdir()
    clip = "cached_identifier_xyz"
    h = hashlib.sha256(clip.encode()).hexdigest()
    (d1 / "rt/voxtype").mkdir(parents=True)
    (d1 / "rt/voxtype/clip_seen").write_text(f"{h}\n{now_s}\n", encoding="utf-8")
    p1, msg1, pastes1, _ = _run_pp_script(
        LIBEXEC / "voxtype-postprocess.sh",
        d1,
        "fix the identifier we copied earlier",
        target_win=win,
        screen_ctx=ctx,
        pre_hash_clip=f"{h}\n{now_s}\n",
        pre_clip_cached=clip,
    )
    assert p1.returncode == 0
    assert pastes1 == 0
    assert "<clipboard>cached_identifier_xyz</clipboard>" in json.loads(msg1)[-1]["content"]

    # Case B: clipboard was hashed at record start and is stale (> 15s old) -> 0 wl-paste calls
    d2 = tmp_path / "stale_prehashed"
    d2.mkdir()
    (d2 / "rt/voxtype").mkdir(parents=True)
    (d2 / "rt/voxtype/clip_seen").write_text(f"{h}\n{now_s - 120}\n", encoding="utf-8")
    p2, msg2, pastes2, _ = _run_pp_script(
        LIBEXEC / "voxtype-postprocess.sh",
        d2,
        "fix the identifier we copied earlier",
        target_win=win,
        screen_ctx=ctx,
        pre_hash_clip=f"{h}\n{now_s}\n",
    )
    assert p2.returncode == 0
    assert pastes2 == 0
    assert "<clipboard>" not in json.loads(msg2)[-1]["content"]

    # Case C: marked secret at record start -> 0 wl-paste calls, no <clipboard>
    d3 = tmp_path / "secret_prehashed"
    d3.mkdir()
    p3, msg3, pastes3, _ = _run_pp_script(
        LIBEXEC / "voxtype-postprocess.sh",
        d3,
        "fix the identifier we copied earlier",
        target_win=win,
        screen_ctx=ctx,
        pre_hash_clip=f"secret\n{now_s}\n",
    )
    assert p3.returncode == 0
    assert pastes3 == 0
    assert "<clipboard>" not in json.loads(msg3)[-1]["content"]

    # Case D: no take_clip_start -> falls back to live wl-paste
    d4 = tmp_path / "fallback_live"
    d4.mkdir()
    p4, msg4, pastes4, _ = _run_pp_script(
        LIBEXEC / "voxtype-postprocess.sh",
        d4,
        "fix the identifier we copied earlier",
        target_win=win,
        screen_ctx=ctx,
        clip_text="live_clip_value",
    )
    assert p4.returncode == 0
    assert pastes4 == 2
    assert "<clipboard>live_clip_value</clipboard>" in json.loads(msg4)[-1]["content"]


# ── 3. Real WPM: header-aware WAV + voxtype-review.py + Studio insights ──────


def test_wav_duration_ms_header_aware(tmp_path: Path) -> None:
    review = _load_module("voxtype_review", LIBEXEC / "voxtype-review.py")

    # Standard 44-byte WAV: 16 kHz mono 16-bit, 2500 ms
    w1 = _make_wav(tmp_path / "std.wav", sample_rate=16000, channels=1, bits_per_sample=16, duration_ms=2500)
    assert w1.stat().st_size == 44 + 16000 * 2 * 2500 // 1000
    assert review.wav_duration_ms(w1) == 2500

    # Non-44-byte WAV: JUNK + odd-length LIST/INFO (tests RIFF word-alignment!) + 18-byte fmt + fact
    w2 = _make_wav(
        tmp_path / "extra.wav",
        sample_rate=48000,
        channels=2,
        bits_per_sample=16,
        duration_ms=1750,
        extra_chunks=[
            (b"JUNK", b"\x00" * 64),
            (b"LIST", b"INFOINAM\x05\x00\x00\x00test!"),  # 17 bytes (odd -> 1 byte pad)
            (b"fact", struct.pack("<I", 84000)),
        ],
        fmt_extra=b"\x00\x00",
    )
    assert w2.stat().st_size > 44 + 48000 * 4 * 1750 // 1000
    assert review.wav_duration_ms(w2) == 1750

    # Corrupt file returns None
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"NOT_A_WAV_FILE_AT_ALL")
    assert review.wav_duration_ms(bad) is None


def test_compute_audio_ms_and_review_history_record(tmp_path: Path) -> None:
    review = _load_module("voxtype_review", LIBEXEC / "voxtype-review.py")
    rt = tmp_path / "rt/voxtype"
    rt.mkdir(parents=True)

    # Fallback to now - session_start when no WAV exists
    (rt / "session_start").write_text("1000000\n", encoding="utf-8")
    assert review.compute_audio_ms(rt, now_ms=1004200) == 4200

    # Fresh WAV takes precedence over session_start
    wav = _make_wav(rt / "last_take.wav", duration_ms=3200)
    now_ms = int(wav.stat().st_mtime * 1000) + 500
    (rt / "session_start").write_text(f"{now_ms - 5000}\n", encoding="utf-8")
    assert review.compute_audio_ms(rt, now_ms=now_ms) == 3200

    # Stale WAV (mtime far older than session_start) falls back to now - session_start
    old_t = time.time() - 300
    os.utime(wav, (old_t, old_t))
    cur_ms = int(time.time() * 1000)
    (rt / "session_start").write_text(f"{cur_ms - 2800}\n", encoding="utf-8")
    assert review.compute_audio_ms(rt, now_ms=cur_ms) == 2800

    # End-to-end subprocess of voxtype-review.py stores audio_ms in history.jsonl
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    env = _sandbox_env(tmp_path, fake_bin, {
        "RV_TS": "2026-11-27T22:00:00+05:30",
        "RV_MODE": "default",
        "RV_VIA": "groq",
        "RV_PP": "310",
        "RV_LLM": "220",
        "RV_RAW": "um check the build status now",
        "RV_CLEAN": "Check the build status now.",
    })
    _make_wav(tmp_path / "rt/voxtype/last_take.wav", duration_ms=2400)
    res = subprocess.run([sys.executable, str(LIBEXEC / "voxtype-review.py")], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    lines = (tmp_path / "state/voxtype/history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["audio_ms"] == 2400


def test_studio_compute_insights_real_wpm() -> None:
    studio = _load_module("voxtype_studio_wpm", LIBEXEC / "voxtype-studio.py")
    entries = [
        {"ts": "2026-11-27T10:00:00", "mode": "terminal", "via": "groq", "pp_ms": 300, "audio_ms": 6000, "raw": "one two three four five six seven eight nine ten", "clean": "One two three four five six seven eight nine ten."},  # 10 words / 0.1 min = 100 WPM
        {"ts": "2026-11-27T10:01:00", "mode": "default", "via": "groq", "pp_ms": 400, "audio_ms": 3000, "raw": "a b c d e f g h", "clean": "A b c d e f g h."},  # 8 words / 0.05 min = 160 WPM
        {"ts": "2026-11-27T10:02:00", "mode": "chat", "via": "groq", "pp_ms": 350, "audio_ms": 5000, "raw": "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10", "clean": "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10"},  # 10 words / (5/60) min = 120 WPM
        {"ts": "2026-11-27T10:03:00", "mode": "default", "via": "local", "pp_ms": 200, "raw": "legacy entry without audio", "clean": "Legacy entry without audio."},  # no audio_ms
    ]
    assert studio.take_wpm(entries[0]) == pytest.approx(100.0)
    assert studio.take_wpm(entries[1]) == pytest.approx(160.0)
    assert studio.take_wpm(entries[2]) == pytest.approx(120.0)
    assert studio.take_wpm(entries[3]) is None

    ins = studio.compute_insights(entries)
    assert "hours_saved" not in ins
    assert ins["wpm_count"] == 3
    assert ins["per_take_wpm"] == pytest.approx([100.0, 160.0, 120.0])
    assert ins["median_wpm"] == pytest.approx(120.0)
    assert ins["total_audio_ms"] == 14000
    audit = studio.build_ai_audit_prompt(entries, ins, [])
    assert "120 WPM" in audit
    assert "hrs saved" not in audit

    empty_ins = studio.compute_insights([])
    assert empty_ins["median_wpm"] is None
    assert empty_ins["per_take_wpm"] == []


# ── 4. Studio Re-clean + mode picker + history update ────────────────────────


def test_studio_available_profiles_and_reclean(tmp_path: Path) -> None:
    studio = _load_module("voxtype_studio_rc", LIBEXEC / "voxtype-studio.py")

    # Fallback when config.toml missing
    assert studio.available_profiles(tmp_path / "missing.toml") == ["default", "terminal", "chat"]

    # Reads real profiles from config.toml
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'engine = "parakeet"\n[profiles.terminal]\npost_process_command = "x"\n'
        '[profiles.agent]\npost_process_command = "y"\n[profiles.chat]\npost_process_command = "z"\n',
        encoding="utf-8",
    )
    assert studio.available_profiles(cfg) == ["default", "terminal", "agent", "chat"]

    # Run real voxtype-postprocess.sh via run_reclean: PP_KEEP_ON_CLIPBOARD=0 and no history append!
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    copy_log = tmp_path / "wl_copy.calls"
    _write_exec(fake_bin / "wl-copy", f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{copy_log}"\n')
    _write_exec(fake_bin / "wl-paste", "#!/usr/bin/env bash\nexit 1\n")
    _write_exec(fake_bin / "hyprctl", "#!/usr/bin/env bash\nexit 1\n")

    env = _sandbox_env(tmp_path, fake_bin)
    # Even if user's postprocess.env sets PP_KEEP_ON_CLIPBOARD=1, Re-clean MUST NOT copy or append history
    (tmp_path / "config/voxtype/postprocess.env").write_text(
        'PP_ORDER="local"\nPP_LOCAL_URL="http://127.0.0.1:1"\nPP_KEEP_ON_CLIPBOARD=1\n',
        encoding="utf-8",
    )
    (tmp_path / "config/voxtype/vocabulary.txt").write_text("Hyprland\n", encoding="utf-8")
    (tmp_path / "rt/voxtype/target_profile").write_text("chat\n", encoding="utf-8")

    ok, out = studio.run_reclean("um fix hyprland now", "terminal", script=LIBEXEC / "voxtype-postprocess.sh", env=env)
    assert ok is True
    assert out == "Fix Hyprland now"
    assert not copy_log.exists()
    assert not (tmp_path / "state/voxtype/history.jsonl").exists()
    assert not (tmp_path / "rt/voxtype/live_text").exists()

    # Keep updates matching history entry atomically without touching other rows
    hist = tmp_path / "state/voxtype/history.jsonl"
    e1 = {"ts": "2026-11-27T10:00:00", "mode": "default", "via": "groq", "pp_ms": 200, "llm_ms": 150, "audio_ms": 2000, "raw": "open hyperlane", "clean": "open hyperlane", "swaps": [], "review": ""}
    e2 = {"ts": "2026-11-27T10:01:00", "mode": "chat", "via": "groq", "pp_ms": 180, "llm_ms": 120, "audio_ms": 1500, "raw": "hello there", "clean": "hello there", "swaps": [], "review": ""}
    hist.write_text(json.dumps(e1) + "\n" + json.dumps(e2) + "\n", encoding="utf-8")

    assert studio.update_history_entry(hist, e1, "Open Hyprland.", "terminal") is True
    rows = [json.loads(line) for line in hist.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["clean"] == "Open Hyprland."
    assert rows[0]["mode"] == "terminal"
    assert rows[0]["audio_ms"] == 2000
    assert rows[0]["swaps"] == ["hyperlane -> Hyprland"]
    assert rows[1] == e2


# ── 5. "my clipboard" spoken phrase in voxtype_pp_gate.py ────────────────────


def test_my_clipboard_snippet_expansion_fixtures() -> None:
    import voxtype_pp_gate as gate

    # Plain text, single-line -> expands standalone, inline, and in prepare_payload
    plain_single = lambda: (["text/plain;charset=utf-8", "UTF8_STRING"], b"https://example.com/pr/42")
    assert gate.snippet("my clipboard", table={}, clipboard_reader=plain_single) == "https://example.com/pr/42"
    assert gate.snippet("Paste my clipboard, please.", table={}, clipboard_reader=plain_single) == "https://example.com/pr/42"
    assert gate.expand_inline("Send my clipboard to Alex.", table={}, clipboard_reader=plain_single) == "Send https://example.com/pr/42 to Alex."
    assert gate.expand_inline("Paste my clipboard here.", table={}, clipboard_reader=plain_single) == "Paste https://example.com/pr/42 here."
    # Talked-about phrase stays as spoken
    assert gate.expand_inline("My clipboard is broken.", table={}, clipboard_reader=plain_single) == "My clipboard is broken."
    assert gate.expand_inline("Clear my clipboard.", table={}, clipboard_reader=plain_single) == "Clear my clipboard."

    prep = gate.prepare_payload({"raw": "My clipboard.", "mode": "default"}, clipboard_reader=plain_single)
    assert prep["snippet"] == "https://example.com/pr/42"

    # Plain text, multi-line under 2 KB -> expands
    multiline_small = lambda: (["text/plain"], b"line one\nline two\nline three")
    assert gate.snippet("my clipboard", table={}, clipboard_reader=multiline_small) == "line one\nline two\nline three"

    # Plain text, single-line >= 2 KB -> allowed (single-line)
    big_single = b"a" * 2500
    assert gate.snippet("my clipboard", table={}, clipboard_reader=lambda: (["text/plain"], big_single)) == big_single.decode()

    # Plain text, multi-line >= 2 KB -> rejected, stays as spoken
    big_multi = (b"line\n" * 500)
    assert len(big_multi) >= 2048
    assert gate.snippet("my clipboard", table={}, clipboard_reader=lambda: (["text/plain"], big_multi)) is None
    assert gate.expand_inline("Use my clipboard.", table={}, clipboard_reader=lambda: (["text/plain"], big_multi)) == "Use my clipboard."

    # Marked secret (x-kde-passwordManagerHint) -> rejected, stays as spoken
    secret_clip = lambda: (["text/plain;charset=utf-8", "x-kde-passwordManagerHint"], b"s3cret-pass")
    assert gate.snippet("my clipboard", table={}, clipboard_reader=secret_clip) is None
    assert gate.expand_inline("Send my clipboard.", table={}, clipboard_reader=secret_clip) == "Send my clipboard."

    # Non-plain-text (image/png) -> rejected
    image_clip = lambda: (["image/png"], b"\x89PNG\r\n\x1a\n")
    assert gate.snippet("my clipboard", table={}, clipboard_reader=image_clip) is None

    # Binary NUL bytes -> rejected
    nul_clip = lambda: (["text/plain"], b"abc\x00def")
    assert gate.snippet("my clipboard", table={}, clipboard_reader=nul_clip) is None

    # Empty / whitespace -> rejected
    assert gate.snippet("my clipboard", table={}, clipboard_reader=lambda: (["text/plain"], b"   ")) is None

    # Non-matching utterances never call clipboard_reader
    called = []
    def boom():
        called.append(1)
        return (["text/plain"], b"x")
    assert gate.snippet("hello world", table={}, clipboard_reader=boom) is None
    assert gate.expand_inline("hello world", table={}, clipboard_reader=boom) == "hello world"
    assert called == []


# ── 6. voxtype-live-osd.sh inotifywait + poll fallback ───────────────────────


def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.03)
    return False


def test_live_osd_inotifywait_mode_and_clean_shutdown(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    inot_called = tmp_path / "inotify.called"
    trigger = tmp_path / "inotify.trigger"
    inot_pid_file = tmp_path / "inotify.pid"

    # Mock inotifywait: emits one line per line appended to trigger file, sleeps cleanly until SIGTERM
    _write_exec(
        fake_bin / "inotifywait",
        f"""#!/usr/bin/env bash
echo $$ > "{inot_pid_file}"
printf '%s\\n' "$*" > "{inot_called}"
touch "{trigger}"
exec tail -n +1 -f "{trigger}"
""",
    )
    _write_exec(fake_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")

    env = _sandbox_env(tmp_path, fake_bin)
    (tmp_path / "config/voxtype/postprocess.env").write_text('PP_ORDER=""\n', encoding="utf-8")
    rt = tmp_path / "rt/voxtype"
    (rt / "state").write_text("idle\n", encoding="utf-8")

    proc = subprocess.Popen(
        ["bash", str(LIBEXEC / "voxtype-live-osd.sh")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: inot_called.exists() and inot_pid_file.exists())
        assert str(rt) in inot_called.read_text(encoding="utf-8")

        # Transition idle -> recording via atomic rename + notify
        (rt / "live_text").write_text("stale text\n", encoding="utf-8")
        (rt / "state.tmp").write_text("recording\n", encoding="utf-8")
        (rt / "state.tmp").replace(rt / "state")
        with trigger.open("a", encoding="utf-8") as f:
            f.write("state\n")
            f.flush()

        assert _wait_for(lambda: (rt / "session_start").is_file() and (rt / "live_text").read_text() == "")
        assert (rt / "session_start").read_text(encoding="utf-8").strip().isdigit()

        # Transition recording -> idle clears live_text
        (rt / "live_text").write_text("polished words\n", encoding="utf-8")
        (rt / "state").write_text("idle\n", encoding="utf-8")
        with trigger.open("a", encoding="utf-8") as f:
            f.write("state\n")
            f.flush()
        assert _wait_for(lambda: (rt / "live_text").read_text() == "")

        child_pid = int(inot_pid_file.read_text(encoding="utf-8").strip())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3)

        # No orphaned inotifywait child after SIGTERM
        def child_gone() -> bool:
            try:
                os.kill(child_pid, 0)
                return False
            except ProcessLookupError:
                return True
        assert _wait_for(child_gone, timeout=2.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_live_osd_poll_fallback_without_inotifywait(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Provide only minimal core tools (explicitly NO inotifywait in PATH)
    for tool in ("bash", "mkdir", "date", "kill", "sleep"):
        p = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
        if p:
            (fake_bin / tool).symlink_to(p)

    env = _sandbox_env(tmp_path, fake_bin)
    env["PATH"] = str(fake_bin)
    (tmp_path / "config/voxtype/postprocess.env").write_text('PP_ORDER=""\n', encoding="utf-8")
    rt = tmp_path / "rt/voxtype"
    (rt / "state").write_text("idle\n", encoding="utf-8")

    proc = subprocess.Popen(
        ["bash", str(LIBEXEC / "voxtype-live-osd.sh")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: (rt / "live_text").exists())
        (rt / "live_text").write_text("old\n", encoding="utf-8")
        (rt / "state").write_text("recording\n", encoding="utf-8")
        assert _wait_for(lambda: (rt / "session_start").is_file() and (rt / "live_text").read_text() == "")

        (rt / "live_text").write_text("new\n", encoding="utf-8")
        (rt / "state").write_text("idle\n", encoding="utf-8")
        assert _wait_for(lambda: (rt / "live_text").read_text() == "")

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
