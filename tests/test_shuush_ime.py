#!/usr/bin/env python3
"""shuush-ime as Shuush uses it: the capability, the PP_IME switch in the
toggle, the cleanup and cancel, `shuush wire` turning the daemon's own input
method off, and `shuush build-ime`.

The helper is a fake here (a script that logs what it was asked); the real
one is tested by `cargo test` in ime/, against fakes and a private compositor.
Nothing here binds an input method, types, or runs the voxtype daemon.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = ROOT / "libexec"
sys.path.insert(0, str(ROOT / "tests"))

import test_dictation_pass as base  # noqa: E402
from test_layer_groundwork import FORK, ORIGINAL, STOCK, caps, run, sandbox, wire, wire_env  # noqa: E402

BUILD = LIBEXEC / "shuush-build-ime.sh"


def fake_ime(d: Path, probe_rc: int = 0, commit_rc: int = 0, version: str = "0.1.0") -> tuple[Path, Path]:
    """A shuush-ime that logs every call (and a commit's text) and answers as told."""
    log = d / "ime.log"
    path = d / "fake-bin" / "shuush-ime"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""#!/usr/bin/env bash
case "$1" in
  --version) echo "shuush-ime {version}"; exit 0 ;;
  probe) printf 'probe\\n' >> "{log}"; exit {probe_rc} ;;
  commit) printf 'commit %s [%s]\\n' "$*" "$(cat)" >> "{log}"; exit {commit_rc} ;;
  serve) printf '%s parent=%s\\n' "$*" "$PPID" >> "{log}"; sleep 2 ;;
  *) printf '%s\\n' "$*" >> "{log}" ;;
