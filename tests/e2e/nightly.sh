#!/usr/bin/env bash
# tests/e2e/nightly.sh — what the timer runs: the whole suite, then a short
# verdict where you will see it (journal, latest.md, one desktop notification
# only when something failed). Never enabled by install.sh; see README.md.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${XDG_STATE_HOME:-$HOME/.local/state}/shuush-e2e/reports"
"$HERE/run.sh" --report "$DIR"
rc=$?
if [ "$rc" -ne 0 ]; then
    summary="$(sed -n '/^\*\*/p' "$DIR/latest.md" | head -1)"
    notify-send -a Shuush -u normal "Dictation e2e failed" "${summary:-see $DIR/latest.md}" >/dev/null 2>&1 || true
fi
exit "$rc"
