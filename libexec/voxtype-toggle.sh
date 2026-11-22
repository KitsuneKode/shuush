#!/usr/bin/env bash
# voxtype-toggle.sh — start and stop dictation from Hyprland binds, with the
# cleanup profile that suits the focused app.
#
#   voxtype-toggle.sh            toggle (Super+Ctrl+D, the Copilot key)
#   voxtype-toggle.sh hold       F9 down: start; while hands-free, stop
#   voxtype-toggle.sh release    F9 up: stop; after a tap (under 300 ms) keep
#                                listening hands-free until F9 is pressed again
#   voxtype-toggle.sh command    edit the selection by voice (again to stop):
#                                say "shorter", "translate to Hindi", …
#
# Every start goes through here, so the cleanup always knows which window the
# take belongs to (target_window: class, title, pid) and which profile fits:
# terminals/editors get literal technical cleanup, chat apps casual, the rest
# the default. Stopping ignores the profile (the daemon keeps the one the
# recording started with).

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
verb="${1:-toggle}"
rt="${XDG_RUNTIME_DIR:-/run/user/$UID}/voxtype"
mkdir -p "$rt"
state=$(cat "$rt/state" 2>/dev/null || echo idle)
now_ms() { date +%s%3N; }

# Pressed while "Polishing": skip the cleanup script and type the raw
# transcript now. (A plain toggle here would queue a NEW recording that
# starts the moment the text lands.)
polishing_now() {
    # This path skips the cleanup script, so the script will not clear the border.
    "$SCRIPT_DIR/voxtype-outline.sh" off >/dev/null 2>&1 || true
    exec voxtype record stop --no-post-process
}

active_window_json() {
    if command -v hyprctl >/dev/null 2>&1; then
        hyprctl activewindow -j 2>/dev/null | jq -c '{address, class, title, pid}' 2>/dev/null && return
    fi
    if command -v niri >/dev/null 2>&1; then
        niri msg -j focused-window 2>/dev/null | jq -c '{address: (.id|tostring), class: (.app_id // ""), title: (.title // ""), pid: (.pid // 0)}' 2>/dev/null && return
    fi
    if command -v swaymsg >/dev/null 2>&1; then
        swaymsg -t get_tree 2>/dev/null | jq -c '.. | objects | select(.focused? == true) | {address: (.id|tostring), class: (.app_id // .window_properties.class // ""), title: (.name // ""), pid: (.pid // 0)}' 2>/dev/null | head -1 && return
    fi
    printf '{}'
}

