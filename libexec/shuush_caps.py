#!/usr/bin/env python3
"""shuush-caps: what the voxtype that runs the daemon can do.

Shuush runs on stock voxtype and on the companion fork. Every place that leans
on something only the fork has asks here first and degrades when the answer is
no. The answer comes from the daemon's own binary (the `voxtype` on PATH can be
a different build) and is cached by binary path, size and mtime, so it is
re-asked only when the binary changes.

    shuush-caps                  the whole answer as JSON
    shuush-caps has CAP...       exit 0 when every CAP is on, 1 when one is off,
                                 2 for a name nobody knows
    shuush-caps sensitive        exit 0 for a present sensitive-field marker on a supporting or unknown build,
                                 or while shuush-ime (which writes it too) is running
    shuush-caps list             one line per capability
    shuush-caps bin              the daemon's binary
    shuush-caps ime              Shuush's input method helper, when it can work here
                                 (exit 1 when it is not installed or the compositor lacks
                                 input-method-v2)
    shuush-caps --refresh        ignore the cache and probe again
    shuush-caps --bin PATH       probe this binary instead of finding the daemon's

Shuush's own helpers are capabilities too (HELPER_CAPS): `shuush_ime` is on when
shuush-ime is installed and the compositor offers input-method-v2. That is asked
with `shuush-ime probe`, which reads the compositor's registry and binds nothing,
and cached by the helper's path, size and mtime and the Wayland display.

The binary is run for `--version`, `config schema --json` and `--help` only,
never as a daemon, with an empty throwaway config directory so that nothing of
the user's config is read and nothing is written. A binary that is not named
voxtype or is not an ELF file is never run (a wrapper script given the wrong
arguments could start a second daemon).
"""
from __future__ import annotations

import json
import mmap
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = 1
PROBE_TIMEOUT_S = 8

# name -> (what it needs, what it is). A key spec is "key:<dotted>" (the
# binary's `config schema` lists it as settable and compiled in), "cli:<args>"
# (that command exits 0 for --help), "help:<args>::<text>" (its --help also
# names a flag) or "mark:<text>" (the text is in the
# binary, for a file or a key `config schema` does not list). Several specs
# separated by "|" are alternatives.
CAPS: dict[str, tuple[str, str]] = {
    "config_set": ("cli:config set", "`voxtype config set/unset`: validated config writes"),
    "handoff": ("key:output.post_process.fallback_on_empty", "an empty post-process answer types nothing"),
    "outcome_file": ("mark:outcome.json", "outcome.json: what the take ended in"),
    "recording_file": ("mark:recording.json", "recording.json: when the take started"),
    "preedit": ("key:output.preedit", "live words as input-method preedit in the field"),
    "read_field": ("key:output.read_field", "the focused field's text, read by the input method"),
    "live_in_field": ("mark:live_in_field", "live_in_field marker: the words are showing in the field"),
    "paste_over_chars": ("key:output.paste_over_chars", "long takes pasted instead of typed"),
    "post_process_streaming": ("key:output.post_process.streaming", "cleanup typed sentence by sentence"),
    "post_process_enabled": ("key:output.post_process_enabled", "cleanup master switch, skip and cancel"),
    "queue_dictations": ("key:audio.queue_dictations", "a new take may start while the last is transcribing"),
    "warm_mic_secs": ("key:audio.warm_mic_secs", "warm microphone"),
    "pre_roll_ms": ("key:audio.pre_roll_ms", "audio from before the key press"),
    "release_tail_ms": ("key:audio.release_tail_ms|mark:release_tail_ms", "the daemon keeps recording a moment after release"),
    "streaming_commit": (
        "key:output.streaming_commit|key:output.streaming_buffer_output",
        "streaming engine, text delivered once at stop",
    ),
    "streaming_preview": ("key:output.streaming_preview_file", "the words so far in a file, for the pill and the field"),
    "final_pass": ("key:parakeet.streaming_final_pass", "offline pass over a streamed take"),
    "vocab_rescoring": ("key:parakeet.vocabulary_file", "acoustic vocabulary rescoring"),
    "idle_unload": ("key:parakeet.idle_unload_secs", "model unloaded from VRAM when idle"),
    "last_take": ("mark:last_take.wav", "last_take.wav: the last recording, for replay and real WPM"),
    "record_recover": ("cli:record recover", "`voxtype record recover`: bring back a cancelled take"),
    "skip_post_process": ("help:record stop::--no-post-process", "`record stop --no-post-process`: skip the cleanup while it runs"),
    "take_identity": ("mark:VOXTYPE_TAKE_ID", "postprocessor and terminal outcome identified by take"),
    "sensitive_field": ("mark:sensitive_field", "runtime marker for password and sensitive fields"),
    "take_recovery": ("mark:recovered.txt", "target-loss recovery file in the identified take directory"),
    "delivered_marker": ("mark:output_done", "output_done marker (the fork's; outcome.json is the stock one)"),
}

