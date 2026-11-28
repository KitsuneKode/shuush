#!/usr/bin/env bash
# Copies the nightly service and timer into ~/.config/systemd/user with this checkout's
# path filled in. It does NOT enable or start anything:
#   systemctl --user daemon-reload && systemctl --user enable --now shuush-e2e.timer
set -eu
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
for u in shuush-e2e.service shuush-e2e.timer; do
    sed "s|@REPO@|$REPO|g" "$HERE/systemd/$u" > "$UNIT_DIR/$u"
    echo "wrote $UNIT_DIR/$u"
done
echo "not enabled. To turn it on: systemctl --user daemon-reload && systemctl --user enable --now shuush-e2e.timer"