esac
""")
    path.chmod(0o755)
    return path, log


def lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def wait_for(what, timeout: float = 5.0) -> bool:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if what():
            return True
        time.sleep(0.02)
    return False


# ── the capability ───────────────────────────────────────────────────────────


def test_no_helper_means_the_capability_is_off(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    doc = json.loads(caps(env).stdout)
    assert doc["caps"]["shuush_ime"] is False
    assert caps(env, "has", "shuush_ime").returncode == 1
    assert caps(env, "ime").returncode == 1
    assert "off  shuush_ime" in caps(env, "list").stdout


def test_an_installed_helper_on_a_compositor_with_the_protocol(tmp_path: Path) -> None:
    helper, log = fake_ime(tmp_path)
    env = sandbox(tmp_path, SHUUSH_IME_BIN=str(helper), WAYLAND_DISPLAY="wayland-test")
    assert caps(env, "has", "shuush_ime").returncode == 0
    assert caps(env, "has", "handoff", "shuush_ime").returncode == 0, "with the daemon's own"
    assert caps(env, "ime").stdout.strip() == str(helper)
    assert json.loads(caps(env).stdout)["caps"]["shuush_ime"] is True
    assert "on   shuush_ime" in caps(env, "list").stdout
    assert lines(log) == ["probe"], "asked once, then cached"
    # Another display, or a rebuilt helper, is asked again.
    env["WAYLAND_DISPLAY"] = "wayland-other"
    caps(env, "has", "shuush_ime")
    assert lines(log) == ["probe", "probe"]
    assert caps(env, "--refresh", "ime").returncode == 0
    assert len(lines(log)) == 3


def test_a_compositor_without_the_protocol_turns_it_off(tmp_path: Path) -> None:
    helper, _ = fake_ime(tmp_path, probe_rc=1)
    env = sandbox(tmp_path, SHUUSH_IME_BIN=str(helper))
    assert caps(env, "has", "shuush_ime").returncode == 1
    assert caps(env, "ime").returncode == 1


def test_no_wayland_session_is_not_cached(tmp_path: Path) -> None:
    helper, log = fake_ime(tmp_path, probe_rc=6)
    env = sandbox(tmp_path, SHUUSH_IME_BIN=str(helper))
    assert caps(env, "has", "shuush_ime").returncode == 1
    assert caps(env, "has", "shuush_ime").returncode == 1
    assert lines(log) == ["probe", "probe"]


def test_the_helper_is_found_in_local_bin(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    local = tmp_path / "home/.local/bin"
    local.mkdir(parents=True)
    helper, _ = fake_ime(tmp_path)
    (local / "shuush-ime").write_bytes(helper.read_bytes())
    (local / "shuush-ime").chmod(0o755)
    assert caps(env, "ime").stdout.strip() == str(local / "shuush-ime")


def test_its_sensitive_field_marker_counts_on_a_stock_daemon(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    marker = tmp_path / "rt/voxtype/sensitive_field"
    marker.touch()
    assert caps(env, "sensitive").returncode == 1, "stock writes no marker: a stale one is ignored"
    (tmp_path / "rt/shuush").mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(tmp_path / "rt/shuush/ime.sock"))
    s.close()
    assert caps(env, "sensitive").returncode == 0, "shuush-ime is running: the marker is its"
    marker.unlink()
    assert caps(env, "sensitive").returncode == 1


def test_the_hud_watches_the_marker_when_shuush_ime_is_on() -> None:
    qml = (ROOT / "osd/kitsune-hud/KitsuneHud.qml").read_text()
    assert "capabilities.shuush_ime === true" in qml


# ── the toggle starts it, the cancel lets it go ──────────────────────────────


def toggle(tmp_path: Path, schema: str = STOCK, pp_ime: str | None = "shuush") -> tuple[subprocess.CompletedProcess, Path, Path, dict]:
    from test_layer_groundwork import toggle_env

    env, vlog = toggle_env(tmp_path, schema)
    helper, log = fake_ime(tmp_path)
    env["SHUUSH_IME_BIN"] = str(helper)
    if pp_ime is not None:
        (tmp_path / "config/voxtype/postprocess.env").write_text(f"PP_IME={pp_ime}\n")
    t0 = time.monotonic()
    res = run([str(LIBEXEC / "voxtype-toggle.sh"), "toggle"], env)
    res.elapsed = time.monotonic() - t0
    return res, log, vlog, env


def test_the_toggle_starts_the_helper_for_the_take(tmp_path: Path) -> None:
    res, log, vlog, _ = toggle(tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.elapsed < 1.5, "the toggle does not wait for the helper"
    assert "record start" in vlog.read_text()
    take = (tmp_path / "rt/voxtype/current_capture").read_text()
    assert wait_for(lambda: any(l.startswith("serve") for l in lines(log)))
    serve = next(l for l in lines(log) if l.startswith("serve"))
    assert serve.startswith(f"serve --take {take}"), serve
    assert "--live" not in serve, "stock voxtype has no live words"
    # Its parent is the subshell that waits for it, still there after the
    # toggle (which exec'd voxtype) has gone: --pdeathsig does not fire.
    parent = int(serve.rsplit("parent=", 1)[1])
    os.kill(parent, 0)


def test_with_the_forks_live_text_the_words_are_followed(tmp_path: Path) -> None:
    _, log, _, _ = toggle(tmp_path, FORK)
    assert wait_for(lambda: any(l.startswith("serve") for l in lines(log)))
    assert "--live auto" in next(l for l in lines(log) if l.startswith("serve"))


def test_off_by_default(tmp_path: Path) -> None:
    for name, pp_ime in (("unset", None), ("other", "fcitx")):
        d = tmp_path / name
        d.mkdir()
        res, log, vlog, _ = toggle(d, pp_ime=pp_ime)
        assert res.returncode == 0
        time.sleep(0.3)
        assert not lines(log), "never started, never probed"
        assert "record start" in vlog.read_text()


def test_cancel_releases_the_helper(tmp_path: Path) -> None:
    from test_layer_groundwork import toggle_env

    env, vlog = toggle_env(tmp_path, STOCK)
    helper, log = fake_ime(tmp_path)
    env["SHUUSH_IME_BIN"] = str(helper)
    (tmp_path / "rt/voxtype/state").write_text("recording\n")
    (tmp_path / "config/voxtype/postprocess.env").write_text("PP_IME=shuush\n")
    res = run([str(LIBEXEC / "voxtype-cancel.sh"), "now"], env)
    assert res.returncode == 0, res.stderr
    assert "release" in lines(log)
    assert "record cancel" in vlog.read_text()
    (tmp_path / "config/voxtype/postprocess.env").write_text("")
    log.unlink()
    run([str(LIBEXEC / "voxtype-cancel.sh"), "now"], env)
    assert not lines(log)


# ── the cleanup commits, or clears before voxtype types ─────────────────────


def cleanup(tmp_path: Path, raw: str = "Hello.", *, mode: str = "default", commit_rc: int = 0,
            fallback_off: bool = True, running: bool = True, pp_ime: str = "shuush",
            password: bool = False, extra: dict | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
    helper, log = fake_ime(tmp_path, commit_rc=commit_rc)
    rt = tmp_path / "rt"
    (rt / "voxtype").mkdir(parents=True, exist_ok=True)
    (rt / "voxtype/current_capture").write_text("4242")
    if running:
        (rt / "shuush").mkdir(exist_ok=True)
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(rt / "shuush/ime.sock"))
        s.close()
    cfg = tmp_path / "config/voxtype"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.toml").write_text(
        "[output.post_process]\ncommand = \"x\"\n" + ("fallback_on_empty = false\n" if fallback_off else ""))
    env = {"SHUUSH_IME_BIN": str(helper), "PP_IME": pp_ime, **(extra or {})}
    win = {"address": "0x5", "class": "org.gnome.TextEditor", "title": "notes", "pid": 0}
    ctx = {"password": password, "draft": "", "screen": ""}
    proc, _, _, _ = base._run_pp_script(LIBEXEC / "voxtype-postprocess.sh", tmp_path, raw, mode,
                                        target_win=win, screen_ctx=ctx, extra_env=env)
    return proc, lines(log)


def test_the_text_goes_into_the_field_in_one_commit(tmp_path: Path) -> None:
    plain, _ = cleanup(tmp_path / "plain", pp_ime="")
    assert plain.stdout, "the same take typed without the helper"
    proc, calls = cleanup(tmp_path / "ime")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "nothing left for voxtype to type"
    assert calls == [f"commit commit --take 4242 [{plain.stdout}]"]


def test_text_the_field_did_not_take_is_typed_after_the_preedit_goes(tmp_path: Path) -> None:
    for rc in (1, 3):
        d = tmp_path / str(rc)
        proc, calls = cleanup(d, commit_rc=rc)
        assert proc.stdout == "Hello."
        assert calls[0].startswith("commit") and calls[1:] == ["end --take 4242"], calls


def test_no_commit_where_voxtype_would_type_the_transcript_again(tmp_path: Path) -> None:
    for name, kw in (
        ("fallback-on", {"fallback_off": False}),        # an empty answer would type the transcript
        ("profile", {"mode": "terminal"}),               # [profiles.*] always fall back
    ):
        proc, calls = cleanup(tmp_path / name, **kw)
        assert proc.stdout == "Hello.", name
        assert not any(c.startswith("commit") for c in calls), (name, calls)
        assert "end --take 4242" in calls, (name, calls)


def test_no_commit_on_a_daemon_without_the_handoff(tmp_path: Path) -> None:
    schema = tmp_path / "schema-no-handoff.json"
    doc = json.loads(Path(STOCK).read_text())
    doc["keys"] = [k for k in doc["keys"] if k["key"] != "output.post_process.fallback_on_empty"]
    schema.write_text(json.dumps(doc))
    proc, calls = cleanup(tmp_path / "d", extra={"FAKE_SCHEMA": str(schema)})
    assert proc.stdout == "Hello."
    assert calls == ["end --take 4242"]


def test_a_private_take_is_typed_and_the_field_is_not_read(tmp_path: Path) -> None:
    rt = tmp_path / "rt/voxtype"
    rt.mkdir(parents=True)
    (rt / "field_text.json").write_text('{"before":"hunter2","after":""}')
    proc, calls = cleanup(tmp_path, raw="my secret words", password=True)
    assert proc.stdout == "my secret words"
    assert calls == ["end --take 4242"], "the live words go; nothing is committed"
    assert (rt / "field_text.off").exists() and not (rt / "field_text.json").exists()


def test_without_a_running_helper_or_with_the_switch_off_nothing_changes(tmp_path: Path) -> None:
    for name, kw in (("not-running", {"running": False}), ("off", {"pp_ime": ""})):
        proc, calls = cleanup(tmp_path / name, **kw)
        assert proc.stdout == "Hello.", name
        assert calls == [], name


# ── shuush wire hands the seat over ──────────────────────────────────────────


FORK_CONFIG = ORIGINAL.replace("[output]\n", "[output]\npreedit = true\nread_field = true\n", 1)


def test_wire_turns_the_daemons_input_method_off_and_gives_it_back(tmp_path: Path) -> None:
    assert "preedit = true" in FORK_CONFIG
    env, cfg = wire_env(tmp_path, FORK, FORK_CONFIG, PP_IME="shuush")
    res = wire(env)
    assert res.returncode == 0, res.stderr
    out = tomllib.loads(cfg.read_text())["output"]
    assert out["preedit"] is False and out["read_field"] is False
    keys = json.loads((tmp_path / "config/shuush/wired.json").read_text())["keys"]
    assert keys["output.preedit"] == {"old": True, "new": False, "via": "voxtype"}
    assert keys["output.read_field"]["old"] is True
    # The switch goes off: the next wire gives them back.
    env["PP_IME"] = ""
    dry = wire(env, "--dry-run")
    assert "restore output.preedit: false -> true" in dry.stdout
    res = wire(env)
    assert res.returncode == 0, res.stderr
    assert "Gave 2 input method key(s) back" in res.stdout
    out = tomllib.loads(cfg.read_text())["output"]
    assert out["preedit"] is True and out["read_field"] is True
    keys = json.loads((tmp_path / "config/shuush/wired.json").read_text())["keys"]
    assert "output.preedit" not in keys


def test_wire_reads_the_switch_from_postprocess_env_and_unwire_restores(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path, FORK, FORK_CONFIG)
    (tmp_path / "config/voxtype/postprocess.env").write_text("PP_SMART_JOIN=1\nPP_IME=shuush\n")
    assert wire(env).returncode == 0
    assert tomllib.loads(cfg.read_text())["output"]["preedit"] is False
    res = wire(env, action="unwire")
    assert res.returncode == 0, res.stderr
    assert cfg.read_text() == FORK_CONFIG


def test_a_key_you_set_since_is_yours(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path, FORK, FORK_CONFIG, PP_IME="shuush")
    wire(env)
    run([str(ROOT / "tests/fixtures/fake-voxtype"), "config", "set", "output.preedit", "true"], env)
    env["PP_IME"] = ""
    res = wire(env)
    assert "skipped output.preedit" in res.stdout
    keys = json.loads((tmp_path / "config/shuush/wired.json").read_text())["keys"]
    assert "output.preedit" in keys and "output.read_field" not in keys


def test_stock_voxtype_has_no_input_method_keys_to_turn_off(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path, STOCK, PP_IME="shuush")
    res = wire(env)
    assert res.returncode == 0, res.stderr
    assert "skipped output.preedit (this voxtype does not know the key)" in res.stdout
    assert "preedit" not in cfg.read_text()


def test_without_the_switch_wire_never_touches_them(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path, FORK, FORK_CONFIG)
    assert wire(env).returncode == 0
    assert tomllib.loads(cfg.read_text())["output"]["preedit"] is True


# ── shuush build-ime ─────────────────────────────────────────────────────────


def build(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(BUILD), *args], env=env or {"PATH": os.environ["PATH"], "HOME": "/nonexistent"},
                          capture_output=True, text=True, timeout=60)


def test_build_dry_run_names_the_steps_and_touches_nothing(tmp_path: Path) -> None:
    res = build("--dry-run", "--prefix", str(tmp_path / "out"))
    assert f"source   {LIBEXEC / '..' / 'ime'} (shuush-ime 0.1.0)" in res.stdout
    assert "cargo build --release --locked" in res.stdout
    assert f"install  {tmp_path / 'out'}/shuush-ime" in res.stdout
    assert not (tmp_path / "out").exists()
    assert ("tools    all present" in res.stdout) is (res.returncode == 0)
    bad = build("--dry-run", "--src", str(tmp_path))
    assert bad.returncode == 1 and "MISSING" in bad.stdout


def test_build_check_tells_what_is_missing(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "c")}
    gone = build("--check", "--bin", str(tmp_path / "missing"), env=env)
    assert gone.returncode == 1 and "not installed" in gone.stdout
    good, _ = fake_ime(tmp_path / "good")
    ok = build("--check", "--bin", str(good), env=env)
    assert ok.returncode == 0, ok.stdout
    assert "offers input-method-v2" in ok.stdout and "PP_IME is off" in ok.stdout
    old, _ = fake_ime(tmp_path / "old", version="0.0.9")
    assert "the source is 0.1.0" in build("--check", "--bin", str(old), env=env).stdout
    lacking, _ = fake_ime(tmp_path / "lacking", probe_rc=1)
    res = build("--check", "--bin", str(lacking), env={**env, "PP_IME": "shuush"})
    assert res.returncode == 1 and "lacks input-method-v2" in res.stdout and "PP_IME=shuush:" in res.stdout


def test_build_unknown_option_and_the_cli_entry(tmp_path: Path) -> None:
    assert build("--nope").returncode == 2
    res = run([str(ROOT / "bin/shuush"), "build-ime", "--dry-run", "--prefix", str(tmp_path / "o")], sandbox(tmp_path))
    assert "shuush build-ime --dry-run" in res.stdout
    assert "build-ime" in run([str(ROOT / "bin/shuush"), "help"], sandbox(tmp_path)).stdout


def test_the_crate_is_shipped_and_its_lock_is_committed() -> None:
    assert (ROOT / "ime/Cargo.lock").is_file()
    for f in ("install.sh", "packaging/aur/shuush-git/PKGBUILD"):
        text = (ROOT / f).read_text()
        assert "ime/Cargo.lock" in text and "ime/src" in text, f
    assert BUILD.stat().st_mode & 0o111
    lic = (ROOT / "ime/LICENSE").read_text()
    assert "Peter Jackson" in lic and "KitsuneKode" in lic, "voxtype's notice kept"
    deps = tomllib.loads((ROOT / "ime/Cargo.toml").read_text())["dependencies"]
    assert set(deps) == {"libc", "serde_json", "wayland-client", "wayland-protocols", "wayland-protocols-misc"}


def test_doctor_checks_the_helper_only_when_it_is_on(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    doc = json.loads(run([str(ROOT / "bin/shuush"), "doctor", "--json"], env).stdout)
    assert "shuush_ime" not in {c["id"] for c in doc["checks"]}
    env["PP_IME"] = "shuush"
    doc = json.loads(run([str(ROOT / "bin/shuush"), "doctor", "--json"], env).stdout)
    check = next(c for c in doc["checks"] if c["id"] == "shuush_ime")
    assert check["status"] == "warn" and "shuush build-ime" in check["fix"]
    assert doc["capabilities"]["caps"]["shuush_ime"] is False
