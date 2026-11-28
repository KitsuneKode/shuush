# desc: a take over the paste threshold goes in as one paste (terminal and web page), text exact
LONG="the quarterly reliability review covered dictation latency, clipboard handling, terminal bracketed paste behaviour, browser textarea input events, focus changes during transcription, cleanup provider fallbacks and the recovery of takes that could not be delivered anywhere at all during the whole week"
scn_run() {
    local want; want="$(expect_clean "$LONG")"
    check_true "the take is longer than the paste threshold" [ "${#want}" -gt 300 ]
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    take_run "$LONG"
    check_eq "outcome (terminal)" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the terminal" "$want" "$(kitty_text)"
    check_eq "it arrived as one bracketed paste" "paste:1" "$(kitty_kinds | sed 's/^ *\([0-9]*\) \(paste\).*/\2:\1/')"
    target_close_all
    target_chrome || { _rec false "the browser target starts" ok failed; return; }
    page_cmd focus:ta; sleep 0.5
    take_run "$LONG"
    check_eq "outcome (web page)" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the textarea" "$want" "$(page_value ta)"
    check_eq "the page saw one paste" 1 "$(fake_count '.type=="paste"')"
}
