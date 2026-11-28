# desc: queue_dictations off: a press while the first take transcribes is ignored; the first still lands
# deliveries: daemon
scn_config() { DAEMON_QUEUE=false; }
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    stt_queue_slow "um first take of the queue" 3000
    stt_queue "um second take that must not happen"
    hold_take "first take of the queue" || { _rec false "the first take starts" yes no; return; }
    local first="$HOLD_TAKE"
    sleep 0.5
    check_eq "the first take is transcribing" transcribing "$(daemon_state)"
    e2e_env shuush hold >> "$E2E_LOGS/toggle.log" 2>&1       # key down while "Polishing"
    sleep 0.2
    hold_release
    sleep 0.3
    check_eq "the press started nothing" "$first" "$(cur_take)"
    check_eq "still transcribing the first" transcribing "$(daemon_state)"
    local o; o=$(wait_outcome "$first" 30)
    sleep 0.6
    check_eq "first take outcome" delivered "$o"
    check_eq "only the first take landed" "First take of the queue." "$(kitty_text)"
    check_eq "the transcriber was asked once" 1 "$(fake_state | jq '.stt_log|length')"
    # and the daemon is free again
    stt_queue "um third take after the queue"
    take_run_queued
    check_eq "a take after the ignored press delivers" delivered "$TAKE_OUTCOME"
}
