#!/usr/bin/env python3
"""voxtype-edit-watch: learn from the fixes you make right after a dictation.

Started (detached) by voxtype-review.py after a dictation is typed. For up to
five minutes it re-reads the focused text field of the window the dictation
went into, finds the dictated text there and diffs it against what was typed.
A word you replaced with a name-like or vocabulary term ("hyper land" ->
"Hyprland", "box type" -> "voxtype") is handed to voxtype_learn.tally(...,
source="edit-watch"), which applies the usual non-poisoning guards: it shows up
in Studio's Learning Inbox at the first sighting (and asks once) and is
auto-learned at the second (PP_AUTO_LEARN=1).

If a previously learned rule fired on this take and you change its word back,
that revert is recorded via voxtype_learn.record_undo(), which demotes and then
removes rules that keep getting undone.

Never learns from private takes or sensitive fields (password fields,
sensitive_field, field_text.off, password managers, sign-in pages, sudo).

The field is read from the input method when it holds one
($XDG_RUNTIME_DIR/voxtype/field_text.json, written by shuush-ime or the fork's
`[output] read_field` / `preedit` while the field stays focused: works in
Electron apps like T3 Code and browsers like Zen that hide from AT-SPI), else
through AT-SPI.

Usage (env from voxtype-review.py): RV_RAW, RV_CLEAN, RV_VIA, RV_APPLIED_RULES;
target from $XDG_RUNTIME_DIR/voxtype/target_window.
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
FIELD_OFF_PAUSE_S = 300
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


def name_like(
    term: str,
    at_sentence_start: bool,
    common: set[str] | None = None,
    vocab: dict[str, str] | None = None,
) -> bool:
    """A term a recognizer plausibly misses: code/proper-noun casing, digits or
    inner punctuation, a capitalized word that is not an ordinary sentence-start
    word, or a known vocabulary term (even lowercase like voxtype/kuwai)."""
    if not term or not any(c.isalpha() for c in term):
        return False
    if any(c.isupper() for c in term[1:]) or any(c.isdigit() for c in term):
        return True
    if any(c in ".-_+#" for c in term.strip(".-")):
        return True
    ws = term.split()
    if any(w[:1].isupper() for w in ws[1:]):
        return True
    if vocab is not None:
        flat = re.sub(r"[^a-z0-9]", "", term.lower())
        if flat and flat in vocab:
            return True
    if term[:1].isupper() and term.lower() != "i":
        if not at_sentence_start:
            return True
        # At sentence start, a capitalized non-dictionary word ("Hyprland", "Groq",
        # "Shuush") is still unmistakably a proper noun, not mere sentence casing.
        if common is not None and term.lower() not in common:
            return True
    return False


def find_hunks(raw: str, clean: str, field: str) -> list[tuple[str, str]] | None:
    """(heard, meant) pairs the user changed, [] when the text is there unchanged,
    None when the dictated text is not in the field (sent, deleted, scrolled away)."""
    import voxtype_learn as learn

    ck, _cc = tokens(clean)
    fk, fc = tokens(field)
    rk, _rc = tokens(raw)
    if len(ck) < 2 or not fk:
        return None
    sm = difflib.SequenceMatcher(a=ck, b=fk, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    matched = sum(b.size for b in blocks)
    if len(ck) == 2:
        if matched < 1 or len(fk) > 10:
            return None
    elif matched < MIN_FOUND * len(ck):
        return None
    lo = blocks[0].b - blocks[0].a
    hi = blocks[-1].b + blocks[-1].size + (len(ck) - blocks[-1].a - blocks[-1].size)
    lo, hi = max(0, lo), min(len(fk), hi)
    region_k, region_c = fk[lo:hi], fc[lo:hi]
    common = learn._common_words()
    vocab = learn._vocab_map(include_scoped=True)
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
        if not name_like(meant, start, common=common, vocab=vocab):
            continue
        heard = " ".join(learn.heard_for(rk, ck, i1, i2))
        if learn.norm(heard) != learn.norm(meant):
            if len(ck) == 2 and not learn.sounds_alike(heard, meant):
                continue
            hunks.append((heard, meant))
    if changed > MAX_CHANGED * len(ck) or len(hunks) > MAX_HUNKS:
        return []
    return hunks


def find_reverts(
    raw: str,
    clean: str,
    field: str,
    applied_rules: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Detect applied rules (heard -> meant) that the user changed back in the field."""
    import voxtype_learn as learn

    if not applied_rules:
        return []
    ck, _cc = tokens(clean)
    fk, fc = tokens(field)
    rk, _rc = tokens(raw)
    if len(ck) < 2 or not fk:
        return []
    sm = difflib.SequenceMatcher(a=ck, b=fk, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    matched = sum(b.size for b in blocks)
    if len(ck) == 2:
        if matched < 1 or len(fk) > 10:
            return []
    elif matched < MIN_FOUND * len(ck):
        return []
    lo = blocks[0].b - blocks[0].a
    hi = blocks[-1].b + blocks[-1].size + (len(ck) - blocks[-1].a - blocks[-1].size)
    lo, hi = max(0, lo), min(len(fk), hi)
    region_k, region_c = fk[lo:hi], fc[lo:hi]
    rule_map = {(learn.norm(h), learn.norm(m)): (learn.norm(h), m) for h, m in applied_rules if h and m}
    reverts: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ck, b=region_k, autojunk=False).get_opcodes():
        if op != "replace" or i2 - i1 > MAX_HUNK_WORDS or j2 - j1 > MAX_HUNK_WORDS:
            continue
        clean_span = learn.norm(" ".join(ck[i1:i2]))
        field_span = learn.norm(" ".join(region_c[j1:j2]).strip(_STRIP))
        heard_span = learn.norm(" ".join(learn.heard_for(rk, ck, i1, i2)))
        hit = rule_map.get((heard_span, clean_span))
        if hit and field_span != clean_span and hit not in reverts:
            reverts.append(hit)
    return reverts


