# desc: a real password input in Chromium: the input method sees it, the take is private and typed as heard
# deliveries: daemon
scn_config() { DAEMON_ENV_EXTRA='PP_IME=shuush'; }
scn_run() {
    if ! e2e_env shuush caps has shuush_ime >/dev/null 2>&1; then
        sc_skip "shuush-ime is not available (needs cargo to build it, and input-method-v2 in the compositor)"
        return
    fi
    target_chrome || { _rec false "the browser target starts" ok failed; return; }
    local rows0; rows0=$(history_rows)
    # an ordinary field first: the take is cleaned, as usual
    page_cmd focus:ta; sleep 1
    take_run "um an ordinary field first"
    sleep 2.5
    check_known "PP_IME=shuush: a take shuush-ime committed into the field is left as outcome \"cancelled\" (chars 0) in outcome.json" \
        "outcome of an ordinary take committed by the input method" delivered "$(outcome_json | jq -r .outcome)"
    check_eq "the ordinary field gets the cleaned text" "An ordinary field first." "$(page_value ta)"
    check_eq "the model was asked about the ordinary one" 1 "$(fake_state | jq '.llm_log|length')"
    # now the password input
    page_cmd focus:pw; sleep 1
    stt_queue "um my password is hunter two"
    take_start
    sleep 1
    check_true "the input method published the sensitive-field marker" test -e "$E2E_RT_VOX/sensitive_field"
    mic_play "$(take_wav "my password is hunter two")"; mic_wait
    take_stop
    local o; o=$(wait_outcome "$TAKE_ID" 20)
    sleep 0.8
    check_eq "outcome (password field)" delivered "$o"
    check_true "the take is marked private" test -e "$(take_dir "$TAKE_ID")/pp_private"
    check_eq "the password input holds the words exactly as heard" "um my password is hunter two" "$(page_value pw)"
    check_eq "the model was not asked again" 1 "$(fake_state | jq '.llm_log|length')"
    check_eq "the clipboard holds the ordinary take, not the password" "An ordinary field first." "$(clip_get)"
    check_eq "history has the ordinary take only" "$((rows0 + 1))" "$(history_rows)"
    check_false "the words are in no history or log" grep -rqs "hunter two" "$(history_file)" "$E2E_RT_VOX/pp.log"
}
