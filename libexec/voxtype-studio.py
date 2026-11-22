#!/usr/bin/env python3
"""Voxtype Studio — tactile shadcn/primitive GTK4 + Libadwaita control center
for dictation history, visual diff & 1-click teaching, self-learning inbox,
vocabulary, snippets, and live engine/model/context settings.
"""
from __future__ import annotations

import collections
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import voxtype_learn as learn  # noqa: E402
import voxtype_pp_gate as gate  # noqa: E402

CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxtype"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "voxtype"
RT = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "voxtype"
HISTORY = STATE / "history.jsonl"
VOCAB = CONF / "vocabulary.txt"
SNIPPETS = Path(os.environ.get("VOXTYPE_SNIPPETS", CONF / "snippets"))
PP_ENV = CONF / "postprocess.env"
SECRETS_ENV = CONF / "secrets.env"
CONFIG_TOML = CONF / "config.toml"

CSS = b"""
window, .background {
  background-color: #0b0c0f;
  color: #f4f4f6;
  font-family: "Inter", "IBM Plex Sans", sans-serif;
  font-size: 13px;
}

headerbar {
  background: #0b0c0f;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  box-shadow: none;
  min-height: 50px;
}

.app-title {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: #fafafa;
}

.app-subtitle {
  font-size: 12px;
  color: #71717a;
}

.section-kicker {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.05em;
  color: #71717a;
}

.kpi-card {
  background: #121318;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 14px;
  padding: 13px 15px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04), 0 2px 8px rgba(0, 0, 0, 0.24);
  transition: transform 150ms cubic-bezier(0.23, 1, 0.32, 1), border-color 150ms ease, background-color 150ms ease, box-shadow 150ms ease;
}

.kpi-card-clickable:hover {
  background: #161820;
  border-color: rgba(255, 255, 255, 0.15);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 6px 18px rgba(0, 0, 0, 0.34);
}

.kpi-card-clickable:active {
  transform: scale(0.985);
}

.kpi-label {
  font-size: 11.5px;
  font-weight: 500;
  color: #a1a1aa;
}

.kpi-value {
  font-family: "Inter", "IBM Plex Sans", sans-serif;
  font-size: 22px;
  font-weight: 650;
  letter-spacing: -0.03em;
  color: #fafafa;
  font-feature-settings: "tnum" 1;
}

.kpi-sub {
  font-size: 11.5px;
  color: #71717a;
  font-feature-settings: "tnum" 1;
}

.seg-bar {
  background: #121318;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 10px;
  padding: 3px;
}

.seg-btn {
  background: transparent;
  border: none;
  border-radius: 7px;
  padding: 5px 12px;
  font-size: 12.5px;
  font-weight: 500;
  color: #a1a1aa;
  box-shadow: none;
  min-height: 0;
  transition: transform 140ms cubic-bezier(0.23, 1, 0.32, 1), background-color 130ms ease, color 130ms ease;
}

.seg-btn:hover {
  color: #f4f4f6;
  background: rgba(255, 255, 255, 0.04);
}

.seg-btn:active {
  transform: scale(0.96);
}

button.seg-btn:checked,
.seg-btn:checked {
  background: #232530;
  color: #fafafa;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 1px 3px rgba(0, 0, 0, 0.35);
}

.surface-card {
  background: #121318;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 14px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04), 0 2px 8px rgba(0, 0, 0, 0.24);
}

.drawer-card {
  background: #13151b;
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 16px;
  padding: 16px;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.52), 0 2px 8px rgba(0, 0, 0, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.06);
}

.table-header {
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  padding: 8px 14px;
}

.table-col {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: #71717a;
}

.data-list {
  background: transparent;
}

.data-list > row {
  background: transparent;
  border: 1px solid transparent;
  border-bottom-color: rgba(255, 255, 255, 0.05);
  border-radius: 10px;
  padding: 2px 6px;
  transition: background-color 110ms ease, border-color 110ms ease;
}

.data-list > row:hover {
  background: rgba(255, 255, 255, 0.03);
}

.data-list > row:selected {
  background: #191c25;
  border-color: rgba(255, 255, 255, 0.14);
}

.mono-num {
  font-family: "Fira Code", monospace;
  font-size: 12px;
  color: #a1a1aa;
  font-feature-settings: "tnum" 1;
}

.pill {
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  font-feature-settings: "tnum" 1;
}

.pill-neutral {
  background: rgba(255, 255, 255, 0.07);
  color: #d4d4d8;
}

.pill-clean {
  background: rgba(34, 197, 94, 0.14);
  color: #4ade80;
}

.pill-review {
  background: rgba(245, 158, 11, 0.17);
  color: #fbbf24;
}

.pill-fix {
  background: rgba(96, 165, 250, 0.15);
  color: #93c5fd;
}

.pill-danger {
  background: rgba(244, 63, 94, 0.16);
  color: #fb7185;
}

.dot-chip {
  min-width: 24px;
  min-height: 24px;
  border-radius: 999px;
  padding: 0 7px;
  font-size: 10.5px;
  font-weight: 650;
  background: #232634;
  border: 1px solid rgba(255, 255, 255, 0.12);
  color: #e4e4e7;
  font-feature-settings: "tnum" 1;
}

.swatch-dot {
  min-width: 14px;
  min-height: 14px;
  border-radius: 999px;
}

.btn-primary {
  background: #f4f4f6;
  color: #0b0c0f;
  border: none;
  border-radius: 10px;
  padding: 7px 14px;
  font-weight: 600;
  font-size: 12.5px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6), 0 1px 3px rgba(0, 0, 0, 0.35);
  transition: transform 140ms cubic-bezier(0.23, 1, 0.32, 1), background-color 130ms ease;
}

.btn-primary:hover {
  background: #ffffff;
}

.btn-primary:active {
  transform: scale(0.96);
}

.btn-secondary {
  background: #1b1d26;
  color: #e4e4e7;
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 10px;
  padding: 6px 12px;
  font-weight: 500;
  font-size: 12.5px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  transition: transform 140ms cubic-bezier(0.23, 1, 0.32, 1), background-color 130ms ease, border-color 130ms ease;
}

.btn-secondary:hover {
  background: #232634;
  border-color: rgba(255, 255, 255, 0.18);
}

.btn-secondary:active {
  transform: scale(0.96);
}

.btn-ghost {
  background: transparent;
  color: #a1a1aa;
  border: none;
  border-radius: 8px;
  padding: 5px 9px;
  min-width: 30px;
  min-height: 30px;
  font-size: 12px;
  box-shadow: none;
  transition: transform 140ms cubic-bezier(0.23, 1, 0.32, 1), background-color 130ms ease, color 130ms ease;
}

.btn-ghost:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #f4f4f6;
}

.btn-ghost:active {
  transform: scale(0.96);
}

.btn-primary:focus-visible,
.btn-secondary:focus-visible,
.btn-ghost:focus-visible,
.seg-btn:focus-visible,
.toast-undo:focus-visible,
.data-list > row:focus-visible {
  outline: 2px solid rgba(147, 197, 253, 0.65);
  outline-offset: 2px;
}

.input-field {
  background: #121318;
  color: #f4f4f6;
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 10px;
  padding: 6px 10px;
  min-height: 34px;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.input-field:focus-within {
  border-color: rgba(147, 197, 253, 0.45);
  box-shadow: 0 0 0 2px rgba(96, 165, 250, 0.14);
}

.diff-card {
  background: #0e1015;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 12.5px;
}

.swap-row {
  background: #181a22;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 8px;
  padding: 7px 10px;
  transition: border-color 130ms ease, background-color 130ms ease;
}

.swap-row:hover {
  border-color: rgba(255, 255, 255, 0.13);
}

.sugg-pill-group {
  background: #1b1d26;
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 10px;
  padding: 2px 3px 2px 4px;
}

.editor-card {
  background: #0e1015;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 10px;
  padding: 6px;
}

.editor-card textview, .editor-card textview text {
  background: transparent;
  color: #f4f4f6;
  font-family: "Inter", "IBM Plex Sans", sans-serif;
  font-size: 13.5px;
}

.floating-toast {
  background: #171921;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 999px;
  padding: 8px 12px 8px 16px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.65), 0 2px 8px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.06);
}

.toast-undo {
  background: rgba(255, 255, 255, 0.08);
  color: #fafafa;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 999px;
  padding: 3px 11px;
  font-size: 12px;
  font-weight: 600;
  min-height: 0;
  transition: transform 140ms cubic-bezier(0.23, 1, 0.32, 1), background-color 130ms ease;
}

.toast-undo:hover {
  background: rgba(255, 255, 255, 0.15);
}

.toast-undo:active {
  transform: scale(0.96);
}

.settings-row {
  padding: 10px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

switch {
  min-width: 38px;
  min-height: 22px;
  border-radius: 999px;
  background-color: #27272a;
  border: 1px solid rgba(255, 255, 255, 0.10);
  transition: background-color 140ms ease, border-color 140ms ease;
}

switch:checked {
  background-color: #f4f4f6;
  border-color: #f4f4f6;
}

switch > slider {
  min-width: 16px;
  min-height: 16px;
  margin: 2px;
  border-radius: 999px;
  background-color: #a1a1aa;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
  transition: background-color 140ms ease;
}

switch:checked > slider {
  background-color: #0b0c0f;
}

.chain-item {
  background: #171922;
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 10px;
  padding: 7px 10px;
  transition: border-color 120ms ease, background-color 120ms ease;
}

.chain-item:hover {
  border-color: rgba(255, 255, 255, 0.18);
  background: #1c1f2a;
}

.chain-item.drop-hover {
  border-color: #60a5fa;
  background: rgba(96, 165, 250, 0.10);
}

.drag-handle {
  color: #71717a;
  font-family: "Fira Code", monospace;
  font-size: 13px;
  padding: 0 4px;
}

.rank-badge {
  min-width: 22px;
  min-height: 22px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.08);
  color: #f4f4f6;
  font-family: "Fira Code", monospace;
  font-size: 11px;
  font-weight: 600;
  font-feature-settings: "tnum" 1;
}

.rank-primary {
  background: #f4f4f6;
  color: #0b0c0f;
}

.combo-popover > contents {
  background: #14161d;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 12px;
  padding: 6px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.65), inset 0 1px 0 rgba(255, 255, 255, 0.05);
}

.combo-row {
  border-radius: 6px;
  padding: 6px 8px;
}

.combo-row:hover {
  background: rgba(255, 255, 255, 0.06);
}
"""


# ── File / system helpers ────────────────────────────────────────────────────