def sentence_starts(s: str) -> list[bool]:
    """Per word that tokens() keeps: whether it starts a sentence."""
    out, after_end = [], True
    for t in s.split():
        k = re.sub(r"[^\w'.+#-]", "", t.strip(_STRIP).lower()).strip(".-'")
        if k:
            out.append(after_end)
            after_end = t.rstrip("\"')]}*_`")[-1:] in ".!?"
    return out


def find_casing(clean: str, field: str) -> list[str]:
    """Words whose spelling the user fixed only in case ("shuush" ->
    "Shuush"): a name not yet in the vocabulary. Not at a sentence start,
    where a capital says nothing."""
    import voxtype_learn as learn

    ck, cc = tokens(clean)
    fk, fc = tokens(field)
    if len(ck) < 2 or not fk:
        return []
    starts = sentence_starts(field)
    common = learn._common_words()
    vocab = learn._vocab_map(include_scoped=True)
    out: list[str] = []
    for a, b, size in difflib.SequenceMatcher(a=ck, b=fk, autojunk=False).get_matching_blocks():
        for k in range(size):
            said, fixed = cc[a + k], fc[b + k]
            if (said != fixed and said.lower() == fixed.lower() and fixed[:1].isupper()
                    and fixed.lower() not in common and fk[b + k] not in vocab
                    and not (b + k < len(starts) and starts[b + k]) and fixed not in out):
                out.append(fixed)
    return out


def _field_capture_paused(rt: Path | None = None) -> bool:
    rt_dir = rt or RT
    if (rt_dir / "sensitive_field").exists():
        return True
    off = rt_dir / "field_text.off"
    try:
        if off.exists() and (time.time() - off.stat().st_mtime) < FIELD_OFF_PAUSE_S:
            return True
    except OSError:
        pass
    return False


