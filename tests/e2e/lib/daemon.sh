#!/usr/bin/env bash
# lib/daemon.sh — voxtype's config and process inside the world.
#
# The daemon is the owner's fork (a copy of the binary), pointed at the fake
# speech-to-text server (`[whisper] mode = "remote"`), so a take is the real
# record -> transcribe -> post-process -> deliver chain with a known transcript.
# Shuush's own keys go in through `shuush wire`, as they do on the owner's machine.

# Knobs a scenario sets before daemon_restart (reset by scenario_begin):
#   DAEMON_QUEUE      queue_dictations true|false (default true, as live)
#   DAEMON_WARM_MIC   warm_mic_secs (3)   DAEMON_RELEASE_TAIL_MS (400)
#   DAEMON_DELIVERY   daemon|shuush       (PP_DELIVERY)
#   DAEMON_STREAM     true|false          ([output.post_process] streaming)
#   DAEMON_PREEDIT    true|false          (the daemon's input method: live words, field privacy marker)
#   DAEMON_PASTE_OVER characters above which a take is pasted instead of typed
#   DAEMON_AUDIO_DEVICE
#   DAEMON_ENV_EXTRA  extra lines for postprocess.env

daemon_defaults() {
    # the owner's live settings: queue_dictations on, a 3 s warm microphone, a 400 ms release tail
    DAEMON_QUEUE=true
    DAEMON_WARM_MIC=3
    DAEMON_RELEASE_TAIL_MS=400
    DAEMON_DELIVERY=daemon
    DAEMON_STREAM=true
    DAEMON_PREEDIT=false
    DAEMON_PASTE_OVER=300
    DAEMON_AUDIO_DEVICE=default
    DAEMON_ENV_EXTRA=""
    DAEMON_TIMEOUT_MS=15000
    # Headless sway drops the first keys of a fresh virtual keyboard (wtype makes one per
    # delivery) unless it has ~50 ms to settle; Hyprland, the owner's compositor, needs none.
    DAEMON_PRE_TYPE_MS=80
}

daemon_conf_dir() { echo "$E2E_HOME/.config/voxtype"; }

daemon_write_config() {
    local c; c="$(daemon_conf_dir)"
    mkdir -p "$c"
    cat > "$c/config.toml" <<TOML
engine = "whisper"
state_file = "auto"

[hotkey]
enabled = false

[audio]
device = "$DAEMON_AUDIO_DEVICE"
sample_rate = 16000
max_duration_secs = 120
duck_media = false
pause_media = false
warm_mic_secs = $DAEMON_WARM_MIC
release_tail_ms = $DAEMON_RELEASE_TAIL_MS
queue_dictations = $DAEMON_QUEUE

[audio.feedback]
enabled = false

[whisper]
mode = "remote"
remote_endpoint = "http://127.0.0.1:$E2E_PORT"
remote_model = "whisper-1"
remote_timeout_secs = 20
language = "en"

[vad]
enabled = false

[output]
mode = "type"
fallback_to_clipboard = true
type_delay_ms = 0
pre_type_delay_ms = $DAEMON_PRE_TYPE_MS
paste_over_chars = $DAEMON_PASTE_OVER
wait_for_modifier_release = false
streaming_commit = "on_stop"
preedit = $DAEMON_PREEDIT
read_field = false

[output.notification]
on_recording_start = false
on_recording_stop = false
on_transcription = false

[text]
spoken_punctuation = false
filter_filler_words = false

[osd]
enabled = false
TOML
    cat > "$c/postprocess.env" <<ENV
PP_ORDER="local"
PP_LOCAL_URL="$FAKE"
PP_LOCAL_MODEL="fake-cleanup"
PP_LOCAL_TIMEOUT=3
PP_BUDGET_MS=5000
PP_STREAM_MIN_WORDS=60
PP_APP_CONTEXT=0
PP_SCREEN_CONTEXT=0
PP_CLIPBOARD_CONTEXT=0
PP_PRIVATE_GUARD=1
PP_AUTO_LEARN=0
PP_WATCH_EDITS=0
PP_SMART_JOIN=0
PP_KEEP_ON_CLIPBOARD=1
PP_PASTE_OVER_CHARS=$DAEMON_PASTE_OVER
PP_DELIVERY=$DAEMON_DELIVERY
VOXTYPE_QUEUE=1
$DAEMON_ENV_EXTRA
ENV
    # Shuush's own keys, written the way the owner's machine gets them.
    # `shuush wire` edits config.toml with `voxtype config set`; the post-process
    # command, timeout and (for the shuush delivery mode) the hand-off keys.
    rm -f "$E2E_HOME/.config/shuush/wired.json"    # a rewritten config.toml is not wired any more
    local wargs=()
    [ "$DAEMON_DELIVERY" = shuush ] && wargs+=(--delivery shuush)
    e2e_env shuush wire "${wargs[@]}" > "$E2E_LOGS/wire.log" 2>&1 || { cat "$E2E_LOGS/wire.log" >&2; log "shuush wire failed"; return 1; }
    # Keep what the scenario asked for if wire changed it.
    e2e_env voxtype config set output.post_process.streaming "$DAEMON_STREAM" >/dev/null 2>&1 || true
    e2e_env voxtype config set output.post_process.timeout_ms "$DAEMON_TIMEOUT_MS" >/dev/null 2>&1 || true
}

