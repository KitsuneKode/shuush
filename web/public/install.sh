#!/usr/bin/env bash
# Shuush 1-line installer — https://shuush.kitsunekode.in/install.sh
# Usage:
#   curl -fsSL https://shuush.kitsunekode.in/install.sh | bash
#   curl -fsSL https://shuush.kitsunekode.in/install.sh | bash -s -- --no-wire
#   curl -fsSL https://shuush.kitsunekode.in/install.sh | bash -s -- --uninstall
set -euo pipefail

REPO_URL="${SHUUSH_REPO:-https://github.com/KitsuneKode/shuush.git}"
SRC_DIR="${SHUUSH_SRC_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/shuush-src}"
WIRE_ARGS=("--wire-config")

for arg in "$@"; do
  case "$arg" in
    --no-wire)   WIRE_ARGS=() ;;
    --uninstall) WIRE_ARGS=("--uninstall") ;;
  esac
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_GRN=$'\033[32m'; C_AMB=$'\033[33m'; C_RED=$'\033[31m'; C_RST=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_GRN=""; C_AMB=""; C_RED=""; C_RST=""
fi

info() { printf '%s==>%s %s\n' "$C_BOLD" "$C_RST" "$*"; }
ok()   { printf '  %s[ok]%s   %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$C_AMB" "$C_RST" "$*"; }
die()  { printf '  %s[err]%s  %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }

printf '\n%sShuush%s %s— local GPU dictation & GTK4 Studio on top of voxtype%s\n\n' "$C_BOLD" "$C_RST" "$C_DIM" "$C_RST"

command -v git >/dev/null 2>&1 || die "git is required. Install git and re-run."

if ! command -v voxtype >/dev/null 2>&1; then
  warn "voxtype binary not found on PATH yet."
  if [ -r /etc/os-release ]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    case "${ID:-}" in
      arch|omarchy|cachyos|endeavouros|manjaro)
        printf '         Install stock voxtype first:\n         %syay -S voxtype-bin wtype wl-clipboard jq python-gobject libadwaita gtk4 at-spi2-core && voxtype setup%s\n\n' "$C_BOLD" "$C_RST" ;;
      fedora)
        printf '         Install voxtype RPM from https://voxtype.io and dependencies:\n         %ssudo dnf install wtype wl-clipboard jq python3-gobject libadwaita gtk4 at-spi2-core%s\n\n' "$C_BOLD" "$C_RST" ;;
      ubuntu|debian|pop)
        printf '         Install voxtype .deb from https://voxtype.io and dependencies:\n         %ssudo apt install wtype wl-clipboard jq python3-gi gir1.2-adw-1 gir1.2-gtk-4.0 gir1.2-atspi-2.0%s\n\n' "$C_BOLD" "$C_RST" ;;
      *)
        printf '         Install voxtype from https://voxtype.io (MIT), then run voxtype setup.\n\n' ;;
    esac
  fi
fi

mkdir -p "$(dirname "$SRC_DIR")"
if [ -d "$SRC_DIR/.git" ]; then
  info "Updating Shuush checkout in $SRC_DIR"
  git -C "$SRC_DIR" pull --ff-only --quiet || warn "Could not fast-forward $SRC_DIR; installing current checkout"
else
  info "Cloning $REPO_URL -> $SRC_DIR"
  git clone --depth 1 --quiet "$REPO_URL" "$SRC_DIR"
fi

# Only wire config.toml automatically when ~/.config/voxtype/config.toml exists
CONF_TOML="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype/config.toml"
if [ "${WIRE_ARGS[0]:-}" = "--wire-config" ] && [ ! -f "$CONF_TOML" ]; then
  warn "No $CONF_TOML yet — skipping auto-wire (run 'voxtype setup && shuush wire-config' next)."
  WIRE_ARGS=()
fi

info "Running Shuush installer"
exec "$SRC_DIR/install.sh" "${WIRE_ARGS[@]}"
