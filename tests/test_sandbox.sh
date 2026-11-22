#!/usr/bin/env bash
# tests/test_sandbox.sh — Multi-environment isolated sandbox test suite for Shuush.
# Never reads or modifies the caller's live ~/.config/voxtype or ~/.local/state/voxtype.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
ok() { printf '  [PASS] %s\n' "$1"; PASS=$((PASS + 1)); }

echo "=== 1. Syntax & Bytecode Checks ==="
bash -n "$ROOT/bin/shuush" "$ROOT/install.sh" "$ROOT/libexec/"*.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "$ROOT/libexec/"*.py
ok "All bash and Python modules parse cleanly"

echo "=== 2. Anti-Paraphrase Span Gate Selftest ==="
python3 -I "$ROOT/libexec/voxtype_pp_gate.py" test >/dev/null
ok "voxtype_pp_gate.py selftest passed"

echo "=== 3. Sandbox Install: Stock voxtype (Whisper / AMD-Intel Vulkan) ==="
TMP1="$(mktemp -d)"
trap 'rm -rf "$TMP1" "${TMP2:-}" "${TMP3:-}" "${TMP4:-}"' EXIT
mkdir -p "$TMP1/.config/voxtype"
cat > "$TMP1/.config/voxtype/config.toml" <<'TOML'
engine = "whisper"

[whisper]
model = "large-v3-turbo"

[output]
mode = "type"

[text]
replacements = { "box type" = "voxtype" }
TOML

HOME="$TMP1" XDG_CONFIG_HOME="$TMP1/.config" XDG_DATA_HOME="$TMP1/.local/share" XDG_STATE_HOME="$TMP1/.local/state" \
  "$ROOT/install.sh" --wire-config >/dev/null
test -x "$TMP1/.local/bin/shuush"
test -x "$TMP1/.local/bin/shuush-studio"
test -f "$TMP1/.config/voxtype/osd/kitsune-hud/KitsuneHud.qml"
test -f "$TMP1/.local/share/applications/shuush-studio.desktop"
test -f "$TMP1/.config/systemd/user/shuush-llm-proxy.service"
test -f "$TMP1/.config/systemd/user/shuush-live-osd.service"
grep -q '\[output\.post_process\]' "$TMP1/.config/voxtype/config.toml"
grep -q '\[profiles\.terminal\]' "$TMP1/.config/voxtype/config.toml"
grep -q '\[profiles\.command\]' "$TMP1/.config/voxtype/config.toml"
grep -q 'style = "kitsune-hud"' "$TMP1/.config/voxtype/config.toml"
grep -q '"box type" = "voxtype"' "$TMP1/.config/voxtype/config.toml"
ls "$TMP1/.config/voxtype/config.toml.bak."* >/dev/null
ok "Fresh install + --wire-config preserves existing TOML and backs up config"

HOME="$TMP1" XDG_CONFIG_HOME="$TMP1/.config" XDG_DATA_HOME="$TMP1/.local/share" XDG_STATE_HOME="$TMP1/.local/state" \
  "$ROOT/install.sh" --uninstall >/dev/null
test ! -e "$TMP1/.local/bin/shuush"
test -f "$TMP1/.config/voxtype/config.toml"
ok "Uninstall removes binaries cleanly while preserving user config"

echo "=== 4. Sandbox Install: First-Party Kuwai Detection ==="
TMP2="$(mktemp -d)"
mkdir -p "$TMP2/.local/share/kuwai" "$TMP2/.config/voxtype"
OUT_KUWAI=$(HOME="$TMP2" KUWAI_ROOT="$TMP2/.local/share/kuwai" XDG_CONFIG_HOME="$TMP2/.config" XDG_DATA_HOME="$TMP2/.local/share" XDG_STATE_HOME="$TMP2/.local/state" "$ROOT/install.sh")
grep -q "K.voxtype_scripts" <<<"$OUT_KUWAI"
ok "Kuwai installation detected and surfaced 1-line user.lua snippet"

