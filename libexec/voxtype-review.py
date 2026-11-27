#!/usr/bin/env python3
"""voxtype-review: decide whether the last dictation needs a second look.

Called by voxtype-postprocess.sh (emit) with the history fields in the
environment. Appends the history line, with two extra fields:
  swaps   words the cleanup replaced, as "heard -> written" (deletions such as
          fillers and self-corrections are not swaps)
  review  why a human should check this one, or "" when it can be trusted
and notifies only when cleanup fails (at most once per half hour). The Waybar widget turns
amber and lists the reason and swaps until the next dictation.

Flagged when:
  - cleanup failed and the raw transcript was inserted (via=passthrough),
  - or the model replaced many words (>= 3 swaps, or > 15% of the words),
    which means it guessed a lot from the context.
"""
import difflib
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path


def tokens(s: str) -> tuple[list[str], list[str]]:
    keys, orig = [], []
    for t in s.split():
        cleaned = t.strip(".,;:!?\"'()[]{}")
        k = re.sub(r"[^\w'.+-]", "", cleaned).lower().strip(".")
        if k:
            keys.append(k)
            orig.append(cleaned or k)
    return keys, orig


def wav_duration_ms(path: Path) -> int | None:
    """Compute WAV duration in ms by walking RIFF/WAVE chunks (header-aware).

    Reads byte_rate from the `fmt ` chunk and payload length from the `data`
    chunk, respecting RIFF word-alignment padding rather than assuming a 44-byte
    header.
    """
    try:
        file_size = path.stat().st_size
        if file_size < 12:
            return None
        with path.open("rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            byte_rate = 0
            data_size: int | None = None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    break
                cid = ch[0:4]
                csize = struct.unpack("<I", ch[4:8])[0]
                pos = f.tell()
                if cid == b"fmt " and csize >= 16:
                    fmt = f.read(16)
                    if len(fmt) < 16:
                        break
                    _afmt, channels, sample_rate, br, _ba, bps = struct.unpack("<HHIIHH", fmt)
                    byte_rate = br if br > 0 else sample_rate * channels * (bps // 8)
                elif cid == b"data":
                    rem = max(0, file_size - pos)
                    data_size = rem if csize == 0xFFFFFFFF else min(csize, rem)
                if byte_rate > 0 and data_size is not None:
                    break
                next_pos = pos + csize + (csize & 1)
                if next_pos <= pos or next_pos > file_size:
                    break
                f.seek(next_pos)
            if byte_rate > 0 and data_size is not None and data_size > 0:
                return max(1, int(round(data_size * 1000.0 / byte_rate)))
    except OSError:
        return None
    return None


def compute_audio_ms(rt_dir: Path, now_ms: int | None = None) -> int:
    """Return take audio duration in ms from last_take.wav, falling back to now - session_start."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    start_ms: int | None = None
    start_file = rt_dir / "session_start"
    try:
        raw_start = start_file.read_text(encoding="utf-8").strip()
        if raw_start:
            start_ms = int(raw_start)
    except (OSError, ValueError):
        start_ms = None

    wav = rt_dir / "last_take.wav"
    if wav.is_file():
        wav_fresh = True
        if start_ms is not None:
            try:
                wav_mtime_ms = int(wav.stat().st_mtime * 1000)
                if wav_mtime_ms < start_ms - 2000:
                    wav_fresh = False
            except OSError:
                wav_fresh = False
        if wav_fresh:
            dur = wav_duration_ms(wav)
            if dur is not None:
                return dur

    if start_ms is not None:
        delta = now_ms - start_ms
        if 0 < delta <= 3_600_000:
            return int(delta)
    return 0


def main() -> int:
    e = os.environ
    raw, clean, via = e.get("RV_RAW", ""), e.get("RV_CLEAN", ""), e.get("RV_VIA", "")

    a, _a_orig = tokens(raw)
    b, b_orig = tokens(clean)
    swaps = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "replace":
            swaps.append(f"{' '.join(a[i1:i2])} -> {' '.join(b_orig[j1:j2])}")
    swapped = sum(len(s.split(" -> ")[0].split()) for s in swaps)

    review = ""
    # An edit (command mode) or a snippet is meant to differ from what was said.
    if e.get("RV_MODE") == "command" or via == "snippet":
        swaps = []
    elif via == "passthrough" and len(a) >= 2:
        review = "cleanup failed, inserted exactly as heard"
    elif len(swaps) >= 3 or (a and swapped / len(a) > 0.15):
        review = f"{len(swaps)} words were repaired from context"

    rt_dir = Path(e.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "voxtype"
    audio_ms_env = e.get("RV_AUDIO_MS", "").strip()
    if audio_ms_env.isdigit():
        audio_ms = int(audio_ms_env)
    else:
        t0_env = e.get("RV_T0", "").strip()
        now_ms = int(t0_env) if t0_env.isdigit() else int(time.time() * 1000)
        audio_ms = compute_audio_ms(rt_dir, now_ms=now_ms)

    hist = Path(e.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "voxtype/history.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": e.get("RV_TS"), "mode": e.get("RV_MODE"), "via": via,
            "pp_ms": int(e.get("RV_PP", 0)), "llm_ms": int(e.get("RV_LLM", 0)),
            "audio_ms": audio_ms,
            "raw": raw, "clean": clean, "swaps": swaps, "review": review,
        }, ensure_ascii=False) + "\n")

    # The model's own swaps are not hints. tally() is a no-op kept so this call
    # stays the one place a future learning rule would hook in. A raw fallback
    # has no repairs. Hints come from voxtype-learn fix / add.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import voxtype_learn
    except Exception:
        voxtype_learn = None
    if voxtype_learn and via != "passthrough" and swaps:
        try:
            voxtype_learn.tally(swaps, screen_terms=e.get("RV_SCREEN_TERMS", ""))
        except Exception:
            pass

    # Watch the field for the next few minutes: a name you fix by hand there is
    # the strongest mishearing signal there is (voxtype_edit_watch.py).
    if (voxtype_learn and e.get("RV_MODE") != "command" and via != "snippet" and len(b) >= 3
            and voxtype_learn.env_flag("PP_WATCH_EDITS") and voxtype_learn.env_flag("PP_SCREEN_CONTEXT")):
        try:
            subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve().parent / "voxtype_edit_watch.py")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass

    # Repairs are marked in the pill and kept in history (Studio, the bar widget);
    # a notification for them only repeated it. A failed cleanup is shown in the
    # pill too: the notification is for an outage, once per half hour.
    if via == "passthrough" and review and voxtype_learn:
        mark = Path(e.get("XDG_RUNTIME_DIR", "/tmp")) / "voxtype/pp_fail_notified"
        try:
            recent = (time.time() - mark.stat().st_mtime) < 1800
        except OSError:
            recent = False
        if not recent:
            try:
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.touch()
                subprocess.Popen(
                    [sys.executable, "-I", "-c",
                     "import sys; sys.path.insert(0, sys.argv[1]); import voxtype_learn as l; "
                     "l.notify('Cleanup is not answering', 'Dictations are typed as heard until it is back. '"
                     "'Check the API keys or the network.', hints={'x-kuwai-group': 'shuush-outage'})",
                     str(Path(__file__).resolve().parent)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