start() {  # [profile]
    # Gone before the background capture refills it: a take that ends first
    # must not read the previous field's caret, draft or password state.
    rm -f "$rt/handsfree" "$rt/take_screen_ctx.json"
    win=$(active_window_json)
    [ -n "$win" ] || win='{}'
    # Which window this take belongs to. The pill reads the class, and the
    # cleanup puts the text back here if focus moves while it is polishing.
    printf '%s' "$win" > "$rt/target_window.tmp" 2>/dev/null \
        && mv -f "$rt/target_window.tmp" "$rt/target_window"
    # A thicker border on that window until the text lands. Color is left alone.
    "$SCRIPT_DIR/voxtype-outline.sh" on >/dev/null 2>&1 || true
    # Superwhisper-style context capture at record start: highlighted primary
    # selection, clipboard baseline hash, AND AT-SPI/kitty screen+draft context
    # all run in the background while you speak, so stop->type pays 0 ms for them.
    (
        umask 077
        sel=$(timeout 0.15 wl-paste --primary --no-newline 2>/dev/null | head -c 600 || true)
        if [ -n "${sel//[[:space:]]/}" ]; then
            printf '%s' "$sel" > "$rt/take_selection"
        else
            rm -f "$rt/take_selection"
        fi
        CONF="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
        keep_clip=$(sed -n 's/^PP_KEEP_ON_CLIPBOARD=\([01]\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
        if [ "${keep_clip:-1}" = 0 ] && command -v wl-paste >/dev/null 2>&1; then
            # Snapshot the clipboard so emit() can restore it 500ms after typing.
            # Plain text when offered (a text/html-only restore pastes nothing
            # into a terminal), else the first real type (an image, a file list).
            btypes=$(timeout 0.15 wl-paste --list-types 2>/dev/null | grep -Ev '^(TARGETS|TIMESTAMP|SAVE_TARGETS|MULTIPLE)$' || true)
            bmime=$(printf '%s\n' "$btypes" | grep -m1 -xE 'text/plain;charset=utf-8|text/plain|UTF8_STRING' || printf '%s\n' "$btypes" | head -1)
            # A password manager's copy must keep its auto-clear; our own
            # previous dictation is not worth restoring.
            if printf '%s\n' "$btypes" | grep -qx 'x-kde-passwordManagerHint' \
                || [ "$(timeout 0.15 wl-paste --no-newline 2>/dev/null | head -c 800 | sha256sum | awk '{print $1}')" = "$(awk 'NR==1{print $1}' "$rt/clip_self_hash" 2>/dev/null)" ]; then
                bmime=""
            fi
            if [ -n "$bmime" ]; then
                if timeout 0.25 wl-paste --no-newline --type "$bmime" 2>/dev/null | head -c 4194304 > "$rt/clip_backup.bin" && [ -s "$rt/clip_backup.bin" ]; then
                    printf '%s' "$bmime" > "$rt/clip_backup.mime"
                else
                    rm -f "$rt/clip_backup.bin" "$rt/clip_backup.mime"
                fi
            else
                rm -f "$rt/clip_backup.bin" "$rt/clip_backup.mime"
            fi
        fi
        clip=$(timeout 0.15 wl-paste --no-newline 2>/dev/null | head -c 800 || true)
        if [ -n "${clip//[[:space:]]/}" ]; then
            chash=$(printf '%s' "$clip" | sha256sum | awk '{print $1}')
            self_hash=$(awk 'NR==1{print $1}' "$rt/clip_self_hash" 2>/dev/null || true)
            prev_hash=$(awk 'NR==1{print $1}' "$rt/clip_seen" 2>/dev/null || true)
            now_s=$(date +%s)
            if [ "$chash" != "$self_hash" ] && [ "$chash" != "$prev_hash" ]; then
                printf '%s\n%s\n' "$chash" "$now_s" > "$rt/clip_seen"
            fi
            printf '%s\n%s\n' "$chash" "$now_s" > "$rt/take_clip_start"
        else
            rm -f "$rt/take_clip_start"
        fi
        # Pre-capture screen & draft context in the background while talking
        sc_on=$(sed -n 's/^PP_SCREEN_CONTEXT=\([01]\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
        if [ "${sc_on:-1}" = 1 ]; then
            w_cls=$(printf '%s' "$win" | jq -r '.class // empty' 2>/dev/null || true)
            w_title=$(printf '%s' "$win" | jq -r '.title // empty' 2>/dev/null || true)
            w_pid=$(printf '%s' "$win" | jq -r '.pid // 0' 2>/dev/null || true)
            ctx_out=""
            if [ "$w_cls" = kitty ] && [ -n "$w_pid" ] && [ "$w_pid" != "0" ]; then
                sock="${XDG_RUNTIME_DIR:-/run/user/$UID}/mykitty-$w_pid"
                if [ -S "$sock" ]; then
                    ktxt=$(timeout 0.35 kitten @ --to "unix:$sock" get-text --match state:focused --extent screen 2>/dev/null || true)
                    ctx_out=$(jq -nc --arg s "$ktxt" '{password:false, draft:"", screen:$s, source:"kitty"}')
                fi
            fi
            if [ -z "$ctx_out" ]; then
                ctx_out=$(timeout 0.35 python3 -I "$SCRIPT_DIR/voxtype_screen_ctx.py" "${w_pid:-0}" "$w_cls" "$w_title" 2>/dev/null || true)
            fi
            if [ -n "$ctx_out" ]; then
                printf '%s' "$ctx_out" > "$rt/take_screen_ctx.tmp" && mv -f "$rt/take_screen_ctx.tmp" "$rt/take_screen_ctx.json"
            else
                rm -f "$rt/take_screen_ctx.json"
            fi
        else
            rm -f "$rt/take_screen_ctx.json"
        fi
    ) >/dev/null 2>&1 &
    profile="${1:-}"
    if [ -z "$profile" ]; then
        class=$(printf '%s' "$win" | jq -r '.class // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]')
        case "$class" in
            ghostty|kitty|alacritty|foot|wezterm|konsole|xterm|*terminal*|neovide|cursor|code|code-oss|zed|dev.zed*|com.t3tools.t3code|devin-desktop*|jetbrains-*|obsidian)
                profile=terminal ;;
            zapzap|slack|discord|vesktop|webcord|telegram*|whatsapp*|signal*|beeper*|ferdium*|franz*)
                profile=chat ;;
        esac
    fi
    printf '%s' "${profile:-default}" > "$rt/target_profile.tmp" 2>/dev/null \
        && mv -f "$rt/target_profile.tmp" "$rt/target_profile"
    if [ -n "$profile" ]; then
        exec voxtype record start --profile "$profile"
    fi
    exec voxtype record start
}