def im_field(field_path: Path | None = None, rt: Path | None = None) -> dict | None:
    """The field text the input method sees, when it holds one."""
    rt_dir = rt or RT
    if _field_capture_paused(rt_dir):
        return {"password": True, "text": ""}
    fp = field_path or (rt_dir / "field_text.json" if rt is not None else FIELD)
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        purpose = str(data.get("purpose") or "").lower()
        is_sensitive = bool(
            data.get("password")
            or data.get("sensitive")
            or purpose in ("password", "pin")
        )
        text = str(data.get("before") or "") + str(data.get("after") or "")
        return {"password": is_sensitive, "text": text}
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

    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
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


# What the prompt calls the app the fix was made in (keys lowercased).
_APP_NAMES = {
    "com.t3tools.t3code": "T3 Code",
    "code": "VS Code",
    "code-oss": "VS Code",
    "cursor": "Cursor",
    "zed": "Zed",
    "zen": "Zen",
    "zen-alpha": "Zen",
    "app.zen_browser.zen": "Zen",
    "kitty": "kitty",
    "ghostty": "Ghostty",
    "slack": "Slack",
    "discord": "Discord",
    "obsidian": "Obsidian",
}


def _app_name(cls: str) -> str:
    if not cls:
        return ""
    return _APP_NAMES.get(cls.lower()) or cls.rsplit(".", 1)[-1]


def _unasked_spellings(words_list: list[str]) -> list[str]:
    """Spellings not offered before (tracked as candidates with no heard
    side, which Studio's inbox skips)."""
    import voxtype_learn as learn

    cands = learn.load_candidates()
    fresh = [w for w in words_list if f"\t{w}" not in cands]
    if fresh:
        for w in fresh:
            cands[f"\t{w}"] = {"n": 1, "asked": True}
        learn.save_candidates(cands)
    return fresh


def learn_from(
    hunks: list[tuple[str, str]],
    app: str = "",
    spellings: list[str] | None = None,
    reverts: list[tuple[str, str]] | None = None,
    notify_fn=None,
) -> dict:
    import voxtype_learn as learn

    notifier = notify_fn or learn.notify
    revert_actions: list[tuple[str, str, str]] = []
    reverted_heard = set()
    for rh, rm in reverts or []:
        act = learn.record_undo(rh, rm)
        revert_actions.append((rh, rm, act))
        reverted_heard.add(learn.norm(rh))
        log(f"reverted {rh} -> {rm}  action={act}")

    # Do not re-learn a hunk that was just reverted back to the heard word.
    filtered_hunks = [(h, m) for h, m in hunks if learn.norm(h) not in reverted_heard]
    swaps = [f"{h} -> {m}" for h, m in filtered_hunks]
    # The meant side is visible in the field the user just edited: that is
    # the grounding is_safe_auto_candidate asks screen terms for.
    terms = " ".join(m for _, m in filtered_hunks)
    via_label = f"edit-watch ({app})" if app else "edit-watch"
    promoted = learn.tally(swaps, screen_terms=terms, source="edit-watch", via=via_label) if swaps else []
    hints = learn.note_hints(swaps, screen_terms=terms, via=via_label) if swaps else []
    if swaps or spellings:
        log(f"saw {'; '.join(swaps + [f'spelling {w}' for w in spellings or []])}"
            + (f"  learned {len(promoted)}" if promoted else "") + (f"  hints {len(hints)}" if hints else ""))
    # A fix seen once used to wait silently in Studio's inbox: ask now, once.
    ask = learn.unasked(swaps, screen_terms=terms) if swaps else []
    spell = _unasked_spellings(spellings or [])
    accepted_prompt: list[tuple[str, str]] = []
    dismissed_prompt: list[tuple[str, str]] = []
    if ask or spell:
        lines = [f"{h} → {m}" for h, m in ask] + [f"{w} (spelling)" for w in spell]
        one = len(lines) == 1
        pairs = "\n".join(lines[:3]) + (f"\n+{len(lines) - 3} more" if len(lines) > 3 else "")
        where = f"in {app}" if app else "after a dictation"
        choice = notifier(
            "Learn this fix?" if one else f"Learn {len(lines)} fixes?",
            pairs,
            actions=[("learn", "Learn" if one else "Learn all"),
                     ("dismiss", "Not a mishearing" if one else "Not mishearings")],
            wait_s=60,
            hints={"x-kuwai-island": "1", "x-kuwai-group": "shuush-learn",
                   "x-kuwai-status": f"You fixed {'it' if one else 'them'} by hand {where}",
                   "x-kuwai-glyph": "\U000f036c"})
        if choice == "learn":
            for w in spell:
                learn._add_vocab_if_distinctive(w)
            refused = []
            for h, m in ask:
                why = learn.accept_pair(h, m, source="edit-watch")
                if why:
                    refused.append(f"{h}: {why}")
                else:
                    accepted_prompt.append((h, m))
            log(f"learned from the prompt: {pairs.replace(chr(10), '; ')}" + (f"  refused {refused}" if refused else ""))
            notifier("Learned" if one else f"Learned {len(lines) - len(refused)} fixes", pairs,
                     hints={"x-kuwai-calm": "1", "x-kuwai-group": "shuush-learn", "x-kuwai-tone": "positive"},
                     transient=True)
        elif choice == "dismiss":
            for h, m in ask:
                learn.reject_pair(h, m)
                dismissed_prompt.append((h, m))
            log(f"dismissed from the prompt: {pairs.replace(chr(10), '; ')}")
    return {
        "promoted": promoted,
        "hints": hints,
        "ask": ask,
        "spellings": spell,
        "accepted_prompt": accepted_prompt,
        "dismissed_prompt": dismissed_prompt,
        "reverts": revert_actions,
    }


