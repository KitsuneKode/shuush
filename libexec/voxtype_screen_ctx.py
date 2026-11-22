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


def _has_state(node, st) -> bool:
    try:
        ss = node.get_state_set()
        return bool(ss and ss.contains(st))
    except Exception:
        return False


def collect(pid: int, cls: str, title: str, budget_s: float = 0.25, max_nodes: int = 500) -> dict:
    empty = {"password": False, "draft": "", "screen": "", "source": "none"}
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        return empty

    deadline = time.monotonic() + budget_s
    try:
        desktop = Atspi.get_desktop(0)
        count = desktop.get_child_count()
    except Exception:
        return empty

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
        return empty

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

    password = False
    draft = ""
    lines: list[str] = []
    seen: set[str] = set()
    visited = 0
    stack = list(reversed(roots))

    while stack and visited < max_nodes and time.monotonic() < deadline:
        node = stack.pop()
        visited += 1
        try:
            role = node.get_role_name() or ""
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

            if focused and (editable or role in EDITABLE_ROLES):
                raw_draft = _safe_text(node, Atspi.Text, 450)
                if raw_draft.strip():
                    draft = gate.screen_block(raw_draft, lines=6, limit=400)

            if role in TEXT_ROLES:
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
    }


def main(argv: list[str]) -> int:
    pid = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
    cls = argv[2] if len(argv) > 2 else ""
    title = argv[3] if len(argv) > 3 else ""
    budget = float(os.environ.get("PP_SCREEN_BUDGET_S", "0.25"))
    res = collect(pid, cls, title, budget_s=budget)
    sys.stdout.write(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
