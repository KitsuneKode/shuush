#!/usr/bin/env python3
"""Shuush delivery queue and worker (PP_DELIVERY=shuush).

When PP_DELIVERY=shuush is active, voxtype-postprocess.sh hands the raw
transcript and take metadata to this service over a Unix domain socket
($XDG_RUNTIME_DIR/shuush/deliver.sock) and exits 0 with empty stdout so
voxtype (with fallback_on_empty = false) returns to idle right after ASR.

This worker owns:
  - Strict take-order delivery (ordered by take_id, bounded waiting queue)
  - Cleanup execution via voxtype-postprocess.sh (PP_WORKER_CLEANUP=1)
  - Focus/target verification right before insertion (and per streamed chunk)
  - Typing short text vs pasting long text (PP_PASTE_OVER_CHARS, default 300)
  - Streamed cleanup typing as sentences arrive when the target supports it
  - Clipboard policy (PP_KEEP_ON_CLIPBOARD=1 keeps text; =0 preserves/restores)
  - Strict privacy fail-closed (private takes NEVER touch clipboard or history)
  - Target-loss recovery (recovered.txt + disposition=recovered, Studio + recover)
  - Empty-string handoff replacements (auto_submit, smart_auto_submit, append_text)
  - Per-take monotonic timing (delivery_start, delivery_done) and review/history
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

LIBEXEC = Path(__file__).resolve().parent
sys.path.insert(0, str(LIBEXEC))
import shuush_caps  # noqa: E402
import shuush_timing  # noqa: E402

SOCKET_TIMEOUT_S = 2.0
DEFAULT_PASTE_OVER_CHARS = 300
DEFAULT_MAX_QUEUE = 3
DEFAULT_RESTORE_DELAY_MS = 200
SMART_JOIN_LOWER = {
    "and", "but", "so", "or", "then", "because", "which", "that", "the", "a",
    "an", "to", "of", "in", "on", "for", "with", "also", "just", "like", "when",
    "if", "as", "at", "by", "from", "maybe", "plus", "and,", "but,", "so,", "then,",
}
SMART_SUBMIT_RE = re.compile(r"(?:(?<=\s)|(?<=^)|(?<=[,.:;!?]))\s*submit[.!?]*\s*$", re.IGNORECASE)


def xdg_runtime() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def voxtype_rt() -> Path:
    return xdg_runtime() / "voxtype"


def voxtype_conf() -> Path:
    return xdg_config() / "voxtype"


def default_socket_path() -> Path:
    env_sock = os.environ.get("SHUUSH_DELIVER_SOCK", "").strip()
    if env_sock:
        return Path(env_sock)
    return xdg_runtime() / "shuush" / "deliver.sock"


@contextlib.contextmanager
def unix_addr(path: Path):
    """Yield a bind/connect-safe AF_UNIX path even when `path` exceeds Linux's 108-byte limit."""
    raw = str(path)
    if len(raw.encode("utf-8")) <= 100:
        yield raw
        return
    dfd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield f"/proc/self/fd/{dfd}/{path.name}"
    finally:
        os.close(dfd)


def load_pp_env(conf_dir: Path | None = None) -> dict[str, str]:
    """Read postprocess.env key=value pairs, overlaid by current process env."""
    cfg = (conf_dir or voxtype_conf()) / "postprocess.env"
    vals: dict[str, str] = {}
    if cfg.is_file():
        try:
            for raw_line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                    continue
                if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
                    v = v[1:-1]
                vals[k] = v
        except OSError:
            pass
    for k, v in os.environ.items():
        if k.startswith(("PP_", "VOXTYPE_", "SHUUSH_", "WTYPE_")):
            vals[k] = v
    return vals


def read_voxtype_toml(conf_dir: Path | None = None) -> dict:
    p = (conf_dir or voxtype_conf()) / "config.toml"
    if not p.is_file():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def delivery_mode_configured(conf_dir: Path | None = None) -> str:
    env = load_pp_env(conf_dir)
    raw = (env.get("PP_DELIVERY") or "daemon").strip().lower()
    if raw == "shuush":
        return "shuush"
    return "daemon"


def config_allows_handoff(conf_dir: Path | None = None) -> tuple[bool, str]:
    """Verify config.toml has fallback_on_empty = false when config.toml exists."""
    p = (conf_dir or voxtype_conf()) / "config.toml"
    if not p.is_file():
        return True, "no_config_toml"
    try:
        doc = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False, "config_toml_unreadable"
    pp = (doc.get("output") or {}).get("post_process") or {}
    if pp.get("fallback_on_empty") is False:
        return True, "ok"
    return False, "fallback_on_empty_not_false"


