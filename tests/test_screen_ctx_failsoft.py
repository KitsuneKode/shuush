"""voxtype_screen_ctx.py must never abort the interpreter when there is no accessibility bus.

libatspi answers a missing bus with g_error(), which is SIGABRT: a core dump and a crash alert
for every take. The checks below run the script with no usable bus and core dumps switched off,
and expect a quiet exit 3 (no answer) instead.
"""
import os
import resource
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "libexec" / "voxtype_screen_ctx.py"


def _run(env_extra, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY")}
    env.update(env_extra)
    return subprocess.run([sys.executable, "-I", str(SCRIPT), "42", "kitty", "zsh", *args], capture_output=True, text=True, env=env,
                          timeout=20, preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))


def test_no_session_bus_is_a_quiet_no_answer(tmp_path):
    r = _run({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert r.returncode == 3 and r.stdout == ""


def test_session_bus_that_is_not_there_is_a_quiet_no_answer(tmp_path):
    r = _run({"XDG_RUNTIME_DIR": str(tmp_path), "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path}/nobus"}, "--sensitivity-only")
    assert r.returncode == 3 and r.stdout == ""


def test_an_a11y_address_that_is_dead_is_a_quiet_no_answer(tmp_path):
    r = _run({"XDG_RUNTIME_DIR": str(tmp_path), "AT_SPI_BUS_ADDRESS": f"unix:path={tmp_path}/dead"})
    assert r.returncode == 3 and r.stdout == ""


def test_explicit_off_switch(tmp_path):
    r = _run({"XDG_RUNTIME_DIR": str(tmp_path), "SHUUSH_NO_ATSPI": "1"})
    assert r.returncode == 3 and r.stdout == ""


def test_focused_field_is_none_without_a_bus(tmp_path):
    code = f"import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import voxtype_screen_ctx as s; print(s.focused_field(42, 'kitty', 'zsh'))"
    env = {k: v for k, v in os.environ.items() if k not in ("DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS")}
    env["XDG_RUNTIME_DIR"] = str(tmp_path)
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=20,
                       preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    assert r.returncode == 0 and r.stdout.strip() == "None"
