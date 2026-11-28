#!/usr/bin/env bash
# lib/scenario.sh — how a scenario file is run.
#
# A scenario file (scenarios/NN_name.sh) defines:
#   # desc: one line for --list
#   scn_config() { ... }   optional: set DAEMON_* knobs before the daemon starts
#   scn_run() { ... }      the scenario itself (targets, takes, checks)
# The runner gives every scenario a fresh daemon, no windows, empty fake logs and a
# clipboard holding a known sentinel (so "the clipboard was not touched" is checkable).

CLIP_SENTINEL="e2e-clipboard-sentinel"

clip_set() { printf '%s' "$1" | e2e_env wl-copy; }
clip_get() { e2e_env wl-paste --no-newline 2>/dev/null; }
history_file() { echo "$E2E_HOME/.local/state/voxtype/history.jsonl"; }
history_rows() { [ -f "$(history_file)" ] && wc -l < "$(history_file)" || echo 0; }

scenario_reset() {
    target_close_all
    ctl_reset
    llm_mode echo
    rm -rf "$E2E_RT_VOX/takes" "$E2E_RT_VOX/outcome.json" "$E2E_RT_VOX/sensitive_field" "$E2E_RT_VOX/current_capture" \
        "$E2E_RT_VOX/take_id" "$E2E_RT_VOX/target_window" "$E2E_RT_VOX/target_profile" "$E2E_RT_VOX/handsfree" \
        "$E2E_RT_VOX/cancelled" "$E2E_RT_VOX/hold_ignore_release" "$E2E_RT_VOX/start_requested" 2>/dev/null
    e2e_sh 'true' >/dev/null
    # a fresh mic: the null sink exists and is the default source
    e2e_pactl list short sinks | grep -q e2e_mic || e2e_pactl load-module module-null-sink sink_name=e2e_mic >/dev/null
    e2e_pactl set-default-source e2e_mic.monitor >/dev/null 2>&1 || true
    daemon_defaults
    unset TAKE_SPEAK TAKE_NOWAIT TAKE_SPEED TAKE_TIMEOUT
}

run_scenario() {  # name file delivery  (runs in a subshell)
    local name="$1" file="$2" delivery="${3:-daemon}"
    scenario_reset
    DAEMON_DELIVERY="$delivery"
    # shellcheck source=/dev/null
    . "$file"
    declare -F scn_config >/dev/null && scn_config
    daemon_restart || { sc_begin "$name"; _rec false "daemon starts" up down; sc_end; return 1; }
    clip_set "$CLIP_SENTINEL"
    sc_begin "$name"
    # A scenario that hangs must not hang the suite: a watchdog ends this subshell.
    local me=$BASHPID
    ( sleep "${SCN_TIMEOUT:-150}"; kill -KILL "$me" 2>/dev/null ) &
    local wd=$!
    scn_run
    kill "$wd" 2>/dev/null
    sc_end
}

# The reliability numbers from this run's own history and outcome ledger.
collect_stats() {
    e2e_env shuush stats reliability --json --days 7 2>/dev/null
}
