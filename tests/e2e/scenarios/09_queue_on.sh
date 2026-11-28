# desc: queue_dictations on: a second take is recorded while the first transcribes; both land, in order
# deliveries: daemon shuush
scn_config() { DAEMON_QUEUE=true; }
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    check_true "the daemon publishes its queue" test -s "$E2E_RT_VOX/queue_pid"
    stt_queue_slow "um first take of the queue" 4000
    stt_queue "um second take of the queue"
    hold_take "first take of the queue" || { _rec false "the first take starts" yes no; return; }
    local first="$HOLD_TAKE"
    sleep 0.5
    # the second press, while the first is still being transcribed
    hold_take "second take of the queue"
    local second="$HOLD_TAKE"
    check_ne "the second press started its own take" "$first" "$second"
    check_ne "the second take got an id" "" "$second"
    local o1 o2
    o1=$(wait_outcome "$first" 30); o2=$(wait_outcome "$second" 30)
    sleep 0.8
    check_eq "first take outcome" delivered "$o1"
    check_eq "second take outcome" delivered "$o2"
    check_eq "both lands, first before second" "First take of the queue.Second take of the queue." "$(kitty_text)"
    check_eq "the transcriber was asked twice" 2 "$(fake_state | jq '.stt_log|length')"
}
