#!/usr/bin/env python3
"""Groundwork for running on stock voxtype: capability probe, config ownership,
the HUD's state files, the patched wtype build.

Nothing here runs the voxtype daemon, records, types, pastes or plays sound.
The CLI is a fake (tests/fixtures/fake-voxtype) answering from the real stock and
fork `config schema --json` keys; the only real binary a test may touch is
/usr/bin/voxtype, and then only for --version, --help and `config schema --json`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = ROOT / "libexec"
FIX = ROOT / "tests" / "fixtures"
FAKE = FIX / "fake-voxtype"
STOCK = str(FIX / "schema-stock.json")
FORK = str(FIX / "schema-fork.json")
sys.path.insert(0, str(LIBEXEC))
sys.path.insert(0, str(ROOT / "tests"))

import shuush_caps  # noqa: E402
import shuush_wire  # noqa: E402


def sandbox(tmp_path: Path, schema: str = STOCK, **extra: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "rt"),
        "SHUUSH_VOXTYPE_BIN": str(FAKE),
        "FAKE_SCHEMA": schema,
        "LC_ALL": "C.UTF-8",
    }
    for sub in ("home", "config/voxtype", "state", "rt/voxtype"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    env.update(extra)
    return env


def run(args: list[str], env: dict[str, str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, **kw)


def caps(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    # Straight through python so that a test may empty PATH; the wrapper only adds the path of the module.
    return run([sys.executable, "-I", str(LIBEXEC / "shuush_caps.py"), *args], env)


def test_the_wrapper_runs_the_module(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    assert json.loads(run([str(LIBEXEC / "shuush-caps")], env).stdout)["probe_ok"] is True
    assert run([str(LIBEXEC / "shuush-caps"), "has", "handoff"], env).returncode == 0
    assert run([str(ROOT / "bin/shuush"), "caps", "has", "preedit"], env).returncode == 1


# ── 1. capability probe ──────────────────────────────────────────────────────

FORK_ONLY = [
    "preedit", "read_field", "live_in_field", "paste_over_chars", "post_process_streaming",
    "post_process_enabled", "queue_dictations", "warm_mic_secs", "pre_roll_ms", "streaming_commit",
    "streaming_preview", "final_pass", "vocab_rescoring", "idle_unload", "last_take", "record_recover",
    "skip_post_process", "delivered_marker",
]


def test_stock_daemon_has_none_of_the_fork_features(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    doc = json.loads(caps(env).stdout)
    assert doc["probe_ok"] and not doc["fork"] and doc["binary"] == str(FAKE)
    for name in FORK_ONLY:
        assert doc["caps"][name] is False, name
    # What stock 1.3.1 does have
    for name in ("config_set", "handoff"):
        assert doc["caps"][name] is True, name


def test_fork_daemon_has_them(tmp_path: Path) -> None:
    env = sandbox(tmp_path, FORK)
    doc = json.loads(caps(env).stdout)
    for name in FORK_ONLY:
        # live_in_field, last_take, delivered_marker are strings in the binary, which a script has not got
        if name in ("live_in_field", "last_take", "delivered_marker"):
            continue
        assert doc["caps"][name] is True, name
    assert caps(env, "has", "preedit", "queue_dictations", "record_recover").returncode == 0
    assert caps(env, "has", "preedit", "release_tail_ms").returncode == 1


def test_has_exit_codes_and_unknown_name(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    assert caps(env, "has", "handoff").returncode == 0
    assert caps(env, "has", "preedit").returncode == 1
    res = caps(env, "has", "no_such_thing")
    assert res.returncode == 2 and "unknown capability" in res.stderr
    assert "off" in caps(env, "list").stdout


def test_answer_is_cached_by_binary_path_and_mtime(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "voxtype-fake"
    binary.parent.mkdir()
    shutil.copy(FAKE, binary)
    env = sandbox(tmp_path, SHUUSH_VOXTYPE_BIN=str(binary))
    first = json.loads(caps(env).stdout)
    assert first["cached"] is False
    cache = tmp_path / "rt" / "shuush" / "caps.json"
    assert cache.is_file()
    assert json.loads(caps(env).stdout)["cached"] is True
    # The binary is replaced by one with other keys (same path, new mtime): probed again.
    env["FAKE_SCHEMA"] = FORK
    assert json.loads(caps(env).stdout)["cached"] is True, "an unchanged binary keeps its answer"
    st = binary.stat()
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    again = json.loads(caps(env).stdout)
    assert again["cached"] is False and again["caps"]["preedit"] is True
    # --refresh asks again even when nothing changed
    assert json.loads(caps(env, "--refresh").stdout)["cached"] is False
    # Two builds side by side do not push each other out
    other = tmp_path / "bin" / "voxtype-other"
    shutil.copy(FAKE, other)
    env2 = {**env, "SHUUSH_VOXTYPE_BIN": str(other), "FAKE_SCHEMA": STOCK}
    assert json.loads(caps(env2).stdout)["caps"]["preedit"] is False
    assert json.loads(caps(env).stdout)["cached"] is True
    assert set(json.loads(cache.read_text())["entries"]) == {str(binary), str(other)}


def test_no_voxtype_means_every_capability_is_off(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    del env["SHUUSH_VOXTYPE_BIN"]
    env["PATH"] = str(tmp_path / "empty")
    doc = json.loads(caps(env).stdout)
    assert doc["binary"] is None and not any(doc["caps"].values()) and doc["error"]
    assert caps(env, "has", "config_set").returncode == 1


def test_a_binary_that_cannot_answer_has_nothing_and_is_not_cached(tmp_path: Path) -> None:
    broken = tmp_path / "voxtype"
    broken.write_text("#!/bin/sh\nexit 2\n")
    broken.chmod(0o755)
    env = sandbox(tmp_path, SHUUSH_VOXTYPE_BIN=str(broken))
    doc = json.loads(caps(env).stdout)
    assert doc["probe_ok"] is False and not any(doc["caps"].values())
    assert not (tmp_path / "rt" / "shuush" / "caps.json").exists()


def test_daemon_binary_comes_before_path_and_scripts_are_never_run(tmp_path: Path) -> None:
    # A PATH `voxtype` that is a script is not run: a wrapper given `config schema` might start a daemon.
    trap = tmp_path / "pathbin" / "voxtype"
    trap.parent.mkdir()
    trap.write_text(f"#!/bin/sh\necho ran > {tmp_path}/trap-ran\n")
    trap.chmod(0o755)
    env = sandbox(tmp_path)
    del env["SHUUSH_VOXTYPE_BIN"]
    env["PATH"] = f"{trap.parent}:/usr/bin"
    assert json.loads(caps(env).stdout)["binary"] != str(trap) or os.environ.get("SHUUSH_CAPS_ALLOW_SCRIPT")
    assert not (tmp_path / "trap-ran").exists()

    # The running daemon's executable, found through its pid file, wins over PATH.
    elf = tmp_path / "daemon" / "voxtype-daemon"
    elf.parent.mkdir()
    shutil.copy("/bin/sleep", elf)
    proc = subprocess.Popen([str(elf), "30"])
    try:
        (tmp_path / "rt" / "voxtype" / "pid").write_text(f"{proc.pid}\n")
        old = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "SHUUSH_VOXTYPE_BIN", "PATH")}
        os.environ["XDG_RUNTIME_DIR"] = str(tmp_path / "rt")
        os.environ.pop("SHUUSH_VOXTYPE_BIN", None)
        os.environ["PATH"] = f"{trap.parent}:/usr/bin"
        try:
            assert shuush_caps.resolve_binary() == (str(elf), "daemon")
            # a pid file that points at some other program is no daemon
            (tmp_path / "rt" / "voxtype" / "pid").write_text(f"{os.getpid()}\n")
            assert shuush_caps._from_pid_file() is None
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(not Path("/usr/bin/voxtype").exists(), reason="stock voxtype-bin not installed")
def test_real_stock_binary_reads_as_stock(tmp_path: Path) -> None:
    """Runs /usr/bin/voxtype for --version, `config schema --json` and --help only."""
    version = subprocess.run(["/usr/bin/voxtype", "--version"], capture_output=True, text=True).stdout
    if "+g" in version:
        pytest.skip("/usr/bin/voxtype is a fork build")
    env = sandbox(tmp_path)
    del env["SHUUSH_VOXTYPE_BIN"]
    doc = json.loads(caps(env, "--bin", "/usr/bin/voxtype").stdout)
    assert doc["probe_ok"] and doc["fork"] is False
    assert doc["caps"]["config_set"] and doc["caps"]["handoff"]
    for name in ("preedit", "queue_dictations", "paste_over_chars", "record_recover", "skip_post_process"):
        assert doc["caps"][name] is False, name
    assert not list((tmp_path / "config" / "voxtype").glob("*")), "the probe reads and writes no config"


# ── the callers degrade ──────────────────────────────────────────────────────


def toggle_env(tmp_path: Path, schema: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "voxtype.log"
    for name, body in {
        "voxtype": f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\n',
        "wpctl": "#!/usr/bin/env bash\necho 'Volume: 1.00'\n",
        "hyprctl": '#!/usr/bin/env bash\necho \'{"address":"0x1","class":"kitty","title":"zsh","pid":0}\'\n',
        "wl-paste": "#!/usr/bin/env bash\nexit 1\n",
    }.items():
        (fake_bin / name).write_text(body)
        (fake_bin / name).chmod(0o755)
    env = sandbox(tmp_path, schema)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    return env, log


def test_press_while_polishing_skips_cleanup_only_where_voxtype_can(tmp_path: Path) -> None:
    for schema, expect in ((FORK, "record stop --no-post-process"), (STOCK, None)):
        d = tmp_path / ("fork" if expect else "stock")
        d.mkdir()
        env, log = toggle_env(d, schema)
        (d / "rt/voxtype/state").write_text("transcribing\n")
        res = run([str(LIBEXEC / "voxtype-toggle.sh"), "toggle"], env)
        assert res.returncode == 0, res.stderr
        if expect:
            assert log.read_text().strip() == expect
        else:
            assert not log.exists(), "stock voxtype has no --no-post-process: the press is ignored"


def test_queue_needs_the_daemon_to_have_it_even_with_a_live_queue_pid(tmp_path: Path) -> None:
    for schema, started in ((FORK, True), (STOCK, False)):
        d = tmp_path / ("fork" if started else "stock")
        d.mkdir()
        env, log = toggle_env(d, schema)
        rt = d / "rt/voxtype"
        (rt / "state").write_text("transcribing\n")
        # A live queue_pid on the daemon's own pid, and an explicit opt-in: stock still cannot queue.
        (rt / "pid").write_text(f"{os.getpid()}\n")
        (rt / "queue_pid").write_text(f"{os.getpid()}\n")
        env["VOXTYPE_QUEUE"] = "1"
        res = run([str(LIBEXEC / "voxtype-toggle.sh"), "hold"], env)
        assert res.returncode == 0
        assert log.exists() is started
        assert (rt / "hold_ignore_release").exists() is (not started)


def test_streamed_cleanup_and_field_privacy_marker_follow_the_daemon(tmp_path: Path) -> None:
    import test_dictation_pass as base

    win = {"address": "0x2", "class": "org.keepassxc.KeePassXC", "title": "Vault.kdbx", "pid": 43}
    ctx = {"password": False, "draft": "", "screen": ""}
    for schema, has_reader in ((FORK, True), (STOCK, False)):
        d = tmp_path / ("fork" if has_reader else "stock")
        d.mkdir()
        proc, _msg, _a, _b = base._run_pp_script(
            LIBEXEC / "voxtype-postprocess.sh", d, "my secret passphrase", "default",
            target_win=win, screen_ctx=ctx, extra_env={"FAKE_SCHEMA": schema},
        )
        assert proc.stdout == "my secret passphrase"
        # field_text.off tells the fork's input method to stop reading the field; stock has no reader
        assert (d / "rt/voxtype/field_text.off").exists() is has_reader


def test_replay_says_why_on_a_daemon_that_keeps_no_recording(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    res = run([str(LIBEXEC / "voxtype-last-audio.sh")], env)
    assert res.returncode == 1 and "needs a voxtype that keeps the last recording" in res.stderr
    env = sandbox(tmp_path / "f", FORK)
    # the fake is a script, so the string marker for last_take.wav is not in it: the plain message
    res = run([str(LIBEXEC / "voxtype-last-audio.sh")], env)
    assert res.returncode == 1


def test_recover_command(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    env = sandbox(tmp_path, STOCK, FAKE_LOG=str(log))
    res = run([str(ROOT / "bin/shuush"), "recover"], env)
    assert res.returncode == 3 and "no 'record recover'" in res.stderr
    assert "record recover" not in (log.read_text() if log.exists() else "").replace("record recover --help", "")
    env = sandbox(tmp_path / "f", FORK, FAKE_LOG=str(log))
    res = run([str(ROOT / "bin/shuush"), "recover"], env)
    assert res.returncode == 0
    assert "record recover\n" in log.read_text()


def test_doctor_json_carries_the_capabilities(tmp_path: Path) -> None:
    for schema, fork in ((STOCK, False), (FORK, True)):
        d = tmp_path / ("fork" if fork else "stock")
        d.mkdir()
        env = sandbox(d, schema)
        res = run([str(ROOT / "bin/shuush"), "doctor", "--json"], env)
        doc = json.loads(res.stdout)
        assert doc["capabilities"]["caps"]["preedit"] is fork
        assert any(c["id"] == "wired_keys" for c in doc["checks"])
        text = run([str(ROOT / "bin/shuush"), "doctor"], {**env, "NO_COLOR": "1"}).stdout
        assert "The daemon" in text and ("preedit" in text)


# ── 2. config ownership ──────────────────────────────────────────────────────

ORIGINAL = """\
# my voxtype config
engine = "whisper"

