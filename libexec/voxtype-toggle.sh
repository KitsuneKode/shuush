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
set -o pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
verb="${1:-toggle}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
rt="${XDG_RUNTIME_DIR:-/run/user/$UID}/voxtype"
mkdir -p "$rt"
state=$(cat "$rt/state" 2>/dev/null || echo idle)
now_ms() { date +%s%3N; }

# What the running voxtype can do (shuush-caps: asked of the daemon's own
# binary, cached until it changes). Anything off falls back to what stock
# voxtype does.
cap() { python3 -I "$SCRIPT_DIR/shuush_caps.py" has "$@" 2>/dev/null; }

delivery_mode() {
    local d="${PP_DELIVERY:-}"
    if [ -z "$d" ] && [ -f "$CONF/postprocess.env" ]; then
        d=$(sed -n 's/^PP_DELIVERY="\?\([a-zA-Z0-9_-]*\)"\?.*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
    fi
    printf '%s' "${d:-daemon}"
}

mark_timing() {  # event [event ...]
    local cur dir
    cur=$(cat "$rt/current_capture" 2>/dev/null || true)
    [[ "$cur" =~ ^[0-9]+$ ]] || return 0
    dir="$rt/takes/$cur"
    [ -d "$dir" ] || return 0
    python3 -I "$SCRIPT_DIR/shuush_timing.py" mark "$dir" "$@" --take-id "$cur" >/dev/null 2>&1 || true
}

# Shuush's own input method (PP_IME=shuush in postprocess.env or the
# environment; off by default): shuush-ime binds for the take, shows the live
# words as preedit, takes the final text in one commit and reads the field.
# It needs the daemon's own `[output] preedit` and `read_field` off, which
# `shuush wire` does while PP_IME=shuush. Prints the helper when it is on.
ime_bin() {
    local on="${PP_IME:-}"
    [ -n "$on" ] || on=$(sed -n 's/^PP_IME=\([a-z]*\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
    [ "$on" = shuush ] || return 1
    python3 -I "$SCRIPT_DIR/shuush_caps.py" ime 2>/dev/null
}

# Started from here so that it lives only for the take: its parent is a
# subshell that waits for it (the toggle itself execs voxtype and is gone),
# and --pdeathsig ends it with that parent. It exits by itself once the take
# is delivered or cancelled and the field it went into loses focus.
start_ime() {  # take dir
    local ime live=()
    ime=$(ime_bin) || return 0
    # The fork's preview file has the words as they are heard; a private
    # take shows none.
    if [ ! -e "$2/pp_private" ] && cap streaming_preview; then live=(--live auto); fi
    if command -v setpriv >/dev/null 2>&1; then
        setpriv --pdeathsig TERM -- "$ime" serve --take "$1" "${live[@]}"
    else
        "$ime" serve --take "$1" "${live[@]}"
    fi
}

# Queued dictation (voxtype-fork, [audio] queue_dictations): allow a new hold
# take while the previous one is still transcribing. Only on a daemon that has
# the feature and an active queue_pid. VOXTYPE_QUEUE=0 opts out.
daemon_supports_queue() {
    # Older queue builds publish queue_pid even with queue_dictations off
    # and do not pass an identity to a queued take's postprocessor.
    cap queue_dictations take_identity || return 1
    local flag="${VOXTYPE_QUEUE:-}"
    if [ -z "$flag" ] && [ -f "$CONF/postprocess.env" ]; then
        flag=$(sed -n 's/^VOXTYPE_QUEUE=\([01]\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
    fi
    case "$flag" in
        0) return 1 ;;
    esac
    local qpid dpid
    [ -r "$rt/queue_pid" ] && [ -r "$rt/pid" ] || return 1
    IFS= read -r qpid < "$rt/queue_pid" || true
    [[ "$qpid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$qpid" 2>/dev/null || return 1
    IFS= read -r dpid < "$rt/pid" || true
    [ "$qpid" = "$dpid" ] || return 1
    return 0
}

# Pressed while "Polishing": skip the cleanup script and type the raw
# transcript now. (A plain toggle here would queue a NEW recording that
# starts the moment the text lands.) Stock voxtype has no way to skip a cleanup
# that is running (`record stop --no-post-process` is the fork's): the press is
# ignored and the text lands when the cleanup finishes.
polishing_now() {
    if [ "$(delivery_mode)" = shuush ]; then
        python3 -I "$SCRIPT_DIR/shuush_deliver.py" skip >/dev/null 2>&1 && exit 0
    fi
    cap skip_post_process || exit 0
    # Only the identified daemon can check a captured target on the raw bypass.
    [ ! -e "$rt/current_capture" ] || cap take_identity || exit 0
    # This path skips the cleanup script, so the script will not clear the border.
    "$SCRIPT_DIR/voxtype-outline.sh" off >/dev/null 2>&1 || true
    # voxtype types the raw text now: the live words leave the field first.
    local ime
    if ime=$(ime_bin); then "$ime" end >/dev/null 2>&1 || true; fi
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
    # Concurrent hotkey processes must publish and submit one start at a time.
    exec 9>"$rt/shuush-start.lock"
    flock -n 9 || return 0
    [ ! -e "$rt/start_requested" ] || return 0
    [ "$(cat "$rt/state" 2>/dev/null || echo idle)" = "$state" ] || return 0
    # A muted microphone records silence: say so instead of starting a take.
    if wpctl get-volume @DEFAULT_AUDIO_SOURCE@ 2>/dev/null | grep -q MUTED; then
        msg="Microphone is muted · Ctrl+Alt+M turns it on"
        qs ipc -p "${KUWAI_SHELL:-$HOME/.local/share/kuwai/shell}" call moment say "$msg" >/dev/null 2>&1 \
            || notify-send -a Voxtype -t 4000 "$msg" 2>/dev/null
        exit 0
    fi
    rm -f "$rt/handsfree"
    take=$(date +%s%N)
    dir="$rt/takes/$take"
    mkdir -p "$rt/takes" && mkdir "$dir" || exit 1
    win=$(active_window_json)
    [ -n "$win" ] || win='{}'
    printf '%s' "$win" > "$dir/target_window" || exit 1
    if [ -e "$rt/sensitive_field" ] && python3 -I "$SCRIPT_DIR/shuush_caps.py" sensitive; then : > "$dir/pp_private"; fi
    if [ "${1:-}" = command ] && [ ! -e "$dir/pp_private" ]; then
        timeout 0.4 wl-paste --primary --no-newline 2>/dev/null | head -c 12000 > "$dir/command_selection"
    fi
    # Readers of the original flat files include the outline and older HUDs.
    ln -s "$dir/target_window" "$dir/window.link" && mv -Tf "$dir/window.link" "$rt/target_window" || exit 1
    # A thicker border on that window until the text lands. Color is left alone.
    "$SCRIPT_DIR/voxtype-outline.sh" on >/dev/null 2>&1 || true
    # Superwhisper-style context capture at record start: highlighted primary
    # selection, clipboard baseline hash, AND AT-SPI/kitty screen+draft context
    # all run in the background while you speak, so stop->type pays 0 ms for them.
    (
        [ ! -e "$dir/pp_private" ] || exit 0
        trap 'if [ -e "$dir/pp_private" ]; then rm -f "$dir"/take_{selection,clip,clip_start,screen_ctx.json} "$dir/command_selection"; fi' EXIT
        sel=$(timeout 0.15 wl-paste --primary --no-newline 2>/dev/null | head -c 600 || true)
        if [ -n "${sel//[[:space:]]/}" ]; then
            printf '%s' "$sel" > "$dir/take_selection"
        else
            rm -f "$dir/take_selection"
        fi
        now_s=$(date +%s)
        clip_types=""
        clip=""
        if clip_types=$(timeout 0.15 wl-paste --list-types 2>/dev/null); then
            if grep -qx 'x-kde-passwordManagerHint' <<<"$clip_types"; then
                rm -f "$dir/take_clip"
                printf 'secret\n%s\n' "$now_s" > "$dir/take_clip_start"
            else
                clip=$(timeout 0.15 wl-paste --no-newline 2>/dev/null | head -c 800 || true)
                if [ -n "${clip//[[:space:]]/}" ]; then
                    chash=$(printf '%s' "$clip" | sha256sum | awk '{print $1}')
                    self_hash=$(awk 'NR==1{print $1}' "$rt/clip_self_hash" 2>/dev/null || true)
                    prev_hash=$(awk 'NR==1{print $1}' "$rt/clip_seen" 2>/dev/null || true)
                    prev_ts=$(awk 'NR==2{print $1}' "$rt/clip_seen" 2>/dev/null || echo 0)
                    if [ "$chash" != "$self_hash" ] && [ "$chash" != "$prev_hash" ]; then
                        printf '%s\n%s\n' "$chash" "$now_s" > "$rt/clip_seen"
                        prev_ts=$now_s
                    fi
                    if [ "$chash" != "$self_hash" ] && [ $(( now_s - prev_ts )) -le 15 ]; then
                        printf '%s' "$clip" > "$dir/take_clip"
                    else
                        rm -f "$dir/take_clip"
                    fi
                    printf '%s\n%s\n' "$chash" "$now_s" > "$dir/take_clip_start"
                else
                    rm -f "$dir/take_clip"
                    printf 'empty\n%s\n' "$now_s" > "$dir/take_clip_start"
                fi
            fi
        else
            rm -f "$dir/take_clip" "$dir/take_clip_start"
        fi
        sc_on=$(sed -n 's/^PP_SCREEN_CONTEXT=\([01]\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
        w_cls=$(jq -r '.class // empty' <<< "$win")
        w_title=$(jq -r '.title // empty' <<< "$win")
        w_pid=$(jq -r '.pid // 0' <<< "$win")
        # Probe the focused role even when collecting text is disabled.
        args=()
        [ "${sc_on:-1}" = 1 ] || args+=(--sensitivity-only)
        ctx=$(timeout 0.45 python3 -I "$SCRIPT_DIR/voxtype_screen_ctx.py" "$w_pid" "$w_cls" "$w_title" "${args[@]}") || exit 0
        if jq -e '.password == true' <<< "$ctx" >/dev/null; then
            : > "$dir/pp_private"
        elif [ "${sc_on:-1}" = 1 ] && [ "$w_cls" = kitty ] && [ -S "${XDG_RUNTIME_DIR:-/run/user/$UID}/mykitty-$w_pid" ]; then
            ktxt=$(timeout 0.35 kitten @ --to "unix:${XDG_RUNTIME_DIR:-/run/user/$UID}/mykitty-$w_pid" get-text --match state:focused --extent screen)
            ctx=$(jq -c --arg s "$ktxt" '.screen=$s' <<< "$ctx")
        fi
        printf '%s' "$ctx" > "$dir/context.tmp" && mv "$dir/context.tmp" "$dir/take_screen_ctx.json"
    ) 9>&- >/dev/null 2>&1 &
    profile="${1:-}"
    if [ -z "$profile" ]; then
        class=$(printf '%s' "$win" | jq -r '.class // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]')
        case "$class" in
            ghostty|kitty|alacritty|foot|wezterm|konsole|xterm|*terminal*|neovide)
                profile=terminal ;;
            # Editors and agent apps: a composer that re-renders on every key, so one paste.
            cursor|code|code-oss|zed|dev.zed*|com.t3tools.t3code|devin-desktop*|jetbrains-*|obsidian)
                profile=agent ;;
            zapzap|slack|discord|vesktop|webcord|telegram*|whatsapp*|signal*|beeper*|ferdium*|franz*)
                profile=chat ;;
            zen*|firefox*|librewolf*|chromium*|google-chrome*|brave*|vivaldi*|microsoft-edge*|thorium*)
                # A browser: the site, from its window title.
                title=$(printf '%s' "$win" | jq -r '.title // empty' 2>/dev/null | tr '[:upper:]' '[:lower:]')
                case "$title" in
                    *chatgpt*|*claude*|*gemini*|*perplexity*|*t3.chat*|*"v0 by vercel"*|*bolt.new*|*lovable*) profile=agent ;;
                    *github*|*gitlab*|*linear*|*jira*|*"stack overflow"*|*localhost*|*vercel*|*supabase*|*codepen*) profile=terminal ;;
                    *whatsapp*|*slack*|*discord*|*telegram*|*messenger*|*" / x"*|*instagram*|*reddit*) profile=chat ;;
                esac ;;
        esac
    fi
    printf '%s' "${profile:-default}" > "$dir/target_profile" || exit 1
    ln -s "$dir/target_profile" "$dir/profile.link" && mv -Tf "$dir/profile.link" "$rt/target_profile" || exit 1
    printf '%s' "$take" > "$dir/id.tmp" && mv "$dir/id.tmp" "$rt/current_capture" || exit 1
    if cap take_identity; then
        printf '%s' "$take" > "$dir/id.tmp" && mv "$dir/id.tmp" "$rt/take_id" || exit 1
    else
        rm -f "$rt/take_id"
    fi
    ( start_ime "$take" "$dir"; : ) 9>&- </dev/null >/dev/null 2>&1 &
    # Keep stopped/cancelled captures out of the runtime directory long term.
    find "$rt/takes" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' -mmin +1440 -exec rm -rf -- {} + 9>&- </dev/null >/dev/null 2>&1 &
    # In PP_DELIVERY=shuush mode, avoid --profile so stock voxtype uses
    # [output.post_process] (where fallback_on_empty = false and trim = false
    # apply; stock profiles hardcode fallback_on_empty = true). The profile is
    # already recorded in target_profile for voxtype-postprocess.sh.
    if [ "$(delivery_mode)" = shuush ] && cap handoff; then
        exec voxtype record start
    fi
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
            *)
                rm -f "$rt/handsfree"
                mark_timing key_release daemon_stop
                exec voxtype record toggle ;;
        esac ;;

    hold)
        case "$state" in
            idle|"")
                rm -f "$rt/hold_ignore_release"
                now_ms > "$rt/hold_start"
                start ;;
            transcribing)
                if daemon_supports_queue; then
                    rm -f "$rt/hold_ignore_release"
                    now_ms > "$rt/hold_start"
                    start
                else
                    : > "$rt/hold_ignore_release"
                    exit 0
                fi ;;
            recording|streaming)
                # Hands-free (or a toggle take): this press ends it, and
                # its release must not count as a tap.
                : > "$rt/hold_ignore_release"
                rm -f "$rt/handsfree"
                mark_timing key_release daemon_stop
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
        # Skipped when the daemon has its own adaptive tail (voxtype-fork's
        # release_tail_ms) and it is configured, in its config file or through
        # its environment variable (VOXTYPE_RELEASE_TAIL_MS is the daemon's; this
        # script's own length is PP_RELEASE_TAIL_MS). A daemon without the
        # feature ignores both, so this script's tail applies.
        CONF="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
        native_tail=0
        if [ -n "${VOXTYPE_RELEASE_TAIL_MS:-}" ] || grep -qE '^\s*release_tail_ms\s*=' "$CONF/config.toml" 2>/dev/null; then
            cap release_tail_ms && native_tail=1
        fi
        if [ "$native_tail" = 0 ]; then
            tail_ms=$(sed -n 's/^PP_RELEASE_TAIL_MS=\([0-9]*\).*/\1/p' "$CONF/postprocess.env" 2>/dev/null | tail -1)
            tail_ms="${tail_ms:-200}"
            if [ "$tail_ms" -gt 0 ] 2>/dev/null; then
                mark_timing key_release
                let_go=$(now_ms)
                sleep "$(awk -v m="$tail_ms" 'BEGIN{printf "%.3f", m/1000}')"
                # Cancelled while the tail ran: that take is gone, and a new one isn't ours to stop.
                [ "$(cat "$rt/cancelled" 2>/dev/null || echo 0)" -ge "$let_go" ] 2>/dev/null && exit 0
                mark_timing daemon_stop
            else
                mark_timing key_release daemon_stop
            fi
        else
            mark_timing key_release daemon_stop
        fi
        exec voxtype record stop ;;

    command)
        case "$state" in
            transcribing) polishing_now ;;
            idle|"") ;;
            *)
                mark_timing key_release daemon_stop
                exec voxtype record stop ;;
        esac
        start command ;;

    *)
        echo "usage: voxtype-toggle.sh [toggle|hold|release|command]" >&2
        exit 2 ;;
esac
