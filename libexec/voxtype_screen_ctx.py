#!/usr/bin/env python3
"""Read bounded, redacted screen & draft context from the active window via AT-SPI.

Called by voxtype-postprocess.sh with a hard wall-clock budget so dictation
never stalls. Prints a single JSON object:
  {"password": bool, "draft": str, "screen": str, "source": str}

Only visible/showing text in the target window is collected (skipping inactive
browser tabs), and secrets are redacted before output.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voxtype_pp_gate as gate  # noqa: E402

TEXT_ROLES = {
    "heading",
    "paragraph",
    "static",
    "text",
    "entry",
    "label",
    "list-item",
    "document-web",
    "document-frame",
    "document-text",
    "terminal",
    "link",
    "table-cell",
    "status-bar",
    "notification",
}
EDITABLE_ROLES = {"entry", "text", "document-web", "document-frame", "document-text", "paragraph"}
SKIP_SUBTREE_ROLES = {"page-tab-list", "menu-bar", "menu", "scroll-bar", "tool-bar"}


def _safe_text(node, atspi_text, max_chars: int = 800) -> str:
    try:
        tif = node.get_text_iface()
        if tif is None:
            return ""
        n = atspi_text.get_character_count(tif)
        if n <= 0:
            return ""
        caret = -1
        try:
            caret = atspi_text.get_caret_offset(tif)
        except Exception:
            caret = -1
        if 0 <= caret <= n:
            start = max(0, caret - max_chars)
            end = min(n, caret + 120)
        else:
            start = max(0, n - max_chars)
            end = n
        return atspi_text.get_text(tif, start, end) or ""
    except Exception:
        return ""


def _caret_split(node, atspi_text, before: int, after: int) -> tuple[str, str] | None:
    """(text before the caret, text after it), or None when the caret is unknown."""
    try:
        tif = node.get_text_iface()
        if tif is None:
            return None
        n = atspi_text.get_character_count(tif)
        caret = atspi_text.get_caret_offset(tif)
        if n < 0 or not 0 <= caret <= n:
            return None
        return (atspi_text.get_text(tif, max(0, caret - before), caret) or "",
                atspi_text.get_text(tif, caret, min(n, caret + after)) or "")
    except Exception:
        return None


def _role(node, Atspi) -> str:
    """The role as these sets spell it. AT-SPI names roles with spaces
    ("password text", "document web"); the hyphenated sets never matched,
    so password fields went unnoticed (2026-11-24)."""
    try:
        if node.get_role() == Atspi.Role.PASSWORD_TEXT:
            return "password-text"
    except Exception:
        pass
    return (node.get_role_name() or "").replace(" ", "-")


def _has_state(node, st) -> bool:
    try:
        ss = node.get_state_set()
        return bool(ss and ss.contains(st))
    except Exception:
        return False


def _roots(Atspi, pid: int, cls: str, title: str, deadline: float) -> list:
    """Top-level frames of the target app, best match first ([] when not found)."""
    try:
        desktop = Atspi.get_desktop(0)
        count = desktop.get_child_count()
    except Exception:
        return []
    cls_l = (cls or "").lower()
    title_l = (title or "").lower()
    target_app = None
    fallback_app = None

    for i in range(count):
        if time.monotonic() >= deadline:
            break
        try:
            app = desktop.get_child_at_index(i)
            if not app:
                continue
            if pid > 0 and app.get_process_id() == pid:
                target_app = app
                break
            aname = (app.get_name() or "").lower()
            if cls_l and aname and (aname in cls_l or cls_l in aname):
                fallback_app = fallback_app or app
        except Exception:
            continue

    app = target_app or fallback_app
    if not app:
        return []

    # Prefer the specific top-level frame matching the window title or active state.
    roots = []
    try:
        wcount = min(app.get_child_count(), 20)
        matched = []
        active_wins = []
        for i in range(wcount):
            w = app.get_child_at_index(i)
            if not w:
                continue
            wname = (w.get_name() or "").lower()
            if title_l and wname and (wname == title_l or title_l in wname or wname in title_l):
                matched.append(w)
            elif _has_state(w, Atspi.StateType.ACTIVE):
                active_wins.append(w)
        roots = matched or active_wins or [app]
    except Exception:
        roots = [app]
    return roots


def collect(pid: int, cls: str, title: str, budget_s: float = 0.25, max_nodes: int = 500,
            sensitivity_only: bool = False) -> dict:
    empty = {"password": False, "draft": "", "screen": "", "source": "none"}
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        return empty

    deadline = time.monotonic() + budget_s
    roots = _roots(Atspi, pid, cls, title, deadline)
    if not roots:
        return empty

    password = False
    draft = ""
    before_caret = ""
    lines: list[str] = []
    seen: set[str] = set()
    visited = 0
    stack = list(reversed(roots))

    while stack and visited < max_nodes and time.monotonic() < deadline:
        node = stack.pop()
        visited += 1
        try:
            role = _role(node, Atspi)
            if role in SKIP_SUBTREE_ROLES:
                continue
            ss = node.get_state_set()
            if ss:
                if ss.contains(Atspi.StateType.DEFUNCT):
                    continue
                if role not in ("application", "frame", "window", "panel", "filler", "scroll-pane", "document-web", "document-frame"):
                    if not ss.contains(Atspi.StateType.SHOWING) and not ss.contains(Atspi.StateType.VISIBLE):
                        continue
                focused = ss.contains(Atspi.StateType.FOCUSED)
                editable = ss.contains(Atspi.StateType.EDITABLE)
            else:
                focused = False
                editable = False

            if role == "password-text" and focused:
                password = True
                break

            if not sensitivity_only and focused and (editable or role in EDITABLE_ROLES):
                raw_draft = _safe_text(node, Atspi.Text, 450)
                if raw_draft.strip():
                    draft = gate.screen_block(raw_draft, lines=6, limit=400)
                split = _caret_split(node, Atspi.Text, 80, 0)
                if split is not None:
                    before_caret = split[0]

            if not sensitivity_only and role in TEXT_ROLES:
                txt = _safe_text(node, Atspi.Text, 400) if role in ("paragraph", "document-text", "terminal", "text", "entry") else ""
                if not txt:
                    txt = (node.get_name() or "").strip()
                txt = re.sub(r"\s+", " ", txt).strip()
                if len(txt) >= 3 and txt not in seen:
                    seen.add(txt)
                    lines.append(txt[:240])

            cnt = min(node.get_child_count(), 60)
            for c in range(cnt - 1, -1, -1):
                ch = node.get_child_at_index(c)
                if ch:
                    stack.append(ch)
        except Exception:
            continue

    if password:
        return {"password": True, "draft": "", "screen": "", "source": "atspi"}

    screen = gate.screen_block("\n".join(lines[-30:]), lines=16, limit=1200) if lines else ""
    return {
        "password": False,
        "draft": draft,
        "screen": screen,
        "source": "atspi" if (draft or screen) else "none",
        # Raw, never sent to a model: emit() only reads its last character(s).
        "before_caret": before_caret,
    }


def focused_field(pid: int, cls: str, title: str, budget_s: float = 0.4,
                  max_nodes: int = 800, max_chars: int = 4000) -> dict | None:
    """Text of the focused editable field in the target window.

    {"password": True} for a password field, {"text": ...} otherwise, None when
    no focused editable field was found within the budget.
    """
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        return None
    deadline = time.monotonic() + budget_s
    stack = list(reversed(_roots(Atspi, pid, cls, title, deadline)))
    visited = 0
    while stack and visited < max_nodes and time.monotonic() < deadline:
        node = stack.pop()
        visited += 1
        try:
            role = _role(node, Atspi)
            if role in SKIP_SUBTREE_ROLES:
                continue
            ss = node.get_state_set()
            if ss and ss.contains(Atspi.StateType.DEFUNCT):
                continue
            if ss and ss.contains(Atspi.StateType.FOCUSED):
                if role == "password-text":
                    return {"password": True}
                if ss.contains(Atspi.StateType.EDITABLE) or role in EDITABLE_ROLES:
                    split = _caret_split(node, Atspi.Text, max_chars // 2, max_chars // 2)
                    if split is None:
                        text = _safe_text(node, Atspi.Text, max_chars)
                    else:
                        text = split[0] + split[1]
                    return {"password": False, "text": text}
            if ss and not ss.contains(Atspi.StateType.SHOWING) and role not in (
                    "application", "frame", "window", "panel", "filler", "scroll-pane",
                    "document-web", "document-frame"):
                continue
            for c in range(min(node.get_child_count(), 60) - 1, -1, -1):
                ch = node.get_child_at_index(c)
                if ch:
                    stack.append(ch)
        except Exception:
            continue
    return None


def main(argv: list[str]) -> int:
    pid = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
    cls = argv[2] if len(argv) > 2 else ""
    title = argv[3] if len(argv) > 3 else ""
    budget = float(os.environ.get("PP_SCREEN_BUDGET_S", "0.25"))
    res = collect(pid, cls, title, budget_s=budget, sensitivity_only="--sensitivity-only" in argv[4:])
    sys.stdout.write(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