echo "=== 5. Cross-Compositor Safety (No hyprctl / GNOME-KDE fallback) ==="
NO_HYPR_PATH="$TMP2/bin"
mkdir -p "$NO_HYPR_PATH"
for tool in bash sh jq cat mkdir rm mv date timeout head printf stat; do
  p="$(command -v "$tool" || true)"
  [ -n "$p" ] && ln -sf "$p" "$NO_HYPR_PATH/$tool"
done
PATH="$NO_HYPR_PATH" XDG_RUNTIME_DIR="$TMP2/rt" "$ROOT/libexec/voxtype-outline.sh" on
PATH="$NO_HYPR_PATH" XDG_RUNTIME_DIR="$TMP2/rt" "$ROOT/libexec/voxtype-outline.sh" off
ok "voxtype-outline.sh is a clean no-op without hyprctl"

echo "=== 6. Cross-Distro Self-Learning, Poisoning Guards & Studio Secrets (chmod 0600) ==="
TMP3="$(mktemp -d)"
HOME="$TMP3" XDG_CONFIG_HOME="$TMP3/.config" XDG_STATE_HOME="$TMP3/.local/state" python3 - "$ROOT" <<'PY'
import importlib.util, os, pathlib, stat, sys, gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "libexec"))
import voxtype_learn as learn

# Simulate minimal Linux distro with NO /usr/share/dict installed
learn._DICT_PATHS = ()
learn._COMMON = None
assert "hosts" in learn._common_words() and "things" in learn._common_words()
assert learn.is_safe_auto_candidate("hosts", "hooks", "hooks") is None
assert learn.is_safe_auto_candidate("things", "tasks", "tasks") is None
assert learn.is_safe_auto_candidate("the umarchi", "Omarchy", "Omarchy") is None
assert learn.is_safe_auto_candidate("like t3 code introduced", "did t3code introduce", "t3code") is None
assert learn.is_safe_auto_candidate("t three", "T3", "T3") == "T3"
assert learn.is_safe_auto_candidate("cube ctl", "kubectl", "kubectl") == "kubectl"

# Reject / unreject roundtrip in sandbox state dir
key = learn.reject_pair("cube ctl", "kubectl")
assert learn.is_rejected("cube ctl", "kubectl")
assert learn.is_safe_auto_candidate("cube ctl", "kubectl", "kubectl") is None
learn.unreject_pair(key)
assert not learn.is_rejected("cube ctl", "kubectl")

# Studio CSS + chmod 0600 secrets writer + TOML str writer
spec = importlib.util.spec_from_file_location("vs", str(root / "libexec/voxtype-studio.py"))
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)
errs = []
p = Gtk.CssProvider()
p.connect("parsing-error", lambda _p, _s, e: errs.append(str(e)))
p.load_from_data(vs.CSS)
assert not errs, f"CSS errors: {errs}"

vs.write_secret_key("GROQ_API_KEY", "dummy-test-value")
mode = stat.S_IMODE(os.stat(vs.SECRETS_ENV).st_mode)
assert mode == 0o600, f"expected 0600 secrets.env, got {oct(mode)}"
assert "GROQ_API_KEY" in vs.configured_secret_keys()

vs.CONFIG_TOML.parent.mkdir(parents=True, exist_ok=True)
vs.CONFIG_TOML.write_text('engine = "whisper"\n\n[osd]\nposition = "bottom-center"\n')
assert vs.write_toml_str("osd", "position", "top-center")
assert vs.read_toml_str("osd", "position") == "top-center"
PY
ok "Cross-distro fallback wordlist, phonetic learning, Studio CSS, chmod 0600 secrets, and TOML writer verified"

echo "=== 7. Real Stock voxtype v1.3.1 default.toml, Proxy No-Secrets & Neutral Prompt ==="
TMP4="$(mktemp -d)"
mkdir -p "$TMP4/.config/voxtype"
cat > "$TMP4/.config/voxtype/config.toml" <<'TOML'
state_file = "auto"

[whisper]
model = "large-v3-turbo"