def watch_field(
    raw: str,
    clean: str,
    target: dict,
    *,
    via: str = "",
    private: bool = False,
    applied_rules: list[tuple[str, str]] | None = None,
    rt: Path | None = None,
    field_reader=None,
    active_addr_fn=None,
    ticks=TICKS,
    sleep_fn=time.sleep,
    notify_fn=None,
) -> dict:
    """Observe the target field across ticks and learn from stabilized user edits.

    Testable directly with a fake field_text.json stream (no sleeping, no live
    compositor, no D-Bus). Never learns from private takes or sensitive fields.
    """
    import voxtype_pp_gate as gate

    rt_dir = rt or RT
    if private or via == "private" or not clean.strip():
        return {"skipped": "private"}
    if _field_capture_paused(rt_dir):
        return {"skipped": "sensitive_field"}
    pid = int(target.get("pid") or 0)
    cls = str(target.get("class") or "")
    title = str(target.get("title") or "")
    addr = target.get("address")
    if pid <= 0 and not cls:
        return {"skipped": "no_target"}
    if gate.private_reason(cls, title, ""):
        return {"skipped": "private_window"}

    if field_reader is None:
        import voxtype_screen_ctx as sc

        def field_reader():
            return im_field(rt=rt_dir) or sc.focused_field(pid, cls, title)

    addr_fn = active_addr_fn or active_address

    last: list[tuple[str, str]] = []
    stable: list[tuple[str, str]] = []    # same fix on two reads: not mid-edit
    last_spell: list[str] = []
    spellings: list[str] = []
    last_reverts: list[tuple[str, str]] = []
    reverts: list[tuple[str, str]] = []
    seen_once = False
    t0 = time.monotonic()

    for tick in ticks:
        wait = max(0.0, tick - (time.monotonic() - t0))
        if wait > 0:
            sleep_fn(wait)
        if _field_capture_paused(rt_dir):
            return {"skipped": "sensitive_field"}
        if addr and addr_fn() not in (None, addr):
            if seen_once and (last or last_spell or last_reverts):
                # User switched away after making an edit: treat it as final.
                stable, spellings, reverts = last, last_spell, last_reverts
                break
            continue
        field = field_reader()
        if field is None:
            continue
        if field.get("password") or field.get("sensitive"):
            return {"skipped": "password_field"}
        text = str(field.get("text") or "")
        if gate.private_reason(cls, title, text):
            return {"skipped": "private_prompt"}
        hunks = find_hunks(raw, clean, text)
        if hunks is None:
            if seen_once:
                # sent or cleared: what was there last is final
                stable, spellings, reverts = last, last_spell, last_reverts
                break
            continue
        seen_once = True
        spell = find_casing(clean, text)
        rev = find_reverts(raw, clean, text, applied_rules)
        if (hunks or spell or rev) and hunks == last and spell == last_spell and rev == last_reverts:
            stable, spellings, reverts = hunks, spell, rev
            break
        last, last_spell, last_reverts = hunks, spell, rev

    if not (stable or spellings or reverts):
        return {"skipped": "no_edits"}
    return learn_from(stable, _app_name(cls), spellings, reverts=reverts, notify_fn=notify_fn)