def wl_copy(text: str) -> None:
    try:
        subprocess.Popen(
            ["wl-copy", "--", text],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def load_history(limit: int = 2000) -> list[dict]:
    if not HISTORY.exists():
        return []
    out: list[dict] = []
    for line in reversed(HISTORY.read_text(errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if isinstance(d, dict) and d.get("raw"):
                out.append(d)
                if len(out) >= limit:
                    break
        except ValueError:
            continue
    return out


PROVIDERS: list[dict[str, str]] = [
    {"id": "groq", "label": "Groq #1", "model": "qwen/qwen3.8-27b (warm proxy)", "secret": "GROQ_API_KEY"},
    {"id": "groq2", "label": "Groq #2", "model": "qwen/qwen3.8-27b (backup key)", "secret": "GROQ_API_KEY2"},
    {"id": "openrouter", "label": "OpenRouter", "model": "gemma-4-31b-it:free / 26b", "secret": "OPENROUTER_API_KEY"},
    {"id": "opencode", "label": "OpenCode", "model": "space-bunny-free", "secret": "OPENCODE_API_KEY"},
    {"id": "cerebras", "label": "Cerebras", "model": "qwen-3.8-27b", "secret": "CEREBRAS_API_KEY"},
    {"id": "local", "label": "Local Ollama", "model": "qwen3:4b (127.0.0.1:11434)", "secret": ""},
]
PROVIDER_BY_ID = {p["id"]: p for p in PROVIDERS}


def make_pill(text: str, variant: str = "neutral") -> Gtk.Label:
    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    lbl.add_css_class("pill")
    lbl.add_css_class(f"pill-{variant}")
    return lbl


def suggest_from_history(entries: list[dict], existing: dict[str, list]) -> list[tuple[str, str, int]]:
    counts: collections.Counter[tuple[str, str]] = collections.Counter()
    for k, v in learn.load_candidates().items():
        if "\t" in k:
            h, m = k.split("\t", 1)
            canonical = learn.is_safe_auto_candidate(h, m)
            if not canonical:
                continue
            hn = learn.norm(h)
            if hn and hn not in existing:
                n, _ = learn._cand_view(v)
                counts[(hn, canonical)] += max(1, n)
    for d in entries:
        for swap in d.get("swaps") or []:
            if " -> " not in swap:
                continue
            h, m = swap.split(" -> ", 1)
            canonical = learn.is_safe_auto_candidate(h, m)
            if not canonical:
                continue
            hn = learn.norm(h)
            if hn and hn not in existing:
                counts[(hn, canonical)] += 1
    return [(h, m, n) for (h, m), n in counts.most_common(12)]


def read_vocab_entries() -> list[str]:
    if not VOCAB.exists():
        return []
    out = []
    for line in VOCAB.read_text(errors="replace").splitlines():
        term = line.split("#", 1)[0].strip()
        if term:
            out.append(term)
    return out


def add_vocab_term(term: str) -> bool:
    t = term.strip()
    if not t:
        return False
    if t.lower() in {x.lower() for x in read_vocab_entries()}:
        return False
    VOCAB.parent.mkdir(parents=True, exist_ok=True)
    text = VOCAB.read_text(errors="replace") if VOCAB.exists() else "# Vocabulary terms\n"
    if text and not text.endswith("\n"):
        text += "\n"
    tmp = VOCAB.with_suffix(".tmp")
    tmp.write_text(text + t + "\n", encoding="utf-8")
    tmp.replace(VOCAB)
    return True


def remove_vocab_term(term: str) -> bool:
    if not VOCAB.exists():
        return False
    want = term.strip().lower()
    lines = VOCAB.read_text(errors="replace").splitlines(keepends=True)
    kept, removed = [], False
    for line in lines:
        if line.split("#", 1)[0].strip().lower() == want and not removed:
            removed = True
            continue
        kept.append(line)
    if removed:
        tmp = VOCAB.with_suffix(".tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        tmp.replace(VOCAB)
    return removed


def read_snippets_raw() -> list[tuple[str, str]]:
    if not SNIPPETS.exists():
        return []
    rows = []
    for line in SNIPPETS.read_text(errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        trig, _, val = line.partition(":")
        if trig.strip() and val.strip():
            rows.append((trig.strip(), val.strip().replace("\\n", "\n")))
    return rows


def save_snippet(trigger: str, body: str) -> bool:
    trig = re.sub(r"[:\n\r]+", " ", trigger).strip()
    enc = body.strip().replace("\r\n", "\n").replace("\n", "\\n")
    if not trig or not enc:
        return False
    SNIPPETS.parent.mkdir(parents=True, exist_ok=True)
    lines = SNIPPETS.read_text(errors="replace").splitlines(keepends=True) if SNIPPETS.exists() else []
    new_line = f"{trig}: {enc}\n"
    replaced, out = False, []
    for line in lines:
        if not line.lstrip().startswith("#") and ":" in line:
            t, _, _ = line.partition(":")
            if t.strip().lower() == trig.lower():
                if not replaced:
                    out.append(new_line)
                    replaced = True
                continue
        out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(new_line)
    tmp = SNIPPETS.with_suffix(".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    tmp.replace(SNIPPETS)
    return True


def delete_snippet(trigger: str) -> str | None:
    if not SNIPPETS.exists():
        return None
    want = trigger.strip().lower()
    lines = SNIPPETS.read_text(errors="replace").splitlines(keepends=True)
    out, old_body = [], None
    for line in lines:
        if not line.lstrip().startswith("#") and ":" in line:
            t, _, v = line.partition(":")
            if t.strip().lower() == want and old_body is None:
                old_body = v.strip().replace("\\n", "\n")
                continue
        out.append(line)
    if old_body is not None:
        tmp = SNIPPETS.with_suffix(".tmp")
        tmp.write_text("".join(out), encoding="utf-8")
        tmp.replace(SNIPPETS)
    return old_body


def read_env_flag(name: str, default: str = "") -> str:
    if not PP_ENV.exists():
        return default
    val = default
    for line in PP_ENV.read_text(errors="replace").splitlines():
        m = re.match(rf"^{re.escape(name)}=(?:\"([^\"]*)\"|'([^']*)'|([^\s#]+))", line.strip())
        if m:
            val = next(g for g in m.groups() if g is not None)
    return val


def write_env_flag(name: str, value: str, quoted: bool = False) -> None:
    PP_ENV.parent.mkdir(parents=True, exist_ok=True)
    lines = PP_ENV.read_text(errors="replace").splitlines(keepends=True) if PP_ENV.exists() else []
    rendered = f'{name}="{value}"\n' if (quoted or " " in value) else f"{name}={value}\n"
    out, found = [], False
    for line in lines:
        if re.match(rf"^{re.escape(name)}=", line.strip()):
            out.append(rendered)
            found = True
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(rendered)
    tmp = PP_ENV.with_suffix(".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    tmp.replace(PP_ENV)


def configured_secret_keys() -> set[str]:
    if not SECRETS_ENV.exists():
        return set()
    keys = set()
    for line in SECRETS_ENV.read_text(errors="replace").splitlines():
        m = re.match(r"^([A-Z0-9_]+)\s*=\s*\S+", line.strip())
        if m:
            keys.add(m.group(1))
    return keys


def write_secret_key(name: str, value: str) -> None:
    SECRETS_ENV.parent.mkdir(parents=True, exist_ok=True)
    lines = SECRETS_ENV.read_text(errors="replace").splitlines(keepends=True) if SECRETS_ENV.exists() else []
    val = value.strip()
    out, found = [], False
    for line in lines:
        if re.match(rf"^{re.escape(name)}\s*=", line.strip()):
            if val and not found:
                out.append(f'{name}="{val}"\n')
                found = True
            continue
        out.append(line)
    if val and not found:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f'{name}="{val}"\n')
    tmp = SECRETS_ENV.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
    os.chmod(tmp, 0o600)
    tmp.replace(SECRETS_ENV)


def read_toml_bool(section: str, key: str, default: bool = False) -> bool:
    if not CONFIG_TOML.exists():
        return default
    cur = ""
    val = default
    for line in CONFIG_TOML.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        sm = re.match(r"^\[([^\]]+)\]$", s)
        if sm:
            cur = sm.group(1).strip()
            continue
        if cur == section:
            km = re.match(rf"^{re.escape(key)}\s*=\s*(true|false)\b", s)
            if km:
                val = km.group(1) == "true"
    return val


def write_toml_bool(section: str, key: str, value: bool) -> bool:
    if not CONFIG_TOML.exists():
        return False
    lines = CONFIG_TOML.read_text(errors="replace").splitlines(keepends=True)
    cur = ""
    hit_idx = -1
    sec_end = -1
    for i, line in enumerate(lines):
        s = line.strip()
        sm = re.match(r"^\[([^\]]+)\]$", s)
        if sm:
            if cur == section and sec_end == -1:
                sec_end = i
            cur = sm.group(1).strip()
            continue
        if cur == section and re.match(rf"^{re.escape(key)}\s*=\s*(true|false)\b", s):
            hit_idx = i
    bval = "true" if value else "false"
    if hit_idx >= 0:
        lines[hit_idx] = re.sub(
            rf"^(\s*{re.escape(key)}\s*=\s*)(true|false)",
            rf"\g<1>{bval}",
            lines[hit_idx],
        )
    else:
        insert = f"{key} = {bval}\n"
        if sec_end >= 0:
            lines.insert(sec_end, insert)
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"\n[{section}]\n{insert}")
    tmp = CONFIG_TOML.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(CONFIG_TOML)
    return True


def read_toml_str(section: str, key: str, default: str = "") -> str:
    if not CONFIG_TOML.exists():
        return default
    cur, val = "", default
    for line in CONFIG_TOML.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        sm = re.match(r"^\[([^\]]+)\]$", s)
        if sm:
            cur = sm.group(1).strip()
            continue
        if cur == section:
            km = re.match(rf'^{re.escape(key)}\s*=\s*"([^"]*)"', s)
            if km:
                val = km.group(1)
    return val


def write_toml_str(section: str, key: str, value: str) -> bool:
    if not CONFIG_TOML.exists():
        return False
    lines = CONFIG_TOML.read_text(errors="replace").splitlines(keepends=True)
    cur = ""
    hit_idx = -1
    sec_end = -1
    for i, line in enumerate(lines):
        s = line.strip()
        sm = re.match(r"^\[([^\]]+)\]$", s)
        if sm:
            if cur == section and sec_end == -1:
                sec_end = i
            cur = sm.group(1).strip()
            continue
        if cur == section and re.match(rf'^{re.escape(key)}\s*=\s*"[^"]*"', s):
            hit_idx = i
    esc = value.replace("\\", "\\\\").replace('"', '\\"')
    if hit_idx >= 0:
        lines[hit_idx] = re.sub(
            rf'^(\s*{re.escape(key)}\s*=\s*)"[^"]*"',
            rf'\g<1>"{esc}"',
            lines[hit_idx],
        )
    else:
        insert = f'{key} = "{esc}"\n'
        if sec_end >= 0:
            lines.insert(sec_end, insert)
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"\n[{section}]\n{insert}")
    tmp = CONFIG_TOML.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(CONFIG_TOML)
    return True


def a11y_enabled() -> bool:
    try:
        out = subprocess.check_output(
            ["busctl", "--user", "get-property", "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled"],
            text=True,
            timeout=1,
        )
        return "true" in out.lower()
    except Exception:
        return False


def daemon_status() -> tuple[bool, str]:
    st = "unknown"
    try:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "voxtype.service"],
            capture_output=True,
            text=True,
            timeout=1,
        ).stdout.strip()
        if active == "active":
            return True, "active"
        if active:
            st = active
    except Exception:
        pass
    # Fallback for users starting voxtype via compositor exec-once instead of systemd
    pid_file = RT / "pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return True, "running"
        except (ValueError, OSError):
            pass
    return False, st


def diff_markup(raw: str, clean: str) -> str:
    rw, cw = raw.split(), clean.split()
    rk = [re.sub(r"[^\w']", "", w.lower()) for w in rw]
    ck = [re.sub(r"[^\w']", "", w.lower()) for w in cw]
    parts: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=rk, b=ck, autojunk=False).get_opcodes():
        if op == "equal":
            parts.append(html.escape(" ".join(cw[j1:j2])))
        elif op == "delete":
            removed = html.escape(" ".join(rw[i1:i2]))
            parts.append(f'<span foreground="#71717a" strikethrough="true">{removed}</span>')
        elif op == "insert":
            added = html.escape(" ".join(cw[j1:j2]))
            parts.append(f'<span foreground="#4ade80" weight="600">{added}</span>')
        else:
            old = html.escape(" ".join(rw[i1:i2]))
            new = html.escape(" ".join(cw[j1:j2]))
            parts.append(
                f'<span foreground="#fb7185" strikethrough="true">{old}</span> '
                f'<span foreground="#fbbf24" weight="600">{new}</span>'
            )
    return " ".join(p for p in parts if p)


def learn_from_pair(raw: str, clean: str, edited: str) -> list[tuple[str, str]]:
    rw = learn.words(learn.norm(raw))
    cw = learn.words(learn.norm(clean))
    nw = learn.words(learn.norm(edited))
    cased = edited.split()
    learned: list[tuple[str, str]] = []
    for op, j1, j2, k1, k2 in difflib.SequenceMatcher(a=cw, b=nw, autojunk=False).get_opcodes():
        if op != "replace" or j2 - j1 > learn.MAX_SPAN or k2 - k1 > learn.MAX_SPAN:
            continue
        heard = " ".join(learn.heard_for(rw, cw, j1, j2))
        meant = " ".join(cased[k1:k2]) if len(cased) == len(nw) else " ".join(nw[k1:k2])
        meant = meant.strip(".,;:!?")
        if learn.norm(heard) != learn.norm(meant):
            why = learn.remember(heard, meant, "user")
            if not why:
                learned.append((learn.norm(heard), meant))
                if any(ch.isupper() for ch in meant) or "." in meant or "-" in meant:
                    add_vocab_term(meant)
    return learned


# ── Custom sparkline widget (Image 3 style) ──────────────────────────────────

class Sparkline(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[float] = []
        self.set_size_request(86, 30)
        self.set_draw_func(self._draw)

    def set_values(self, vals: list[float]) -> None:
        self.values = vals[-20:]
        self.queue_draw()

    def _draw(self, _area, cr, w: int, h: int) -> None:
        if len(self.values) < 2:
            return
        lo, hi = min(self.values), max(self.values)
        span = max(hi - lo, 1.0)
        pad_x, pad_y = 6.0, 6.0
        pts = []
        for i, v in enumerate(self.values):
            x = pad_x + (w - 2 * pad_x) * (i / (len(self.values) - 1))
            y = (h - pad_y) - (h - 2 * pad_y) * ((v - lo) / span)
            pts.append((x, y))
        cr.set_line_width(1.6)
        cr.set_source_rgba(0.63, 0.65, 0.72, 0.65)
        cr.move_to(*pts[0])
        for p in pts[1:]:
            cr.line_to(*p)
        cr.stroke()
        ex, ey = pts[-1]
        cr.set_source_rgba(0.38, 0.56, 0.98, 1.0)
        cr.arc(ex, ey, 3.2, 0, 6.2831853)
        cr.fill()


class BarChart(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.bars: list[tuple[str, int]] = []
        self.set_size_request(-1, 110)
        self.set_draw_func(self._draw)

    def set_bars(self, bars: list[tuple[str, int]]) -> None:
        self.bars = bars[-14:]
        self.queue_draw()

    def _draw(self, _area, cr, w: int, h: int) -> None:
        if not self.bars:
            return
        n = len(self.bars)
        hi = max((v for _, v in self.bars), default=1) or 1
        pad_x, pad_top, pad_bot = 8.0, 10.0, 20.0
        usable_w = max(10.0, w - 2 * pad_x)
        usable_h = max(10.0, h - pad_top - pad_bot)
        gap = 6.0
        bw = max(4.0, (usable_w - gap * (n - 1)) / n)
        for i, (day, val) in enumerate(self.bars):
            x = pad_x + i * (bw + gap)
            bh = max(3.0, usable_h * (val / hi)) if val > 0 else 2.0
            y = pad_top + usable_h - bh
            if i == n - 1:
                cr.set_source_rgba(0.38, 0.56, 0.98, 0.95)
            elif val > 0:
                cr.set_source_rgba(0.63, 0.65, 0.72, 0.42)
            else:
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.08)
            r = min(4.0, bw / 2, bh / 2)
            cr.new_sub_path()
            cr.arc(x + bw - r, y + r, r, -1.5708, 0)
            cr.arc(x + bw - r, y + bh - r, r, 0, 1.5708)
            cr.arc(x + r, y + bh - r, r, 1.5708, 3.1416)
            cr.arc(x + r, y + r, r, 3.1416, 4.7124)
            cr.close_path()
            cr.fill()
            if i in (0, n // 2, n - 1):
                cr.set_source_rgba(0.45, 0.45, 0.50, 0.9)
                cr.set_font_size(10)
                cr.move_to(x, h - 4)
                cr.show_text(day[5:])


def compute_insights(entries: list[dict]) -> dict:
    total = len(entries)
    words_total = sum(len((e.get("clean") or e.get("raw") or "").split()) for e in entries)
    hours_saved = words_total / (40.0 * 60.0)
    pp = sorted(int(e.get("pp_ms") or 0) for e in entries if e.get("pp_ms"))
    p50 = pp[len(pp) // 2] if pp else 0
    p90 = pp[int(len(pp) * 0.9)] if pp else 0
    fallbacks = sum(1 for e in entries if e.get("via") == "passthrough")
    success_pct = ((total - fallbacks) * 100.0 / total) if total else 100.0
    by_mode = collections.Counter(e.get("mode") or "default" for e in entries)
    by_via = collections.Counter(e.get("via") or "?" for e in entries)
    by_day: collections.Counter[str] = collections.Counter()
    swaps: collections.Counter[str] = collections.Counter()
    for e in entries:
        day = (e.get("ts") or "")[:10]
        if len(day) == 10:
            by_day[day] += len((e.get("clean") or "").split())
        for s in e.get("swaps") or []:
            swaps[s] += 1
    days_sorted = sorted(by_day.items())[-14:]
    return {
        "total": total,
        "words": words_total,
        "hours_saved": hours_saved,
        "p50": p50,
        "p90": p90,
        "success_pct": success_pct,
        "fallbacks": fallbacks,
        "by_mode": by_mode,
        "by_via": by_via,
        "by_day": days_sorted,
        "top_swaps": swaps.most_common(12),
    }


def build_ai_audit_prompt(entries: list[dict], ins: dict, suggs: list[tuple[str, str, int]]) -> str:
    flagged = [e for e in entries if e.get("review")][:10]
    lines = [
        "# Voxtype Dictation Audit",
        f"- Total takes: {ins['total']} | Words: {ins['words']:,} (~{ins['hours_saved']:.1f} hrs saved) | Cleanup success: {ins['success_pct']:.1f}% | Latency P50/P90: {ins['p50']}ms / {ins['p90']}ms",
        f"- Modes: {dict(ins['by_mode'])} | Providers: {dict(ins['by_via'])}",
        "",
        "## Unlearned safe candidates",
    ]
    for h, m, n in suggs[:12]:
        lines.append(f"- {h} -> {m} ({n}x)")
    lines.append("\n## Top recurring swaps")
    for s, c in ins["top_swaps"][:10]:
        lines.append(f"- {c}x: {s}")
    lines.append("\n## Recent review-flagged takes (raw -> clean)")
    for e in flagged[:6]:
        lines.append(f"- [{e.get('mode')}/{e.get('via')}] RAW: {(e.get('raw') or '')[:160]}")
        lines.append(f"  CLEAN: {(e.get('clean') or '')[:160]}")
    lines.append("\nSuggest specific additions to ~/.config/voxtype/vocabulary.txt, corrections.tsv, or prompt rules based on these real errors.")
    return "\n".join(lines)


# ── Main Window ──────────────────────────────────────────────────────────────

class VoxtypeStudioWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, initial_tab: str = "history") -> None:
        super().__init__(application=app, title="Voxtype Studio")
        self.set_default_size(1140, 720)
        self.history: list[dict] = []
        self.selected_entry: dict | None = None
        self.filter_mode = "all"
        self.swap_checks: list[tuple[Gtk.CheckButton, str, str]] = []
        self._toast_timer_id: int | None = None
        self._toast_hovered = False
        self._undo_cb: Callable[[], None] | None = None
        self._reduced_motion = os.environ.get("VOXTYPE_OSD_REDUCED_MOTION") == "1"

        overlay = Gtk.Overlay()
        self.set_content(overlay)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        overlay.set_child(root)

        # Floating bottom pill toast (Sonner principles: asymmetric exit + hover pause)
        self.toast_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.NONE if self._reduced_motion else Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=0 if self._reduced_motion else 220,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.END,
            hexpand=False,
            vexpand=False,
            margin_bottom=20,
        )
        toast_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        toast_box.add_css_class("floating-toast")
        toast_motion = Gtk.EventControllerMotion()
        toast_motion.connect("enter", self._on_toast_hover_enter)
        toast_motion.connect("leave", self._on_toast_hover_leave)
        toast_box.add_controller(toast_motion)
        self.toast_icon = Gtk.Label(label="✓")
        self.toast_icon.add_css_class("pill-clean")
        self.toast_lbl = Gtk.Label(label="")
        self.toast_undo_btn = Gtk.Button(label="↺ Undo")
        self.toast_undo_btn.add_css_class("toast-undo")
        self.toast_undo_btn.connect("clicked", self._on_undo_clicked)
        toast_box.append(self.toast_icon)
        toast_box.append(self.toast_lbl)
        toast_box.append(self.toast_undo_btn)
        self.toast_revealer.set_child(toast_box)
        overlay.add_overlay(self.toast_revealer)
        overlay.set_measure_overlay(self.toast_revealer, False)

        # Top Header
        header = Adw.HeaderBar()
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, valign=Gtk.Align.CENTER)
        t_lbl = Gtk.Label(label="Voxtype Studio", valign=Gtk.Align.CENTER)
        t_lbl.add_css_class("app-title")
        self.hdr_pill = make_pill("checking…", "neutral")
        title_box.append(t_lbl)
        title_box.append(self.hdr_pill)
        header.pack_start(title_box)

        # Segmented navigation tabs
        self.tab_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=120,
            hhomogeneous=False,
            vhomogeneous=False,
        )
        seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        seg.add_css_class("seg-bar")
        self.tab_buttons: dict[str, Gtk.ToggleButton] = {}
        first_btn = None
        for key, label in [
            ("history", "Dictations"),
            ("insights", "Insights"),
            ("corrections", "Learning"),
            ("vocab", "Vocabulary"),
            ("snippets", "Snippets"),
            ("settings", "Settings"),
        ]:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("seg-btn")
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.connect("toggled", self._on_tab_toggled, key)
            self.tab_buttons[key] = btn
            seg.append(btn)
        header.set_title_widget(seg)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload (Ctrl+R)")
        refresh_btn.add_css_class("btn-ghost")
        refresh_btn.connect("clicked", lambda *_: (self.reload_all(), self.show_toast("Refreshed")))
        header.pack_end(refresh_btn)
        root.append(header)

        # Main workspace container
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_top(14)
        body.set_margin_bottom(14)
        body.set_margin_start(18)
        body.set_margin_end(18)
        body.set_vexpand(True)
        root.append(body)

        # 3-Card KPI Strip (Image 3 style)
        body.append(self._build_kpi_strip())

        self.tab_stack.set_vexpand(True)
        self.tab_stack.add_named(self._build_history_page(), "history")
        self.tab_stack.add_named(self._build_insights_page(), "insights")
        self.tab_stack.add_named(self._build_corrections_page(), "corrections")
        self.tab_stack.add_named(self._build_vocab_page(), "vocab")
        self.tab_stack.add_named(self._build_snippets_page(), "snippets")
        self.tab_stack.add_named(self._build_settings_page(), "settings")
        body.append(self.tab_stack)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

        self._live_reload_id: int | None = None
        self._state_reload_id: int | None = None
        self._monitors: list[Gio.FileMonitor] = []

        self.reload_all()
        self.select_tab(initial_tab if initial_tab in self.tab_buttons else "history")
        self._start_live_watchers()

    def _start_live_watchers(self) -> None:
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            hm = Gio.File.new_for_path(str(HISTORY)).monitor_file(Gio.FileMonitorFlags.NONE, None)
            hm.connect("changed", self._on_history_file_changed)
            self._monitors.append(hm)
        except Exception:
            pass
        try:
            RT.mkdir(parents=True, exist_ok=True)
            sm = Gio.File.new_for_path(str(RT / "state")).monitor_file(Gio.FileMonitorFlags.NONE, None)
            sm.connect("changed", self._on_state_file_changed)
            self._monitors.append(sm)
        except Exception:
            pass

    def _on_history_file_changed(self, *_args) -> None:
        if self._live_reload_id is not None:
            GLib.source_remove(self._live_reload_id)
        self._live_reload_id = GLib.timeout_add(160, self._do_live_reload)

    def _do_live_reload(self) -> bool:
        self._live_reload_id = None
        prev_ts = (self.history[0].get("ts") if self.history else None)
        self.reload_all()
        if self.history and self.history[0].get("ts") != prev_ts:
            self.show_toast("New dictation recorded")
        return False

    def _on_state_file_changed(self, *_args) -> None:
        if self._state_reload_id is not None:
            GLib.source_remove(self._state_reload_id)
        self._state_reload_id = GLib.timeout_add(60, self._refresh_live_state)

    def _refresh_live_state(self) -> bool:
        self._state_reload_id = None
        st = ""
        try:
            st = (RT / "state").read_text().strip()
        except Exception:
            pass
        for c in ("pill-clean", "pill-danger", "pill-neutral", "pill-review", "pill-fix"):
            self.hdr_pill.remove_css_class(c)
        if st in ("recording", "streaming"):
            self.hdr_pill.set_text("● Recording…")
            self.hdr_pill.add_css_class("pill-danger")
        elif st == "transcribing":
            self.hdr_pill.set_text("● Polishing…")
            self.hdr_pill.add_css_class("pill-fix")
        else:
            running, d_st = daemon_status()
            self.hdr_pill.set_text("● Daemon live" if running else f"● Daemon {d_st}")
            self.hdr_pill.add_css_class("pill-clean" if running else "pill-danger")
        return False

    def select_tab(self, key: str, *, instant: bool = False) -> None:
        if key in self.tab_buttons:
            dur = 0 if (instant or self._reduced_motion) else 110
            self.tab_stack.set_transition_duration(dur)
            self.tab_buttons[key].set_active(True)
            self.tab_stack.set_visible_child_name(key)

    def _on_tab_toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if btn.get_active():
            if not self._reduced_motion and self.tab_stack.get_visible_child_name() != key:
                self.tab_stack.set_transition_duration(110)
            self.tab_stack.set_visible_child_name(key)

    def _set_drawer_open(self, open_: bool) -> None:
        if not self._reduced_motion:
            # Asymmetric timing: enter 200ms, exit faster at 140ms
            self.drawer_revealer.set_transition_duration(200 if open_ else 140)
        else:
            self.drawer_revealer.set_transition_duration(0)
        self.drawer_revealer.set_reveal_child(open_)

    def _is_text_focused(self) -> bool:
        focus = self.get_focus()
        return isinstance(focus, (Gtk.Entry, Gtk.SearchEntry, Gtk.TextView, Gtk.Text))

    def _on_key(self, _ctrl, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if keyval == Gdk.KEY_Escape:
            if self.tab_stack.get_visible_child_name() == "history" and self.drawer_revealer.get_reveal_child():
                self._set_drawer_open(False)
                self.hist_list.unselect_all()
                return True
            self.close()
            return True
        if ctrl and keyval == Gdk.KEY_w:
            self.close()
            return True
        if ctrl and keyval == Gdk.KEY_r:
            self.reload_all()
            self.show_toast("Refreshed")
            return True
        if ctrl and keyval == Gdk.KEY_f:
            cur = self.tab_stack.get_visible_child_name()
            target = {"history": self.hist_search, "corrections": self.corr_search, "vocab": self.vocab_search}.get(cur)
            if target is not None:
                target.grab_focus()
                return True
        if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.tab_stack.get_visible_child_name() == "history" and self.selected_entry:
                self._on_learn_edit(None)
                return True
        if alt and keyval in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3, Gdk.KEY_4, Gdk.KEY_5, Gdk.KEY_6):
            keys = ["history", "insights", "corrections", "vocab", "snippets", "settings"]
            self.select_tab(keys[keyval - Gdk.KEY_1], instant=True)
            return True
        if not ctrl and not alt and not self._is_text_focused() and self.tab_stack.get_visible_child_name() == "history":
            if keyval in (Gdk.KEY_j, Gdk.KEY_k):
                row = self.hist_list.get_selected_row()
                idx = row.get_index() if row else -1
                nxt = idx + 1 if keyval == Gdk.KEY_j else max(0, idx - 1)
                target_row = self.hist_list.get_row_at_index(nxt)
                if target_row is not None:
                    self.hist_list.select_row(target_row)
                    target_row.grab_focus()
                    return True
        return False

    def show_toast(self, msg: str, undo_cb: Callable[[], None] | None = None) -> None:
        self.toast_lbl.set_text(msg)
        self._undo_cb = undo_cb
        self.toast_undo_btn.set_visible(undo_cb is not None)
        self.toast_revealer.set_transition_duration(0 if self._reduced_motion else 220)
        self.toast_revealer.set_reveal_child(True)
        if self._toast_timer_id is not None:
            GLib.source_remove(self._toast_timer_id)
            self._toast_timer_id = None
        if not self._toast_hovered:
            self._toast_timer_id = GLib.timeout_add(5200, self._hide_toast)

    def _on_toast_hover_enter(self, *_args) -> None:
        self._toast_hovered = True
        if self._toast_timer_id is not None:
            GLib.source_remove(self._toast_timer_id)
            self._toast_timer_id = None

    def _on_toast_hover_leave(self, *_args) -> None:
        self._toast_hovered = False
        if self.toast_revealer.get_reveal_child() and self._toast_timer_id is None:
            self._toast_timer_id = GLib.timeout_add(1800, self._hide_toast)

    def _hide_toast(self) -> bool:
        self._toast_timer_id = None
        self.toast_revealer.set_transition_duration(0 if self._reduced_motion else 150)
        self.toast_revealer.set_reveal_child(False)
        return False

    def _on_undo_clicked(self, _btn) -> None:
        cb = self._undo_cb
        self._undo_cb = None
        self.toast_revealer.set_transition_duration(0 if self._reduced_motion else 140)
        self.toast_revealer.set_reveal_child(False)
        if cb:
            cb()
            self.reload_all()
            self.show_toast("Undone")

    def _make_kpi_clickable(self, card: Gtk.Widget, tab_key: str, tip: str) -> None:
        card.add_css_class("kpi-card-clickable")
        card.set_tooltip_text(tip)
        card.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a, k=tab_key: self.select_tab(k))
        card.add_controller(click)

    # ── KPI Strip ────────────────────────────────────────────────────────
    def _build_kpi_strip(self) -> Gtk.Widget:
        grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)

        # Card 1: Pace + Sparkline
        c1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        c1.add_css_class("kpi-card")
        self._make_kpi_clickable(c1, "insights", "Open Insights & latency breakdown (Alt+2)")
        c1_lbl = Gtk.Label(label="Cleanup latency (median)", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        c1_lbl.add_css_class("kpi-label")
        c1_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.kpi_latency = Gtk.Label(label="—", xalign=0)
        self.kpi_latency.add_css_class("kpi-value")
        self.kpi_prov_pill = Gtk.Label(label="groq")
        self.kpi_prov_pill.add_css_class("pill")
        self.kpi_prov_pill.add_css_class("pill-neutral")
        self.kpi_prov_pill.set_valign(Gtk.Align.CENTER)
        spacer1 = Gtk.Box(hexpand=True)
        self.sparkline = Sparkline()
        c1_row.append(self.kpi_latency)
        c1_row.append(self.kpi_prov_pill)
        c1_row.append(spacer1)
        c1_row.append(self.sparkline)
        c1.append(c1_lbl)
        c1.append(c1_row)

        # Card 2: Review & Learning Queue
        c2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        c2.add_css_class("kpi-card")
        self._make_kpi_clickable(c2, "corrections", "Open Learning inbox & corrections (Alt+3)")
        c2_lbl = Gtk.Label(label="Review & learning queue", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        c2_lbl.add_css_class("kpi-label")
        c2_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.kpi_review_num = Gtk.Label(label="0", xalign=0)
        self.kpi_review_num.add_css_class("kpi-value")
        self.kpi_review_sub = Gtk.Label(label="waiting", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.kpi_review_sub.add_css_class("kpi-sub")
        self.kpi_review_chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.kpi_review_chips.set_valign(Gtk.Align.CENTER)
        c2_row.append(self.kpi_review_num)
        c2_row.append(self.kpi_review_sub)
        c2_row.append(self.kpi_review_chips)
        c2.append(c2_lbl)
        c2.append(c2_row)

        # Card 3: Dictionary & Context
        c3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        c3.add_css_class("kpi-card")
        self._make_kpi_clickable(c3, "vocab", "Open Vocabulary & learned memory (Alt+4)")
        c3_lbl = Gtk.Label(label="Memory & screen context", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        c3_lbl.add_css_class("kpi-label")
        c3_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.kpi_mem_num = Gtk.Label(label="0", xalign=0)
        self.kpi_mem_num.add_css_class("kpi-value")
        self.kpi_mem_sub = Gtk.Label(label="—", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.kpi_mem_sub.add_css_class("kpi-sub")
        self.kpi_a11y_pill = Gtk.Label(label="AT-SPI")
        self.kpi_a11y_pill.add_css_class("pill")
        self.kpi_a11y_pill.set_valign(Gtk.Align.CENTER)
        c3_row.append(self.kpi_mem_num)
        c3_row.append(self.kpi_mem_sub)
        c3_row.append(self.kpi_a11y_pill)
        c3.append(c3_lbl)
        c3.append(c3_row)

        grid.append(c1)
        grid.append(c2)
        grid.append(c3)
        return grid

    # ── Tab 1: Dictations + Contextual Inspector Drawer (Image 3 & 4) ────
    def _build_history_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        # Left: Filter bar + Table card
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        fbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        fbar.add_css_class("seg-bar")
        first_f = None
        for key, label in [("all", "All"), ("review", "Needs review"), ("fixes", "With fixes"), ("fallback", "Fallback")]:
            b = Gtk.ToggleButton(label=label)
            b.add_css_class("seg-btn")
            if first_f is None:
                first_f = b
                b.set_active(True)
            else:
                b.set_group(first_f)
            b.connect("toggled", self._on_filter_toggled, key)
            fbar.append(b)
        toolbar.append(fbar)

        self.hist_search = Gtk.SearchEntry(placeholder_text="Search transcripts… (Ctrl+F)", hexpand=True)
        self.hist_search.add_css_class("input-field")
        self.hist_search.connect("search-changed", lambda *_: self._render_history_list())
        toolbar.append(self.hist_search)
        left.append(toolbar)

        table_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        table_card.add_css_class("surface-card")

        # Column header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hdr.add_css_class("table-header")
        for text, w, exp in [("TIME", 54, False), ("MODE", 70, False), ("TRANSCRIPT", 0, True), ("STATUS", 92, False), ("SPEED", 66, False)]:
            lbl = Gtk.Label(label=text, xalign=0, hexpand=exp)
            lbl.add_css_class("table-col")
            if w:
                lbl.set_size_request(w, -1)
            hdr.append(lbl)
        table_card.append(hdr)

        sc = Gtk.ScrolledWindow(vexpand=True)
        self.hist_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.hist_list.add_css_class("data-list")
        self.hist_list.connect("row-selected", self._on_history_selected)
        sc.set_child(self.hist_list)
        table_card.append(sc)
        left.append(table_card)
        outer.append(left)

        # Right: Contextual Inspector Drawer (Image 4 style)
        self.drawer_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.NONE if self._reduced_motion else Gtk.RevealerTransitionType.SLIDE_LEFT,
            transition_duration=0 if self._reduced_motion else 200,
            reveal_child=False,
            hexpand=False,
        )
        drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=False)
        drawer.add_css_class("drawer-card")
        drawer.set_margin_start(14)
        drawer.set_size_request(420, -1)

        d_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        d_title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.detail_title = Gtk.Label(label="Dictation", xalign=0)
        self.detail_title.add_css_class("app-title")
        self.detail_meta = Gtk.Label(label="", xalign=0, wrap=True)
        self.detail_meta.add_css_class("app-subtitle")
        d_title_box.append(self.detail_title)
        d_title_box.append(self.detail_meta)
        close_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close inspector (Esc)")
        close_btn.add_css_class("btn-ghost")
        close_btn.connect("clicked", lambda *_: (self._set_drawer_open(False), self.hist_list.unselect_all()))
        d_top.append(d_title_box)
        d_top.append(close_btn)
        drawer.append(d_top)

        # Visual inline diff
        diff_lbl = Gtk.Label(label="HEARD → CLEANED DIFF", xalign=0)
        diff_lbl.add_css_class("section-kicker")
        drawer.append(diff_lbl)

        diff_sc = Gtk.ScrolledWindow()
        diff_sc.set_min_content_height(64)
        diff_sc.set_max_content_height(155)
        diff_sc.set_propagate_natural_height(True)
        self.diff_label = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
        self.diff_label.set_use_markup(True)
        self.diff_label.add_css_class("diff-card")
        diff_sc.set_child(self.diff_label)
        drawer.append(diff_sc)

        # Interactive Swap Checklist (Image 4's checkable item cards)
        self.swaps_hdr = Gtk.Label(label="WORD REPAIRS (CHECK TO TEACH)", xalign=0)
        self.swaps_hdr.add_css_class("section-kicker")
        drawer.append(self.swaps_hdr)
        self.swaps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        drawer.append(self.swaps_box)

        # Editable Cleaned Text
        ed_lbl = Gtk.Label(label="EDIT & TEACH ANY MISTAKE", xalign=0)
        ed_lbl.add_css_class("section-kicker")
        drawer.append(ed_lbl)

        ed_sc = Gtk.ScrolledWindow(vexpand=True)
        ed_sc.add_css_class("editor-card")
        self.clean_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.clean_view.set_pixels_above_lines(2)
        self.clean_view.set_pixels_below_lines(2)
        self.clean_view.set_top_margin(6)
        self.clean_view.set_bottom_margin(6)
        self.clean_view.set_left_margin(8)
        self.clean_view.set_right_margin(8)
        ed_sc.set_child(self.clean_view)
        drawer.append(ed_sc)

        # Primary CTA row
        acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.learn_btn = Gtk.Button(label="Learn & Copy", hexpand=True, tooltip_text="Save word edits & copy cleaned text (Ctrl+Enter)")
        self.learn_btn.add_css_class("btn-primary")
        self.learn_btn.connect("clicked", self._on_learn_edit)
        self.replay_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Replay last recorded audio")
        self.replay_btn.add_css_class("btn-secondary")
        self.replay_btn.set_visible(False)
        self.replay_btn.connect("clicked", self._on_replay_audio)
        vocab_btn = Gtk.Button(label="+ Vocab", tooltip_text="Add highlighted word in editor to vocabulary.txt")
        vocab_btn.add_css_class("btn-secondary")
        vocab_btn.connect("clicked", self._on_add_selection_vocab)
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.add_css_class("btn-secondary")
        copy_btn.connect("clicked", self._on_copy_clean)
        acts.append(self.learn_btn)
        acts.append(self.replay_btn)
        acts.append(vocab_btn)
        acts.append(copy_btn)
        drawer.append(acts)

        self.drawer_revealer.set_child(drawer)
        outer.append(self.drawer_revealer)
        return outer

    def _on_filter_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if btn.get_active():
            self.filter_mode = mode
            self._render_history_list()

    def _render_history_list(self) -> None:
        while (row := self.hist_list.get_first_child()) is not None:
            self.hist_list.remove(row)
        q = self.hist_search.get_text().strip().lower()
        shown = 0
        for d in self.history:
            if self.filter_mode == "review" and not d.get("review"):
                continue
            if self.filter_mode == "fixes" and not d.get("swaps"):
                continue
            if self.filter_mode == "fallback" and d.get("via") != "passthrough":
                continue
            if q and q not in (d.get("raw", "") + " " + d.get("clean", "")).lower():
                continue
            shown += 1

            row = Gtk.ListBoxRow()
            row._entry = d  # type: ignore[attr-defined]
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            line.set_margin_top(10)
            line.set_margin_bottom(10)
            line.set_margin_start(8)
            line.set_margin_end(8)

            ts = Gtk.Label(label=(d.get("ts") or "")[11:16] or "—", xalign=0)
            ts.add_css_class("mono-num")
            ts.set_size_request(54, -1)
            line.append(ts)

            mode = Gtk.Label(label=d.get("mode") or "default", xalign=0)
            mode.add_css_class("pill")
            mode.add_css_class("pill-neutral")
            mode.set_size_request(70, -1)
            line.append(mode)

            txt = Gtk.Label(
                label=(d.get("clean") or d.get("raw") or "").replace("\n", " "),
                xalign=0,
                hexpand=True,
                ellipsize=Pango.EllipsizeMode.END,
            )
            line.append(txt)

            status = Gtk.Label()
            status.add_css_class("pill")
            if d.get("via") == "passthrough":
                status.set_text("● Fallback")
                status.add_css_class("pill-danger")
            elif d.get("review"):
                status.set_text("● Review")
                status.add_css_class("pill-review")
            elif d.get("swaps"):
                n_sw = len(d["swaps"])
                status.set_text(f"{n_sw} {'fix' if n_sw == 1 else 'fixes'}")
                status.add_css_class("pill-fix")
            else:
                status.set_text("Clean")
                status.add_css_class("pill-clean")
            status.set_size_request(92, -1)
            line.append(status)

            ms = Gtk.Label(label=f"{d.get('pp_ms', 0)} ms", xalign=1)
            ms.add_css_class("mono-num")
            ms.set_size_request(66, -1)
            line.append(ms)

            row.set_child(line)
            self.hist_list.append(row)
        if shown == 0:
            er = Gtk.ListBoxRow(selectable=False, activatable=False)
            msg = (
                "No matching dictations for this filter."
                if self.history
                else "No dictations yet — press Super+Ctrl+D or hold F9 to speak. Live takes appear here automatically."
            )
            el = Gtk.Label(label=msg, xalign=0, wrap=True)
            el.add_css_class("app-subtitle")
            el.set_margin_top(18)
            el.set_margin_bottom(18)
            el.set_margin_start(14)
            el.set_margin_end(14)
            er.set_child(el)
            self.hist_list.append(er)

    def _on_history_selected(self, _lb, row) -> None:
        if row is None:
            self.selected_entry = None
            return
        d = row._entry
        self.selected_entry = d
        ts = (d.get("ts") or "")[11:16] or "—"
        date = (d.get("ts") or "")[:10]
        self.detail_title.set_text(f"Dictation · {ts}")
        rev = f" · {d['review']}" if d.get("review") else ""
        self.detail_meta.set_text(
            f"{date} · {d.get('mode', '?')} · {d.get('via', '?')} · {d.get('pp_ms', 0)} ms (LLM {d.get('llm_ms', 0)} ms){rev}"
        )
        self.diff_label.set_markup(diff_markup(d.get("raw", ""), d.get("clean", "")))
        self.clean_view.get_buffer().set_text(d.get("clean", ""))

        while (ch := self.swaps_box.get_first_child()) is not None:
            self.swaps_box.remove(ch)
        self.swap_checks.clear()
        existing = learn.load()
        swaps = d.get("swaps") or []
        self.swaps_hdr.set_visible(bool(swaps))
        self.swaps_box.set_visible(bool(swaps))
        for swap in swaps[:6]:
            if " -> " not in swap:
                continue
            h, m = swap.split(" -> ", 1)
            hn = learn.norm(h)
            known = hn in existing
            card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            card.add_css_class("swap-row")
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            title = Gtk.Label(label=f"{h}  →  {m}", xalign=0)
            sub = Gtk.Label(
                label="Already in memory" if known else ("Safe auto candidate" if learn.is_safe_auto_candidate(h, m) else "Check to teach"),
                xalign=0,
            )
            sub.add_css_class("app-subtitle")
            info.append(title)
            info.append(sub)
            chk = Gtk.CheckButton(active=False, sensitive=not known)
            card.append(info)
            card.append(chk)
            self.swaps_box.append(card)
            if not known:
                self.swap_checks.append((chk, h, m))

        is_latest = bool(self.history) and d is self.history[0]
        self.replay_btn.set_visible(is_latest and (RT / "last_take.wav").exists())
        self._set_drawer_open(True)

    def _on_replay_audio(self, _btn) -> None:
        wav = RT / "last_take.wav"
        if not wav.exists():
            self.show_toast("No recent take audio found")
            return
        cmd = None
        if shutil.which("pw-play"):
            cmd = ["pw-play", str(wav)]
        elif shutil.which("paplay"):
            cmd = ["paplay", str(wav)]
        elif shutil.which("mpv"):
            cmd = ["mpv", "--no-video", "--really-quiet", str(wav)]
        elif shutil.which("ffplay"):
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(wav)]
        if not cmd:
            self.show_toast("Install pipewire (pw-play), paplay, or mpv to replay audio")
            return
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.show_toast("Playing last take audio…")
        except OSError:
            self.show_toast("Could not launch audio player")

    def _edited_text(self) -> str:
        buf = self.clean_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()

    def _on_learn_edit(self, _btn) -> None:
        if not self.selected_entry:
            return
        edited = self._edited_text()
        if not edited:
            return
        before_pairs = learn.load()
        learned = learn_from_pair(self.selected_entry.get("raw", ""), self.selected_entry.get("clean", ""), edited)
        for chk, h, m in self.swap_checks:
            if chk.get_active():
                if not learn.remember(h, m, "user"):
                    learned.append((learn.norm(h), m))
        wl_copy(edited)
        if learned:
            summary = ", ".join(f"{h} → {m}" for h, m in learned[:3])
            self.reload_all()
            self.show_toast(f"Learned & copied: {summary}", undo_cb=lambda: learn.save(before_pairs))
        else:
            self.show_toast("Copied to clipboard (edit a word or check a repair to teach)")

    def _on_copy_clean(self, _btn) -> None:
        txt = self._edited_text()
        if txt:
            wl_copy(txt)
            self.show_toast("Copied to clipboard")

    def _on_add_selection_vocab(self, _btn) -> None:
        buf = self.clean_view.get_buffer()
        bounds = buf.get_selection_bounds()
        if not bounds:
            self.show_toast("Highlight a word or phrase in the editor first")
            return
        sel = buf.get_text(bounds[0], bounds[1], True).strip(" .,;:!?\"'()[]{}")
        if not sel:
            return
        if add_vocab_term(sel):
            self._render_vocab()
            self._refresh_kpis()
            self.show_toast(f"Added '{sel}' to vocabulary", undo_cb=lambda s=sel: remove_vocab_term(s))
        else:
            self.show_toast(f"'{sel}' is already in vocabulary")

    # ── Tab 2: Learning & Corrections ────────────────────────────────────
    def _build_corrections_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # Quick add + Sync bar
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.corr_heard = Gtk.Entry(placeholder_text="Heard (e.g. webar)", hexpand=True)
        self.corr_heard.add_css_class("input-field")
        self.corr_meant = Gtk.Entry(placeholder_text="Meant (e.g. Waybar)", hexpand=True)
        self.corr_meant.add_css_class("input-field")
        self.corr_meant.connect("activate", self._on_add_correction)
        add_btn = Gtk.Button(label="Add correction")
        add_btn.add_css_class("btn-primary")
        add_btn.connect("clicked", self._on_add_correction)
        sync_btn = Gtk.Button(label="Sync to daemon", tooltip_text="Copy sure-name rules into config.toml [text] replacements")
        sync_btn.add_css_class("btn-secondary")
        sync_btn.connect("clicked", self._on_sync_toml)
        top.append(self.corr_heard)
        top.append(Gtk.Label(label="→"))
        top.append(self.corr_meant)
        top.append(add_btn)
        top.append(sync_btn)
        box.append(top)

        self.sugg_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.sugg_card.add_css_class("kpi-card")
        box.append(self.sugg_card)

        self.corr_search = Gtk.SearchEntry(placeholder_text="Filter learned corrections…")
        self.corr_search.add_css_class("input-field")
        self.corr_search.connect("search-changed", lambda *_: self._render_corrections())
        box.append(self.corr_search)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        card.add_css_class("surface-card")
        sc = Gtk.ScrolledWindow(vexpand=True)
        self.corr_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.corr_list.add_css_class("data-list")
        sc.set_child(self.corr_list)
        card.append(sc)
        box.append(card)
        return box

    def _on_sync_toml(self, _btn) -> None:
        n = learn.sync_replacements()
        self.show_toast(f"Synced {n} new sure-name rule(s) into config.toml (restart daemon to apply)")

    def _on_add_correction(self, *_args) -> None:
        h = self.corr_heard.get_text().strip()
        m = self.corr_meant.get_text().strip()
        if not h or not m:
            return
        before = learn.load()
        why = learn.remember(h, m, "user")
        if why:
            self.show_toast(f"Refused: {why}")
            return
        self.corr_heard.set_text("")
        self.corr_meant.set_text("")
        self._render_corrections()
        self._refresh_kpis()
        self.show_toast(f"Learned: {learn.norm(h)} → {m}", undo_cb=lambda: learn.save(before))

    def _render_corrections(self) -> None:
        pairs = learn.load()
        while (ch := self.sugg_card.get_first_child()) is not None:
            self.sugg_card.remove(ch)
        suggs = suggest_from_history(self.history, pairs)
        self.sugg_card.set_visible(bool(suggs))
        if suggs:
            hdr = Gtk.Label(label="SELF-LEARNING INBOX (CLICK TO TEACH)", xalign=0)
            hdr.add_css_class("section-kicker")
            self.sugg_card.append(hdr)
            flow = Gtk.FlowBox(
                selection_mode=Gtk.SelectionMode.NONE,
                row_spacing=6,
                column_spacing=6,
                min_children_per_line=1,
                max_children_per_line=3,
            )
            for h, m, n in suggs[:10]:
                grp = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
                grp.add_css_class("sugg-pill-group")
                btn = Gtk.Button(label=f"+ {h} → {m}" + (f" · {n}×" if n > 1 else ""), hexpand=True, tooltip_text="Teach this fix & sync to daemon")
                btn.add_css_class("btn-ghost")
                btn.connect("clicked", self._on_accept_sugg, h, m)
                dis = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Dismiss forever (won't suggest again)")
                dis.add_css_class("btn-ghost")
                dis.connect("clicked", self._on_reject_sugg, h, m)
                grp.append(btn)
                grp.append(dis)
                flow.append(grp)
            self.sugg_card.append(flow)

        while (row := self.corr_list.get_first_child()) is not None:
            self.corr_list.remove(row)
        q = self.corr_search.get_text().strip().lower()
        for h, (m, src, n) in sorted(pairs.items()):
            if q and q not in f"{h} {m}".lower():
                continue
            r = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            line.set_margin_top(8)
            line.set_margin_bottom(8)
            line.set_margin_start(10)
            line.set_margin_end(10)

            hlbl = Gtk.Label(label=h, xalign=0)
            hlbl.add_css_class("mono-num")
            hlbl.set_size_request(150, -1)
            arrow = Gtk.Label(label="→")
            arrow.add_css_class("app-subtitle")
            mlbl = Gtk.Label(label=m, xalign=0, hexpand=True)
            badge = Gtk.Label(label="context hint" if h in gate.AMBIGUOUS else "instant rewrite")
            badge.add_css_class("pill")
            badge.add_css_class("pill-review" if h in gate.AMBIGUOUS else "pill-clean")
            srclbl = Gtk.Label(label=f"{src} · {n}×")
            srclbl.add_css_class("mono-num")
            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("btn-ghost")
            del_btn.connect("clicked", self._on_del_correction, h)

            for w in (hlbl, arrow, mlbl, badge, srclbl, del_btn):
                line.append(w)
            r.set_child(line)
            self.corr_list.append(r)

    def _on_accept_sugg(self, _btn, h: str, m: str) -> None:
        before = learn.load()
        why = learn.remember(h, m, "user")
        if why:
            self.show_toast(f"Refused: {why}")
            return
        learn._add_vocab_if_distinctive(m)
        learn.sync_replacements()
        cands = learn.load_candidates()
        for k in [k for k in cands if k.split("\t", 1)[0] == h]:
            cands.pop(k, None)
        learn.save_candidates(cands)
        self.reload_all()
        self.show_toast(
            f"Learned & synced: {h} → {m}",
            undo_cb=lambda: (learn.save(before), learn.sync_replacements()),
        )

    def _on_reject_sugg(self, _btn, h: str, m: str) -> None:
        key = learn.reject_pair(h, m)
        self.reload_all()
        self.show_toast(f"Dismissed: {h} → {m}", undo_cb=lambda k=key: learn.unreject_pair(k))

    def _on_del_correction(self, _btn, h: str) -> None:
        before = learn.load()
        pairs = learn.load()
        if pairs.pop(h, None) is not None:
            learn.save(pairs)
            self._render_corrections()
            self._refresh_kpis()
            self.show_toast(f"Removed '{h}'", undo_cb=lambda: learn.save(before))

    # ── Tab 3: Vocabulary ────────────────────────────────────────────────
    def _build_vocab_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.vocab_entry = Gtk.Entry(placeholder_text="Exact spelling (e.g. Hyprland, Waybar, Next.js, Kubernetes)", hexpand=True)
        self.vocab_entry.add_css_class("input-field")
        self.vocab_entry.connect("activate", self._on_add_vocab)
        add_btn = Gtk.Button(label="Add term")
        add_btn.add_css_class("btn-primary")
        add_btn.connect("clicked", self._on_add_vocab)
        top.append(self.vocab_entry)
        top.append(add_btn)
        box.append(top)

        self.vocab_search = Gtk.SearchEntry(placeholder_text="Filter vocabulary…")
        self.vocab_search.add_css_class("input-field")
        self.vocab_search.connect("search-changed", lambda *_: self._render_vocab())
        box.append(self.vocab_search)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        card.add_css_class("surface-card")
        sc = Gtk.ScrolledWindow(vexpand=True)
        self.vocab_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.vocab_list.add_css_class("data-list")
        sc.set_child(self.vocab_list)
        card.append(sc)
        box.append(card)
        return box

    def _on_add_vocab(self, *_args) -> None:
        term = self.vocab_entry.get_text().strip()
        if not term:
            return
        if add_vocab_term(term):
            self.vocab_entry.set_text("")
            self._render_vocab()
            self._refresh_kpis()
            self.show_toast(f"Added '{term}'", undo_cb=lambda t=term: remove_vocab_term(t))
        else:
            self.show_toast(f"'{term}' is already in vocabulary")

    def _render_vocab(self) -> None:
        while (row := self.vocab_list.get_first_child()) is not None:
            self.vocab_list.remove(row)
        q = self.vocab_search.get_text().strip().lower()
        for term in sorted(read_vocab_entries(), key=str.lower):
            if q and q not in term.lower():
                continue
            r = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            line.set_margin_top(7)
            line.set_margin_bottom(7)
            line.set_margin_start(10)
            line.set_margin_end(10)
            lbl = Gtk.Label(label=term, xalign=0, hexpand=True)
            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("btn-ghost")
            del_btn.connect("clicked", self._on_del_vocab, term)
            line.append(lbl)
            line.append(del_btn)
            r.set_child(line)
            self.vocab_list.append(r)

    def _on_del_vocab(self, _btn, term: str) -> None:
        if remove_vocab_term(term):
            self._render_vocab()
            self._refresh_kpis()
            self.show_toast(f"Removed '{term}'", undo_cb=lambda t=term: add_vocab_term(t))

    # ── Tab 4: Snippets ──────────────────────────────────────────────────
    def _build_snippets_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        form.add_css_class("kpi-card")
        h = Gtk.Label(label="VOICE SNIPPETS — SAY THE TRIGGER ALONE FOR INSTANT ZERO-LATENCY EXPANSION", xalign=0)
        h.add_css_class("section-kicker")
        form.append(h)

        self.snip_trig = Gtk.Entry(placeholder_text="Voice trigger (e.g. my email, sign off, calendly link)")
        self.snip_trig.add_css_class("input-field")
        form.append(self.snip_trig)

        sc_in = Gtk.ScrolledWindow()
        sc_in.set_min_content_height(78)
        sc_in.add_css_class("editor-card")
        self.snip_body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        sc_in.set_child(self.snip_body)
        form.append(sc_in)

        save_btn = Gtk.Button(label="Save snippet", halign=Gtk.Align.START)
        save_btn.add_css_class("btn-primary")
        save_btn.connect("clicked", self._on_save_snippet)
        form.append(save_btn)
        box.append(form)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        card.add_css_class("surface-card")
        sc = Gtk.ScrolledWindow(vexpand=True)
        self.snip_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.snip_list.add_css_class("data-list")
        sc.set_child(self.snip_list)
        card.append(sc)
        box.append(card)
        return box

    def _on_save_snippet(self, _btn) -> None:
        trig = self.snip_trig.get_text().strip()
        buf = self.snip_body.get_buffer()
        body = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        if save_snippet(trig, body):
            self.snip_trig.set_text("")
            buf.set_text("")
            self._render_snippets()
            self._refresh_kpis()
            self.show_toast(f"Saved snippet '{trig}'", undo_cb=lambda t=trig: delete_snippet(t))
        else:
            self.show_toast("Enter both a trigger and expansion text")

    def _render_snippets(self) -> None:
        while (row := self.snip_list.get_first_child()) is not None:
            self.snip_list.remove(row)
        rows = read_snippets_raw()
        if not rows:
            r = Gtk.ListBoxRow()
            lbl = Gtk.Label(label="No snippets yet — add one above (e.g. 'my email' or 'sign off')", xalign=0)
            lbl.add_css_class("app-subtitle")
            lbl.set_margin_top(14)
            lbl.set_margin_bottom(14)
            lbl.set_margin_start(14)
            r.set_child(lbl)
            self.snip_list.append(r)
            return
        for trig, body in rows:
            r = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            line.set_margin_top(8)
            line.set_margin_bottom(8)
            line.set_margin_start(10)
            line.set_margin_end(10)
            tl = Gtk.Label(label=trig, xalign=0)
            tl.add_css_class("pill")
            tl.add_css_class("pill-neutral")
            bl = Gtk.Label(label=body.replace("\n", " ↵ "), xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            ed = Gtk.Button(icon_name="document-edit-symbolic")
            ed.add_css_class("btn-ghost")
            ed.connect("clicked", lambda _b, t=trig, v=body: (self.snip_trig.set_text(t), self.snip_body.get_buffer().set_text(v)))
            dl = Gtk.Button(icon_name="user-trash-symbolic")
            dl.add_css_class("btn-ghost")
            dl.connect("clicked", self._on_del_snippet, trig)
            for w in (tl, bl, ed, dl):
                line.append(w)
            r.set_child(line)
            self.snip_list.append(r)

    def _on_del_snippet(self, _btn, trig: str) -> None:
        old = delete_snippet(trig)
        if old is not None:
            self._render_snippets()
            self._refresh_kpis()
            self.show_toast(f"Deleted snippet '{trig}'", undo_cb=lambda t=trig, b=old: save_snippet(t, b))

    # ── Tab 5: Settings & Models (replaces needing the TUI) ──────────────
    def _build_settings_page(self) -> Gtk.Widget:
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, homogeneous=True)
        sc.set_child(cols)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        cols.append(left)
        cols.append(right)

        # Card A: Daemon & ASR Engine
        eng = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        eng.add_css_class("kpi-card")
        eh = Gtk.Label(label="DAEMON & SPEECH ENGINE", xalign=0)
        eh.add_css_class("section-kicker")
        eng.append(eh)
        self.eng_status_lbl = Gtk.Label(xalign=0, wrap=True)
        self.eng_status_lbl.add_css_class("mono-num")
        eng.append(self.eng_status_lbl)

        ebtns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rst = Gtk.Button(label="Restart daemon")
        rst.add_css_class("btn-primary")
        rst.connect("clicked", self._on_restart_daemon)
        ebtns.append(rst)
        if (SCRIPTS / "voxtype-engine.sh").exists():
            gpu_b = Gtk.Button(label="GPU mode")
            gpu_b.add_css_class("btn-secondary")
            gpu_b.connect("clicked", lambda *_: self._run_engine("fork"))
            cpu_b = Gtk.Button(label="CPU mode")
            cpu_b.add_css_class("btn-secondary")
            cpu_b.connect("clicked", lambda *_: self._run_engine("fork-cpu"))
            ebtns.append(gpu_b)
            ebtns.append(cpu_b)
        if Path("/usr/bin/voxtype-configure-launcher").exists():
            tui_b = Gtk.Button(label="Open TUI", tooltip_text="Open the voxtype terminal configurator")
            tui_b.add_css_class("btn-ghost")
            tui_b.connect("clicked", self._on_open_tui)
            ebtns.append(tui_b)
        eng.append(ebtns)
        left.append(eng)

        # Card B: LLM Cleanup & Sortable Multi-Select Provider Chain (shadcn Combobox + Sortable)
        llm = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        llm.add_css_class("kpi-card")
        llm_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lh = Gtk.Label(label="CLEANUP LLM & FALLBACK ORDER", xalign=0, hexpand=True)
        lh.add_css_class("section-kicker")
        self.keys_btn = Gtk.MenuButton(label="API keys", tooltip_text="Configure provider API keys in ~/.config/voxtype/secrets.env (chmod 600)")
        self.keys_btn.add_css_class("btn-ghost")
        self.keys_btn.set_popover(self._build_keys_popover())
        self.add_prov_btn = Gtk.MenuButton(label="+ Add provider")
        self.add_prov_btn.add_css_class("btn-secondary")
        self.add_prov_btn.set_popover(self._build_provider_combobox())
        llm_top.append(lh)
        llm_top.append(self.keys_btn)
        llm_top.append(self.add_prov_btn)
        llm.append(llm_top)

        hint = Gtk.Label(label="Drag rows or use arrows to set failover priority. Saves automatically.", xalign=0, wrap=True)
        hint.add_css_class("app-subtitle")
        llm.append(hint)

        self.chain_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        llm.append(self.chain_box)

        # 1-click Air-Gapped Local Ollama mode + model configurator
        off_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        off_row.set_margin_top(2)
        off_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        ol = Gtk.Label(label="Air-gapped offline mode (Ollama only)", xalign=0)
        ol.add_css_class("kpi-label")
        osub = Gtk.Label(label="Zero cloud calls — pins cleanup to 127.0.0.1:11434", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        osub.add_css_class("app-subtitle")
        off_txt.append(ol)
        off_txt.append(osub)
        self.offline_sw = Gtk.Switch(active=(self._current_provider_order() == ["local"]), valign=Gtk.Align.CENTER)
        self.offline_sw.connect("state-set", self._on_toggle_offline)
        off_row.append(off_txt)
        off_row.append(self.offline_sw)
        llm.append(off_row)

        mrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mlbl = Gtk.Label(label="Local Ollama model", xalign=0)
        mlbl.add_css_class("kpi-label")
        self.local_model_entry = Gtk.Entry(placeholder_text="qwen3:4b", hexpand=True)
        self.local_model_entry.add_css_class("input-field")
        self.local_model_entry.set_text(read_env_flag("PP_LOCAL_MODEL", "qwen3:4b"))
        self.local_model_entry.connect("activate", self._on_save_local_model)
        msave = Gtk.Button(label="Save")
        msave.add_css_class("btn-secondary")
        msave.connect("clicked", self._on_save_local_model)
        mrow.append(mlbl)
        mrow.append(self.local_model_entry)
        mrow.append(msave)
        llm.append(mrow)

        # Budget presets + exact spinner (shadcn segmented + numeric primitive)
        brow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        brow.set_margin_top(4)
        blbl = Gtk.Label(label="Timeout budget", xalign=0, hexpand=True)
        blbl.add_css_class("kpi-label")
        self.budget_seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.budget_seg.add_css_class("seg-bar")
        self.budget_btns: dict[int, Gtk.ToggleButton] = {}
        first_b = None
        for ms, lbl in [(3500, "3.5s Fast"), (6000, "6s Balanced"), (9000, "9s Patient")]:
            tb = Gtk.ToggleButton(label=lbl)
            tb.add_css_class("seg-btn")
            if first_b is None:
                first_b = tb
            else:
                tb.set_group(first_b)
            tb.connect("toggled", self._on_budget_preset, ms)
            self.budget_btns[ms] = tb
            self.budget_seg.append(tb)
        brow.append(blbl)
        brow.append(self.budget_seg)
        llm.append(brow)

        srow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        slbl = Gtk.Label(label="Stream cleanup above", xalign=0, hexpand=True)
        slbl.add_css_class("kpi-label")
        self.stream_seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.stream_seg.add_css_class("seg-bar")
        self.stream_btns: dict[int, Gtk.ToggleButton] = {}
        first_s = None
        for w, lbl in [(0, "Always"), (35, "35+ words"), (60, "60+ words")]:
            tb = Gtk.ToggleButton(label=lbl)
            tb.add_css_class("seg-btn")
            if first_s is None:
                first_s = tb
            else:
                tb.set_group(first_s)
            tb.connect("toggled", self._on_stream_preset, w)
            self.stream_btns[w] = tb
            self.stream_seg.append(tb)
        srow.append(slbl)
        srow.append(self.stream_seg)
        llm.append(srow)
        left.append(llm)

        # Card E: Live Dictionary & Gate Sandbox (balances left column + tests rules instantly)
        sb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sb.add_css_class("kpi-card")
        sh = Gtk.Label(label="TEST RULES & VOCABULARY LIVE", xalign=0)
        sh.add_css_class("section-kicker")
        sb.append(sh)
        self.sandbox_in = Gtk.Entry(placeholder_text="Type a misheard phrase (e.g. open webar in next year)…")
        self.sandbox_in.add_css_class("input-field")
        self.sandbox_in.connect("changed", self._on_sandbox_changed)
        self.sandbox_out = Gtk.Label(label="Instant rewrite + vocab + hints preview appears here.", xalign=0, wrap=True, selectable=True)
        self.sandbox_out.add_css_class("diff-card")
        sb.append(self.sandbox_in)
        sb.append(self.sandbox_out)
        left.append(sb)

        # Card C: Context, Privacy & Self-Learning
        ctx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        ctx.add_css_class("surface-card")
        ch = Gtk.Label(label="CONTEXT, PRIVACY & SELF-LEARNING", xalign=0)
        ch.add_css_class("section-kicker")
        ch.set_margin_top(12)
        ch.set_margin_start(14)
        ch.set_margin_bottom(4)
        ctx.append(ch)
        self.env_switches: dict[str, Gtk.Switch] = {}
        for key, title, sub in [
            ("PP_APP_CONTEXT", "App & window title context", "Match project/file terms from active window"),
            ("PP_SCREEN_CONTEXT", "AT-SPI screen, draft & selection", "Read visible window, focused input & highlighted text"),
            ("PP_CLIPBOARD_CONTEXT", "Fresh clipboard context (≤15s)", "Use text copied right before or during dictation"),
            ("PP_PRIVATE_GUARD", "Privacy guard", "Bypass LLM on password fields & sign-in pages"),
            ("PP_AUTO_LEARN", "Safe grounded self-learning", "Auto-learn non-English mishearings seen 2+ times"),
        ]:
            sw = self._make_switch_row(ctx, title, sub, read_env_flag(key, "1") == "1", self._on_toggle_env, key)
            self.env_switches[key] = sw
        right.append(ctx)

        # Card D: Audio, VAD & Output (config.toml)
        aud = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        aud.add_css_class("surface-card")
        ah = Gtk.Label(label="AUDIO, VAD & OUTPUT (CONFIG.TOML)", xalign=0)
        ah.add_css_class("section-kicker")
        ah.set_margin_top(12)
        ah.set_margin_start(14)
        ah.set_margin_bottom(4)
        aud.append(ah)
        for sec, key, title, sub in [
            ("audio", "pause_media", "Pause media while recording", "Auto-pause music players during dictation"),
            ("audio", "duck_media", "Duck media volume (20%)", "Lower background audio when speaking"),
            ("audio.feedback", "enabled", "Start/stop audio cues", "Subtle chime when recording starts & stops"),
            ("vad", "enabled", "Voice Activity Detection", "Drop silence-only taps under 300 ms"),
            ("text", "spoken_punctuation", "Spoken punctuation", "Convert 'period', 'comma', 'question mark'"),
            ("text", "filter_filler_words", "Daemon filler filter", "Strip um/uh before LLM cleanup"),
            ("output.post_process", "streaming", "Stream cleaned sentences", "Type first sentence while LLM finishes"),
            ("output", "shift_enter_newlines", "Shift+Enter for newlines", "Keep Enter from submitting in chat apps"),
        ]:
            self._make_switch_row(aud, title, sub, read_toml_bool(sec, key, False), self._on_toggle_toml, (sec, key))

        # OSD pill position selector
        pos_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        pos_row.add_css_class("settings-row")
        plbl = Gtk.Label(label="OSD pill position", xalign=0, hexpand=True)
        plbl.add_css_class("kpi-label")
        pos_seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        pos_seg.add_css_class("seg-bar")
        self.osd_pos_btns: dict[str, Gtk.ToggleButton] = {}
        first_p = None
        for pkey, plab in [("bottom-center", "Bottom"), ("top-center", "Top"), ("bottom-right", "Corner")]:
            tb = Gtk.ToggleButton(label=plab)
            tb.add_css_class("seg-btn")
            if first_p is None:
                first_p = tb
            else:
                tb.set_group(first_p)
            tb.connect("toggled", self._on_osd_pos_preset, pkey)
            self.osd_pos_btns[pkey] = tb
            pos_seg.append(tb)
        pos_row.append(plbl)
        pos_row.append(pos_seg)
        aud.append(pos_row)
        right.append(aud)

        return sc

    def _make_switch_row(self, parent: Gtk.Box, title: str, sub: str, initial: bool, cb, arg) -> Gtk.Switch:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("settings-row")
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        t = Gtk.Label(label=title, xalign=0)
        s = Gtk.Label(label=sub, xalign=0, wrap=True)
        s.add_css_class("app-subtitle")
        txt.append(t)
        txt.append(s)
        sw = Gtk.Switch(active=initial, valign=Gtk.Align.CENTER)
        sw.connect("state-set", cb, arg)
        row.append(txt)
        row.append(sw)
        parent.append(row)
        return sw

    def _on_toggle_env(self, _sw: Gtk.Switch, state: bool, key: str) -> bool:
        write_env_flag(key, "1" if state else "0")
        self.show_toast(f"{key} = {'on' if state else 'off'}")
        return False

    def _on_toggle_toml(self, _sw: Gtk.Switch, state: bool, sec_key: tuple[str, str]) -> bool:
        sec, key = sec_key
        if write_toml_bool(sec, key, state):
            self.show_toast(f"Saved [{sec}] {key} = {str(state).lower()} (Restart daemon to apply)")
        return False

    def _build_keys_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.add_css_class("combo-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_size_request(340, -1)
        hdr = Gtk.Label(label="PROVIDER API KEYS (SAVED CHMOD 600)", xalign=0)
        hdr.add_css_class("section-kicker")
        box.append(hdr)
        sub = Gtk.Label(label="Stored locally in ~/.config/voxtype/secrets.env. Leave blank to keep unchanged.", xalign=0, wrap=True)
        sub.add_css_class("app-subtitle")
        box.append(sub)
        self._key_entries: dict[str, Gtk.PasswordEntry] = {}
        self._key_pills: dict[str, Gtk.Label] = {}
        for p in PROVIDERS:
            sec = p["secret"]
            if not sec:
                continue
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lbl = Gtk.Label(label=f"{p['label']} ({sec})", xalign=0, hexpand=True)
            lbl.add_css_class("kpi-label")
            st = make_pill("○ Missing", "neutral")
            top.append(lbl)
            top.append(st)
            ent = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text=f"Paste {sec}…")
            ent.add_css_class("input-field")
            r.append(top)
            r.append(ent)
            box.append(r)
            self._key_entries[sec] = ent
            self._key_pills[sec] = st
        save_btn = Gtk.Button(label="Save keys")
        save_btn.add_css_class("btn-primary")
        save_btn.connect("clicked", lambda _b: (self._on_save_keys(), pop.popdown()))
        box.append(save_btn)
        pop.set_child(box)
        pop.connect("show", lambda *_: self._refresh_key_pills())
        return pop

    def _refresh_key_pills(self) -> None:
        keys = configured_secret_keys()
        for sec, pill in getattr(self, "_key_pills", {}).items():
            ok = sec in keys
            pill.set_text("● Ready" if ok else "○ Missing")
            for c in ("pill-clean", "pill-neutral"):
                pill.remove_css_class(c)
            pill.add_css_class("pill-clean" if ok else "pill-neutral")

    def _on_save_keys(self) -> None:
        saved = 0
        for sec, ent in getattr(self, "_key_entries", {}).items():
            val = ent.get_text().strip()
            if val:
                write_secret_key(sec, val)
                ent.set_text("")
                saved += 1
        if saved:
            self._refresh_key_pills()
            self._render_provider_chain()
            self._render_provider_combobox()
            self.show_toast(f"Saved {saved} API key(s) to secrets.env (0600)")
        else:
            self.show_toast("No new keys entered")

    def _on_toggle_offline(self, _sw: Gtk.Switch, state: bool) -> bool:
        if getattr(self, "_syncing_presets", False):
            return False
        if state:
            cur = self._current_provider_order()
            if cur and cur != ["local"]:
                write_env_flag("PP_ORDER_PREV", " ".join(cur), quoted=True)
            self._save_provider_order(["local"], "Offline-only mode on (Ollama local)")
        else:
            prev = read_env_flag("PP_ORDER_PREV", "groq groq2 openrouter").split()
            restored = [p for p in prev if p in PROVIDER_BY_ID] or ["groq", "openrouter"]
            self._save_provider_order(restored, f"Restored cloud chain: {' → '.join(restored)}")
        return False

    def _on_save_local_model(self, *_args) -> None:
        m = self.local_model_entry.get_text().strip()
        if not m:
            return
        write_env_flag("PP_LOCAL_MODEL", m, quoted=True)
        self.show_toast(f"Local Ollama model → {m}")

    def _on_osd_pos_preset(self, btn: Gtk.ToggleButton, pos: str) -> None:
        if btn.get_active() and not getattr(self, "_syncing_presets", False):
            if write_toml_str("osd", "position", pos):
                self.show_toast(f"OSD pill position → {pos} (Restart daemon to apply)")

    def _build_provider_combobox(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.add_css_class("combo-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_size_request(310, -1)

        self.prov_search = Gtk.SearchEntry(placeholder_text="Search providers…")
        self.prov_search.add_css_class("input-field")
        self.prov_search.connect("search-changed", lambda *_: self._render_provider_combobox())
        box.append(self.prov_search)

        self.prov_combo_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.prov_combo_list.add_css_class("data-list")
        box.append(self.prov_combo_list)
        pop.set_child(box)
        pop.connect("show", lambda *_: self._render_provider_combobox())
        return pop

    def _current_provider_order(self) -> list[str]:
        raw = read_env_flag("PP_ORDER", "groq groq2 openrouter").split()
        seen: list[str] = []
        for p in raw:
            if p in PROVIDER_BY_ID and p not in seen:
                seen.append(p)
        return seen

    def _save_provider_order(self, new_order: list[str], toast_msg: str) -> None:
        old_order = self._current_provider_order()
        if new_order == old_order:
            return
        write_env_flag("PP_ORDER", " ".join(new_order), quoted=True)
        self._render_provider_chain()
        self._render_provider_combobox()
        self._refresh_kpis()
        self.show_toast(
            toast_msg,
            undo_cb=lambda prev=old_order: (
                write_env_flag("PP_ORDER", " ".join(prev), quoted=True),
                self._render_provider_chain(),
                self._render_provider_combobox(),
                self._refresh_kpis(),
            ),
        )

    def _render_provider_combobox(self) -> None:
        if not hasattr(self, "prov_combo_list"):
            return
        while (row := self.prov_combo_list.get_first_child()) is not None:
            self.prov_combo_list.remove(row)
        active = set(self._current_provider_order())
        keys = configured_secret_keys()
        q = self.prov_search.get_text().strip().lower() if hasattr(self, "prov_search") else ""
        for p in PROVIDERS:
            if q and q not in f"{p['id']} {p['label']} {p['model']}".lower():
                continue
            r = Gtk.ListBoxRow()
            btn = Gtk.Button()
            btn.add_css_class("btn-ghost")
            btn.add_css_class("combo-row")
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            chk = Gtk.Label(label="✓" if p["id"] in active else "")
            chk.set_size_request(16, -1)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            t = Gtk.Label(label=p["label"], xalign=0)
            s = Gtk.Label(label=p["model"], xalign=0)
            s.add_css_class("app-subtitle")
            info.append(t)
            info.append(s)
            ready = (not p["secret"]) or (p["secret"] in keys)
            st = make_pill("● Ready" if ready else "○ No key", "clean" if ready else "neutral")
            row_box.append(chk)
            row_box.append(info)
            row_box.append(st)
            btn.set_child(row_box)
            btn.connect("clicked", self._on_toggle_provider, p["id"])
            r.set_child(btn)
            self.prov_combo_list.append(r)

    def _on_toggle_provider(self, _btn, pid: str) -> None:
        order = self._current_provider_order()
        label = PROVIDER_BY_ID[pid]["label"]
        if pid in order:
            if len(order) == 1:
                self.show_toast("Keep at least one cleanup provider")
                return
            order = [x for x in order if x != pid]
            self._save_provider_order(order, f"Removed {label} from fallback chain")
        else:
            order = order + [pid]
            self._save_provider_order(order, f"Added {label} (#{len(order)}) to fallback chain")

    def _move_provider(self, pid: str, target_idx: int) -> None:
        order = self._current_provider_order()
        if pid not in order:
            return
        cur = order.index(pid)
        target_idx = max(0, min(len(order) - 1, target_idx))
        if cur == target_idx:
            return
        order.pop(cur)
        order.insert(target_idx, pid)
        self._save_provider_order(order, f"Moved {PROVIDER_BY_ID[pid]['label']} to #{target_idx + 1}")

    def _render_provider_chain(self) -> None:
        while (ch := self.chain_box.get_first_child()) is not None:
            self.chain_box.remove(ch)
        order = self._current_provider_order()
        keys = configured_secret_keys()
        if not order:
            empty = Gtk.Label(label="No providers selected — click '+ Add provider' above.", xalign=0)
            empty.add_css_class("app-subtitle")
            self.chain_box.append(empty)
            return

        for idx, pid in enumerate(order):
            p = PROVIDER_BY_ID[pid]
            ready = (not p["secret"]) or (p["secret"] in keys)
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            item.add_css_class("chain-item")

            handle = Gtk.Label(label="⋮⋮")
            handle.add_css_class("drag-handle")
            rank = Gtk.Label(label=str(idx + 1), valign=Gtk.Align.CENTER)
            rank.add_css_class("rank-badge")
            if idx == 0:
                rank.add_css_class("rank-primary")

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            tl = Gtk.Label(label=p["label"], xalign=0)
            role_txt = "Primary" if idx == 0 else f"Fallback {idx}"
            rl = make_pill(role_txt, "fix" if idx == 0 else "neutral")
            title_row.append(tl)
            title_row.append(rl)
            sub = Gtk.Label(label=p["model"], xalign=0)
            sub.add_css_class("app-subtitle")
            info.append(title_row)
            info.append(sub)

            key_pill = make_pill("● Ready" if ready else "○ No key", "clean" if ready else "review")

            up_btn = Gtk.Button(icon_name="go-up-symbolic", sensitive=idx > 0, tooltip_text="Move up")
            up_btn.add_css_class("btn-ghost")
            up_btn.connect("clicked", lambda _b, i=pid, t=idx - 1: self._move_provider(i, t))
            dn_btn = Gtk.Button(icon_name="go-down-symbolic", sensitive=idx < len(order) - 1, tooltip_text="Move down")
            dn_btn.add_css_class("btn-ghost")
            dn_btn.connect("clicked", lambda _b, i=pid, t=idx + 1: self._move_provider(i, t))
            rm_btn = Gtk.Button(icon_name="window-close-symbolic", sensitive=len(order) > 1, tooltip_text="Remove")
            rm_btn.add_css_class("btn-ghost")
            rm_btn.connect("clicked", self._on_toggle_provider, pid)

            for w in (handle, rank, info, key_pill, up_btn, dn_btn, rm_btn):
                item.append(w)

            # Native GTK4 drag-and-drop reorder
            drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            drag.connect("prepare", lambda _src, _x, _y, i=pid: Gdk.ContentProvider.new_for_value(i))
            item.add_controller(drag)

            drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
            drop.connect("enter", lambda _d, _x, _y, box=item: (box.add_css_class("drop-hover"), Gdk.DragAction.MOVE)[1])
            drop.connect("leave", lambda _d, box=item: box.remove_css_class("drop-hover"))
            drop.connect("drop", lambda _d, val, _x, _y, t=idx, box=item: (box.remove_css_class("drop-hover"), self._move_provider(str(val), t), True)[2])
            item.add_controller(drop)

            self.chain_box.append(item)

    def _on_budget_preset(self, btn: Gtk.ToggleButton, ms: int) -> None:
        if btn.get_active() and not getattr(self, "_syncing_presets", False):
            write_env_flag("PP_BUDGET_MS", str(ms))
            self.show_toast(f"Cleanup timeout budget → {ms / 1000:.1f}s")

    def _on_stream_preset(self, btn: Gtk.ToggleButton, words_min: int) -> None:
        if btn.get_active() and not getattr(self, "_syncing_presets", False):
            write_env_flag("PP_STREAM_MIN_WORDS", str(words_min))
            self.show_toast("Streaming: always" if words_min == 0 else f"Streaming above {words_min} words")

    def _on_sandbox_changed(self, entry: Gtk.Entry) -> None:
        raw = entry.get_text().strip()
        if not raw:
            self.sandbox_out.set_text("Instant rewrite + vocab + hints preview appears here.")
            return
        hard = gate.hard_apply(raw)
        fin = gate.finish(hard)
        hints = gate.hint_line(hard)
        lines = [f"Output: {fin}"]
        if hints:
            lines.append(f"LLM hints: {hints}")
        self.sandbox_out.set_text("\n".join(lines))

    def _on_restart_daemon(self, _btn) -> None:
        try:
            subprocess.Popen(["systemctl", "--user", "restart", "voxtype.service"])
            self.show_toast("Restarting voxtype daemon…")
        except OSError:
            self.show_toast("systemctl not available — restart voxtype manually")
        GLib.timeout_add(1400, lambda: (self._refresh_settings_card(), False)[1])

    def _run_engine(self, mode: str) -> None:
        script = SCRIPTS / "voxtype-engine.sh"
        if script.exists():
            subprocess.Popen([str(script), mode])
            self.show_toast(f"Switching voxtype engine → {mode}…")
            GLib.timeout_add(2000, lambda: (self._refresh_settings_card(), False)[1])

    def _on_open_tui(self, _btn) -> None:
        subprocess.Popen(["/usr/bin/voxtype-configure-launcher"], start_new_session=True)

    def _detect_gpu_label(self) -> str:
        if shutil.which("nvidia-smi"):
            return "NVIDIA / CUDA"
        if Path("/dev/kfd").exists():
            return "AMD / ROCm·Vulkan"
        if any(Path("/dev/dri").glob("renderD*")):
            return "Vulkan / iGPU"
        return "CPU"

    def _refresh_settings_card(self) -> None:
        running, st = daemon_status()
        engine = read_toml_str("", "engine", "whisper")
        model_raw = read_toml_str(engine, "model", "") or read_toml_str("parakeet", "model", "") or read_toml_str("whisper", "model", "default")
        model = Path(model_raw).name if model_raw else "default"
        gpu = self._detect_gpu_label()
        self.eng_status_lbl.set_text(f"Daemon: {st}  ·  Engine: {engine}  ·  {gpu}\nModel:  {model}")
        self._refresh_live_state()

        self._render_provider_chain()
        self._render_provider_combobox()

        self._syncing_presets = True
        try:
            b_val = int(read_env_flag("PP_BUDGET_MS", "6000") or 6000)
            closest_b = min(self.budget_btns, key=lambda k: abs(k - b_val))
            self.budget_btns[closest_b].set_active(True)
            s_val = int(read_env_flag("PP_STREAM_MIN_WORDS", "60") or 60)
            closest_s = min(self.stream_btns, key=lambda k: abs(k - s_val))
            self.stream_btns[closest_s].set_active(True)
            if hasattr(self, "offline_sw"):
                self.offline_sw.set_active(self._current_provider_order() == ["local"])
            if hasattr(self, "local_model_entry"):
                self.local_model_entry.set_text(read_env_flag("PP_LOCAL_MODEL", "qwen3:4b"))
            if hasattr(self, "osd_pos_btns"):
                pos = read_toml_str("osd", "position", "bottom-center")
                if pos in self.osd_pos_btns:
                    self.osd_pos_btns[pos].set_active(True)
        finally:
            self._syncing_presets = False

    def _refresh_kpis(self) -> None:
        recent = self.history[:24]
        latencies = [float(d.get("pp_ms") or 0) for d in reversed(recent) if d.get("pp_ms")]
        if latencies:
            med = sorted(latencies)[len(latencies) // 2]
            self.kpi_latency.set_text(f"{int(med)} ms")
        else:
            self.kpi_latency.set_text("—")
        self.sparkline.set_values(latencies)
        self.kpi_prov_pill.set_text(read_env_flag("PP_ORDER", "groq").split()[0])

        pairs = learn.load()
        suggs = suggest_from_history(self.history, pairs)
        rev_count = sum(1 for d in self.history[:50] if d.get("review"))
        self.kpi_review_num.set_text(str(len(suggs)))
        rev_summary = f"safe fixes · {rev_count} flagged"
        self.kpi_review_sub.set_text(rev_summary)
        self.kpi_review_sub.set_tooltip_text(rev_summary)
        while (ch := self.kpi_review_chips.get_first_child()) is not None:
            self.kpi_review_chips.remove(ch)
        for _h, m, _n in suggs[:2]:
            chip = Gtk.Label(label=m if len(m) <= 9 else m[:8] + "…")
            chip.add_css_class("dot-chip")
            self.kpi_review_chips.append(chip)

        vcount = len(read_vocab_entries())
        scount = len(read_snippets_raw())
        total_mem = len(pairs) + vcount + scount
        mem_summary = f"{len(pairs)} fixes · {vcount} vocab · {scount} snip"
        self.kpi_mem_num.set_text(str(total_mem))
        self.kpi_mem_sub.set_text(mem_summary)
        self.kpi_mem_sub.set_tooltip_text(f"{len(pairs)} learned fixes · {vcount} vocabulary terms · {scount} voice snippets")
        live = a11y_enabled()
        self.kpi_a11y_pill.set_text("● AT-SPI" if live else "○ AT-SPI")
        self.kpi_a11y_pill.set_tooltip_text("AT-SPI screen & focused draft context is active" if live else "AT-SPI screen context is off")
        for c in ("pill-clean", "pill-review"):
            self.kpi_a11y_pill.remove_css_class(c)
        self.kpi_a11y_pill.add_css_class("pill-clean" if live else "pill-review")

    # ── Tab 1B: Insights & Auto-Mine (Superwhisper-inspired) ─────────────
    def _build_insights_page(self) -> Gtk.Widget:
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sc.set_child(root)

        # 4 impact summary cards
        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        self.ins_words = Gtk.Label(label="—", xalign=0)
        self.ins_saved = Gtk.Label(label="—", xalign=0)
        self.ins_acc = Gtk.Label(label="—", xalign=0)
        self.ins_p90 = Gtk.Label(label="—", xalign=0)
        for lbl_title, val_w, sub in [
            ("Words dictated", self.ins_words, "all recorded takes"),
            ("Est. time saved", self.ins_saved, "vs 40 WPM typing"),
            ("Cleanup reliability", self.ins_acc, "non-fallback rate"),
            ("P50 / P90 latency", self.ins_p90, "end-to-end cleanup"),
        ]:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("kpi-card")
            t = Gtk.Label(label=lbl_title, xalign=0)
            t.add_css_class("kpi-label")
            val_w.add_css_class("kpi-value")
            s = Gtk.Label(label=sub, xalign=0)
            s.add_css_class("kpi-sub")
            card.append(t)
            card.append(val_w)
            card.append(s)
            stats_row.append(card)
        root.append(stats_row)

        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, homogeneous=True)
        root.append(cols)

        # Left: 14-day Volume Chart + Profile/Provider Breakdown
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        chart_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        chart_card.add_css_class("kpi-card")
        ch = Gtk.Label(label="DAILY WORDS DICTATED (LAST 14 ACTIVE DAYS)", xalign=0)
        ch.add_css_class("section-kicker")
        self.vol_chart = BarChart()
        chart_card.append(ch)
        chart_card.append(self.vol_chart)
        left.append(chart_card)

        brk_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        brk_card.add_css_class("kpi-card")
        bh = Gtk.Label(label="PROFILES & CLEANUP PROVIDERS", xalign=0)
        bh.add_css_class("section-kicker")
        brk_card.append(bh)
        self.brk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        brk_card.append(self.brk_box)
        left.append(brk_card)
        cols.append(left)

        # Right: Auto-Mine Self-Learning + AI Audit Export
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        mine_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        mine_card.add_css_class("kpi-card")
        mh_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mh = Gtk.Label(label="FULL-HISTORY AUTO-MINE & AI AUDIT", xalign=0, hexpand=True)
        mh.add_css_class("section-kicker")
        self.learn_all_btn = Gtk.Button(label="Learn all safe")
        self.learn_all_btn.add_css_class("btn-primary")
        self.learn_all_btn.connect("clicked", self._on_learn_all_safe)
        audit_btn = Gtk.Button(label="Copy AI audit", tooltip_text="Copy a Superwhisper-style tuning report to paste into T3 Code / Claude")
        audit_btn.add_css_class("btn-secondary")
        audit_btn.connect("clicked", self._on_copy_ai_audit)
        mh_row.append(mh)
        mh_row.append(audit_btn)
        mh_row.append(self.learn_all_btn)
        mine_card.append(mh_row)

        self.mine_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mine_card.append(self.mine_list)
        right.append(mine_card)

        hot_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hot_card.add_css_class("kpi-card")
        hh = Gtk.Label(label="TOP RECURRING SWAPS ACROSS ALL HISTORY", xalign=0)
        hh.add_css_class("section-kicker")
        hot_card.append(hh)
        self.hot_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hot_card.append(self.hot_box)
        right.append(hot_card)
        cols.append(right)
        return sc

    def _render_insights(self) -> None:
        ins = compute_insights(self.history)
        self.ins_words.set_text(f"{ins['words']:,}")
        self.ins_saved.set_text(f"{ins['hours_saved']:.1f} hrs")
        self.ins_acc.set_text(f"{ins['success_pct']:.1f}%")
        self.ins_p90.set_text(f"{ins['p50']} / {ins['p90']} ms")
        self.vol_chart.set_bars(ins["by_day"])

        while (ch := self.brk_box.get_first_child()) is not None:
            self.brk_box.remove(ch)
        total = max(ins["total"], 1)
        for label, counter in [("Profiles", ins["by_mode"]), ("Providers", ins["by_via"])]:
            flow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.add_css_class("kpi-label")
            lbl.set_size_request(68, -1)
            flow.append(lbl)
            pills = Gtk.FlowBox(
                selection_mode=Gtk.SelectionMode.NONE,
                row_spacing=4,
                column_spacing=6,
                min_children_per_line=1,
                max_children_per_line=4,
                hexpand=True,
            )
            for k, c in counter.most_common(4):
                pct = int(round(c * 100 / total))
                pills.append(make_pill(f"{k} {pct}% ({c})", "fix" if k in ("terminal", "groq") else "neutral"))
            flow.append(pills)
            self.brk_box.append(flow)

        pairs = learn.load()
        suggs = suggest_from_history(self.history, pairs)
        self.learn_all_btn.set_label(f"Learn all safe ({len(suggs)})")
        self.learn_all_btn.set_sensitive(bool(suggs))
        while (ch := self.mine_list.get_first_child()) is not None:
            self.mine_list.remove(ch)
        if not suggs:
            empty = Gtk.Label(label="All grounded mishearings from your history are already learned.", xalign=0, wrap=True)
            empty.add_css_class("app-subtitle")
            self.mine_list.append(empty)
        else:
            for h, m, n in suggs[:8]:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.add_css_class("swap-row")
                hl = Gtk.Label(label=f"{h}  →  {m}", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
                nl = make_pill(f"{n}×", "fix" if n >= 2 else "neutral")
                btn = Gtk.Button(label="Teach")
                btn.add_css_class("btn-secondary")
                btn.connect("clicked", self._on_accept_sugg, h, m)
                dis = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Dismiss forever")
                dis.add_css_class("btn-ghost")
                dis.connect("clicked", self._on_reject_sugg, h, m)
                row.append(hl)
                row.append(nl)
                row.append(btn)
                row.append(dis)
                self.mine_list.append(row)

        while (ch := self.hot_box.get_first_child()) is not None:
            self.hot_box.remove(ch)
        for s, c in ins["top_swaps"][:8]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            cl = Gtk.Label(label=f"{c}×", xalign=0)
            cl.add_css_class("mono-num")
            cl.set_size_request(34, -1)
            sl = Gtk.Label(label=s, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            row.append(cl)
            row.append(sl)
            self.hot_box.append(row)

    def _on_learn_all_safe(self, _btn) -> None:
        before = learn.load()
        suggs = suggest_from_history(self.history, before)
        added = 0
        for h, m, _n in suggs:
            if not learn.remember(h, m, "user"):
                added += 1
        if added:
            learn.sync_replacements()
            self.reload_all()
            self.show_toast(
                f"Learned {added} safe fix(es) & synced to daemon",
                undo_cb=lambda: (learn.save(before), learn.sync_replacements()),
            )

    def _on_copy_ai_audit(self, _btn) -> None:
        ins = compute_insights(self.history)
        suggs = suggest_from_history(self.history, learn.load())
        wl_copy(build_ai_audit_prompt(self.history, ins, suggs))
        self.show_toast("Copied AI audit summary — paste into T3 Code or Claude to tune further")

    def reload_all(self) -> None:
        self.history = load_history()
        self._render_history_list()
        self._render_insights()
        self._render_corrections()
        self._render_vocab()
        self._render_snippets()
        self._refresh_settings_card()
        self._refresh_kpis()


class VoxtypeStudioApp(Adw.Application):
    def __init__(self, initial_tab: str = "history") -> None:
        super().__init__(application_id="dev.kitsunekode.voxtype-studio", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.initial_tab = initial_tab

    def do_activate(self) -> None:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = self.props.active_window
        if not win:
            win = VoxtypeStudioWindow(self, initial_tab=self.initial_tab)
        else:
            win.reload_all()
            if self.initial_tab:
                win.select_tab(self.initial_tab)
        win.present()


def main() -> int:
    tab = "history"
    args = [sys.argv[0]]
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--tab" and i + 1 < len(sys.argv):
            tab = sys.argv[i + 1]
            i += 2
        else:
            args.append(sys.argv[i])
            i += 1
    return VoxtypeStudioApp(initial_tab=tab).run(args)


if __name__ == "__main__":
    sys.exit(main())
