#!/usr/bin/env python3
"""voxtype-review: decide whether the last dictation needs a second look.

Called by voxtype-postprocess.sh (emit) with the history fields in the
environment. Appends the history line, with two extra fields:
  swaps   words the cleanup replaced, as "heard -> written" (deletions such as
          fillers and self-corrections are not swaps)
  review  why a human should check this one, or "" when it can be trusted
and sends a desktop notification when review is set. The Waybar widget turns
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
import subprocess
from pathlib import Path

e = os.environ
raw, clean, via = e.get("RV_RAW", ""), e.get("RV_CLEAN", ""), e.get("RV_VIA", "")


def tokens(s: str) -> tuple[list[str], list[str]]:
    keys, orig = [], []
    for t in s.split():
        cleaned = t.strip(".,;:!?\"'()[]{}")
        k = re.sub(r"[^\w'.+-]", "", cleaned).lower().strip(".")
        if k:
            keys.append(k)
            orig.append(cleaned or k)
    return keys, orig


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

hist = Path(e.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "voxtype/history.jsonl"
hist.parent.mkdir(parents=True, exist_ok=True)
with hist.open("a") as f:
    f.write(json.dumps({
        "ts": e.get("RV_TS"), "mode": e.get("RV_MODE"), "via": via,
        "pp_ms": int(e.get("RV_PP", 0)), "llm_ms": int(e.get("RV_LLM", 0)),
        "raw": raw, "clean": clean, "swaps": swaps, "review": review,
    }, ensure_ascii=False) + "\n")

# The model's own swaps are not hints. tally() is a no-op kept so this call
# stays the one place a future learning rule would hook in. A raw fallback
# has no repairs. Hints come from voxtype-learn fix / add.
import sys
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

if review:
    body = "\n".join(swaps[:6]) if swaps else clean[:120]
    nbin = next(
        (str(p) for p in (
            Path(e.get("KUWAI_ROOT", "")) / "bin/kuwai-notify",
            Path.home() / ".local/share/kuwai/bin/kuwai-notify",
            Path.home() / "Projects/rice/bin/kuwai-notify",
        ) if p.is_file() and os.access(p, os.X_OK)),
        "notify-send",
    )
    try:
        subprocess.Popen(
            # You're looking at the text it just typed: repairs go in the list
            # quietly (low: no toast, no notch); a failed cleanup still says so.
            [nbin, "-a", "Shuush", "-u", "normal" if via == "passthrough" else "low",
             "-i", "audio-input-microphone", f"Check this dictation: {review}", body],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        pass
