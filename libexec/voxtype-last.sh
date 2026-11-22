#!/usr/bin/env bash
# voxtype-last.sh — recover the last dictation from history.
#   voxtype-last.sh          copy the cleaned text to the clipboard
#   voxtype-last.sh raw      copy the raw (pre-cleanup) transcript instead
set -u
HIST="${XDG_STATE_HOME:-$HOME/.local/state}/voxtype/history.jsonl"
field=clean; [ "${1:-}" = raw ] && field=raw
[ -s "$HIST" ] || exit 0
text=$(tail -n 1 "$HIST" | jq -r ".$field")
[ -n "$text" ] && wl-copy -- "$text" </dev/null >/dev/null 2>&1