[whisper]
model = "large-v3-turbo"   # fast one

[output]
mode = "type"

[output.post_process]
# command = "ollama run llama3.2:1b 'Clean up'"
timeout_ms = 30000

[osd]
frontend = "auto"
style = "default"
layout = "compact"
position = "top-center"

[profiles.terminal]
post_process_command = "mine --terminal"

[history]
enabled = true
"""

PP = "/opt/shuush/libexec/voxtype-postprocess.sh"


def wire_env(tmp_path: Path, schema: str = STOCK, config: str = ORIGINAL, **extra: str) -> tuple[dict[str, str], Path]:
    env = sandbox(tmp_path, schema, **extra)
    cfg = tmp_path / "config/voxtype/config.toml"
    cfg.write_text(config)
    return env, cfg


def wire(env: dict[str, str], *args: str, action: str = "wire") -> subprocess.CompletedProcess:
    return run([sys.executable, "-I", str(LIBEXEC / "shuush_wire.py"), action, "--command", PP, *args], env)


def manifest(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "config/shuush/wired.json").read_text())


def test_dry_run_prints_and_writes_nothing(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    env, cfg = wire_env(tmp_path, FAKE_LOG=str(log))
    before = cfg.read_bytes()
    res = wire(env, "--dry-run")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "nothing is written" in out
    assert f'set   output.post_process.command = "{PP}"' in out
    assert "voxtype config set output.post_process.timeout_ms 20000" in out
    assert "(now: 30000;" in out
    assert "osd.position" in out and "yours" in out, "a position you chose is left alone"
    assert 'profiles.terminal.post_process_command = "' + PP + ' terminal"' in out
    assert "voxtype has no settable profile key" in out
    assert cfg.read_bytes() == before
    assert not (tmp_path / "config/shuush/wired.json").exists()
    assert not list((tmp_path / "config/voxtype").glob("config.toml.bak.*"))
    assert "config set" not in log.read_text().replace("config set --help", "")
    js = json.loads(wire(env, "--dry-run", "--json").stdout)
    by = {r["key"]: r for r in js["keys"]}
    assert by["output.post_process.timeout_ms"]["now"] == 30000 and by["output.post_process.timeout_ms"]["action"] == "set"
    assert by["osd.position"]["action"] == "skip"


def test_wire_sets_through_voxtype_and_records_old_and_new(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    env, cfg = wire_env(tmp_path, FAKE_LOG=str(log))
    res = wire(env)
    assert res.returncode == 0, res.stderr
    data = tomllib.loads(cfg.read_text())
    pp = data["output"]["post_process"]
    assert pp == {"timeout_ms": 20000, "command": PP, "trim": False}
    assert data["osd"]["style"] == "kitsune-hud" and data["osd"]["position"] == "top-center"
    assert data["osd"]["margin_px"] == 20 and data["osd"]["frontend"] == "auto"
    assert data["profiles"]["terminal"]["post_process_command"] == f"{PP} terminal"
    assert data["profiles"]["chat"]["post_process_command"] == f"{PP} chat"
    assert data["profiles"]["command"]["post_process_command"] == f"{PP} command"
    assert "# my voxtype config" in cfg.read_text() and "# fast one" in cfg.read_text()
    assert len(list((tmp_path / "config/voxtype").glob("config.toml.bak.*"))) == 1

    keys = manifest(tmp_path)["keys"]
    assert keys["output.post_process.timeout_ms"] == {"old": 30000, "new": 20000, "via": "voxtype"}
    assert keys["output.post_process.command"]["old"] is None
    assert keys["osd.style"] == {"old": "default", "new": "kitsune-hud", "via": "voxtype"}
    assert "osd.position" not in keys, "a key you set and Shuush did not change is not Shuush's to restore"
    assert keys["profiles.terminal.post_process_command"]["old"] == "mine --terminal"
    assert keys["profiles.terminal.post_process_command"]["via"] == "toml"

    # Only validated writes through the CLI, only the keys Shuush owns
    sets = [line.split()[2] for line in log.read_text().splitlines() if line.startswith("config set ") and "--help" not in line]
    assert sorted(sets) == ["osd.margin_px", "osd.style", "output.post_process.command",
                            "output.post_process.timeout_ms", "output.post_process.trim"]


def test_wire_twice_changes_nothing_and_keeps_the_first_old_values(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path)
    wire(env)
    once, m1 = cfg.read_bytes(), manifest(tmp_path)["keys"]
    log = tmp_path / "calls"
    env["FAKE_LOG"] = str(log)
    res = wire(env)
    assert res.returncode == 0
    assert cfg.read_bytes() == once
    assert manifest(tmp_path)["keys"] == m1
    assert "config set output" not in log.read_text()


def test_unwire_puts_every_key_back_exactly(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path)
    wire(env)
    assert cfg.read_text() != ORIGINAL
    res = wire(env, action="unwire")
    assert res.returncode == 0, res.stderr
    assert "Restored" in res.stdout
    assert cfg.read_text() == ORIGINAL
    assert not (tmp_path / "config/shuush/wired.json").exists()
    assert "Nothing to unwire" in wire(env, action="unwire").stdout


def test_unwire_after_a_rewire_still_restores_the_first_values(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path)
    wire(env)
    # The script moved: the command and the profile commands change, and still go back to the original
    res = run([sys.executable, "-I", str(LIBEXEC / "shuush_wire.py"), "wire", "--command", "/new/place/voxtype-postprocess.sh"], env)
    assert res.returncode == 0, res.stderr
    assert tomllib.loads(cfg.read_text())["output"]["post_process"]["command"] == "/new/place/voxtype-postprocess.sh"
    assert manifest(tmp_path)["keys"]["output.post_process.command"]["old"] is None
    assert manifest(tmp_path)["keys"]["profiles.terminal.post_process_command"]["old"] == "mine --terminal"
    wire(env, action="unwire")
    assert cfg.read_text() == ORIGINAL


def test_a_key_you_changed_since_is_yours(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path)
    wire(env)
    subprocess.run([str(FAKE), "config", "set", "osd.style", "mine"], env=env, check=True)
    status = wire(env, action="status")
    assert "changed" in status.stdout and "osd.style" in status.stdout
    assert json.loads(wire(env, "--json", action="status").stdout)["changed"] == ["osd.style"]
    # wire leaves it, and says so
    out = wire(env)
    assert "osd.style" in out.stdout and "you changed it" in out.stdout
    assert tomllib.loads(cfg.read_text())["osd"]["style"] == "mine"
    # unwire restores the rest and leaves it, keeping the record
    res = wire(env, action="unwire")
    assert res.returncode == 0
    assert tomllib.loads(cfg.read_text())["osd"]["style"] == "mine"
    assert list(manifest(tmp_path)["keys"]) == ["osd.style"]
    wire(env, "--force", action="unwire")
    assert cfg.read_text() == ORIGINAL
    assert not (tmp_path / "config/shuush/wired.json").exists()


def test_unwire_dry_run_writes_nothing(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path)
    wire(env)
    wired = cfg.read_bytes()
    res = wire(env, "--dry-run", action="unwire")
    assert "nothing is written" in res.stdout and "restore output.post_process.timeout_ms: 20000 -> 30000" in res.stdout
    assert "restore output.post_process.command: " in res.stdout and "-> not set" in res.stdout
    assert cfg.read_bytes() == wired and (tmp_path / "config/shuush/wired.json").exists()


def test_wire_creates_missing_sections_and_unwire_removes_the_profile_blocks_it_made(tmp_path: Path) -> None:
    minimal = 'engine = "whisper"\n\n[output]\nmode = "type"\n'
    env, cfg = wire_env(tmp_path, config=minimal)
    assert wire(env).returncode == 0
    data = tomllib.loads(cfg.read_text())
    assert data["output"]["post_process"]["trim"] is False and data["osd"]["style"] == "kitsune-hud"
    assert set(data["profiles"]) == {"terminal", "chat", "command"}
    assert wire(env, action="unwire").returncode == 0
    after = tomllib.loads(cfg.read_text())
    assert "profiles" not in after
    assert after["output"]["mode"] == "type" and "command" not in after["output"].get("post_process", {})


def test_a_voxtype_that_does_not_know_a_key_skips_it(tmp_path: Path) -> None:
    schema = json.loads(Path(STOCK).read_text())
    schema["keys"] = [k for k in schema["keys"] if k["key"] != "osd.margin_px"]
    custom = tmp_path / "schema-old.json"
    custom.write_text(json.dumps(schema))
    env, cfg = wire_env(tmp_path, str(custom))
    res = wire(env, "--dry-run")
    assert "osd.margin_px" in res.stdout and "does not know the key" in res.stdout
    assert wire(env).returncode == 0
    assert "osd.margin_px" not in manifest(tmp_path)["keys"]


def test_wire_without_a_usable_voxtype(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path)
    broken = tmp_path / "voxtype"
    broken.write_text("#!/bin/sh\nexit 2\n")
    broken.chmod(0o755)
    env["SHUUSH_VOXTYPE_BIN"] = str(broken)
    before = cfg.read_bytes()
    res = wire(env)
    assert res.returncode == 1 and "needs voxtype 1.3.1 or newer" in res.stderr
    assert cfg.read_bytes() == before and not (tmp_path / "config/shuush").exists()
    del env["SHUUSH_VOXTYPE_BIN"]
    env["PATH"] = str(tmp_path / "empty")
    assert "voxtype is not installed" in wire(env).stderr


def test_wire_needs_a_config_to_wire(tmp_path: Path) -> None:
    env = sandbox(tmp_path)
    res = wire(env)
    assert res.returncode == 1 and "voxtype setup" in res.stderr


def test_wire_never_sets_a_fork_only_key_on_either_daemon(tmp_path: Path) -> None:
    for schema in (STOCK, FORK):
        d = tmp_path / Path(schema).stem
        d.mkdir()
        log = d / "calls"
        env, _cfg = wire_env(d, schema, FAKE_LOG=str(log))
        assert wire(env).returncode == 0
        fork_keys = {k["key"] for k in json.loads(Path(FORK).read_text())["keys"]} - {
            k["key"] for k in json.loads(Path(STOCK).read_text())["keys"]}
        touched = {line.split()[2] for line in log.read_text().splitlines() if line.startswith("config set ") and "--help" not in line}
        assert touched and not (touched & fork_keys)


def test_shuush_cli_wire_unwire_and_the_old_name(tmp_path: Path) -> None:
    env, cfg = wire_env(tmp_path)
    cli = str(ROOT / "bin/shuush")
    res = run([cli, "wire", "--dry-run"], env)
    assert res.returncode == 0 and "nothing is written" in res.stdout and cfg.read_text() == ORIGINAL
    assert not (tmp_path / "config/voxtype/osd").exists(), "a dry run seeds no files either"
    res = run([cli, "wire-config"], env)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "config/voxtype/vocabulary.txt").exists() and (tmp_path / "config/voxtype/snippets").exists()
    status = run([cli, "wire", "--status"], env).stdout
    assert "output.post_process.command" in status and "changed" not in status
    assert run([cli, "unwire"], env).returncode == 0
    assert cfg.read_text() == ORIGINAL


# ── 3. HUD state files ───────────────────────────────────────────────────────


def test_hud_reads_voxtypes_state_files_with_the_fork_files_as_fallback() -> None:
    qml = (ROOT / "osd/kitsune-hud/KitsuneHud.qml").read_text()
    assert '"/voxtype/outcome.json"' in qml and '"/voxtype/recording.json"' in qml
    assert "o.outcome === \"ok\"" in qml and "started_at_ms" in qml and "at_ms" in qml
    # fallbacks stay: the fork's marker and the watcher's session_start
    assert '"/voxtype/output_done"' in qml and '"/voxtype/session_start"' in qml
    # delivered = the newest of the three
    assert "Math.max(root.outcomeOkAt, root.outputDoneAt, root.ppResultAt)" in qml
    # only a successful outcome counts as delivered
    assert 'if (o.outcome === "ok") root.outcomeOkAt = at;' in qml
    # a stale recording.json from another take is ignored the way session_start is
    assert "Math.abs(localStart - recordingStart) < 3000" in qml


@pytest.mark.skipif(shutil.which("qmllint") is None, reason="qmllint not installed")
def test_hud_qml_still_lints() -> None:
    res = subprocess.run(["qmllint", str(ROOT / "osd/kitsune-hud/KitsuneHud.qml")], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_review_takes_the_start_from_recording_json_then_session_start(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("review", str(LIBEXEC / "voxtype-review.py"))
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    rt = tmp_path
    assert review.compute_audio_ms(rt, now_ms=1_000_000) == 0
    (rt / "session_start").write_text("990000")
    assert review.compute_audio_ms(rt, now_ms=1_000_000) == 10_000
    (rt / "recording.json").write_text(json.dumps({"started_at_ms": 996_000, "mode": "toggle", "engine": "whisper", "model": "x"}))
    assert review.compute_audio_ms(rt, now_ms=1_000_000) == 4_000
    (rt / "recording.json").write_text("{not json")
    assert review.compute_audio_ms(rt, now_ms=1_000_000) == 10_000


# ── 4. the patched wtype build ───────────────────────────────────────────────

BUILD = LIBEXEC / "shuush-build-wtype.sh"


def build(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(BUILD), *args], env=env or {"PATH": os.environ["PATH"], "HOME": "/nonexistent"},
                          capture_output=True, text=True, timeout=300)


def test_wtype_patch_is_a_patch_for_wtype() -> None:
    patch = LIBEXEC / "wtype-key-gap.patch"
    stat = subprocess.run(["git", "apply", "--numstat", str(patch)], capture_output=True, text=True)
    assert stat.returncode == 0 and stat.stdout.split()[2] == "main.c"
    assert "WTYPE_KEY_GAP_US" in patch.read_text()


def test_wtype_check_tells_patched_from_stock(tmp_path: Path) -> None:
    patched = tmp_path / "patched-wtype"
    patched.write_bytes(b"\x7fELF....WTYPE_KEY_GAP_US\x00....")
    patched.chmod(0o755)
    stock = tmp_path / "stock-wtype"
    stock.write_bytes(b"\x7fELF....usleep....")
    stock.chmod(0o755)
    ok = build("--check", "--bin", str(patched))
    assert ok.returncode == 0 and "has the key-gap patch" in ok.stdout
    bad = build("--check", "--bin", str(stock))
    assert bad.returncode == 1 and "is stock wtype" in bad.stdout and "shuush build-wtype" in bad.stdout
    gone = build("--check", "--bin", str(tmp_path / "missing"))
    assert gone.returncode == 1 and "no wtype found" in gone.stdout


def test_wtype_dry_run_names_the_steps_and_touches_nothing(tmp_path: Path) -> None:
    res = build("--dry-run", "--prefix", str(tmp_path / "out"))
    assert "wtype-key-gap.patch" in res.stdout and "github.com/atx/wtype at 4b9cc29" in res.stdout
    assert f"install  {tmp_path / 'out'}/wtype" in res.stdout
    assert not (tmp_path / "out").exists()
    if res.returncode == 0:
        assert "tools    all present" in res.stdout
    else:
        assert "missing" in res.stdout


def test_wtype_unknown_option_is_rejected() -> None:
    assert build("--nope").returncode == 2


@pytest.mark.skipif(not os.environ.get("WTYPE_SRC"), reason="set WTYPE_SRC to a wtype checkout at 4b9cc29 to build it")
def test_wtype_builds_from_a_checkout_into_a_prefix(tmp_path: Path) -> None:
    if build("--dry-run").returncode != 0:
        pytest.skip("build tools missing")
    res = build("--src", os.environ["WTYPE_SRC"], "--prefix", str(tmp_path / "out"))
    assert res.returncode == 0, res.stdout + res.stderr
    assert build("--check", "--bin", str(tmp_path / "out" / "wtype")).returncode == 0
    assert not (tmp_path / "home").exists()


def test_installers_ship_the_new_files() -> None:
    for f in ("install.sh", "packaging/aur/shuush-git/PKGBUILD"):
        text = (ROOT / f).read_text()
        for name in ("shuush-caps", "shuush_caps.py", "shuush_wire.py"):
            assert name in text, f"{f} does not mention {name}"
    assert (LIBEXEC / "shuush-caps").stat().st_mode & 0o111
    assert (BUILD).stat().st_mode & 0o111
