# desc: a take with line breaks lands with its lines intact, in the terminal and a textarea
RAW=$'first line of the note\num second line of the note\nthird line of the note'
scn_run() {
    local want; want="$(expect_clean "$RAW")"
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    take_run "$RAW"
    check_eq "outcome (terminal)" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the terminal" "$want" "$(kitty_text)"
    target_close_all
    target_chrome || { _rec false "the browser target starts" ok failed; return; }
    page_cmd focus:ta; sleep 0.5
    take_run "$RAW"
    check_eq "outcome (web page)" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the textarea" "$want" "$(page_value ta)"
}
