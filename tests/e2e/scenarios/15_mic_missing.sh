# desc: the microphone is missing (configured device absent; default source gone): no stuck state, the user is told, recovery when it is back
# deliveries: daemon
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }

    # --- the configured device does not exist: restart the daemon pointed at it
    DAEMON_AUDIO_DEVICE="e2e-no-such-microphone"
    daemon_restart
    stt_queue "um nobody heard this"
    e2e_env shuush toggle >> "$E2E_LOGS/toggle.log" 2>&1 &
    sleep 3
    local st; st="$(daemon_state)"
    check_ne "the daemon is not left recording a device that is not there ($st)" recording "$st"
    check_ne "nor stuck transcribing ($st)" transcribing "$st"
    check_eq "nothing was sent to the transcriber" 0 "$(fake_state | jq '.stt_log|length')"
    check_eq "nothing was typed" "" "$(kitty_text)"
    check_true "the user is told which microphone is missing" grep -qi "microphone not found" "$E2E_LOGS/notify.log"
    check_true "and what to do about it" grep -q "voxtype config set audio.device" "$E2E_LOGS/notify.log"

    # --- the default source disappears while the daemon runs (a USB mic pulled)
    DAEMON_AUDIO_DEVICE=default
    daemon_restart
    local mod; mod=$(e2e_pactl list short modules | awk '/module-null-sink/{print $1}' | head -1)
    e2e_pactl unload-module "$mod" >/dev/null 2>&1
    sleep 0.5
    stt_queue "um still nobody heard this"
    e2e_env shuush toggle >> "$E2E_LOGS/toggle.log" 2>&1 &
    sleep 2
    st="$(daemon_state)"
    check_eq "the daemon gave up on silence instead of recording forever ($st)" idle "$st"
    check_eq "nothing was typed from a dead microphone" "" "$(kitty_text)"
    check_true "the user is told the microphone could not be opened" grep -qi "could not open the microphone" "$E2E_LOGS/notify.log"

    # --- the microphone is back
    e2e_pactl load-module module-null-sink sink_name=e2e_mic >/dev/null
    e2e_pactl set-default-source e2e_mic.monitor
    sleep 0.5
    ctl_reset
    take_run "um the microphone is back"
    check_eq "the next take delivers" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "and lands" "The microphone is back." "$(kitty_text)"
}
