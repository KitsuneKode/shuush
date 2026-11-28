# desc: focus moves to another window while the take is transcribing: it is recovered, not typed anywhere
# deliveries: daemon shuush
scn_run() {
    target_kitty other "e2e-other" || { _rec false "the second terminal starts" ok failed; return; }
    local other_log="$TARGET_KITTY_LOG" other_id="$TARGET_LAST_ID"
    target_kitty main "e2e-main" || { _rec false "the first terminal starts" ok failed; return; }
    local main_log="$TARGET_KITTY_LOG"
    check_eq "the take is made in the first window" "$TARGET_LAST_ID" "$(sway_focused)"
    stt_queue_slow "um this should never be typed into the wrong window" 3000
    TAKE_SPEAK=1 TAKE_NOWAIT=1 take_run_queued
    local id="$TAKE_ID"
    sleep 0.5
    sway_focus "$other_id"                         # the user clicks away while it is "Polishing"
    check_eq "the other window has focus" "$other_id" "$(sway_focused)"
    local o; o=$(wait_outcome "$id" 20)
    sleep 0.5
    check_eq "outcome" recovered "$o"
    check_eq "nothing typed into the window that took focus" "" "$(kitty_text "$other_log")"
    check_eq "nothing typed into the window the take was made in" "" "$(kitty_text "$main_log")"
    check_eq "the take's text was kept for recovery" "This should never be typed into the wrong window." "$(cat "$(take_dir "$id")/recovered.txt" 2>/dev/null)"
    check_eq "the take's disposition" recovered "$(cat "$(take_dir "$id")/disposition" 2>/dev/null)"
    e2e_env shuush recover >/dev/null 2>&1
    sleep 0.3
    check_eq "shuush recover puts it on the clipboard" "This should never be typed into the wrong window." "$(clip_get)"
    sleep 1
    local row; row=$(tail -1 "$(history_file)" 2>/dev/null | jq -r .outcome 2>/dev/null)
    check_eq "history records it as recovered" recovered "$row"
}