def _int_opt(env: dict[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if raw and re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    return default


def _bool_opt(env: dict[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# ── Compositor & target focus ─────────────────────────────────────────────────


def active_window_json() -> dict:
    """Query the live focused window from Hyprland, Niri, or Sway."""
    if shutil.which("hyprctl"):
        try:
            r = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=0.5, stdin=subprocess.DEVNULL)
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                if isinstance(d, dict):
                    return {
                        "address": d.get("address"),
                        "class": d.get("class") or "",
                        "title": d.get("title") or "",
                        "pid": d.get("pid") or 0,
                    }
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    if shutil.which("niri"):
        try:
            r = subprocess.run(["niri", "msg", "-j", "focused-window"], capture_output=True, text=True, timeout=0.5, stdin=subprocess.DEVNULL)
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                if isinstance(d, dict):
                    return {
                        "address": str(d.get("id")) if d.get("id") is not None else "",
                        "class": d.get("app_id") or "",
                        "title": d.get("title") or "",
                        "pid": d.get("pid") or 0,
                    }
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    if shutil.which("swaymsg"):
        try:
            r = subprocess.run(["swaymsg", "-t", "get_tree"], capture_output=True, text=True, timeout=0.5, stdin=subprocess.DEVNULL)
            if r.returncode == 0 and r.stdout.strip():
                tree = json.loads(r.stdout)
                stack = [tree]
                while stack:
                    node = stack.pop()
                    if not isinstance(node, dict):
                        continue
                    if node.get("focused") is True:
                        wp = node.get("window_properties") or {}
                        return {
                            "address": str(node.get("id")) if node.get("id") is not None else "",
                            "class": node.get("app_id") or wp.get("class") or "",
                            "title": node.get("name") or "",
                            "pid": node.get("pid") or 0,
                        }
                    stack.extend(node.get("nodes") or [])
                    stack.extend(node.get("floating_nodes") or [])
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return {}


def read_target_window(target_path: Path) -> dict | None:
    if not target_path.is_file():
        return None
    try:
        d = json.loads(target_path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def target_still_focused(capture_dir: Path | None, *, bad_id: bool = False, tagged: bool = False) -> bool:
    if bad_id:
        return False
    if capture_dir is None:
        return True
    target_path = capture_dir / "target_window"
    if not target_path.is_file():
        return True
    target = read_target_window(target_path) or {}
    addr = target.get("address")
    if not isinstance(addr, str) or not addr or addr in ("null", "0x0"):
        return not tagged
    pid = str(target.get("pid") or 0)
    cur = active_window_json()
    if cur.get("address") != addr:
        return False
    if pid != "0" and str(cur.get("pid") or 0) != pid:
        return False
    return True


def is_private_take(rt: Path, capture_dir: Path | None) -> bool:
    if capture_dir and (capture_dir / "pp_private").exists():
        return True
    if (rt / "sensitive_field").exists():
        doc = shuush_caps.capabilities()
        if not doc.get("probe_ok") or (doc.get("caps") or {}).get("sensitive_field"):
            return True
    return False


# ── Text transforms owned by Shuush in shuush delivery mode ───────────────────


def apply_smart_join(text: str, rt: Path, capture_dir: Path | None, *, mode: str, enabled: bool) -> str:
    if not enabled or mode == "command" or not text:
        return text
    before = ""
    if capture_dir and (capture_dir / "take_screen_ctx.json").is_file():
        try:
            before = str(json.loads((capture_dir / "take_screen_ctx.json").read_text(encoding="utf-8")).get("before_caret") or "")[-40:]
        except (OSError, ValueError, AttributeError):
            before = ""
    elif (rt / "field_text.json").is_file():
        try:
            before = str(json.loads((rt / "field_text.json").read_text(encoding="utf-8")).get("before") or "")[-40:]
        except (OSError, ValueError, AttributeError):
            before = ""
    if not before:
        return text
    prev = before[-1]
    if prev.isspace() or prev in "([{\"'`/-@#":
        return text
    if text[0].isspace() or text[0] in ".,;:!?)]}":
        return text
    if re.match(r"[a-z0-9,;:]$", prev):
        parts = text.split(None, 1)
        first = parts[0] if parts else ""
        rest = text[len(first):]
        lower = first.lower()
        if lower in SMART_JOIN_LOWER and first == lower[:1].upper() + lower[1:]:
            text = lower + rest
    return " " + text


def apply_smart_auto_submit(text: str, enabled: bool) -> tuple[str, bool]:
    if not enabled or not text.strip():
        return text, False
    m = SMART_SUBMIT_RE.search(text)
    if not m:
        return text, False
    stripped = text[:m.start()].rstrip(" ,;:")
    return stripped, True


# ── Output drivers (typing, pasting, clipboard policy) ────────────────────────


def detect_type_driver(env: dict[str, str]) -> str | None:
    pref = (env.get("SHUUSH_TYPE_DRIVER") or "").strip().lower()
    if pref in ("wtype", "ydotool", "dotool"):
        return pref if shutil.which(pref) else None
    for cand in ("wtype", "ydotool", "dotool"):
        if shutil.which(cand):
            return cand
    return None


def type_text(text: str, env: dict[str, str]) -> tuple[bool, str]:
    if not text:
        return True, ""
    driver = detect_type_driver(env)
    if not driver:
        return False, "no_typing_driver"
    run_env = os.environ.copy()
    if "WTYPE_KEY_GAP_US" in env:
        run_env["WTYPE_KEY_GAP_US"] = env["WTYPE_KEY_GAP_US"]
    try:
        if driver == "wtype":
            r = subprocess.run(["wtype", "--", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=60, env=run_env)
        elif driver == "ydotool":
            r = subprocess.run(["ydotool", "type", "--key-delay", "0", "--", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=60, env=run_env)
        else:
            r = subprocess.run(["dotool"], input=f"type {text}\n", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=60, env=run_env)
        return r.returncode == 0, (r.stderr.strip() if r.returncode != 0 else "")
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def send_return(env: dict[str, str]) -> bool:
    driver = detect_type_driver(env)
    if not driver:
        return False
    try:
        if driver == "wtype":
            r = subprocess.run(["wtype", "-k", "Return"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        elif driver == "ydotool":
            r = subprocess.run(["ydotool", "key", "28:1", "28:0"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            r = subprocess.run(["dotool"], input="key enter\n", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_paste_keys(env: dict[str, str], app_family: str, toml_doc: dict) -> str:
    out_cfg = toml_doc.get("output") or {}
    pref = (env.get("PP_PASTE_KEYS") or "").strip().lower()
    if not pref:
        if app_family == "terminal" and out_cfg.get("paste_keys_terminal"):
            pref = str(out_cfg["paste_keys_terminal"]).strip().lower()
        elif out_cfg.get("paste_keys"):
            pref = str(out_cfg["paste_keys"]).strip().lower()
        else:
            pref = "auto"
    if pref == "auto":
        return "ctrl+shift+v" if app_family == "terminal" else "ctrl+v"
    if pref in ("shift+insert", "ctrl+v", "ctrl+shift+v", "super+v"):
        return pref
    return "ctrl+shift+v" if app_family == "terminal" else "ctrl+v"


def send_paste_keys(combo: str, env: dict[str, str]) -> tuple[bool, str]:
    driver = detect_type_driver(env)
    if not driver:
        return False, "no_typing_driver"
    try:
        if driver == "wtype":
            cmd_map = {
                "ctrl+v": ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"],
                "ctrl+shift+v": ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"],
                "shift+insert": ["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"],
                "super+v": ["wtype", "-M", "logo", "-k", "v", "-m", "logo"],
            }
            cmd = cmd_map.get(combo, cmd_map["ctrl+v"])
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
        elif driver == "ydotool":
            # evdev keycodes: ctrl=29, shift=42, v=47, insert=110, super=125
            key_map = {
                "ctrl+v": ["29:1", "47:1", "47:0", "29:0"],
                "ctrl+shift+v": ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],
                "shift+insert": ["42:1", "110:1", "110:0", "42:0"],
                "super+v": ["125:1", "47:1", "47:0", "125:0"],
            }
            r = subprocess.run(["ydotool", "key", *key_map.get(combo, key_map["ctrl+v"])], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
        else:
            dt_map = {
                "ctrl+v": "key ctrl+v\n",
                "ctrl+shift+v": "key ctrl+shift+v\n",
                "shift+insert": "key shift+insert\n",
                "super+v": "key super+v\n",
            }
            r = subprocess.run(["dotool"], input=dt_map.get(combo, "key ctrl+v\n"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
        return r.returncode == 0, (r.stderr.strip() if r.returncode != 0 else "")
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def record_self_clipboard_hash(rt: Path, text: str) -> None:
    h = hashlib.sha256(text[:800].encode("utf-8", errors="replace")).hexdigest()
    rt.mkdir(parents=True, exist_ok=True)
    for name, content in (("clip_seen", f"{h}\n0\n"), ("clip_self_hash", f"{h}\n")):
        p = rt / name
        tmp = rt / f"{name}.tmp.{os.getpid()}"
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, p)
        except OSError:
            pass


def wl_copy_bytes(data: bytes, mime: str | None = None) -> bool:
    if not shutil.which("wl-copy"):
        return False
    cmd = ["wl-copy"]
    if mime:
        cmd += ["--type", mime]
    try:
        # Must NOT capture stdout/stderr: wl-copy forks a clipboard owner that inherits FDs.
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        assert p.stdin is not None
        p.stdin.write(data)
        p.stdin.close()
        p.wait(timeout=2.0)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wl_copy_text(text: str) -> bool:
    if not shutil.which("wl-copy"):
        return False
    try:
        p = subprocess.Popen(["wl-copy", "--", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        p.wait(timeout=2.0)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wl_clear() -> None:
    if not shutil.which("wl-copy"):
        return
    try:
        subprocess.run(["wl-copy", "--clear"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
    except (OSError, subprocess.SubprocessError):
        pass


def snapshot_clipboard() -> tuple[str, bytes | None, str | None]:
    """Return (status, raw_bytes, mime) where status is 'ok'|'empty'|'secret'|'unavailable'."""
    if not shutil.which("wl-paste"):
        return "unavailable", None, None
    try:
        lt = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=0.5, stdin=subprocess.DEVNULL)
        if lt.returncode != 0:
            return "empty", None, None
        types = [t.strip() for t in lt.stdout.splitlines() if t.strip()]
        if "x-kde-passwordManagerHint" in types:
            return "secret", None, None
        if not types:
            return "empty", None, None
        # Prefer richest text type if present, otherwise first available MIME.
        chosen = next((t for t in ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain") if t in types), types[0])
        val = subprocess.run(["wl-paste", "--no-newline", "--type", chosen], capture_output=True, timeout=0.8, stdin=subprocess.DEVNULL)
        if val.returncode != 0:
            return "empty", None, None
        return "ok", val.stdout, chosen
    except (OSError, subprocess.SubprocessError):
        return "unavailable", None, None


def paste_text(
    text: str,
    rt: Path,
    env: dict[str, str],
    *,
    app_family: str,
    toml_doc: dict,
    keep_on_clipboard: bool,
) -> tuple[bool, str, str]:
    """Paste text via wl-copy + paste keystroke.

    Returns (ok, actual_method, err). If keep_on_clipboard is False and the
    current clipboard carries x-kde-passwordManagerHint, refuses to touch the
    clipboard and falls back to typing!
    """
    if not shutil.which("wl-copy"):
        ok, err = type_text(text, env)
        return ok, ("type" if ok else "failed"), err

    saved_status = "unused"
    saved_bytes: bytes | None = None
    saved_mime: str | None = None
    if not keep_on_clipboard:
        saved_status, saved_bytes, saved_mime = snapshot_clipboard()
        if saved_status == "secret":
            # Never clobber or read a password manager clipboard when user opted out of clipboard writes.
            ok, err = type_text(text, env)
            return ok, ("type" if ok else "failed"), err

    record_self_clipboard_hash(rt, text)
    if not wl_copy_text(text):
        ok, err = type_text(text, env)
        return ok, ("type" if ok else "failed"), err

    combo = resolve_paste_keys(env, app_family, toml_doc)
    ok, err = send_paste_keys(combo, env)

    if not keep_on_clipboard:
        delay_ms = _int_opt(env, "PP_RESTORE_CLIPBOARD_DELAY_MS", DEFAULT_RESTORE_DELAY_MS)
        if delay_ms > 0:
            time.sleep(min(delay_ms, 5000) / 1000.0)
        # Only restore if the clipboard still holds our pasted text (user didn't copy something newer).
        still_ours = True
        if shutil.which("wl-paste"):
            try:
                cur = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=0.5, stdin=subprocess.DEVNULL)
                if cur.returncode == 0 and cur.stdout != text:
                    still_ours = False
            except (OSError, subprocess.SubprocessError):
                pass
        if still_ours:
            if saved_status == "ok" and saved_bytes is not None:
                wl_copy_bytes(saved_bytes, saved_mime)
            elif saved_status == "empty":
                wl_clear()

    return ok, ("paste" if ok else "failed"), err


# ── Runtime state updates & notifications ─────────────────────────────────────


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def notify_user(message: str) -> None:
    if not message:
        return
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", "Voxtype", message], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass


def clear_outline_if_current(rt: Path, take_id: str) -> None:
    if not is_current_take(rt, take_id):
        return
    outline = LIBEXEC / "voxtype-outline.sh"
    if outline.is_file():
        try:
            subprocess.run([str(outline), "off"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass


def is_current_take(rt: Path, take_id: str) -> bool:
    if not take_id:
        return True
    try:
        cur = (rt / "current_capture").read_text(encoding="utf-8").strip()
        return cur == take_id
    except OSError:
        return True


def write_outcome(rt: Path, take_id: str, disposition: str, *, chars: int, session_id: str, method: str, private: bool) -> None:
    doc: dict = {
        "take_id": take_id,
        "outcome": disposition,
        "chars": 0 if private else int(chars),
        "at_ms": int(time.time() * 1000),
        "delivery_mode": "shuush",
        "delivery_method": method,
    }
    if session_id and session_id.isdigit():
        doc["session_started_at_ms"] = int(session_id)
    if private:
        doc["private"] = True
    try:
        atomic_write_text(rt / "outcome.json", json.dumps(doc))
    except OSError:
        pass


# ── Job model & ordered delivery worker ───────────────────────────────────────


@dataclasses.dataclass
class TakeJob:
    take_id: str
    seq: int
    raw: str
    mode: str
    profile: str
    session_id: str
    capture_dir: Path | None
    transcript_ready_ms: int
    voxtype_context: str = ""
    bad_id: bool = False
    tagged: bool = False
    cancel_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    skip_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    proc_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    proc: subprocess.Popen | None = None

    @property
    def sort_key(self) -> tuple[int, int]:
        num = int(self.take_id) if self.take_id.isdigit() else 0
        return (num, self.seq)

    def terminate_proc(self) -> None:
        with self.proc_lock:
            p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass


class DeliveryServer:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._deliver_lock = threading.Lock()
        self._order: list[tuple[tuple[int, int], TakeJob]] = []
        self._seq = 0
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._sock: socket.socket | None = None

    def _is_head(self, job: TakeJob) -> bool:
        with self._lock:
            return bool(self._order) and self._order[0][1] is job

    def _wait_for_turn(self, job: TakeJob) -> bool:
        with self._cond:
            while not self._stop.is_set() and not job.cancel_event.is_set():
                if self._order and self._order[0][1] is job:
                    return True
                self._cond.wait(timeout=0.1)
            return False

    def _finish_order(self, job: TakeJob) -> None:
        with self._cond:
            self._order = [(k, j) for k, j in self._order if j is not job]
            self._cond.notify_all()

    def _active_summary(self) -> dict:
        with self._lock:
            head = self._order[0][1].take_id if self._order else None
            queued = [j.take_id for _, j in self._order[1:]]
        return {"ok": True, "pid": os.getpid(), "active": head, "queued": len(queued), "queue": queued}

    def enqueue(self, req: dict) -> dict:
        env = load_pp_env()
        max_q = max(0, _int_opt(env, "PP_DELIVERY_MAX_QUEUE", DEFAULT_MAX_QUEUE))
        raw = str(req.get("raw") or "")
        take_id = str(req.get("take_id") or "").strip()
        if not take_id or not re.fullmatch(r"[0-9]+", take_id):
            take_id = str(time.time_ns())
        cap_str = str(req.get("capture_dir") or "").strip()
        capture_dir = Path(cap_str) if cap_str and Path(cap_str).is_dir() else None
        if capture_dir is None:
            cand = voxtype_rt() / "takes" / take_id
            if cand.is_dir():
                capture_dir = cand
        t_ready = req.get("transcript_ready_ms")
        if not isinstance(t_ready, int):
            t_ready = shuush_timing.mono_ms()

        with self._cond:
            if len(self._order) >= 1 + max_q:
                return {"ok": False, "error": "queue_full", "queued": max(0, len(self._order) - 1)}
            self._seq += 1
            job = TakeJob(
                take_id=take_id,
                seq=self._seq,
                raw=raw,
                mode=str(req.get("mode") or "default"),
                profile=str(req.get("profile") or ""),
                session_id=str(req.get("session_id") or ""),
                capture_dir=capture_dir,
                transcript_ready_ms=t_ready,
                voxtype_context=str(req.get("voxtype_context") or ""),
                bad_id=bool(req.get("bad_id")),
                tagged=bool(req.get("tagged")),
            )
            self._order.append((job.sort_key, job))
            self._order.sort(key=lambda item: item[0])
            t = threading.Thread(target=self._run_job_wrapper, args=(job,), daemon=True)
            self._threads.add(t)
            t.start()
        return {"ok": True, "take_id": take_id, "queued": max(0, len(self._order) - 1)}

    def cancel(self, take_id: str = "") -> dict:
        cancelled: list[str] = []
        with self._cond:
            for _, j in list(self._order):
                if not take_id or j.take_id == take_id:
                    j.cancel_event.set()
                    j.terminate_proc()
                    cancelled.append(j.take_id)
            self._cond.notify_all()
        return {"ok": True, "cancelled": cancelled}

    def skip(self) -> dict:
        with self._cond:
            if not self._order:
                return {"ok": False, "error": "no_active_take"}
            head = self._order[0][1]
            head.skip_event.set()
            head.terminate_proc()
            tid = head.take_id
        return {"ok": True, "take_id": tid}

    def _run_job_wrapper(self, job: TakeJob) -> None:
        try:
            self._process_job(job)
        except Exception as e:
            sys.stderr.write(f"shuush-deliver: take {job.take_id} error: {e}\n")
        finally:
            self._finish_order(job)
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _cleanup_env(self, job: TakeJob, meta_out: Path, stream: bool) -> dict[str, str]:
        e = os.environ.copy()
        e["PP_WORKER_CLEANUP"] = "1"
        e["PP_DELIVERY"] = "daemon"
        e["PP_META_OUT"] = str(meta_out)
        e["VOXTYPE_CONTEXT"] = job.voxtype_context
        e["VOXTYPE_STREAM_OUTPUT"] = "1" if stream else "0"
        e["PP_ALLOW_STREAM_WITH_TAKE"] = "1" if stream else "0"
        e["PP_MONO_T0"] = str(job.transcript_ready_ms)
        if job.capture_dir:
            e["PP_TAKE_DIR_OVERRIDE"] = str(job.capture_dir)
            e["PP_TIMING_DIR"] = str(job.capture_dir)
        if job.take_id:
            e["PP_TAKE_ID_OVERRIDE"] = job.take_id
        if job.bad_id:
            e["PP_FORCE_BAD_ID"] = "1"
        return e

    def _cleanup_budget_s(self, raw: str, mode: str, env: dict[str, str]) -> float:
        words = len(raw.split())
        if mode == "command":
            ms = _int_opt(env, "PP_COMMAND_BUDGET_MS", 11000)
        else:
            default_ms = min(20000, 6000 + (max(0, words - 100) * 15))
            ms = min(20000, _int_opt(env, "PP_BUDGET_MS", default_ms))
        return max(1.0, ms / 1000.0) + 1.5

    def _can_stream(self, job: TakeJob, env: dict[str, str], rt: Path) -> bool:
        if not self._is_head(job):
            return False
        if not _bool_opt(env, "PP_STREAM_TYPING", True):
            return False
        if job.mode == "command" or job.bad_id:
            return False
        if _bool_opt(env, "PP_SMART_AUTO_SUBMIT", False):
            return False
        if is_private_take(rt, job.capture_dir):
            return False
        min_words = _int_opt(env, "PP_STREAM_MIN_WORDS", 60)
        if len(job.raw.split()) < min_words:
            return False
        paste_over = _int_opt(env, "PP_PASTE_OVER_CHARS", DEFAULT_PASTE_OVER_CHARS)
        if paste_over > 0 and len(job.raw) > paste_over:
            return False
        if not detect_type_driver(env):
            return False
        if not target_still_focused(job.capture_dir, bad_id=job.bad_id, tagged=job.tagged):
            return False
        return True

    def _run_whole_cleanup(self, job: TakeJob, env: dict[str, str], meta_out: Path) -> tuple[str, dict]:
        script = str(LIBEXEC / "voxtype-postprocess.sh")
        cmd = [script, job.mode] if job.mode and job.mode != "default" else [script]
        ce = self._cleanup_env(job, meta_out, stream=False)
        timeout_s = self._cleanup_budget_s(job.raw, job.mode, env)
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=ce,
        )
        with job.proc_lock:
            job.proc = p
        try:
            assert p.stdin is not None
            try:
                p.stdin.write(job.raw.encode("utf-8"))
                p.stdin.close()
            except BrokenPipeError:
                pass
            deadline = time.monotonic() + timeout_s
            chunks: list[bytes] = []
            assert p.stdout is not None
            fd = p.stdout.fileno()
            while True:
                if job.cancel_event.is_set() or job.skip_event.is_set():
                    p.terminate()
                    break
                rem = deadline - time.monotonic()
                if rem <= 0:
                    p.terminate()
                    break
                r, _, _ = select.select([fd], [], [], min(0.05, rem))
                if r:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    chunks.append(data)
            try:
                p.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=1.0)
        finally:
            with job.proc_lock:
                job.proc = None

        text = b"".join(chunks).decode("utf-8", errors="replace")
        meta: dict = {}
        if meta_out.is_file():
            try:
                meta = json.loads(meta_out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        if job.skip_event.is_set() and not job.cancel_event.is_set():
            return job.raw, {"via": "skip", "private": is_private_take(voxtype_rt(), job.capture_dir), "mode": job.mode, "pp_ms": 0, "llm_ms": 0}
        if not meta and p.returncode != 0:
            # Fail-safe: never drop words on unexpected cleanup crash unless private or command mode.
            priv = is_private_take(voxtype_rt(), job.capture_dir) or job.bad_id
            fallback_text = "" if job.mode == "command" else job.raw
            return fallback_text, {"via": "private" if priv else "passthrough", "private": priv, "mode": job.mode, "pp_ms": 0, "llm_ms": 0}
        return text, meta

    def _run_streaming_take(self, job: TakeJob, env: dict[str, str], rt: Path, toml_doc: dict, meta_out: Path) -> None:
        script = str(LIBEXEC / "voxtype-postprocess.sh")
        cmd = [script, job.mode] if job.mode and job.mode != "default" else [script]
        ce = self._cleanup_env(job, meta_out, stream=True)
        timeout_s = self._cleanup_budget_s(job.raw, job.mode, env)
        smart_join_on = _bool_opt(env, "PP_SMART_JOIN", True)
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=ce,
        )
        with job.proc_lock:
            job.proc = p

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        typed_parts: list[str] = []
        all_parts: list[str] = []
        untyped_parts: list[str] = []
        streamed_live = False
        lost_target = False
        driver_failed = False
        buffered_whole: list[str] = []
        deadline = time.monotonic() + timeout_s
        try:
            assert p.stdin is not None and p.stdout is not None
            try:
                p.stdin.write(job.raw.encode("utf-8"))
                p.stdin.close()
            except BrokenPipeError:
                pass
            fd = p.stdout.fileno()
            while True:
                if job.cancel_event.is_set() or job.skip_event.is_set():
                    p.terminate()
                    break
                rem = deadline - time.monotonic()
                if rem <= 0:
                    p.terminate()
                    break
                r, _, _ = select.select([fd], [], [], min(0.05, rem))
                if not r:
                    continue
                data = os.read(fd, 65536)
                if not data:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        all_parts.append(tail)
                        if streamed_live:
                            if lost_target or driver_failed:
                                untyped_parts.append(tail)
                            elif target_still_focused(job.capture_dir, bad_id=job.bad_id, tagged=job.tagged):
                                ok, _ = type_text(tail, env)
                                if ok:
                                    typed_parts.append(tail)
                                else:
                                    driver_failed = True
                                    untyped_parts.append(tail)
                            else:
                                lost_target = True
                                untyped_parts.append(tail)
                        else:
                            buffered_whole.append(tail)
                    break
                piece = decoder.decode(data, final=False)
                if not piece:
                    continue
                # If meta_out already exists before we typed anything, postprocess finished
                # non-streaming (e.g. private, snippet, short, or fallback): defer to whole-text path.
                if not streamed_live and meta_out.is_file():
                    buffered_whole.append(piece)
                    continue
                streamed_live = True
                all_parts.append(piece)
                if lost_target or driver_failed:
                    untyped_parts.append(piece)
                    continue
                # Re-verify target focus right before typing every streamed chunk.
                if not target_still_focused(job.capture_dir, bad_id=job.bad_id, tagged=job.tagged):
                    lost_target = True
                    untyped_parts.append(piece)
                    continue
                to_type = piece
                if not typed_parts:
                    to_type = apply_smart_join(to_type, rt, job.capture_dir, mode=job.mode, enabled=smart_join_on)
                    if job.capture_dir:
                        shuush_timing.record_events(job.capture_dir, ["delivery_start"], take_id=job.take_id)
                ok, _ = type_text(to_type, env)
                if ok:
                    typed_parts.append(to_type)
                else:
                    driver_failed = True
                    untyped_parts.append(piece)
            try:
                p.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=1.0)
        finally:
            with job.proc_lock:
                job.proc = None

        meta: dict = {}
        if meta_out.is_file():
            try:
                meta = json.loads(meta_out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}

        if job.cancel_event.is_set():
            self._finalize_cancelled(job, rt)
            return

        if not streamed_live:
            if job.skip_event.is_set():
                text = job.raw
                meta = {"via": "skip", "private": is_private_take(rt, job.capture_dir), "mode": job.mode, "pp_ms": 0, "llm_ms": 0}
            else:
                text = "".join(buffered_whole)
            self._deliver_whole(job, text, meta, env, rt, toml_doc)
            return

        # Streamed path completed (or lost focus mid-stream / driver failed / skipped).
        full_text = "".join(typed_parts) + "".join(untyped_parts)
        if not full_text:
            full_text = "".join(all_parts)
        private = bool(meta.get("private")) or is_private_take(rt, job.capture_dir)
        append_text = (env.get("PP_APPEND_TEXT") or "") if (not private and job.mode != "command") else ""
        auto_submit = _bool_opt(env, "PP_AUTO_SUBMIT", False)

        if not lost_target and not driver_failed and typed_parts:
            if append_text and target_still_focused(job.capture_dir, bad_id=job.bad_id, tagged=job.tagged):
                type_text(append_text, env)
                full_text += append_text
            if auto_submit and target_still_focused(job.capture_dir, bad_id=job.bad_id, tagged=job.tagged):
                send_return(env)
            if job.capture_dir:
                shuush_timing.record_events(job.capture_dir, ["delivery_done"], take_id=job.take_id)
            self._finalize_result(
                job, full_text, meta, env, rt,
                disposition="delivered", method="stream_type", private=private,
            )
            return

        if lost_target:
            recover_text = "".join(untyped_parts) if typed_parts else full_text
            if job.capture_dir:
                shuush_timing.record_events(job.capture_dir, ["delivery_done"], take_id=job.take_id)
            self._finalize_target_lost(job, recover_text, meta, env, rt, private=private)
            return

        if job.capture_dir:
            shuush_timing.record_events(job.capture_dir, ["delivery_done"], take_id=job.take_id)
        self._finalize_driver_failed(job, full_text, meta, env, rt, private=private)

    def _process_job(self, job: TakeJob) -> None:
        rt = voxtype_rt()
        env = load_pp_env()
        toml_doc = read_voxtype_toml()
        if job.cancel_event.is_set():
            self._finalize_cancelled(job, rt)
            return

        with tempfile.TemporaryDirectory(prefix="shuush-deliver-meta.") as tmpdir:
            meta_out = Path(tmpdir) / "meta.json"
            if self._can_stream(job, env, rt):
                with self._deliver_lock:
                    if job.cancel_event.is_set():
                        self._finalize_cancelled(job, rt)
                        return
                    self._run_streaming_take(job, env, rt, toml_doc, meta_out)
                return

            text, meta = self._run_whole_cleanup(job, env, meta_out)
            if job.cancel_event.is_set():
                self._finalize_cancelled(job, rt)
                return

            if not self._wait_for_turn(job):
                self._finalize_cancelled(job, rt)
                return

            with self._deliver_lock:
                if job.cancel_event.is_set():
                    self._finalize_cancelled(job, rt)
                    return
                self._deliver_whole(job, text, meta, env, rt, toml_doc)

    def _deliver_whole(
        self,
        job: TakeJob,
        text: str,
        meta: dict,
        env: dict[str, str],
        rt: Path,
        toml_doc: dict,
    ) -> None:
        private = bool(meta.get("private")) or job.bad_id or is_private_take(rt, job.capture_dir)
        mode = str(meta.get("mode") or job.mode or "default")
        via = "private" if private else str(meta.get("via") or "passthrough")
        meta = {**meta, "via": via, "mode": mode, "private": private}

        if private:
            self._mark_private_runtime(job, rt)
            if mode == "command" or job.bad_id:
                text = ""

        if not text:
            if private:
                self._finalize_result(job, "", meta, env, rt, disposition="private_not_inserted", method="none", private=True)
            else:
                clear_outline_if_current(rt, job.take_id)
                write_outcome(rt, job.take_id, "empty", chars=0, session_id=job.session_id, method="none", private=False)
            return

        # Re-check focus RIGHT BEFORE insertion.
        if not target_still_focused(job.capture_dir, bad_id=job.bad_id, tagged=job.tagged):
            self._finalize_target_lost(job, text, meta, env, rt, private=private)
            return

        smart_join_on = _bool_opt(env, "PP_SMART_JOIN", True)
        if not private and mode != "command":
            text = apply_smart_join(text, rt, job.capture_dir, mode=mode, enabled=smart_join_on)

        smart_submit_on = _bool_opt(env, "PP_SMART_AUTO_SUBMIT", False)
        auto_submit_on = _bool_opt(env, "PP_AUTO_SUBMIT", False)
        do_submit = auto_submit_on
        if not private and mode != "command" and smart_submit_on:
            text, triggered = apply_smart_auto_submit(text, True)
            if triggered:
                do_submit = True

        append_text = env.get("PP_APPEND_TEXT") or ""
        if not private and mode != "command" and append_text and text:
            text = text + append_text

        app_class = self._app_class(job, meta)
        app_family = shuush_timing.classify_app_family(app_class, mode=mode)
        paste_over = _int_opt(env, "PP_PASTE_OVER_CHARS", DEFAULT_PASTE_OVER_CHARS)
        keep_clip = _bool_opt(env, "PP_KEEP_ON_CLIPBOARD", True)

        # Private takes NEVER go via clipboard — always type directly regardless of length!
        use_paste = (not private) and (paste_over > 0) and (len(text) > paste_over)

        if job.capture_dir:
            shuush_timing.record_events(job.capture_dir, ["delivery_start"], take_id=job.take_id)

        if not text and do_submit:
            ok = send_return(env)
            method = "type" if ok else "failed"
        elif use_paste:
            ok, method, _err = paste_text(
                text, rt, env,
                app_family=app_family, toml_doc=toml_doc, keep_on_clipboard=keep_clip,
            )
            if ok and do_submit:
                send_return(env)
        else:
            ok, _err = type_text(text, env)
            method = "type" if ok else "failed"
            if ok and do_submit:
                send_return(env)

        if job.capture_dir:
            shuush_timing.record_events(job.capture_dir, ["delivery_done"], take_id=job.take_id)

        if ok:
            self._finalize_result(
                job, text, meta, env, rt,
                disposition="delivered", method=method, private=private,
                already_on_clipboard=(method == "paste" and keep_clip),
            )
        else:
            self._finalize_driver_failed(job, text, meta, env, rt, private=private)

    def _app_class(self, job: TakeJob, meta: dict) -> str:
        c = str(meta.get("app_class") or "").strip()
        if c:
            return c
        if job.capture_dir and (job.capture_dir / "target_window").is_file():
            w = read_target_window(job.capture_dir / "target_window") or {}
            return str(w.get("class") or "").strip()
        return ""

    def _mark_private_runtime(self, job: TakeJob, rt: Path) -> None:
        try:
            atomic_write_text(rt / "pp_private", f"{int(time.time())}\n")
        except OSError:
            pass
        if job.capture_dir:
            try:
                (job.capture_dir / "pp_private").touch()
            except OSError:
                pass
        if shuush_caps.has(["read_field"]) == 0 or shuush_caps.has(["preedit"]) == 0:
            try:
                (rt / "field_text.off").touch()
            except OSError:
                pass
        for p in (rt / "field_text.json", rt / "last_take.wav"):
            p.unlink(missing_ok=True)

    def _purge_private_capture(self, job: TakeJob) -> None:
        if not job.capture_dir:
            return
        for name in ("take_selection", "take_screen_ctx.json", "take_clip_start", "take_clip", "command_selection", "recovered.txt"):
            (job.capture_dir / name).unlink(missing_ok=True)

    def _finalize_cancelled(self, job: TakeJob, rt: Path) -> None:
        clear_outline_if_current(rt, job.take_id)
        if job.capture_dir:
            try:
                atomic_write_text(job.capture_dir / "disposition", "cancelled\n")
                (job.capture_dir / "shuush_delivery").touch()
            except OSError:
                pass
        write_outcome(rt, job.take_id, "cancelled", chars=0, session_id=job.session_id, method="cancelled", private=is_private_take(rt, job.capture_dir))

    def _finalize_target_lost(self, job: TakeJob, text: str, meta: dict, env: dict[str, str], rt: Path, *, private: bool) -> None:
        if private:
            self._mark_private_runtime(job, rt)
            notify_user("Private dictation not inserted · target changed")
            self._finalize_result(job, "", meta, env, rt, disposition="private_not_inserted", method="none", private=True)
            return
        disposition = "recovered"
        if job.capture_dir and text:
            try:
                atomic_write_text(job.capture_dir / "recovered.txt", text, mode=0o600)
            except OSError:
                disposition = "failed"
        notify_user("Target changed · open Shuush Studio to copy this take")
        self._finalize_result(job, text, meta, env, rt, disposition=disposition, method="recovered", private=False)

    def _finalize_driver_failed(self, job: TakeJob, text: str, meta: dict, env: dict[str, str], rt: Path, *, private: bool) -> None:
        if private:
            self._mark_private_runtime(job, rt)
            notify_user("Private dictation failed · not copied to clipboard")
            self._finalize_result(job, "", meta, env, rt, disposition="private_not_inserted", method="failed", private=True)
            return
        keep_clip = _bool_opt(env, "PP_KEEP_ON_CLIPBOARD", True)
        disposition = "failed"
        method = "failed"
        if job.capture_dir and text:
            try:
                atomic_write_text(job.capture_dir / "recovered.txt", text, mode=0o600)
            except OSError:
                pass
        if keep_clip and text and wl_copy_text(text):
            record_self_clipboard_hash(rt, text)
            disposition = "clipboard_only"
            method = "clipboard_only"
            notify_user("Typing failed · dictation copied to clipboard")
        else:
            notify_user("Delivery failed · open Shuush Studio to copy this take")
        self._finalize_result(job, text, meta, env, rt, disposition=disposition, method=method, private=False, already_on_clipboard=(disposition == "clipboard_only"))

    def _finalize_result(
        self,
        job: TakeJob,
        text: str,
        meta: dict,
        env: dict[str, str],
        rt: Path,
        *,
        disposition: str,
        method: str,
        private: bool,
        already_on_clipboard: bool = False,
    ) -> None:
        clear_outline_if_current(rt, job.take_id)
        via = "private" if private else str(meta.get("via") or "passthrough")
        mode = str(meta.get("mode") or job.mode or "default")
        app_class = self._app_class(job, meta)
        app_family = shuush_timing.classify_app_family(app_class, mode=mode)

        if job.capture_dir:
            try:
                atomic_write_text(job.capture_dir / "disposition", f"{disposition}\n")
                (job.capture_dir / "shuush_delivery").touch()
            except OSError:
                pass

        write_outcome(rt, job.take_id, disposition, chars=len(text), session_id=job.session_id, method=method, private=private)

        if is_current_take(rt, job.take_id):
            if not private and mode != "command" and text:
                try:
                    atomic_write_text(rt / "pp_final", f"{int(time.time() * 1000)}\n{text.lstrip(' ')}")
                except OSError:
                    pass
            else:
                (rt / "pp_final").unlink(missing_ok=True)
            try:
                atomic_write_text(rt / "pp_result", f"{int(time.time() * 1000)} {via} {job.take_id}\n")
            except OSError:
                pass

        if private:
            self._purge_private_capture(job)
            return

        if not text.strip():
            return

        if is_current_take(rt, job.take_id):
            (rt / "pp_private").unlink(missing_ok=True)
            (rt / "field_text.off").unlink(missing_ok=True)

        # Clipboard policy: unchanged from daemon mode.
        # On delivered takes, copy to clipboard when PP_KEEP_ON_CLIPBOARD != 0;
        # on recovered takes, leave clipboard untouched (user recovers via Studio or `shuush recover`).
        keep_clip = _bool_opt(env, "PP_KEEP_ON_CLIPBOARD", True)
        if disposition == "delivered":
            record_self_clipboard_hash(rt, text)
            if keep_clip and not already_on_clipboard:
                wl_copy_text(text)

        # Summarize per-take timing into timing.json and history.jsonl via voxtype-review.py.
        extra_ts: dict[str, int] = {}
        for k in ("cleanup_first_byte", "cleanup_last_byte"):
            v = meta.get(f"{k}_ms")
            if isinstance(v, (int, float)) and v > 0:
                extra_ts[k] = int(v)
        summary = shuush_timing.summarize(
            job.capture_dir,
            take_id=job.take_id,
            delivery_mode="shuush",
            delivery_method=method,
            app_class=app_class,
            mode=mode,
            text_chars=len(text),
            extra_ts=extra_ts or None,
            write_file=bool(job.capture_dir),
        )
        timing_compact = shuush_timing.compact_for_history(summary)

        rv_env = os.environ.copy()
        rv_env.update({
            "RV_TAKE_ID": job.take_id,
            "RV_TAGGED": "1",
            "RV_DISPOSITION": disposition,
            "RV_TARGET": str(job.capture_dir / "target_window") if job.capture_dir else "",
            "RV_SESSION_ID": job.session_id,
            "RV_TS": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "RV_MODE": mode,
            "RV_VIA": via,
            "RV_RAW": job.raw,
            "RV_CLEAN": text,
            "RV_PP": str(int(meta.get("pp_ms") or 0)),
            "RV_LLM": str(int(meta.get("llm_ms") or 0)),
            "RV_T0": str(int(time.time() * 1000)),
            "RV_SCREEN_TERMS": str(meta.get("screen_terms") or ""),
            "RV_DELIVERY_MODE": "shuush",
            "RV_DELIVERY_METHOD": method,
            "RV_APP_CLASS": app_class,
            "RV_APP_FAMILY": app_family,
            "RV_TIMING_JSON": json.dumps(timing_compact, separators=(",", ":")),
        })
        try:
            subprocess.run(
                [sys.executable, "-I", str(LIBEXEC / "voxtype-review.py")],
                env=rv_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            buf = b""
            while b"\n" not in buf and len(buf) < 4 * 1024 * 1024:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
            if not line:
                resp = {"ok": False, "error": "empty_request"}
            else:
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "ping" or cmd == "status":
                    resp = self._active_summary()
                elif cmd == "enqueue":
                    resp = self.enqueue(req)
                elif cmd == "cancel":
                    resp = self.cancel(str(req.get("take_id") or ""))
                elif cmd == "skip":
                    resp = self.skip()
                elif cmd == "shutdown":
                    self._stop.set()
                    resp = {"ok": True}
                else:
                    resp = {"ok": False, "error": f"unknown_cmd:{cmd}"}
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve(self) -> int:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.socket_path.parent, 0o700)
        except OSError:
            pass
        if self.socket_path.exists():
            self.socket_path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock = srv
        with unix_addr(self.socket_path) as addr:
            srv.bind(addr)
        os.chmod(self.socket_path, 0o600)
        srv.listen(16)
        srv.settimeout(0.25)

        pid_file = self.socket_path.with_name("deliver.pid")
        try:
            atomic_write_text(pid_file, f"{os.getpid()}\n")
        except OSError:
            pass

        def _on_sig(_signum, _frame):
            self._stop.set()

        signal.signal(signal.SIGTERM, _on_sig)
        signal.signal(signal.SIGINT, _on_sig)

        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._handle_client(conn)
        finally:
            srv.close()
            self.socket_path.unlink(missing_ok=True)
            pid_file.unlink(missing_ok=True)
            with self._lock:
                threads = list(self._threads)
            for t in threads:
                t.join(timeout=5.0)
        return 0


# ── Client RPC helpers ────────────────────────────────────────────────────────


def rpc(payload: dict, socket_path: Path | None = None, timeout: float = SOCKET_TIMEOUT_S) -> dict | None:
    sock_path = socket_path or default_socket_path()
    if not sock_path.exists():
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        with unix_addr(sock_path) as addr:
            s.connect(addr)
        s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < 1024 * 1024:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        return json.loads(line) if line else None
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def cmd_enqueue(args) -> int:
    ok_cfg, reason = config_allows_handoff()
    if not ok_cfg:
        sys.stderr.write(f"shuush-deliver: handoff refused ({reason})\n")
        return 1
    raw = sys.stdin.read()
    payload = {
        "cmd": "enqueue",
        "take_id": args.take_id or "",
        "raw": raw,
        "mode": args.mode or "default",
        "profile": args.profile or "",
        "session_id": args.session_id or "",
        "capture_dir": args.capture_dir or "",
        "transcript_ready_ms": args.transcript_ready_ms if args.transcript_ready_ms is not None else shuush_timing.mono_ms(),
        "voxtype_context": os.environ.get("VOXTYPE_CONTEXT", ""),
        "bad_id": args.bad_id,
        "tagged": args.tagged,
    }
    resp = rpc(payload, Path(args.socket) if args.socket else None)
    if resp and resp.get("ok"):
        return 0
    err = (resp or {}).get("error", "worker_unreachable")
    sys.stderr.write(f"shuush-deliver: handoff failed ({err})\n")
    return 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="shuush-deliver")
    ap.add_argument("--socket", default="", help="Unix socket path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the delivery queue service")
    sub.add_parser("ping", help="check if the delivery service is running")
    sub.add_parser("status", help="print delivery service queue status as JSON")
    sub.add_parser("skip", help="skip cleanup on the active take and deliver raw text")

    p_can = sub.add_parser("cancel", help="cancel active/queued take(s)")
    p_can.add_argument("--take-id", default="")

    p_enq = sub.add_parser("enqueue", help="enqueue a take from voxtype-postprocess.sh")
    p_enq.add_argument("--take-id", default="")
    p_enq.add_argument("--mode", default="default")
    p_enq.add_argument("--profile", default="")
    p_enq.add_argument("--session-id", default="")
    p_enq.add_argument("--capture-dir", default="")
    p_enq.add_argument("--transcript-ready-ms", type=int, default=None)
    p_enq.add_argument("--bad-id", action="store_true")
    p_enq.add_argument("--tagged", action="store_true")

    sub.add_parser("doctor-info", help="print delivery status summary as JSON for shuush doctor")

    a = ap.parse_args(argv)
    sock = Path(a.socket) if a.socket else default_socket_path()

    if a.cmd == "serve":
        return DeliveryServer(sock).serve()
    if a.cmd == "enqueue":
        return cmd_enqueue(a)
    if a.cmd == "doctor-info":
        mode = delivery_mode_configured()
        toml_doc = read_voxtype_toml()
        out_cfg = toml_doc.get("output") or {}
        pp_cfg = out_cfg.get("post_process") or {}
        txt_cfg = toml_doc.get("text") or {}
        caps_doc = shuush_caps.capabilities()
        handoff_cap = bool((caps_doc.get("caps") or {}).get("handoff"))
        ping_resp = rpc({"cmd": "ping"}, sock, timeout=0.6)
        worker_active = bool(ping_resp and ping_resp.get("ok"))
        fallback_on_empty = pp_cfg.get("fallback_on_empty")
        auto_submit = out_cfg.get("auto_submit")
        smart_auto_submit = txt_cfg.get("smart_auto_submit")
        append_text = out_cfg.get("append_text")
        empty_safe = (
            auto_submit is not True
            and smart_auto_submit is not True
            and not (isinstance(append_text, str) and len(append_text) > 0)
        )
        effective = "shuush" if (mode == "shuush" and handoff_cap and worker_active and fallback_on_empty is False) else "daemon"
        print(json.dumps({
            "mode": mode,
            "effective": effective,
            "worker_active": worker_active,
            "handoff_capable": handoff_cap,
            "fallback_on_empty": fallback_on_empty,
            "auto_submit": auto_submit,
            "smart_auto_submit": smart_auto_submit,
            "append_text": append_text,
            "empty_safe": empty_safe,
            "socket": str(sock),
        }))
        return 0
    if a.cmd in ("ping", "status"):
        resp = rpc({"cmd": a.cmd}, sock)
        if not resp or not resp.get("ok"):
            return 1
        if a.cmd == "status":
            print(json.dumps(resp))
        return 0
    if a.cmd == "skip":
        resp = rpc({"cmd": "skip"}, sock)
        return 0 if resp and resp.get("ok") else 1
    if a.cmd == "cancel":
        resp = rpc({"cmd": "cancel", "take_id": a.take_id}, sock)
        return 0 if resp and resp.get("ok") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
