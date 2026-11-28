#!/usr/bin/env bash
# Play the dictation that just stopped (last_take.wav in runtime dir).
set -u
f="${XDG_RUNTIME_DIR:-/run/user/$UID}/voxtype/last_take.wav"
if [ ! -f "$f" ]; then
  # Only a daemon that keeps the last recording (voxtype-fork's last_take.wav) has one to play.
  if ! python3 -I "$(dirname "$(readlink -f "$0")")/shuush_caps.py" has last_take 2>/dev/null; then
    echo "replay needs a voxtype that keeps the last recording (the companion fork does; see docs/voxtype-fork.md)" >&2
  else
    echo "no last take at $f" >&2
  fi
  exit 1
fi
if command -v pw-play >/dev/null 2>&1; then
  exec pw-play "$f"
elif command -v paplay >/dev/null 2>&1; then
  exec paplay "$f"
elif command -v mpv >/dev/null 2>&1; then
  exec mpv --no-video --really-quiet "$f"
elif command -v ffplay >/dev/null 2>&1; then
  exec ffplay -nodisp -autoexit -loglevel quiet "$f"
else
  echo "no audio player found (install pipewire/pw-play, paplay, or mpv)" >&2
  exit 1
fi
