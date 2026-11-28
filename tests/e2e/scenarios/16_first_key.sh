# desc: the first keys of a delivery are not lost, and pre_type_delay_ms is honoured for identified takes (settle shim off)
# deliveries: daemon
# Headless sway needs a moment between a fresh virtual keyboard and its first key. The knob that exists for
# this, [output] pre_type_delay_ms, sleeps before wtype starts for an identified take instead of passing -s,
# so it cannot help. The world's wtype shim is turned off here to show it.
scn_config() { DAEMON_PRE_TYPE_MS=150; E2E_EXTRA_ENV+=("E2E_WTYPE_SETTLE_MS=0"); e2e_mkenv; }
scn_run() {
    target_kitty main || { _rec false "the terminal target starts" ok failed; return; }
    local n=4 i intact=0 got
    : > "$E2E_LOGS/wtype.argv"
    for i in $(seq 1 $n); do
        : > "$TARGET_KITTY_LOG"
        take_run "um the first key of take $i must survive"
        sleep 0.5
        got="$(kitty_text)"
        [ "$got" = "The first key of take $i must survive." ] && intact=$((intact + 1))
    done
    check_known "voxtype: [output] pre_type_delay_ms is slept before wtype starts but never passed to it (-s) for identified takes, so on a compositor that needs settle time the first key is still dropped (seen on headless sway: ${intact} of ${n} takes intact)" \
        "wtype was started with -s 150 for the delivery" yes "$(grep -c -- '-s 150' "$E2E_LOGS/wtype.argv" | sed 's/^0$/no/; s/^[1-9].*/yes/')"
}
