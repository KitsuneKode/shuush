#!/usr/bin/env bash
# voxtype-waybar.sh — Waybar exec for custom/voxtype.
#
# Wraps `voxtype status --follow` (emits only on state change) and adds:
#   - elapsed time in the hover tooltip while recording (the OSD pill owns
#     the visible clock)
#   - a tooltip with the last dictation and the click controls
# Elapsed time comes from $XDG_RUNTIME_DIR/voxtype/session_start, written by
# voxtype-live-osd.sh, so the bar and the OSD pill always agree.

set -u

RT="${XDG_RUNTIME_DIR:-/run/user/$UID}/voxtype"
HIST="${XDG_STATE_HOME:-$HOME/.local/state}/voxtype/history.jsonl"

ICON_IDLE="󰍬"
ICON_REC="󰍬"
ICON_BUSY="󰔟"

last_dictation() {
  [ -s "$HIST" ] || { printf 'nothing yet'; return; }
  tail -n 1 "$HIST" | jq -r '.clean' 2>/dev/null | cut -c1-90
}

emit() {
  local state="$1" text tip
  case "$state" in
    recording|streaming)
      local start now secs file
      now=$(date +%s%3N)
      # The watcher writes session_start up to ~150ms after the state flips,
      # so a stale file from the last session can be read first. Trust it only
      # when it lands within 3s of when we saw recording begin.
      file=$(cat "$RT/session_start" 2>/dev/null || echo 0)
      start=$LOCAL_START
      (( file > 0 && file - LOCAL_START < 3000 && LOCAL_START - file < 3000 )) && start=$file
      secs=$(( (now - start) / 1000 ))
      # The OSD pill shows the clock; the bar stays an icon and keeps the
      # time on hover so the two never duplicate each other.
      text="$ICON_REC"
      tip=$(printf 'Listening for %d:%02d\nClick: stop\nRight-click: cancel' $((secs / 60)) $((secs % 60))) ;;
    transcribing)
      text="$ICON_BUSY"
      tip="Polishing your dictation…" ;;
    *)
      state="idle"
      text="$ICON_IDLE"
      tip="Dictation ready\nLast: $(last_dictation)\n\nClick: start / stop\nRight-click: cancel\nMiddle-click: copy last dictation"
      # voxtype-review.py flags a dictation worth a second look (raw text
      # inserted, or many words repaired from context). Amber until the next one.
      local review swaps
      review=$(tail -n 1 "$HIST" 2>/dev/null | jq -r '.review // empty' 2>/dev/null)
      if [ -n "$review" ]; then
        state="review"
        swaps=$(tail -n 1 "$HIST" | jq -r '(.swaps // [])[:8] | map("  " + .) | join("\n")')
        tip="Check the last dictation: $review\n${swaps:+$swaps\n}\nLast: $(last_dictation)\n\nMiddle-click: copy it    (voxtype-last.sh raw: as heard)\nClick: start / stop"
      fi ;;
  esac
  jq -nc --arg t "$text" --arg c "$state" --arg a "$state" --arg tip "$tip" \
    '{text:$t, class:$c, alt:$a, tooltip:($tip | gsub("\\\\n"; "\n"))}'
}

state="idle"
LOCAL_START=0
emit "$state"
# Re-read on every daemon state change; tick every 0.5s while recording so
# the displayed second never skips.
voxtype status --follow --format json 2>/dev/null | while :; do
  if IFS= read -r -t 0.5 line; then
    new=$(jq -r '.class // "idle"' <<<"$line" 2>/dev/null)
    case "$new" in recording|streaming)
      case "$state" in recording|streaming) ;; *) LOCAL_START=$(date +%s%3N) ;; esac ;;
    esac
    state="$new"
    emit "$state"
  else
    [ $? -gt 128 ] || exit 0          # EOF: daemon gone; Waybar restarts us
    case "$state" in recording|streaming) emit "$state" ;; esac
  fi
done
