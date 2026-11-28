# desc: a private take is typed as heard and leaves no trace: no model call, no clipboard, no history, no recovery file
# deliveries: daemon shuush
scn_run() {
    target_kitty main "e2e-main" || { _rec false "the terminal target starts" ok failed; return; }
    local rows0; rows0=$(history_rows)
    # What a password field does: the input method publishes this marker while it has focus.
    : > "$E2E_RT_VOX/sensitive_field"
    take_run "um the card pin is four four one two"
    check_eq "outcome" delivered "$TAKE_OUTCOME"
    sleep 0.6
    check_eq "typed exactly as heard" "um the card pin is four four one two" "$(kitty_text)"
    check_eq "the cleanup model was never called" 0 "$(fake_state | jq '.llm_log|length')"
    check_eq "the clipboard was not touched" "$CLIP_SENTINEL" "$(clip_get)"
    check_eq "nothing was written to history" "$rows0" "$(history_rows)"
    check_true "the take is marked private" test -e "$(take_dir "$TAKE_ID")/pp_private"
    check_true "no copy of the text was kept for recovery" test ! -e "$(take_dir "$TAKE_ID")/recovered.txt"
    check_false "the words are in no log the world wrote" grep -rqs "card pin is four" "$E2E_ROOT/run/voxtype/pp.log" "$(history_file)"

    # private, and the window changes before it is typed: not inserted, not kept, not copied
    rm -f "$E2E_RT_VOX/sensitive_field"
    target_kitty other "e2e-other" || return
    local other_id="$TARGET_LAST_ID" other_log="$TARGET_KITTY_LOG"
    sway_focus "$(sway_find kitty e2e-main)"
    : > "$E2E_RT_VOX/sensitive_field"
    stt_queue_slow "um the private take that must go nowhere" 3000
    TAKE_NOWAIT=1 take_run_queued
    local id="$TAKE_ID"; sleep 0.5
    sway_focus "$other_id"
    local o; o=$(wait_outcome "$id" 20)
    sleep 0.6
    check_eq "outcome when the window changed" private_not_inserted "$o"
    check_eq "nothing typed into the window that took focus" "" "$(kitty_text "$other_log")"
    check_true "no recovery file for a private take" test ! -s "$(take_dir "$id")/recovered.txt"
    check_eq "the clipboard was not touched" "$CLIP_SENTINEL" "$(clip_get)"
    check_eq "still nothing in history" "$rows0" "$(history_rows)"
    check_eq "the cleanup model was still never called" 0 "$(fake_state | jq '.llm_log|length')"
}
