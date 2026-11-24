#!/usr/bin/env python3
"""voxtype-edit-watch: learn from the fixes you make right after a dictation.

Started (detached) by voxtype-review.py after a dictation is typed. For up to
five minutes it re-reads the focused text field of the window the dictation
went into, finds the dictated text there and
diffs it against what was typed. A word you replaced with a name-like term
("hyper land" -> "Hyprland") is handed to voxtype_learn.tally(), which applies
the usual non-poisoning guards: it shows up in Studio's Learning Inbox at the
first sighting and is auto-learned at the second (PP_AUTO_LEARN=1).

Only replacements count. Words you add or delete are new content, not a
mishearing, and a rewrite of more than 60% of the take is ignored.

The field is read from voxtype's input method when it holds one
($XDG_RUNTIME_DIR/voxtype/field_text.json, written by the fork's `[output]
read_field` or `preedit` while the field stays focused: no polling, works in
Electron apps that hide from AT-SPI), else through AT-SPI.

Usage (env from voxtype-review.py): RV_RAW, RV_CLEAN; target from
$XDG_RUNTIME_DIR/voxtype/target_window.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RT = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "voxtype"
PIDFILE = RT / "edit_watch.pid"
FIELD = RT / "field_text.json"
LOG = RT / "pp.log"
# Seconds after the dictation at which the field is read again.
TICKS = (2, 4, 6, 10, 15, 25, 40, 60, 90, 120, 180, 240, 300)
MAX_HUNK_WORDS = 3
MAX_HUNKS = 5
MAX_CHANGED = 0.6
MIN_FOUND = 0.6
_STRIP = ".,;:!?\"'()[]{}*_`"


def tokens(s: str) -> tuple[list[str], list[str]]:
    """(lowercase keys, original-cased words) with surrounding punctuation stripped."""
    keys, cased = [], []
    for t in s.split():
        c = t.strip(_STRIP)
        k = re.sub(r"[^\w'.+#-]", "", c.lower()).strip(".-'")
        if k:
            keys.append(k)
            cased.append(c)
    return keys, cased


def name_like(term: str, at_sentence_start: bool) -> bool:
    """A term a recognizer plausibly misses: code/proper-noun casing, digits or
    inner punctuation, or a capitalized word that is not just starting a sentence."""
    if not term or not any(c.isalpha() for c in term):
        return False
    if any(c.isupper() for c in term[1:]) or any(c.isdigit() for c in term):
        return True
    if any(c in ".-_+#" for c in term.strip(".-")):
        return True
    words = term.split()
    if any(w[:1].isupper() for w in words[1:]):
        return True
    return term[:1].isupper() and not at_sentence_start and term.lower() != "i"


def find_hunks(raw: str, clean: str, field: str) -> list[tuple[str, str]] | None:
    """(heard, meant) pairs the user changed, [] when the text is there unchanged,
    None when the dictated text is not in the field (sent, deleted, scrolled away)."""
    import voxtype_learn as learn

    ck, _cc = tokens(clean)
    fk, fc = tokens(field)
    rk, _rc = tokens(raw)
    if len(ck) < 3 or not fk:
        return None
    sm = difflib.SequenceMatcher(a=ck, b=fk, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if sum(b.size for b in blocks) < MIN_FOUND * len(ck):
        return None
    lo = blocks[0].b - blocks[0].a
    hi = blocks[-1].b + blocks[-1].size + (len(ck) - blocks[-1].a - blocks[-1].size)
    lo, hi = max(0, lo), min(len(fk), hi)
    region_k, region_c = fk[lo:hi], fc[lo:hi]
    hunks: list[tuple[str, str]] = []
    changed = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ck, b=region_k, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        changed += max(i2 - i1, j2 - j1)
        if op != "replace":
            continue
        if i2 - i1 > MAX_HUNK_WORDS or j2 - j1 > MAX_HUNK_WORDS:
            continue
        meant = " ".join(region_c[j1:j2]).strip(_STRIP)
        start = j1 == 0 or region_c[j1 - 1][-1:] in ".!?"
        if not name_like(meant, start):
            continue
        heard = " ".join(learn.heard_for(rk, ck, i1, i2))
        if learn.norm(heard) != learn.norm(meant):
            hunks.append((heard, meant))
    if changed > MAX_CHANGED * len(ck) or len(hunks) > MAX_HUNKS:
        return []
    return hunks


def im_field() -> dict | None:
    """The field text voxtype's input method sees, when it holds one."""
    try:
        data = json.loads(FIELD.read_text())
        return {"password": False, "text": (data.get("before") or "") + (data.get("after") or "")}
    except (OSError, ValueError, AttributeError):
        return None


def active_address() -> str | None:
    try:
        out = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True,
                             text=True, timeout=0.5).stdout
        return json.loads(out).get("address")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def log(msg: str) -> None:
    try:
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%T')}  edit-watch  {msg}\n")
    except OSError:
        pass


