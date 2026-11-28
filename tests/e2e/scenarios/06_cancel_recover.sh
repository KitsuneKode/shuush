# desc: a cancelled take types nothing; "recover" brings its audio back and delivers it
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    stt_queue "um this take was cancelled and then recovered"
    take_start || { _rec false "the take starts" recording "$(daemon_state)"; return; }
    local id="$TAKE_ID"
    # A cancelled take is kept for recovery only when it is >= 2 s with >= 1 s of speech.
    mic_play "$(take_wav "this take was cancelled and then recovered by hand" 150)"; mic_wait
    sleep 0.3
    e2e_env shuush cancel >> "$E2E_LOGS/cancel.log" 2>&1          # first Escape only arms the cancel
    sleep 0.15
    check_eq "one Escape press does not cancel" recording "$(daemon_state)"
    e2e_env shuush cancel >> "$E2E_LOGS/cancel.log" 2>&1          # the second, inside 600 ms, does
    check_true "the daemon goes back to idle" wait_state idle 8
    sleep 0.8
    check_eq "nothing was typed" "" "$(kitty_text)"
    check_eq "outcome is cancelled" cancelled "$(outcome_json | jq -r .outcome)"
    check_eq "the fake transcriber was not asked" 0 "$(fake_state | jq '.stt_log|length')"

    local t0; t0=$(now_ms)
    e2e_env shuush recover >> "$E2E_LOGS/recover.log" 2>&1
    local o; o=$(wait_outcome_since "$t0" 6)
    sleep 0.6
    check_eq "the recovered take is transcribed once" 1 "$(fake_state | jq '.stt_log|length')"
    check_eq "the recovered take's text lands" "This take was cancelled and then recovered." "$(kitty_text)"
    check_known "voxtype: record recover never publishes outcome.json (osd_outcome_sent stays latched from the cancel)" \
        "the recovered take has a receipt after the cancel's" "delivered" "$o"
}
