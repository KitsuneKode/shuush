# desc: the daemon is restarted (and crashed) in the middle of a take: no stale state, the next take delivers
# deliveries: daemon
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }

    # --- while recording, stopped politely (a service restart)
    stt_queue "um this take is lost with the restart"
    take_start || { _rec false "the take starts" recording "$(daemon_state)"; return; }
    mic_play "$(take_wav "this take is lost with the restart")"; mic_wait
    daemon_kill TERM
    check_false "the old daemon is gone" daemon_running
    daemon_start --keep-state
    check_eq "the new daemon is idle" idle "$(daemon_state)"
    sleep 0.5
    check_eq "the interrupted take typed nothing" "" "$(kitty_text)"
    fake_state >/dev/null; ctl_reset
    take_run "um the take after the restart"
    check_eq "the next take delivers" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "and lands once" "The take after the restart." "$(kitty_text)"

    # --- while transcribing, killed (a crash)
    : > "$TARGET_KITTY_LOG"; ctl_reset
    stt_queue_slow "um this take dies in transcription" 4000
    TAKE_NOWAIT=1 take_run_queued
    local lost="$TAKE_ID"
    sleep 0.8
    check_eq "the take was being transcribed" transcribing "$(daemon_state)"
    daemon_kill KILL
    check_eq "a crash leaves the state file behind (what a restart must cope with)" transcribing "$(daemon_state)"
    daemon_start --keep-state
    check_eq "the restarted daemon resets the state to idle" idle "$(daemon_state)"
    sleep 4.5                                        # the abandoned request is answered by now
    check_eq "the lost take does not come back and type itself later" "" "$(kitty_text)"
    ctl_reset
    take_run "um still working after the crash"
    check_eq "the next take delivers" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "and lands" "Still working after the crash." "$(kitty_text)"
}
