#!/usr/bin/env bash
# lib/take.sh — drive a take through the same scripts the owner's key binds run
# (`shuush toggle` -> voxtype-toggle.sh -> `voxtype record start|stop`), with the
# audio coming from the private null microphone.

ctl() { curl -fsS -X POST "$FAKE/_ctl" -d "$1" >/dev/null; }
ctl_reset() { curl -fsS -X POST "$FAKE/_reset" >/dev/null; }

stt_queue() {  # transcript...   (answers, in order, for the next takes)
    ctl "$(jq -nc --args '{stt:{queue:$ARGS.positional}}' "$@")"
}
# One queued answer with its own delay: stt_queue_slow "text" 3000
stt_queue_slow() { ctl "$(jq -nc --arg t "$1" --argjson d "$2" '{stt:{queue:[{text:$t,delay_ms:$d}]}}')"; }
stt_mode() {  # status delay_ms
    ctl "$(jq -nc --argjson s "${1:-200}" --argjson d "${2:-0}" '{stt:{status:$s,delay_ms:$d}}')"
}
llm_mode() {  # echo|fail500|reset|hang  [delay_ms] [chunk_delay_ms]
    ctl "$(jq -nc --arg m "$1" --argjson d "${2:-0}" --argjson c "${3:-0}" '{llm:{mode:$m,delay_ms:$d,chunk_delay_ms:$c}}')"
}

# A WAV of the words (espeak), cached; falls back to a tone when espeak is missing.
take_wav() {  # text [speed]
    local text="$1" speed="${2:-330}" key f
    key=$(printf '%s|%s' "$text" "$speed" | sha1sum | cut -c1-16)
    f="$E2E_ROOT/wav/$key.wav"
    if [ ! -s "$f" ]; then
        if command -v espeak-ng >/dev/null 2>&1; then
            espeak-ng -s "$speed" -w "$f.raw.wav" -- "$text" >/dev/null 2>&1
            if command -v sox >/dev/null 2>&1; then sox "$f.raw.wav" -r 48000 -c 2 -b 16 "$f" 2>/dev/null; else mv "$f.raw.wav" "$f"; fi
            rm -f "$f.raw.wav"
        fi
        [ -s "$f" ] || sox -n -r 48000 -c 2 -b 16 "$f" synth 1.5 sine 440 vol 0.4
    fi
    echo "$f"
}

# Plays into the null microphone (in the background unless told otherwise); the
# pid is left in PLAY_PID so a scenario can wait for it.
mic_play() {  # wav
    e2e_env paplay --device=e2e_mic "$1" &
    PLAY_PID=$!
}
mic_wait() { [ -n "${PLAY_PID:-}" ] && wait "$PLAY_PID" 2>/dev/null; PLAY_PID=""; }

cur_take() { cat "$E2E_RT_VOX/current_capture" 2>/dev/null; }
outcome_json() { cat "$E2E_RT_VOX/outcome.json" 2>/dev/null; }
take_dir() { echo "$E2E_RT_VOX/takes/$1"; }

# Wait until the daemon is in a state ("recording", "idle", ...).
wait_state() { e2e_wait "state $1" "${2:-10}" bash -c "[ \"\$(cat '$E2E_RT_VOX/state' 2>/dev/null)\" = '$1' ]"; }

# Wait for the receipt of a take: outcome.json naming it, or the take's own disposition.
# Prints the outcome ("delivered", "recovered", ...) or "timeout".
wait_outcome() {  # take_id [timeout_s]
    local take="$1" t="${2:-25}" i=0 max d o
    max=$((t * 20))
    while [ "$i" -lt "$max" ]; do
        # In shuush delivery the daemon first publishes "cancelled" (it handed over an empty
        # string); the worker's own receipt (delivery_mode "shuush") is the take's real end.
        o=$(jq -r --arg t "$take" --arg dm "${DAEMON_DELIVERY:-daemon}" \
            'select(.take_id==$t and ($dm != "shuush" or .delivery_mode == "shuush")) | .outcome // empty' "$E2E_RT_VOX/outcome.json" 2>/dev/null)
        if [ -n "$o" ]; then echo "$o"; return 0; fi
        # (In shuush delivery the daemon marks the take recovered itself for a moment, with an empty
        # recovered.txt, before the worker writes the real one: only the worker's receipt counts there.)
        if [ "${DAEMON_DELIVERY:-daemon}" != shuush ]; then
            d=$(cat "$E2E_RT_VOX/takes/$take/disposition" 2>/dev/null)
            if [ -n "$d" ]; then echo "$d"; return 0; fi
        fi
        sleep 0.05; i=$((i + 1))
    done
    echo timeout; return 1
}

