# desc: a short take typed into a terminal and into a web page; delivered, text exact
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    take_run "um this is a short take"
    check_eq "outcome" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the terminal" "This is a short take." "$(kitty_text)"
    check_eq "outcome.json names the take" "$TAKE_ID" "$(outcome_json | jq -r .take_id)"
    check_eq "the cleanup model saw the transcript once" 1 "$(fake_state | jq '.llm_log|length')"
    check_true "audio reached the daemon (not silence)" bash -c "[ \"\$(curl -s $FAKE/_state | jq '.stt_log[0].audio.rms > 0.005')\" = true ]"
    check_eq "the clipboard keeps what the take put there" "This is a short take." "$(clip_get)"
}
