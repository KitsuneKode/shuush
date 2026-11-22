#!/usr/bin/env bash
# Thicken the border of the window this dictation belongs to, then put the
# width back. Works on standard Hyprland and Hyprland 0.57+ Lua; no-op elsewhere.
set -u
command -v hyprctl >/dev/null 2>&1 || exit 0
RT="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/voxtype"
MARK="$RT/outline"

prop() {  # addr value
  if [ "$2" = "unset" ]; then
    hyprctl eval "hl.dispatch(hl.dsp.window.set_prop({ prop = \"border_size\", value = \"unset\", window = \"address:$1\" }))" >/dev/null 2>&1 \
      || hyprctl dispatch setprop "address:$1" bordersize unset >/dev/null 2>&1 || true
  else
    hyprctl eval "hl.dispatch(hl.dsp.window.set_prop({ prop = \"border_size\", value = \"$2\", window = \"address:$1\" }))" >/dev/null 2>&1 \
      || hyprctl dispatch setprop "address:$1" bordersize "$2" >/dev/null 2>&1 || true
  fi
}

off() {
  [ -f "$MARK" ] || return 0
  addr=""
  old=""
  read -r addr old < "$MARK" || true
  rm -f "$MARK"
  [ -n "$addr" ] || return 0
  prop "$addr" unset
  now=$(hyprctl getprop "address:$addr" border_size 2>/dev/null | head -1 || true)
  if [ -n "$old" ] && [ -n "$now" ] && [ "$now" != "$old" ]; then
    prop "$addr" "$old"
  fi
}

on() {
  off
  [ -f "$RT/target_window" ] || return 0
  addr=$(jq -r '.address // empty' "$RT/target_window" 2>/dev/null || true)
  [ -n "$addr" ] || return 0
  old=$(hyprctl getprop "address:$addr" border_size 2>/dev/null | head -1 || echo 2)
  case "$old" in
    ''|*[!0-9]*) old=2 ;;
  esac
  printf '%s %s\n' "$addr" "$old" > "$MARK"
  wide=$((old + 3))
  [ "$wide" -gt 8 ] && wide=8
  [ "$wide" -le "$old" ] && wide=$((old + 1))
  prop "$addr" "$wide"
}

case "${1:-off}" in
  on) on ;;
  *) off ;;
esac