[output]
mode = "type"

# [output.post_process]
# command = "ollama run llama3.2:1b 'Clean up this dictation'"
# timeout_ms = 30000

[text]
# replacements = { "vox type" = "voxtype" }
filter_filler_words = true
repair_sentence_breaks = true

[osd]
frontend = "auto"
style = "default"
layout = "compact"

[history]
enabled = true
retention_days = 30
exclude_apps = ["KeePassXC", "Bitwarden"]
TOML

HOME="$TMP4" XDG_CONFIG_HOME="$TMP4/.config" XDG_DATA_HOME="$TMP4/.local/share" XDG_STATE_HOME="$TMP4/.local/state" \
  "$ROOT/install.sh" --wire-config >/dev/null

# Also test idempotency on a second --wire-config run
HOME="$TMP4" XDG_CONFIG_HOME="$TMP4/.config" XDG_DATA_HOME="$TMP4/.local/share" XDG_STATE_HOME="$TMP4/.local/state" \
  "$TMP4/.local/bin/shuush" wire-config >/dev/null

HOME="$TMP4" XDG_CONFIG_HOME="$TMP4/.config" XDG_STATE_HOME="$TMP4/.local/state" python3 - "$ROOT" "$TMP4/.config/voxtype/config.toml" <<'PY'
import importlib.util, pathlib, sys, tomllib

root = pathlib.Path(sys.argv[1])
cfg = pathlib.Path(sys.argv[2])
parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))

# Verify voxtype v1.3.1 sections preserved + Shuush sections properly wired
assert parsed["output"]["post_process"]["command"].endswith("voxtype-postprocess.sh")
assert parsed["output"]["post_process"]["timeout_ms"] == 20000
for prof in ("terminal", "chat", "command"):
    assert parsed["profiles"][prof]["post_process_command"].endswith(f"voxtype-postprocess.sh {prof}")
assert parsed["osd"]["style"] == "kitsune-hud"
assert parsed["osd"]["frontend"] == "auto"  # preserves auto fallback for non-Quickshell users
assert parsed["history"]["enabled"] is True
assert parsed["history"]["exclude_apps"] == ["KeePassXC", "Bitwarden"]

# Verify sync_replacements works on stock v1.3.1 config where # replacements was commented out
sys.path.insert(0, str(root / "libexec"))
import voxtype_learn as learn
learn.remember("webar", "Waybar", "user")
added = learn.sync_replacements()
assert added >= 1, f"expected replacement added on stock v1.3.1 config, got {added}"
parsed2 = tomllib.loads(cfg.read_text(encoding="utf-8"))
assert parsed2["text"]["replacements"]["webar"] == "Waybar"

# Verify sync_replacements also supports v1.3.1 [text.replacements] sub-table format
cfg.write_text('[text]\nfilter_filler_words = true\n\n[text.replacements]\n"cone ogres" = "CODEOWNERS"\n', encoding="utf-8")
added_sub = learn.sync_replacements()
assert added_sub >= 1
parsed3 = tomllib.loads(cfg.read_text(encoding="utf-8"))
assert parsed3["text"]["replacements"]["cone ogres"] == "CODEOWNERS"
assert parsed3["text"]["replacements"]["webar"] == "Waybar"

# Verify voxtype-llm-proxy.py handles missing secrets.env cleanly (no FileNotFoundError)
spec = importlib.util.spec_from_file_location("proxy", str(root / "libexec/voxtype-llm-proxy.py"))
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
assert proxy.api_keys() == []
status, body = proxy.UP.request("POST", "/openai/v1/chat/completions", b"{}")
assert status == 503 and b"no_api_key" in body
PY

# Verify no personal bio leaked into default postprocess script
! grep -q "Manash" "$ROOT/libexec/voxtype-postprocess.sh"
ok "Stock voxtype v1.3.1 wiring, replacements sync (inline + sub-table), proxy no-secrets & neutral prompt verified"

echo ""
echo "ALL $PASS SANDBOX TEST SUITES PASSED"
