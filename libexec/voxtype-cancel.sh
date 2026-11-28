#!/bin/sh
# voxtype-cancel.sh [now] — cancel the dictation in progress, if any.
#
# Escape cancels only while dictating (kuwai's Escape bind) and needs a
# double tap: the first press only arms cancel for 600ms and the pill shows
# "Esc again to cancel", so one stray Escape never throws a dictation away.
# Neither press reaches the app; outside dictation there is no bind at all. `now` (SUPER+SHIFT+D) cancels at once.
# No-op when idle. Writes $XDG_RUNTIME_DIR/voxtype/cancel_armed and
# .../cancelled (epoch ms) for the pill.
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RT="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/voxtype"
case "$(cat "$RT/state" 2>/dev/null)" in
  recording|streaming|transcribing) ;;
  *)
    # Idle: nothing to cancel. (Escape only reaches this script while
    # dictating — kuwai's auto-consuming Escape bind hands it to the app
    # otherwise, see ~/Projects/rice/hypr/kuwai/voxtype.lua.)
    exit 0 ;;
esac
now=$(date +%s%3N)
if [ "${1:-}" != now ]; then
  last=$(cat "$RT/cancel_armed" 2>/dev/null || echo 0)
  if [ $((now - ${last:-0})) -gt 600 ]; then
    echo "$now" > "$RT/cancel_armed"
    exit 0
  fi
fi
rm -f "$RT/cancel_armed" "$RT/handsfree" "$RT/command_selection"
echo "$now" > "$RT/cancelled"
"$SCRIPT_DIR/voxtype-outline.sh" off >/dev/null 2>&1 || true
# Shuush's input method (PP_IME=shuush): the live words leave the field and
# the seat is let go now.
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
pp_ime=${PP_IME:-$(sed -n 's/^PP_IME=\([a-z]*\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)}
if [ "$pp_ime" = shuush ] && ime=$(python3 -I "$SCRIPT_DIR/shuush_caps.py" ime 2>/dev/null); then
  "$ime" release >/dev/null 2>&1 || true
fi
exec voxtype record cancel