def _pid_lock():
    """The pid file is read and written under this lock, or two watchers
    starting or ending together each miss the other."""
    import fcntl

    lock = open(PIDFILE.with_suffix(".lock"), "a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def _claim() -> None:
    """One watcher at a time: the newest dictation replaces an older watch."""
    with _pid_lock():
        try:
            old = int(PIDFILE.read_text().strip())
            cmd = Path(f"/proc/{old}/cmdline").read_bytes()
            if old != os.getpid() and b"voxtype_edit_watch" in cmd:
                os.kill(old, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        PIDFILE.write_text(str(os.getpid()))


# What the prompt calls the app the fix was made in.
_APP_NAMES = {"com.t3tools.T3Code": "T3 Code", "code": "VS Code", "code-oss": "VS Code",
              "Slack": "Slack", "discord": "Discord", "obsidian": "Obsidian"}


def _app_name(cls: str) -> str:
    return _APP_NAMES.get(cls) or (cls.rsplit(".", 1)[-1] if cls else "")


def learn_from(hunks: list[tuple[str, str]], app: str = "") -> None:
    import voxtype_learn as learn

    swaps = [f"{h} -> {m}" for h, m in hunks]
    # The meant side is visible in the field the user just edited: that is
    # the grounding is_safe_auto_candidate asks screen terms for.
    terms = " ".join(m for _, m in hunks)
    promoted = learn.tally(swaps, screen_terms=terms)
    log(f"saw {'; '.join(swaps)}" + (f"  learned {len(promoted)}" if promoted else ""))
    # A fix seen once used to wait silently in Studio's inbox: ask now, once.
    ask = learn.unasked(swaps, screen_terms=terms)
    if not ask:
        return
    one = len(ask) == 1
    shown = ask[:3]
    pairs = "\n".join(f"{h} → {m}" for h, m in shown) + (f"\n+{len(ask) - 3} more" if len(ask) > 3 else "")
    where = f"in {app}" if app else "after a dictation"
    choice = learn.notify(
        "Learn this fix?" if one else f"Learn {len(ask)} fixes?",
        pairs,
        actions=[("learn", "Learn" if one else "Learn all"),
                 ("dismiss", "Not a mishearing" if one else "Not mishearings")],
        wait_s=60,
        hints={"x-kuwai-island": "1", "x-kuwai-group": "shuush-learn",
               "x-kuwai-status": f"You fixed {'it' if one else 'them'} by hand {where}",
               "x-kuwai-glyph": "\U000f036c"})
    if choice == "learn":
        refused = [f"{h}: {why}" for h, m in ask if (why := learn.accept_pair(h, m))]
        log(f"learned from the prompt: {pairs.replace(chr(10), '; ')}" + (f"  refused {refused}" if refused else ""))
        learn.notify("Learned" if one else f"Learned {len(ask) - len(refused)} fixes", pairs,
                     hints={"x-kuwai-calm": "1", "x-kuwai-group": "shuush-learn", "x-kuwai-tone": "positive"},
                     transient=True)
    elif choice == "dismiss":
        for h, m in ask:
            learn.reject_pair(h, m)
        log(f"dismissed from the prompt: {pairs.replace(chr(10), '; ')}")


def main() -> int:
    raw, clean = os.environ.get("RV_RAW", ""), os.environ.get("RV_CLEAN", "")
    try:
        target = json.loads((RT / "target_window").read_text())
    except (OSError, ValueError):
        return 0
    pid = int(target.get("pid") or 0)
    cls, title, addr = target.get("class") or "", target.get("title") or "", target.get("address")
    if not clean.strip() or (pid <= 0 and not cls):
        return 0
    _claim()
    import voxtype_screen_ctx as sc

    last: list[tuple[str, str]] = []
    stable: list[tuple[str, str]] = []    # same fix on two reads: not mid-edit
    seen_once = False
    t0 = time.monotonic()
    for tick in TICKS:
        time.sleep(max(0.0, tick - (time.monotonic() - t0)))
        if addr and active_address() not in (None, addr):
            continue
        field = im_field() or sc.focused_field(pid, cls, title)
        if field is None:
            continue
        if field.get("password"):
            return 0
        hunks = find_hunks(raw, clean, field.get("text", ""))
        if hunks is None:
            if seen_once:
                stable = last  # sent or cleared: what was there last is final
                break
            continue
        seen_once = True
        if hunks and hunks == last:
            stable = hunks
            break
        last = hunks
    # Before the prompt, which waits for a click: the next take's watcher
    # must not stop this one while it is asking.
    with _pid_lock():
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except OSError:
            pass
    if stable:
        learn_from(stable, _app_name(cls))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        cases = [
            ("open the hyper land config", "Open the hyper land config.", "Open the Hyprland config.",
             [("hyper land", "Hyprland")]),
            ("open the hyper land config", "Open the hyper land config.", "Open the hyper land config.", []),
            ("we should fix the bounce", "We should fix the bounce.", "We should fix the debounce.", []),
            ("ask grog about it today", "Ask grog about it today.", "Hey, Ask Groq about it today please", [("grog", "Groq")]),
            ("ask grog about it today", "Ask grog about it today.", "something else entirely", None),
            ("ship it on friday with the team", "Ship it on Friday with the team.",
             "Totally different words now here", None),
        ]
        bad = 0
        for raw, clean, field, want in cases:
            got = find_hunks(raw, clean, field)
            if got != want:
                bad += 1
                print(f"FAIL {field!r}: {got!r} != {want!r}")
        print("edit-watch selftest ok" if not bad else f"{bad} failed")
        sys.exit(1 if bad else 0)
    sys.exit(main())