# Start a take the way the toggle bind does and wait until it is recording.
take_start() {
    e2e_env shuush toggle >> "$E2E_LOGS/toggle.log" 2>&1 &
    wait_state recording 10 || { log "take did not start (state: $(daemon_state))"; return 1; }
    TAKE_ID="$(cur_take)"
}
take_stop() { e2e_env shuush toggle >> "$E2E_LOGS/toggle.log" 2>&1; }

# A whole take: queue the transcript, record, speak the words into the mic, stop.
# Sets TAKE_ID and TAKE_OUTCOME. Options through env: TAKE_SPEAK=0 (silence),
# TAKE_NOWAIT=1 (return after stop), TAKE_SPEED, TAKE_TIMEOUT.
take_run() {  # transcript
    local text="$1" wav
    stt_queue "$text"
    take_start || { TAKE_OUTCOME=nostart; return 1; }
    if [ "${TAKE_SPEAK:-1}" = 1 ]; then
        # What is said only has to be audible: the fake transcriber answers with the queued text.
        wav=$(take_wav "$(printf '%s' "${TAKE_SAY:-$text}" | tr '\n' ' ' | cut -d' ' -f1-10)" "${TAKE_SPEED:-330}")
        mic_play "$wav"
        mic_wait
        sleep 0.2
    else
        sleep 1
    fi
    take_stop
    [ "${TAKE_NOWAIT:-0}" = 1 ] && return 0
    TAKE_OUTCOME=$(wait_outcome "$TAKE_ID" "${TAKE_TIMEOUT:-25}")
}

# What the fake cleanup model returns for a transcript (the same rule fake_services.clean_text applies).
expect_clean() { python3 -I "$E2E_DIR/fake_services.py" clean "$1"; }

now_ms() { date +%s%3N; }

# Wait for an outcome.json written after `since_ms` (a recovered take has no take id of its own).
wait_outcome_since() {  # since_ms [timeout_s]
    local since="$1" t="${2:-25}" i=0 max o
    max=$((t * 20))
    while [ "$i" -lt "$max" ]; do
        o=$(jq -r --argjson s "$since" 'select(.at_ms > $s) | .outcome // empty' "$E2E_RT_VOX/outcome.json" 2>/dev/null)
        if [ -n "$o" ]; then echo "$o"; return 0; fi
        sleep 0.05; i=$((i + 1))
    done
    echo timeout; return 1
}

# Like take_run, for a transcript already queued by the caller (stt_queue_slow ...).
take_run_queued() {
    local wav
    take_start || { TAKE_OUTCOME=nostart; return 1; }
    wav=$(take_wav "${TAKE_SAY:-this is spoken into the null microphone}" "${TAKE_SPEED:-330}")
    mic_play "$wav"; mic_wait; sleep 0.2
    take_stop
    [ "${TAKE_NOWAIT:-0}" = 1 ] && return 0
    TAKE_OUTCOME=$(wait_outcome "$TAKE_ID" "${TAKE_TIMEOUT:-25}")
}

# Push-to-talk, the way the hold bind does it: `shuush hold` on key down, `shuush release`
# on key up. Sets HOLD_TAKE to the take the press started ("" when the press was ignored).
hold_press() {
    local prev; prev="$(cur_take)"
    e2e_env shuush hold >> "$E2E_LOGS/toggle.log" 2>&1
    local i
    for i in $(seq 1 100); do
        [ "$(cur_take)" != "$prev" ] && { HOLD_TAKE="$(cur_take)"; return 0; }
        sleep 0.05
    done
    HOLD_TAKE=""; return 1
}
hold_release() { e2e_env shuush release >> "$E2E_LOGS/toggle.log" 2>&1; }
# press, speak for the length of a phrase, release
hold_take() {  # [what is said]
    hold_press || return 1
    mic_play "$(take_wav "${1:-this is spoken into the null microphone}" "${TAKE_SPEED:-330}")"; mic_wait; sleep 0.15
    hold_release
}