# Capabilities of the helpers Shuush ships, not of the daemon.
HELPER_CAPS: dict[str, str] = {
    "shuush_ime": "shuush-ime: field text and the input-method commit (shuush build-ime; PP_IME=shuush)",
}


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def cache_path() -> Path:
    rd = os.environ.get("XDG_RUNTIME_DIR")
    if rd and os.path.isdir(rd):
        return Path(rd) / "shuush" / "caps.json"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "shuush" / "caps.json"


def _is_voxtype_elf(path: str) -> bool:
    try:
        if not os.path.basename(path).startswith("voxtype"):
            return False
        if not os.access(path, os.X_OK) or not os.path.isfile(path):
            return False
        if os.environ.get("SHUUSH_CAPS_ALLOW_SCRIPT") == "1":
            return True
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _from_pid_file() -> str | None:
    """The running daemon's binary, from its pid file (no subprocess)."""
    try:
        pid = int((runtime_dir() / "voxtype" / "pid").read_text().split()[0])
        exe = os.readlink(f"/proc/{pid}/exe")
    except (OSError, ValueError, IndexError):
        return None
    exe = exe.removesuffix(" (deleted)")
    return exe if _is_voxtype_elf(exe) else None


def _from_unit() -> str | None:
    """ExecStart of voxtype.service: the binary the unit would start."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ExecStart", "voxtype.service"],
            capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"path=(\S+)", out)
    if m and _is_voxtype_elf(m.group(1)):
        return m.group(1)
    return None


def _from_path() -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d or ".", "voxtype")
        if _is_voxtype_elf(p):
            return p
    return None


def resolve_binary() -> tuple[str | None, str]:
    """(path, how it was found). The daemon's own binary first."""
    env = os.environ.get("SHUUSH_VOXTYPE_BIN")
    if env:
        return (env, "env") if os.path.isfile(env) else (None, "env-missing")
    for how, fn in (("daemon", _from_pid_file), ("unit", _from_unit), ("path", _from_path)):
        p = fn()
        if p:
            return p, how
    return None, "none"


