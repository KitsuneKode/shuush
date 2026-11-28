# desc: the target window is closed while the take is transcribing: recovered, nothing typed elsewhere
# deliveries: daemon shuush
scn_run() {
    target_kitty bystander "e2e-bystander" || { _rec false "the bystander window starts" ok failed; return; }
    local by_log="$TARGET_KITTY_LOG"
    target_kitty main "e2e-main" || { _rec false "the target window starts" ok failed; return; }
    local main_id="$TARGET_LAST_ID"
    check_eq "the target has focus" "$main_id" "$(sway_focused)"
    stt_queue_slow "um this take loses its window" 3000
    TAKE_NOWAIT=1 take_run_queued
    local id="$TAKE_ID"
    sleep 0.5
    sway "[con_id=$main_id] kill" >/dev/null
    local o; o=$(wait_outcome "$id" 20)
    sleep 0.5
    check_eq "outcome" recovered "$o"
    check_eq "nothing typed into the window that inherited focus" "" "$(kitty_text "$by_log")"
    check_eq "the take's text was kept" "This take loses its window." "$(cat "$(take_dir "$id")/recovered.txt" 2>/dev/null)"

    # and with no window left at all
    sway "[con_id=$(sway_find kitty e2e-bystander)] kill" >/dev/null; sleep 0.5
    target_kitty last "e2e-last" || return
    local last_id="$TARGET_LAST_ID"
    stt_queue_slow "um nobody is left to type into" 3000
    TAKE_NOWAIT=1 take_run_queued
    id="$TAKE_ID"; sleep 0.5
    sway "[con_id=$last_id] kill" >/dev/null
    o=$(wait_outcome "$id" 20)
    check_eq "outcome with no window left" recovered "$o"
    check_eq "the take's text was kept" "Nobody is left to type into." "$(cat "$(take_dir "$id")/recovered.txt" 2>/dev/null)"
}
