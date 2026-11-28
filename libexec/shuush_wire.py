#!/usr/bin/env python3
"""shuush wire / unwire: the few keys Shuush owns in voxtype's config.toml.

Shuush writes them with `voxtype config set` (validated, comments kept, exit 2
for a key the binary does not know) and writes down what it changed in
$XDG_CONFIG_HOME/shuush/wired.json: the value before and the value set. `unwire`
puts every key back the way it was. A key you changed after wiring is yours:
neither command touches it without --force.

    shuush wire [--dry-run] [--force] [--json]
    shuush unwire [--dry-run] [--force] [--json]
    shuush wire --status [--json]

With PP_IME=shuush (postprocess.env or the environment) Shuush's own input
method helper, shuush-ime, holds the seat during a take, so the daemon's own
`[output] preedit` and `read_field` are turned off and recorded like every other
key; with PP_IME off again, `wire` puts them back as they were.

The one exception to `voxtype config set` is `[profiles.*] post_process_command`:
the binary has no settable key for a profile, so those lines are edited in
config.toml directly, as exact line operations the manifest can reverse.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shuush_caps  # noqa: E402

MANIFEST_VERSION = 1
PROFILES = ("terminal", "chat", "command")
DELIVERY_ONLY_KEYS = ("output.auto_submit", "text.smart_auto_submit", "output.append_text")
# The daemon's own input method; off while shuush-ime has the seat.
IME_KEYS = ("output.preedit", "output.read_field")
HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


def configured_delivery_mode() -> str:
    env_val = (os.environ.get("PP_DELIVERY") or "").strip().lower()
    if env_val in ("shuush", "daemon", "voxtype"):
        return "shuush" if env_val == "shuush" else "daemon"
    pp_env = conf_dir() / "postprocess.env"
    if pp_env.is_file():
        try:
            for line in pp_env.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r'^\s*(?:export\s+)?PP_DELIVERY\s*=\s*"?([A-Za-z0-9_-]+)"?', line)
                if m:
                    env_val = m.group(1).lower()
        except OSError:
            pass
    return "shuush" if env_val == "shuush" else "daemon"


def _seed_postprocess_env(key: str, value: str) -> bool:
    """Record a migrated setting in postprocess.env if not already present."""
    p = conf_dir() / "postprocess.env"
    existing = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    if re.search(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", existing, re.MULTILINE):
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    encoded = json.dumps(value) if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) else value
    line = f"{key}={encoded}\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(existing + sep + line, encoding="utf-8")
    if p.is_file():
        shutil.copymode(p, tmp)
    os.replace(tmp, p)
    return True


def conf_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "voxtype"


def manifest_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "shuush" / "wired.json"


def load_manifest() -> dict:
    try:
        d = json.loads(manifest_path().read_text())
        if isinstance(d, dict) and isinstance(d.get("keys"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"version": MANIFEST_VERSION, "keys": {}}


def save_manifest(m: dict) -> None:
    p = manifest_path()
    if not m["keys"]:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, p)


def fmt(v) -> str:
    """The text `voxtype config set` takes for a value."""
    return "true" if v is True else "false" if v is False else str(v)


def ime_wanted() -> bool:
    """PP_IME=shuush in the environment, else in postprocess.env."""
    v = os.environ.get("PP_IME")
    if v is None:
        try:
            for line in (conf_dir() / "postprocess.env").read_text(encoding="utf-8").splitlines():
                m = re.match(r"^PP_IME=([a-z]*)", line)
                if m:
                    v = m.group(1)
        except OSError:
            pass
    return v == "shuush"


def show(v) -> str:
    return "not set" if v is None else json.dumps(v)


# ── the voxtype side ──────────────────────────────────────────────────────────


class Voxtype:
    def __init__(self, binary: str):
        self.binary = binary

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.binary, *args], capture_output=True, text=True,
                              timeout=20, stdin=subprocess.DEVNULL)

    def schema(self) -> tuple[dict[str, dict], dict[str, object]]:
        """(settable keys, what config.toml holds for each: None when absent)."""
        r = self.run("config", "schema", "--json")
        if r.returncode != 0:
            raise RuntimeError("`voxtype config schema --json` failed: " + (r.stderr.strip().splitlines() or ["?"])[0])
        doc = json.loads(r.stdout)
        keys = {k["key"]: k for k in doc["keys"] if k.get("compiled", True)}
        return keys, {k: v.get("file_value") for k, v in keys.items()}

    def set(self, key: str, value) -> str | None:
        r = self.run("config", "set", key, fmt(value))
        return None if r.returncode == 0 else ((r.stderr.strip() or r.stdout.strip()).splitlines() or ["failed"])[0]

    def unset(self, key: str) -> str | None:
        r = self.run("config", "unset", key)
        return None if r.returncode == 0 else ((r.stderr.strip() or r.stdout.strip()).splitlines() or ["failed"])[0]


def find_voxtype() -> Voxtype:
    binary, _ = shuush_caps.resolve_binary()
    if not binary:
        raise RuntimeError("voxtype is not installed (install voxtype-bin 1.3.1 or newer)")
    caps = shuush_caps.capabilities(binary)["caps"]
    if not caps.get("config_set"):
        raise RuntimeError(f"{binary} has no `config set`: Shuush needs voxtype 1.3.1 or newer")
    return Voxtype(binary)


# ── config.toml, for the profile lines only ───────────────────────────────────


def read_cfg() -> str:
    return (conf_dir() / "config.toml").read_text(encoding="utf-8")


def write_cfg(text: str) -> None:
    tomllib.loads(text)
    p = conf_dir() / "config.toml"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    shutil.copymode(p, tmp)
    os.replace(tmp, p)


def toml_get(text: str, section: str, key: str):
    d = tomllib.loads(text)
    for part in section.split("."):
        d = d.get(part, {}) if isinstance(d, dict) else {}
    return d.get(key) if isinstance(d, dict) else None


def _section_span(lines: list[str], section: str) -> tuple[int, int] | None:
    start = None
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line)
        if m:
            if start is not None:
                return start, i
            if m.group(1).strip() == section:
                start = i
    return (start, len(lines)) if start is not None else None


def toml_set(text: str, section: str, key: str, value: str) -> tuple[str, dict]:
    """Set `key = "value"` under [section]; the record says how to take it back."""
    new_line = f"{key} = {json.dumps(value)}\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    span = _section_span(lines, section)
    if span is None:
        block = ("\n" if lines and lines[-1].strip() else "") + f"[{section}]\n{new_line}"
        return "".join(lines) + block, {"mode": "section", "block": block}
    start, end = span
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            old = lines[i]
            lines[i] = new_line
            return "".join(lines), {"mode": "line", "old_line": old, "new_line": new_line}
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    lines.insert(at, new_line)
    return "".join(lines), {"mode": "insert", "new_line": new_line}


def toml_restore(text: str, record: dict) -> str | None:
    """Undo a toml_set; None when the file no longer looks the way it left it."""
    mode = record["mode"]
    if mode == "section":
        return text.replace(record["block"], "", 1) if text.count(record["block"]) == 1 else None
    new = record["new_line"]
    if text.count(new) != 1:
        return None
    return text.replace(new, record.get("old_line", ""), 1)


def toml_update(text: str, section: str, key: str, value: str, entry: dict | None) -> tuple[str, dict]:
    """toml_set, but a line Shuush wrote earlier is changed in place, so the record
    still takes the file back to what it was before the first wiring."""
    new_line = f"{key} = {json.dumps(value)}\n"
    rec = (entry or {}).get("toml")
    if rec:
        prev = f"{key} = {json.dumps(entry['new'])}\n"
        if rec["mode"] == "section" and text.count(rec["block"]) == 1 and prev in rec["block"]:
            block = rec["block"].replace(prev, new_line, 1)
            return text.replace(rec["block"], block, 1), {"mode": "section", "block": block}
        if rec["mode"] in ("line", "insert") and text.count(rec["new_line"]) == 1:
            return text.replace(rec["new_line"], new_line, 1), {**rec, "new_line": new_line}
    return toml_set(text, section, key, value)


# ── the plan ──────────────────────────────────────────────────────────────────


def plan(pp_cmd: str, delivery: str = "daemon", ime: bool = False) -> list[dict]:
    items = [
        {"key": "output.post_process.command", "value": pp_cmd},
        {"key": "output.post_process.timeout_ms", "value": 20000},
        # Keep the leading space smart join adds (Shuush trims the model's own).
        {"key": "output.post_process.trim", "value": False},
        {"key": "output.post_process.fallback_on_empty", "value": False},
        {"key": "osd.style", "value": "kitsune-hud"},
        # Left alone when you already chose one.
        {"key": "osd.frontend", "value": "auto", "seed": True},
        {"key": "osd.position", "value": "bottom-center", "seed": True},
        {"key": "osd.margin_px", "value": 20, "seed": True},
    ]
    if delivery == "shuush":
        # Stock voxtype still acts on an empty post-process reply when these are on:
        # Shuush performs submit and append_text itself in shuush delivery mode.
        items.extend([
            {"key": "output.auto_submit", "value": False, "delivery_only": True},
            {"key": "text.smart_auto_submit", "value": False, "delivery_only": True},
            {"key": "output.append_text", "value": None, "delivery_only": True},
        ])
    for p in PROFILES:
        items.append({"key": f"profiles.{p}.post_process_command", "value": f"{pp_cmd} {p}", "via": "toml"})
    if ime:
        items += [{"key": k, "value": False, "ime": True} for k in IME_KEYS]
    return items


def decide(item: dict, cur, entry: dict | None, force: bool) -> tuple[str, str]:
    """(action, why). Actions: set, unset, ok, skip."""
    want = item["value"]
    op = "unset" if want is None else "set"
    if entry is not None:
        if cur == want:
            return "ok", "already off (not set)" if want is None else "already set"
        if cur == entry["new"] or force:
            return op, "ours, updating" if cur == entry["new"] else "changed since, --force"
        return "skip", f"you changed it to {show(cur)} since; --force overwrites"
    if cur == want:
        return "ok", "already off (not set)" if want is None else "already set"
    if item.get("seed") and cur is not None:
        return "skip", f"yours ({show(cur)}), left alone"
    return op, "not set" if cur is None else f"was {show(cur)}"


def current_values(vox: Voxtype, items: list[dict]) -> tuple[dict[str, dict], dict[str, object]]:
    keys, filevals = vox.schema()
    cur: dict[str, object] = {}
    cfg = read_cfg() if (conf_dir() / "config.toml").exists() else ""
    for it in items:
        if it.get("via") == "toml":
            section, _, name = it["key"].rpartition(".")
            cur[it["key"]] = toml_get(cfg, section, name) if cfg else None
        else:
            cur[it["key"]] = filevals.get(it["key"])
    return keys, cur


def backup() -> Path | None:
    p = conf_dir() / "config.toml"
    if not p.exists():
        return None
    b = p.with_name(f"config.toml.bak.{datetime.datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(p, b)
    return b


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_wire(a) -> int:
    if not (conf_dir() / "config.toml").exists():
        print(f"No {conf_dir() / 'config.toml'} found: run 'voxtype setup' first, then 'shuush wire'.", file=sys.stderr)
        return 1
    delivery = a.delivery or configured_delivery_mode()
    man = load_manifest()
    ime = ime_wanted()
    # PP_IME is off again: the daemon's own input method keys go back.
    giveback = [] if ime else [{"key": k, "via": "voxtype"} for k in IME_KEYS
                               if man["keys"].get(k, {}).get("via", "voxtype") == "voxtype" and k in man["keys"]]
    try:
        vox = find_voxtype()
        items = plan(a.command, delivery=delivery, ime=ime)
        extra_restore_keys = [k for k in DELIVERY_ONLY_KEYS if delivery != "shuush" and k in man["keys"]]
        probe_items = list(items) + [{"key": k} for k in extra_restore_keys] + giveback
        keys, cur = current_values(vox, probe_items)
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
        print(f"shuush wire: {e}", file=sys.stderr)
        return 1
    rows = []
    for it in giveback:
        entry = man["keys"][it["key"]]
        now = cur.get(it["key"])
        if now == entry["new"] or a.force:
            rows.append({**it, "action": "restore", "why": "PP_IME is off: back to the daemon", "now": now, "value": entry["old"]})
        else:
            rows.append({**it, "action": "skip", "why": f"you changed it to {show(now)} since; --force restores anyway", "now": now, "value": entry["old"]})
    for it in items:
        entry = man["keys"].get(it["key"])
        if it.get("via") != "toml" and it["key"] not in keys:
            rows.append({**it, "action": "skip", "why": "this voxtype does not know the key", "now": None})
            continue
        action, why = decide(it, cur[it["key"]], entry, a.force)
        rows.append({**it, "action": action, "why": why, "now": cur[it["key"]]})

    # Switching back from shuush -> daemon delivery: restore delivery-only keys Shuush turned off.
    for key in extra_restore_keys:
        entry = man["keys"][key]
        now = cur.get(key)
        if now != entry["new"] and not a.force:
            rows.append({"key": key, "value": entry["old"], "action": "skip", "why": f"you changed it to {show(now)} since; --force restores anyway", "now": now, "restore_delivery": True})
        else:
            rows.append({"key": key, "value": entry["old"], "action": "restore", "why": "restored for daemon delivery", "now": now, "restore_delivery": True})

    if a.dry_run:
        out = {"dry_run": True, "delivery": delivery, "binary": vox.binary, "manifest": str(manifest_path()), "keys": rows}
        if a.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"shuush wire --dry-run: nothing is written ({vox.binary}, delivery={delivery})")
            for r in rows:
                if r["action"] == "unset":
                    print(f"  unset {r['key']}   (now: {show(r['now'])}; voxtype config unset {r['key']})")
                elif r["action"] == "restore":
                    how = f"voxtype config unset {r['key']}" if r["value"] is None else f"voxtype config set {r['key']} {fmt(r['value'])}"
                    print(f"  restore {r['key']}: {show(r['now'])} -> {show(r['value'])}   ({how})")
                elif r["action"] == "set":
                    how = ("edits config.toml: voxtype has no settable profile key" if r.get("via") == "toml"
                           else f"voxtype config set {r['key']} {fmt(r['value'])}")
                    print(f"  set   {r['key']} = {json.dumps(r['value'])}   (now: {show(r['now'])}; {how})")
                else:
                    print(f"  {r['action']:<5} {r['key']}   ({r['why']})")
            print(f"  would record old and new values in {manifest_path()}")
        return 0

    todo = [r for r in rows if r["action"] in ("set", "unset", "restore")]
    bak = backup() if todo else None
    if bak and not man["keys"]:
        man["backup"] = str(bak)
    failed = 0
    migrated: list[str] = []
    for r in rows:
        if r["action"] == "restore":
            err = vox.unset(r["key"]) if r["value"] is None else vox.set(r["key"], r["value"])
            if err:
                r["action"], r["why"] = "failed", err
                failed += 1
                continue
            del man["keys"][r["key"]]
            continue
        if r["action"] not in ("set", "unset"):
            if r["action"] == "ok" and r["key"] in man["keys"]:
                man["keys"][r["key"]]["new"] = r["value"]
            continue
        entry = man["keys"].get(r["key"])
        old = entry["old"] if entry else r["now"]
        # Carry active empty-string-sensitive settings over into Shuush's postprocess.env.
        if delivery == "shuush":
            if r["key"] == "output.auto_submit" and old is True:
                if _seed_postprocess_env("PP_AUTO_SUBMIT", "1"):
                    migrated.append("PP_AUTO_SUBMIT=1")
            elif r["key"] == "text.smart_auto_submit" and old is True:
                if _seed_postprocess_env("PP_SMART_AUTO_SUBMIT", "1"):
                    migrated.append("PP_SMART_AUTO_SUBMIT=1")
            elif r["key"] == "output.append_text" and isinstance(old, str) and old:
                if _seed_postprocess_env("PP_APPEND_TEXT", old):
                    migrated.append(f"PP_APPEND_TEXT={json.dumps(old)}")
        if r.get("via") == "toml":
            section, _, name = r["key"].rpartition(".")
            text, record = toml_update(read_cfg(), section, name, r["value"], entry)
            write_cfg(text)
            man["keys"][r["key"]] = {"old": old, "new": r["value"], "via": "toml", "toml": record}
        else:
            err = vox.unset(r["key"]) if r["action"] == "unset" else vox.set(r["key"], r["value"])
            if err:
                r["action"], r["why"] = "failed", err
                failed += 1
                continue
            man["keys"][r["key"]] = {"old": old, "new": r["value"], "via": "voxtype"}
    man.update({"version": MANIFEST_VERSION, "binary": vox.binary, "delivery": delivery,
                "wired_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
    save_manifest(man)
    if a.json:
        print(json.dumps({"dry_run": False, "delivery": delivery, "binary": vox.binary,
                          "backup": str(bak) if bak else None, "migrated": migrated, "keys": rows}, indent=2))
    else:
        for r in rows:
            if r["action"] == "failed":
                print(f"  failed {r['key']}: {r['why']}", file=sys.stderr)
            elif r["action"] == "skip":
                print(f"  skipped {r['key']} ({r['why']})")
        n = sum(1 for r in rows if r["action"] in ("set", "unset"))
        back = sum(1 for r in rows if r["action"] == "restore")
        print(f"Wired {n} key(s) in {conf_dir() / 'config.toml'} (delivery={delivery}); restore them with 'shuush unwire'.")
        if back:
            print(f"Gave {back} key(s) back to the daemon (shuush delivery or PP_IME switched off).")
        if migrated:
            print(f"Carried over to {conf_dir() / 'postprocess.env'}: {', '.join(migrated)}")
        n += back
        if n:
            print("Restart voxtype for the changes to apply: systemctl --user restart voxtype")
    return 1 if failed else 0


def cmd_unwire(a) -> int:
    man = load_manifest()
    if not man["keys"]:
        print("Nothing to unwire: Shuush has not recorded any change to config.toml.")
        return 0
    try:
        vox = find_voxtype()
        items = [{"key": k, "via": v.get("via", "voxtype")} for k, v in man["keys"].items()]
        keys, cur = current_values(vox, items)
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
        print(f"shuush unwire: {e}", file=sys.stderr)
        return 1
    rows = []
    for key, entry in reversed(list(man["keys"].items())):
        now = cur.get(key)
        if now != entry["new"] and not a.force:
            rows.append({"key": key, "action": "skip", "why": f"you changed it to {show(now)} since; --force restores anyway", "now": now, "old": entry["old"]})
        else:
            rows.append({"key": key, "action": "restore", "why": "", "now": now, "old": entry["old"]})
    if a.dry_run:
        if a.json:
            print(json.dumps({"dry_run": True, "keys": rows}, indent=2))
        else:
            print("shuush unwire --dry-run: nothing is written")
            for r in rows:
                if r["action"] == "restore":
                    how = "voxtype config unset" if r["old"] is None else f"voxtype config set {fmt(r['old'])}"
                    print(f"  restore {r['key']}: {show(r['now'])} -> {show(r['old'])}   ({how})")
                else:
                    print(f"  skip    {r['key']}   ({r['why']})")
        return 0
    if any(r["action"] == "restore" for r in rows):
        backup()
    failed = 0
    for r in rows:
        if r["action"] != "restore":
            continue
        entry = man["keys"][r["key"]]
        if entry.get("via") == "toml":
            text = toml_restore(read_cfg(), entry["toml"])
            if text is None:
                r["action"], r["why"] = "failed", "config.toml no longer looks the way wiring left it; edit it by hand"
                failed += 1
                continue
            write_cfg(text)
        else:
            err = vox.unset(r["key"]) if entry["old"] is None else vox.set(r["key"], entry["old"])
            if err:
                r["action"], r["why"] = "failed", err
                failed += 1
                continue
        del man["keys"][r["key"]]
    save_manifest(man)
    if a.json:
        print(json.dumps({"dry_run": False, "keys": rows}, indent=2))
    else:
        for r in rows:
            if r["action"] in ("skip", "failed"):
                print(f"  {r['action']} {r['key']} ({r['why']})", file=sys.stderr if r["action"] == "failed" else sys.stdout)
        n = sum(1 for r in rows if r["action"] == "restore")
        print(f"Restored {n} key(s) in {conf_dir() / 'config.toml'}.")
    return 1 if failed else 0


def cmd_status(a) -> int:
    man = load_manifest()
    rows = []
    if man["keys"]:
        try:
            vox = find_voxtype()
            items = [{"key": k, "via": v.get("via", "voxtype")} for k, v in man["keys"].items()]
            _, cur = current_values(vox, items)
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
            print(f"shuush wire --status: {e}", file=sys.stderr)
            return 1
        for key, entry in man["keys"].items():
            now = cur.get(key)
            state = "ok" if now == entry["new"] else "changed"
            rows.append({"key": key, "state": state, "now": now, "old": entry["old"], "new": entry["new"]})
    drift = [r["key"] for r in rows if r["state"] == "changed"]
    if a.json:
        print(json.dumps({"wired": bool(rows), "keys": rows, "changed": drift}, indent=2))
    elif not rows:
        print("not wired: Shuush has recorded no change to config.toml")
    else:
        for r in rows:
            print(f"  {r['state']:<7} {r['key']} = {show(r['now'])}" + (f"   (Shuush set {show(r['new'])}, was {show(r['old'])})" if r["state"] == "changed" else ""))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="shuush-wire", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("wire", "unwire", "status"))
    ap.add_argument("--dry-run", action="store_true", help="print what would change and write nothing")
    ap.add_argument("--force", action="store_true", help="also touch a key you changed after wiring")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--command", default="", help="path of the post-process script (wire)")
    ap.add_argument("--delivery", choices=("daemon", "shuush"), default=None,
                    help="delivery mode to wire for (default: PP_DELIVERY from postprocess.env)")
    a = ap.parse_args(argv)
    if a.action == "wire" and not a.command:
        a.command = str(Path(os.path.abspath(__file__)).with_name("voxtype-postprocess.sh"))
    return {"wire": cmd_wire, "unwire": cmd_unwire, "status": cmd_status}[a.action](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