def _run(binary: str, args: list[str], cfg: str) -> subprocess.CompletedProcess[str] | None:
    env = dict(os.environ)
    env.update({"HOME": cfg, "XDG_CONFIG_HOME": cfg, "XDG_DATA_HOME": cfg, "XDG_STATE_HOME": cfg,
                "XDG_CACHE_HOME": cfg, "NO_COLOR": "1"})
    env.pop("VOXTYPE_CONFIG", None)
    try:
        return subprocess.run([binary, *args], capture_output=True, text=True, env=env,
                              timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _binary_has(binary: str, needle: bytes) -> bool:
    try:
        with open(binary, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return mm.find(needle) != -1
    except (OSError, ValueError):
        return False


def evaluate(spec: str, keys: set[str], cli_ok: dict[str, bool], binary: str, marks: dict[str, bool]) -> bool:
    for alt in spec.split("|"):
        kind, _, arg = alt.partition(":")
        if kind == "key" and arg in keys:
            return True
        if kind in ("cli", "help") and cli_ok.get(alt):
            return True
        if kind == "mark":
            if arg not in marks:
                marks[arg] = _binary_has(binary, arg.encode())
            if marks[arg]:
                return True
    return False


def probe(binary: str, source: str = "env") -> dict:
    st = os.stat(binary)
    out: dict = {
        "schema": SCHEMA, "binary": binary, "source": source,
        "size": st.st_size, "mtime_ns": st.st_mtime_ns,
        "version": None, "fork": False, "probe_ok": False, "error": None,
        "caps": {name: False for name in CAPS},
    }
    with tempfile.TemporaryDirectory(prefix="shuush-caps.") as cfg:
        v = _run(binary, ["--version"], cfg)
        if v and v.returncode == 0:
            out["version"] = v.stdout.strip().splitlines()[0] if v.stdout.strip() else None
        sc = _run(binary, ["config", "schema", "--json"], cfg)
        keys: set[str] = set()
        if sc is None or sc.returncode != 0:
            out["error"] = "config schema --json failed" if sc else "could not run the binary"
        else:
            try:
                doc = json.loads(sc.stdout)
                for k in doc.get("keys", []):
                    if k.get("compiled", True) and isinstance(k.get("key"), str):
                        keys.add(k["key"])
                out["probe_ok"] = True
                dv = doc.get("daemon_version")
                if dv and not out["version"]:
                    out["version"] = dv
            except (ValueError, AttributeError):
                out["error"] = "config schema --json did not print JSON"
        cli_ok: dict[str, bool] = {}
        for alt in {x for spec, _ in CAPS.values() for x in spec.split("|") if x.split(":", 1)[0] in ("cli", "help")}:
            kind, _, arg = alt.partition(":")
            words, _, needle = arg.partition("::")
            r = _run(binary, [*words.split(), "--help"], cfg)
            cli_ok[alt] = bool(r and r.returncode == 0 and needle in (r.stdout + r.stderr))
    out["keys"] = len(keys)
    marks: dict[str, bool] = {}
    for name, (spec, _) in CAPS.items():
        out["caps"][name] = evaluate(spec, keys, cli_ok, binary, marks)
    # A build that is not a release says so in its version (1.3.1+g828874e2).
    out["fork"] = bool(out["version"] and re.search(r"\+g[0-9a-f]{6,}", out["version"]))
    return out


def _read_cache() -> dict:
    """Cached answers by binary path (a switch between two builds re-asks nothing)."""
    try:
        d = json.loads(cache_path().read_text())
        if isinstance(d, dict) and d.get("schema") == SCHEMA and isinstance(d.get("entries"), dict):
            return d["entries"]
    except (OSError, ValueError):
        pass
    return {}


def _write_cache(doc: dict) -> None:
    entries = _read_cache()
    entries[doc["binary"]] = doc
    if len(entries) > 4:
        for k in sorted(entries, key=lambda k: entries[k].get("probed_at", 0))[:-4]:
            del entries[k]
    p = cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}")
        tmp.write_text(json.dumps({"schema": SCHEMA, "entries": entries}, sort_keys=True))
        os.replace(tmp, p)
    except OSError:
        pass


def capabilities(binary: str | None = None, refresh: bool = False) -> dict:
    """The answer for the daemon's binary: from the cache when it still fits."""
    source = "env"
    if binary is None:
        binary, source = resolve_binary()
    if not binary:
        return {"schema": SCHEMA, "binary": None, "source": source, "version": None, "fork": False,
                "probe_ok": False, "error": "voxtype not found", "keys": 0,
                "caps": {name: False for name in CAPS}, "cached": False}
    try:
        st = os.stat(binary)
    except OSError:
        return {"schema": SCHEMA, "binary": binary, "source": source, "version": None, "fork": False,
                "probe_ok": False, "error": "voxtype not readable", "keys": 0,
                "caps": {name: False for name in CAPS}, "cached": False}
    if not refresh:
        c = _read_cache().get(binary)
        if (c and c.get("mtime_ns") == st.st_mtime_ns
                and c.get("size") == st.st_size and c.get("probe_ok")
                and set(c.get("caps", {})) == set(CAPS)):
            c["cached"] = True
            c["source"] = source
            return c
    doc = probe(binary, source)
    doc["probed_at"] = int(time.time())
    if doc["probe_ok"]:
        _write_cache(doc)
    doc["cached"] = False
    return doc


def ime_binary() -> str | None:
    """shuush-ime: SHUUSH_IME_BIN, else PATH, else ~/.local/bin (where build-ime puts it)."""
    env = os.environ.get("SHUUSH_IME_BIN")
    dirs = os.environ.get("PATH", "").split(os.pathsep) + [str(Path.home() / ".local" / "bin")]
    for p in [env] if env else [os.path.join(d or ".", "shuush-ime") for d in dirs]:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def ime_works(refresh: bool = False) -> tuple[str | None, bool]:
    """(helper, whether the compositor offers input-method-v2), cached."""
    binary = ime_binary()
    if not binary:
        return None, False
    try:
        st = os.stat(binary)
    except OSError:
        return None, False
    key = {"binary": binary, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
           "display": os.environ.get("WAYLAND_DISPLAY", "")}
    path = cache_path().with_name("ime-caps.json")
    if not refresh:
        try:
            c = json.loads(path.read_text())
            if isinstance(c, dict) and {k: c.get(k) for k in key} == key and isinstance(c.get("ok"), bool):
                return binary, c["ok"]
        except (OSError, ValueError):
            pass
    try:
        rc = subprocess.run([binary, "probe"], capture_output=True, timeout=3,
                            stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.SubprocessError):
        return binary, False
    ok = rc == 0
    # Exit 0 offers it, 1 lacks it; anything else (no Wayland now) is not cached.
    if rc in (0, 1):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}")
            tmp.write_text(json.dumps({**key, "ok": ok}))
            os.replace(tmp, path)
        except OSError:
            pass
    return binary, ok


