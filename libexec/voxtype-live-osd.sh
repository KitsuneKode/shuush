#!/usr/bin/env bash
# voxtype-live-osd — session bookkeeping for the OSD pill and Waybar widget.
#
# Owns four things, driven by the daemon's one-word state file:
#   - prewarm         loads the local cleanup LLM when recording starts
#   - session_start   epoch ms of recording start (OSD clock + Waybar tooltip)
#   - live_text       cleared at session start and at idle; the text itself is
#                     written by voxtype-postprocess.sh (it receives the raw
#                     transcript on stdin) — no journal scraping anymore
#   - idle inhibitor  `systemd-inhibit --what=idle` while not idle; hypridle
#                     honours it, so long dictations don't dim/lock the screen
#
# History: this used to tail `journalctl -f` per session. Killing the tail's
# subshell orphaned the journalctl|while pipeline, and every orphan kept
# writing its stale accumulated text into live_text — the "previous
# transcription shows up in the pill" bug (12 orphans found 2026-10-10).

set -u

RT="${XDG_RUNTIME_DIR:-/run/user/$UID}/voxtype"
STATE="$RT/state"
LIVE="$RT/live_text"
SESSION_START="$RT/session_start"
POLL=0.1

INHIBIT_PID=""
ACTIVE=""

PP_ORDER="local groq"; PP_LOCAL_URL="http://127.0.0.1:11434"
PP_LOCAL_MODEL="qwen3:4b-instruct-2507-q4_K_M"
# shellcheck source=/dev/null
[ -f ~/.config/voxtype/postprocess.env ] && . ~/.config/voxtype/postprocess.env

# Load the local cleanup model while the user is still talking, so a cold
# start (seconds) never lands on the stop→text path. A no-prompt generate
# request just loads the model and refreshes keep_alive. No-op if Ollama is down.
prewarm() {
  # Groq: open/refresh the proxy's keep-alive TLS connection while the user talks.
  case " $PP_ORDER " in *" groq "*)
    curl -fsS --max-time 5 http://127.0.0.1:11435/warm >/dev/null 2>&1 & ;;
  esac
  case " $PP_ORDER " in *" local "*) ;; *) return ;; esac
  curl -fsS --max-time 30 "$PP_LOCAL_URL/api/generate" \
    -d "{\"model\":\"$PP_LOCAL_MODEL\",\"keep_alive\":\"30m\"}" >/dev/null 2>&1 &
}

inhibit_on() {
  [ -n "$INHIBIT_PID" ] && kill -0 "$INHIBIT_PID" 2>/dev/null && return
  systemd-inhibit --what=idle --who=voxtype --why="Dictation in progress" \
    --mode=block sleep infinity >/dev/null 2>&1 &
  INHIBIT_PID=$!
}
inhibit_off() {
  [ -n "$INHIBIT_PID" ] && kill "$INHIBIT_PID" 2>/dev/null
  INHIBIT_PID=""
}

trap 'inhibit_off; : > "$LIVE"; exit 0' TERM INT

mkdir -p "$RT"
: > "$LIVE"

# The 10 Hz poll without a process per tick: `$(cat …)` and `sleep` forked
# twice every 100 ms, all day (~20 processes a second while idle). Bash's own
# read does both: the file directly, and a timed wait on a pipe nobody writes.
exec {TICK}<> <(:)

while true; do
  if [ -r "$STATE" ]; then IFS= read -r state < "$STATE" || true; else state=idle; fi
  case "$state" in
    "")
      ;;   # torn read during the daemon's rewrite — not a state change
    recording|streaming|transcribing)
      if [ -z "$ACTIVE" ]; then
        ACTIVE=1
        : > "$LIVE"                       # never show the last session's text
        date +%s%3N > "$SESSION_START"
        prewarm
      fi
      inhibit_on
      ;;
    *)
      if [ -n "$ACTIVE" ]; then
        ACTIVE=""
        : > "$LIVE"
      fi
      inhibit_off
      ;;
  esac
  read -r -t "$POLL" -u "$TICK" _ || true
done