case "$verb" in
    toggle)
        case "$state" in
            transcribing) polishing_now ;;
            idle|"") start ;;
            *) rm -f "$rt/handsfree"; exec voxtype record toggle ;;
        esac ;;

    hold)
        case "$state" in
            idle|"")
                rm -f "$rt/hold_ignore_release"
                now_ms > "$rt/hold_start"
                start ;;
            recording|streaming)
                # Hands-free (or a toggle take): this press ends it, and
                # its release must not count as a tap.
                : > "$rt/hold_ignore_release"
                rm -f "$rt/handsfree"
                exec voxtype record stop ;;
            *)
                : > "$rt/hold_ignore_release"
                exit 0 ;;
        esac ;;

    release)
        if [ -f "$rt/hold_ignore_release" ]; then
            rm -f "$rt/hold_ignore_release"
            exit 0
        fi
        held=$(( $(now_ms) - $(cat "$rt/hold_start" 2>/dev/null || echo 0) ))
        if [ "$held" -lt "${VOXTYPE_TAP_MS:-300}" ]; then
            # A tap: nothing worth transcribing was said yet, so it means
            # "keep listening". The pill reads handsfree.
            now_ms > "$rt/handsfree"
            exit 0
        fi
        # Short release tail so the last syllable isn't clipped when lifting off F9.
        # Skipped automatically if voxtype-fork's native adaptive release_tail_ms is configured.
        CONF="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
        if ! grep -qE '^\s*release_tail_ms\s*=' "$CONF/config.toml" 2>/dev/null; then
            tail_ms="${VOXTYPE_RELEASE_TAIL_MS:-}"
            [ -z "$tail_ms" ] && tail_ms=$(sed -n 's/^PP_RELEASE_TAIL_MS=\([0-9]*\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
            tail_ms="${tail_ms:-200}"
            if [ "$tail_ms" -gt 0 ] 2>/dev/null; then
                sleep "$(awk -v m="$tail_ms" 'BEGIN{printf "%.3f", m/1000}')"
            fi
        fi
        exec voxtype record stop ;;

    command)
        case "$state" in
            transcribing) polishing_now ;;
            idle|"") ;;
            *) exec voxtype record stop ;;
        esac
        # What is highlighted now (the primary selection). Empty: the model
        # writes what was asked for instead of rewriting.
        sel=$(timeout 0.4 wl-paste --primary --no-newline 2>/dev/null | head -c 12000)
        if [ -n "${sel//[[:space:]]/}" ]; then
            (umask 077; printf '%s' "$sel" > "$rt/command_selection")
        else
            rm -f "$rt/command_selection"
        fi
        start command ;;

    *)
        echo "usage: voxtype-toggle.sh [toggle|hold|release|command]" >&2
        exit 2 ;;
esac