daemon_pid() { cat "$E2E_RT_VOX/pid" 2>/dev/null; }

daemon_running() {
    local p; p="$(daemon_pid)"
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

daemon_state() { cat "$E2E_RT_VOX/state" 2>/dev/null || echo "none"; }

# daemon_start [--keep-state]: --keep-state leaves the runtime files a crashed daemon left behind
# (state, pid, outcome.json), the way a real restart finds them.
daemon_start() {
    daemon_write_config || return 1
    [ "${1:-}" = --keep-state ] || rm -f "$E2E_RT_VOX/state" "$E2E_RT_VOX/pid" "$E2E_RT_VOX/outcome.json"
    e2e_spawn "voxtype-$(date +%s%N)" voxtype -v daemon
    echo "$!" > "$E2E_ROOT/daemon.shpid"
    if ! e2e_wait "voxtype daemon" 15 bash -c "[ \"\$(cat '$E2E_RT_VOX/state' 2>/dev/null)\" = idle ]"; then
        log "daemon did not reach idle"; tail -20 "$E2E_LOGS"/voxtype-*.log >&2
        return 1
    fi
}

daemon_kill() {  # [signal]  — a crash: no cleanup runs
    local p; p="$(daemon_pid)"
    [ -n "$p" ] && kill -"${1:-KILL}" "$p" 2>/dev/null
    e2e_wait "daemon exit" 6 bash -c "! kill -0 $p 2>/dev/null"
}

daemon_stop() {
    local p; p="$(daemon_pid)"
    [ -n "$p" ] && kill -TERM "$p" 2>/dev/null
    e2e_wait "daemon exit" 6 bash -c "! kill -0 $p 2>/dev/null" || kill -KILL "$p" 2>/dev/null
    return 0
}

# Shuush's own delivery queue (PP_DELIVERY=shuush): the worker the unit file runs.
deliver_pid() { cat "$E2E_ROOT/deliver.pid" 2>/dev/null; }
deliver_start() {
    deliver_stop
    e2e_spawn "deliver-$(date +%s%N)" python3 -I "$E2E_PREFIX/libexec/shuush_deliver.py" serve
    echo "$!" > "$E2E_ROOT/deliver.pid"
    e2e_wait "the delivery worker" 10 e2e_env python3 -I "$E2E_PREFIX/libexec/shuush_deliver.py" ping
}
deliver_stop() {
    local p; p="$(deliver_pid)"
    [ -n "$p" ] && { kill -TERM "$p" 2>/dev/null; sleep 0.2; kill -KILL "$p" 2>/dev/null; }
    rm -f "$E2E_ROOT/deliver.pid" "$E2E_RUN/shuush/deliver.sock"
    return 0
}

daemon_restart() {
    daemon_stop; deliver_stop
    daemon_start || return 1
    if [ "$DAEMON_DELIVERY" = shuush ]; then deliver_start || { log "the shuush delivery worker did not start"; return 1; }; fi
}
