#!/usr/bin/env bash
# Shuush standalone pre-flight & health doctor — https://shuush.kitsunekode.in/doctor.sh
# Usage:
#   curl -fsSL https://shuush.kitsunekode.in/doctor.sh | bash
#   shuush doctor [--json]
set -euo pipefail

if command -v shuush >/dev/null 2>&1; then
  exec shuush doctor "$@"
fi

# Pre-install check when shuush is not installed yet
JSON=0
[ "${1:-}" = "--json" ] && JSON=1
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "$JSON" = 0 ]; then
  G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; R=$'\033[0m'
else
  G=""; Y=""; B=""; R=""
fi

OK=0; WARN=0; ITEMS=()
chk() {
  local id="$1" cond="$2" label="$3" fix="$4"
  if eval "$cond" >/dev/null 2>&1; then
    OK=$((OK + 1))
    [ "$JSON" = 1 ] && ITEMS+=("{\"id\":\"$id\",\"status\":\"ok\",\"label\":\"$label\"}") || printf '  %s[ok]%s   %s\n' "$G" "$R" "$label"
  else
    WARN=$((WARN + 1))
    [ "$JSON" = 1 ] && ITEMS+=("{\"id\":\"$id\",\"status\":\"warn\",\"label\":\"$label\",\"fix\":\"$fix\"}") || printf '  %s[warn]%s %s — %s\n' "$Y" "$R" "$label" "$fix"
  fi
}

[ "$JSON" = 0 ] && printf '%sShuush pre-flight doctor%s (https://shuush.kitsunekode.in)\n' "$B" "$R"
chk "wayland"  '[ -n "${WAYLAND_DISPLAY:-}" ]' "Wayland session active" "Shuush targets Wayland compositors (Hyprland, Sway, Niri)"
chk "voxtype"  'command -v voxtype' "voxtype binary installed" "yay -S voxtype-bin (or https://voxtype.io)"
chk "gpu"      'command -v nvidia-smi || [ -e /dev/kfd ] || ls /dev/dri/renderD* >/dev/null 2>&1' "GPU acceleration available (CUDA/ROCm/Vulkan)" "CPU AVX2 mode still works"
chk "clip"     'command -v wl-copy && command -v wl-paste' "wl-clipboard (wl-copy / wl-paste)" "pacman -S wl-clipboard"
chk "type"     'command -v wtype || command -v ydotool || command -v dotool' "Wayland text injector (wtype/ydotool/dotool)" "pacman -S wtype"
chk "jq"       'command -v jq' "jq installed" "pacman -S jq"
chk "gtk4"     'python3 -c "import gi; gi.require_version(\"Gtk\",\"4.0\"); gi.require_version(\"Adw\",\"1\")"' "GTK4 + Libadwaita Python bindings" "pacman -S python-gobject libadwaita gtk4"
chk "atspi"    'python3 -c "import gi; gi.require_version(\"Atspi\",\"2.0\"); from gi.repository import Atspi"' "AT-SPI 2.0 screen & draft context" "pacman -S at-spi2-core python-gobject"
chk "qs"       'command -v qs || command -v quickshell' "Quickshell (for KitsuneHud waveform pill)" "yay -S quickshell (optional)"

if [ "$JSON" = 1 ]; then
  printf '{"ok":%d,"warnings":%d,"checks":[%s]}\n' "$OK" "$WARN" "$(IFS=,; echo "${ITEMS[*]}")"
else
  printf '\nSummary: %d ok, %d warning(s)\n' "$OK" "$WARN"
  printf 'Install Shuush: %scurl -fsSL https://shuush.kitsunekode.in/install.sh | bash%s\n' "$B" "$R"
fi
