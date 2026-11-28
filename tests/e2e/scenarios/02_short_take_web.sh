# desc: a short take typed into a Chromium textarea and a contenteditable; delivered, text exact
scn_run() {
    target_chrome || { _rec false "the browser target starts" ok failed; return; }
    page_cmd focus:ta; sleep 0.5
    take_run "um this is the first take for the page"
    check_eq "outcome (textarea)" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the textarea" "This is the first take for the page." "$(page_value ta)"
    page_cmd clear; page_cmd focus:ce; sleep 0.5
    take_run "and this one goes into the editable div"
    check_eq "outcome (contenteditable)" delivered "$TAKE_OUTCOME"
    sleep 0.5
    check_eq "text in the contenteditable" "And this one goes into the editable div." "$(page_value ce | sed 's/\xc2\xa0/ /g')"
}
