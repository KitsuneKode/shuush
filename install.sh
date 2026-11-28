#!/usr/bin/env bash
# install.sh — Install shuush on top of any voxtype installation (Arch / Omarchy / Hyprland).
# Safe & idempotent: never overwrites existing user secrets, vocabulary, or corrections.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${SHUUSH_PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}/shuush}"
BIN_DIR="${SHUUSH_BIN_DIR:-$HOME/.local/bin}"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

WIRE_CONFIG=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --wire-config) WIRE_CONFIG=1 ;;
    --uninstall)   UNINSTALL=1 ;;
    -h|--help)
      echo "Usage: ./install.sh [--wire-config] [--uninstall]"
      echo "  --wire-config  Wire [output.post_process], profiles & OSD into ~/.config/voxtype/config.toml with `voxtype config set` (backed up; `shuush unwire` restores)"
      echo "  --uninstall    Remove installed shuush binaries, units, and desktop entry (keeps user data)"
      exit 0 ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  if [ -s "${XDG_CONFIG_HOME:-$HOME/.config}/shuush/wired.json" ]; then
    echo "Note: config.toml still has the keys 'shuush wire' set; 'shuush unwire' puts them back (run it first next time)."
  fi
  rm -f "$BIN_DIR/shuush" "$BIN_DIR/shuush-studio" "$BIN_DIR/shuush-ime" "$APP_DIR/shuush-studio.desktop" \
        "$UNIT_DIR/shuush-llm-proxy.service" "$UNIT_DIR/shuush-live-osd.service"
  rm -rf "$PREFIX"
  echo "Uninstalled shuush from $PREFIX (user config & history in $CONF_DIR preserved)."
  exit 0
fi

echo "Installing shuush -> $PREFIX"
mkdir -p "$PREFIX/bin" "$PREFIX/libexec" "$BIN_DIR" "$CONF_DIR/osd/kitsune-hud" "$APP_DIR" "$UNIT_DIR"

install -m 0755 "$SRC/bin/shuush" "$PREFIX/bin/shuush"
for f in "$SRC/libexec/"*; do
  [ -f "$f" ] || continue
  base="$(basename "$f")"
  case "$base" in
    *.sh|voxtype-studio.py|voxtype_learn.py|voxtype-review.py|voxtype-llm-proxy.py|voxtype_screen_ctx.py|voxtype_pp_gate.py|shuush-caps|shuush_caps.py|shuush_wire.py)
      install -m 0755 "$f" "$PREFIX/libexec/$base" ;;
    *)
      install -m 0644 "$f" "$PREFIX/libexec/$base" ;;
  esac
done

# shuush-ime's source, for `shuush build-ime` (optional; built on demand with cargo).
rm -rf "$PREFIX/ime"
mkdir -p "$PREFIX/ime"
cp -r "$SRC/ime/Cargo.toml" "$SRC/ime/Cargo.lock" "$SRC/ime/LICENSE" "$SRC/ime/src" "$SRC/ime/tests" "$PREFIX/ime/"

ln -sfn "$PREFIX/bin/shuush" "$BIN_DIR/shuush"
cat > "$BIN_DIR/shuush-studio" <<EOF
#!/usr/bin/env bash
exec "$BIN_DIR/shuush" studio "\$@"
EOF
chmod 0755 "$BIN_DIR/shuush-studio"

# Install KitsuneHud Quickshell OSD style package
install -m 0644 "$SRC/osd/kitsune-hud/KitsuneHud.qml" "$CONF_DIR/osd/kitsune-hud/KitsuneHud.qml"
install -m 0644 "$SRC/osd/kitsune-hud/voxtype-osd.toml" "$CONF_DIR/osd/kitsune-hud/voxtype-osd.toml"

# Seed starter config files only if missing
[ -f "$CONF_DIR/postprocess.env" ] || install -m 0644 "$SRC/config/postprocess.env.example" "$CONF_DIR/postprocess.env"
[ -f "$CONF_DIR/vocabulary.txt" ] || printf '# Exact spellings for technical terms & proper nouns (one per line)\n' > "$CONF_DIR/vocabulary.txt"
[ -f "$CONF_DIR/snippets" ] || printf '# Voice snippets: trigger -> expansion text\n' > "$CONF_DIR/snippets"

# Desktop entry & systemd user services
sed "s|@BIN_DIR@|$BIN_DIR|g" "$SRC/config/shuush-studio.desktop" > "$APP_DIR/shuush-studio.desktop"
sed "s|@LIBEXEC@|$PREFIX/libexec|g" "$SRC/config/shuush-llm-proxy.service" > "$UNIT_DIR/shuush-llm-proxy.service"
sed "s|@LIBEXEC@|$PREFIX/libexec|g" "$SRC/config/shuush-live-osd.service" > "$UNIT_DIR/shuush-live-osd.service"
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

if [ "$WIRE_CONFIG" = 1 ]; then
  "$BIN_DIR/shuush" wire-config
fi

echo ""
echo "Installed shuush successfully!"
echo "  CLI:     $BIN_DIR/shuush"
echo "  Studio:  $BIN_DIR/shuush-studio"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  PATH:    Add $BIN_DIR to your PATH (e.g. export PATH=\"$BIN_DIR:\$PATH\")" ;;
esac
if [ -d "${KUWAI_ROOT:-$HOME/.local/share/kuwai}" ] || [ -d "$HOME/Projects/rice/hypr/kuwai" ]; then
  echo "  Kuwai:   Detected! Add to ~/.config/hypr/user.lua:"
  echo "           K.voxtype_scripts = K.home .. \"/.local/share/shuush/libexec/\""
else
  echo "  Binds:   $SRC/config/hyprland-binds.conf"
fi
echo ""
"$BIN_DIR/shuush" doctor || true
