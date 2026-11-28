# desc: shuush delivery: a long take is cleaned as a stream and typed sentence by sentence, text exact
# deliveries: shuush
RAW="um the first sentence of this long take is about the build. the second sentence is about the tests that ran last night. uh the third sentence covers the deploy and what went wrong. and the last sentence says what we will change next week"
scn_config() { DAEMON_DELIVERY=shuush; DAEMON_ENV_EXTRA='PP_STREAM_MIN_WORDS=20'; }
scn_run() {
    local want; want="$(expect_clean "$RAW")"
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    llm_mode echo 0 150            # the fake model streams its answer slowly
    take_run "$RAW"
    check_eq "outcome" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the terminal" "$want" "$(kitty_text)"
    local bursts; bursts=$(jq -s '[.[] | select(.kind=="type") | .t] as $t | [range(1; $t|length) | select($t[.] - $t[. - 1] > 0.15)] | length + 1' "$TARGET_KITTY_LOG")
    check_true "it arrived in several bursts, not all at once ($bursts)" [ "$bursts" -ge 3 ]
    check_eq "the cleanup model streamed" true "$(fake_state | jq '.llm_log[0].stream')"
    local span; span=$(jq -s '[.[] | select(.kind=="type") | .t] | (max - min) * 1000 | floor' "$TARGET_KITTY_LOG")
    check_true "typing was spread over time (${span} ms)" [ "$span" -ge 400 ]
}