def _parse_applied_rules(env_val: str) -> list[tuple[str, str]]:
    if not env_val.strip():
        return []
    try:
        data = json.loads(env_val)
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and item[0] and item[1]:
                out.append((str(item[0]), str(item[1])))
    return out


def main() -> int:
    raw, clean = os.environ.get("RV_RAW", ""), os.environ.get("RV_CLEAN", "")
    via = os.environ.get("RV_VIA", "")
    if via == "private" or os.environ.get("RV_PRIVATE") == "1":
        return 0
    take_id = os.environ.get("RV_TAKE_ID", "")
    if take_id and re.fullmatch(r"[0-9]+", take_id) and (RT / "takes" / take_id / "pp_private").exists():
        return 0
    try:
        target = json.loads(Path(os.environ.get("RV_TARGET", RT / "target_window")).read_text())
    except (OSError, ValueError):
        return 0
    pid = int(target.get("pid") or 0)
    cls = target.get("class") or ""
    if not clean.strip() or (pid <= 0 and not cls):
        return 0
    applied_rules = _parse_applied_rules(os.environ.get("RV_APPLIED_RULES", ""))
    _claim()
    try:
        # Release pid lock before prompt inside learn_from would wait: wait loop runs first.
        res = watch_field(raw, clean, target, via=via, applied_rules=applied_rules)
        _ = res
    finally:
        with _pid_lock():
            try:
                if PIDFILE.read_text().strip() == str(os.getpid()):
                    PIDFILE.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        cases = [
            ("open the hyper land config", "Open the hyper land config.", "Open the Hyprland config.",
             [("hyper land", "Hyprland")]),
            ("hyper land is fast today", "Hyper land is fast today.", "Hyprland is fast today.",
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
        casing = [
            ("Also check the kwarzle pill today.", "Also check the Kwarzle pill today.", ["Kwarzle"]),
            ("Also check the shuush pill today.", "Also check the shuush pill today.", []),
            ("done. the plan is fine here.", "done. The plan is fine here.", []),   # sentence start
            ("we ship the build today.", "we ship the Build today.", []),         # English word
        ]
        for clean, field, want in casing:
            got = find_casing(clean, field)
            if got != want:
                bad += 1
                print(f"FAIL casing {field!r}: {got!r} != {want!r}")
        revs = find_reverts(
            "I went swimming today", "I went streaming today.", "I went swimming today.",
            [("swimming", "streaming")],
        )
        if revs != [("swimming", "streaming")]:
            bad += 1
            print(f"FAIL revert: {revs!r}")
        import voxtype_learn as learn
        for heard, meant, want in [("grog", "Groq", "Groq"), ("get hub", "GitHub", "GitHub"),
                                   ("the", "The", None), ("cargo", "Car", None)]:
            got = learn.hint_candidate(heard, meant)
            if got != want:
                bad += 1
                print(f"FAIL hint {heard!r} -> {meant!r}: {got!r} != {want!r}")
        print("edit-watch selftest ok" if not bad else f"{bad} failed")
        sys.exit(1 if bad else 0)
    sys.exit(main())