def helper_caps(refresh: bool = False) -> dict[str, bool]:
    return {"shuush_ime": ime_works(refresh)[1]}


def has(names: list[str], binary: str | None = None) -> int:
    unknown = [n for n in names if n not in CAPS and n not in HELPER_CAPS]
    if unknown:
        print(f"shuush-caps: unknown capability {unknown[0]}", file=sys.stderr)
        return 2
    caps = capabilities(binary)["caps"] if any(n in CAPS for n in names) else {}
    if any(n in HELPER_CAPS for n in names):
        caps = {**caps, **helper_caps()}
    return 0 if all(caps.get(n) for n in names) else 1


def main(argv: list[str]) -> int:
    if argv == ["sensitive"]:
        if not (runtime_dir() / "voxtype" / "sensitive_field").exists():
            return 1
        # shuush-ime writes the marker too, on any daemon, and removes it
        # when it exits: while its socket is there, the marker is its.
        if (runtime_dir() / "shuush" / "ime.sock").exists():
            return 0
        doc = capabilities()
        return 0 if not doc["probe_ok"] or doc["caps"]["sensitive_field"] else 1
    refresh = False
    binary = None
    args: list[str] = []
    it = iter(argv)
    for a in it:
        if a == "--refresh":
            refresh = True
        elif a == "--bin":
            binary = next(it, None)
            if not binary:
                print("shuush-caps: --bin needs a path", file=sys.stderr)
                return 2
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            args.append(a)
    if args and args[0] == "bin":
        b = binary or resolve_binary()[0]
        if not b:
            print("shuush-caps: voxtype not found", file=sys.stderr)
            return 1
        print(b)
        return 0
    if args and args[0] == "ime":
        helper, ok = ime_works(refresh)
        if not (helper and ok):
            return 1
        print(helper)
        return 0
    if args and args[0] == "has":
        if len(args) < 2:
            print("usage: shuush-caps has CAP...", file=sys.stderr)
            return 2
        if refresh:
            capabilities(binary, refresh=True)
        return has(args[1:], binary)
    doc = capabilities(binary, refresh=refresh)
    doc["caps"].update(helper_caps(refresh))
    if args and args[0] == "list":
        rows = [(name, what) for name, (_, what) in CAPS.items()] + list(HELPER_CAPS.items())
        for name, what in rows:
            print(f"{'on ' if doc['caps'][name] else 'off'}  {name:<22} {what}")
        return 0
    if args:
        print(f"shuush-caps: unknown command {args[0]}", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
