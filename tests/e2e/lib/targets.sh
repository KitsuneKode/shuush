#!/usr/bin/env bash
# lib/targets.sh — the apps a take is delivered into: a kitty terminal that logs
# every byte it is sent, and a Chromium page with a textarea, a contenteditable
# and a password input that reports its input events to the fake server.

TARGET_KITTY_LOG=""
TARGET_LAST_ID=""
TARGET_LAST_PID=""

sway_tree() { sway -t get_tree 2>/dev/null; }

# con id of a window by app_id (newest first), empty when there is none.
sway_find() {  # app_id [title-regex]
    sway_tree | jq -r --arg a "$1" --arg t "${2:-}" '[.. | objects | select(.type? == "con" and (.app_id // "") == $a and ((.name // "") | test($t)))] | last | .id // empty'
}
sway_pid() { sway_tree | jq -r --argjson i "$1" '[.. | objects | select(.id? == $i)] | first | .pid // empty'; }
sway_focused() { sway_tree | jq -r '[.. | objects | select(.focused? == true and .type? == "con")] | first | .id // empty'; }
sway_focus() { sway "[con_id=$1] focus" >/dev/null; }
focus_is() { [ "$(sway_focused)" = "$1" ]; }
fake_state() { curl -fsS "$FAKE/_state"; }
fake_count() {  # jq filter on .target
    fake_state | jq "[.target[] | select($1)] | length"
}
fake_has() { [ "$(fake_count "$1")" -gt 0 ]; }

# kitty NAME [TITLE]: a terminal running the logging program; sets TARGET_LAST_ID/PID and the log path.
target_kitty() {
    local name="${1:-main}" title tlog
    title="${2:-e2e-$name}"
    tlog="$E2E_ROOT/targets/kitty-$name.log"
    : > "$tlog"
    e2e_spawn "kitty-$name" kitty --config NONE --class kitty --title "$title" \
        -o confirm_os_window_close=0 -o shell_integration=disabled -o allow_remote_control=no \
        -o update_check_interval=0 -o remember_window_size=no -o initial_window_width=900 -o initial_window_height=400 \
        python3 -I "$E2E_DIR/apps/term_target.py" "$tlog"
    e2e_wait "kitty $name to show" 15 bash -c "grep -q ready '$tlog'" || { log "kitty did not come up"; tail -5 "$E2E_LOGS/kitty-$name.log" >&2; return 1; }
    TARGET_LAST_ID="$(sway_find kitty "$title")"
    [ -n "$TARGET_LAST_ID" ] || { log "kitty window not in the tree"; return 1; }
    TARGET_LAST_PID="$(sway_pid "$TARGET_LAST_ID")"
    sway_focus "$TARGET_LAST_ID"
    e2e_wait "kitty $name focus" 5 focus_is "$TARGET_LAST_ID" || true
    TARGET_KITTY_LOG="$tlog"
}

# the page: sets TARGET_LAST_ID/PID; fields are ta | ce | pw
target_chrome() {
    local prof="$E2E_ROOT/chrome-profile"
    local bin; bin="$(command -v google-chrome-stable || command -v chromium || true)"
    [ -n "$bin" ] || { log "no chromium or chrome on PATH"; return 1; }
    curl -fsS -X POST "$FAKE/_reset" >/dev/null
    e2e_spawn chrome "$bin" --user-data-dir="$prof" --ozone-platform=wayland --enable-wayland-ime \
        --no-sandbox --disable-gpu --disable-dev-shm-usage --no-first-run --no-default-browser-check \
        --disable-background-networking --disable-sync --disable-component-update --disable-features=Translate \
        --window-size=900,500 --app="$FAKE/_page/page.html"
    e2e_wait "chrome page to load" 40 fake_has '.type=="ready"' || { log "chrome page did not report ready"; tail -20 "$E2E_LOGS/chrome.log" >&2; return 1; }
    TARGET_LAST_ID="$(sway_tree | jq -r '[.. | objects | select(.type? == "con" and ((.app_id // "") | test("chrome|chromium")))] | last | .id // empty')"
    [ -n "$TARGET_LAST_ID" ] || { log "chrome window not in the tree"; return 1; }
    TARGET_LAST_PID="$(sway_pid "$TARGET_LAST_ID")"
    sway_focus "$TARGET_LAST_ID"
}

page_cmd() { curl -fsS -X POST "$FAKE/_ctl" -d "$(jq -nc --arg c "$1" '{page_cmd:$c}')" >/dev/null; }

# The text a field holds right now: ask the page to dump, wait for the answer.
page_value() {  # ta|ce|pw
    local n; n=$(fake_count '.type=="dump"')
    page_cmd dump
    local i
    for i in $(seq 1 100); do
        [ "$(fake_count '.type=="dump"')" -gt "$n" ] && break
        sleep 0.05
    done
    fake_state | jq -r --arg f "$1" '[.target[] | select(.type=="dump")] | last | .[$f]'
}

# What reached the kitty target: typed and pasted text joined in arrival order.
kitty_text() { jq -rj 'select(.kind=="type" or .kind=="paste") | .text' "${1:-$TARGET_KITTY_LOG}"; }
kitty_pieces() { jq -c 'select(.kind=="type" or .kind=="paste") | {kind, n: (.text|length)}' "${1:-$TARGET_KITTY_LOG}"; }
kitty_kinds() { jq -r 'select(.kind=="type" or .kind=="paste") | .kind' "${1:-$TARGET_KITTY_LOG}" | sort | uniq -c | tr '\n' ' '; }

target_close_all() {
    local id
    for id in $(sway_tree | jq -r '[.. | objects | select(.type? == "con")] | .[].id'); do
        sway "[con_id=$id] kill" >/dev/null 2>&1 || true
    done
    sleep 0.3
}
