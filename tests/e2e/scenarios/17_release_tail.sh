# desc: the release tail keeps recording after key-up, so the last syllable is not clipped (400 ms live setting vs none)
# deliveries: daemon
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    local wav; wav="$(take_wav "the last syllable matters" 330)"
    local d_wav; d_wav=$(sox --i -D "$wav" 2>/dev/null || echo 1.2)
    stt_queue "um with the tail on"
    hold_take "the last syllable matters"
    wait_outcome "$HOLD_TAKE" 20 >/dev/null
    local with; with=$(fake_state | jq '.stt_log[0].audio.seconds')
    DAEMON_RELEASE_TAIL_MS=0
    daemon_restart
    ctl_reset
    stt_queue "um with the tail off"
    hold_take "the last syllable matters"
    wait_outcome "$HOLD_TAKE" 20 >/dev/null
    local without; without=$(fake_state | jq '.stt_log[0].audio.seconds')
    local gain; gain=$(awk -v a="$with" -v b="$without" 'BEGIN{printf "%d", (a-b)*1000}')
    check_true "audio sent with the tail is longer (the tail is adaptive: it ends early on silence; at least 80 ms) than without (${gain} ms: ${with}s vs ${without}s)" [ "$gain" -ge 80 ]
}
