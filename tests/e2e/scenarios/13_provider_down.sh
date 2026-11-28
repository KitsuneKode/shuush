# desc: the cleanup provider fails (HTTP 500, reset, hang): the raw words are typed, filler removed, and the take is delivered
# deliveries: daemon shuush
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    local mode want
    for mode in fail500 reset hang; do
        llm_mode "$mode"
        # the transcript has a filler and no full stop: the fallback cleans the first and capitalises
        : > "$TARGET_KITTY_LOG"; echo '{"kind":"ready","text":""}' >> "$TARGET_KITTY_LOG"
        local t0; t0=$(now_ms)
        take_run "um the provider is down for this take"
        local took=$(( $(now_ms) - t0 ))
        check_eq "[$mode] outcome" delivered "$TAKE_OUTCOME"
        sleep 0.6
        check_eq "[$mode] the words still land, without the filler, first letter capital" "The provider is down for this take" "$(kitty_text)"
        check_true "[$mode] it did not take longer than the cleanup budget plus slack (${took} ms)" [ "$took" -lt 16000 ]
    done
    check_eq "the provider was really asked each time" 3 "$(fake_state | jq '.llm_log|length')"
    # and it recovers: the next take with the provider back is cleaned again
    llm_mode echo
    : > "$TARGET_KITTY_LOG"
    take_run "um the provider is back"
    sleep 0.6
    check_eq "provider back: cleaned again" "The provider is back." "$(kitty_text)"
}
