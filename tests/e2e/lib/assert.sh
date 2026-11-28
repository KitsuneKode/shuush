#!/usr/bin/env bash
# lib/assert.sh — checks that are recorded, not just printed. One scenario is
# many checks; the scenario passes when none failed. Results go to
# $E2E_RESULTS as JSON lines for the report.

SC_NAME=""; SC_FAILS=0; SC_CHECKS=0; SC_NOTES=(); SC_BUGS=()

sc_begin() { SC_NAME="$1"; SC_FAILS=0; SC_CHECKS=0; SC_NOTES=(); SC_BUGS=(); SC_T0=$(date +%s); log "── scenario: $SC_NAME"; }

_rec() {  # ok desc expected actual
    SC_CHECKS=$((SC_CHECKS + 1))
    if [ "$1" = true ]; then
        printf '   ok   %s\n' "$2" >&2
    else
        SC_FAILS=$((SC_FAILS + 1))
        printf '   FAIL %s\n        want: %s\n        got : %s\n' "$2" "$3" "$4" >&2
        SC_NOTES+=("$(jq -nc --arg d "$2" --arg w "$3" --arg g "$4" '{check:$d,want:$w,got:$g}')")
    fi
}

check_eq() {  # desc expected actual
    if [ "$2" = "$3" ]; then _rec true "$1" "$2" "$3"; else _rec false "$1" "$2" "$3"; fi
}
check_ne() { if [ "$2" != "$3" ]; then _rec true "$1" "≠ $2" "$3"; else _rec false "$1" "≠ $2" "$3"; fi; }
check_match() {  # desc regex actual
    if [[ "$3" =~ $2 ]]; then _rec true "$1" "$2" "$3"; else _rec false "$1" "matches /$2/" "$3"; fi
}
check_empty() { if [ -z "$2" ]; then _rec true "$1" "" ""; else _rec false "$1" "(nothing)" "$2"; fi; }
check_true() {  # desc cmd...
    local d="$1"; shift
    if "$@" >/dev/null 2>&1; then _rec true "$d" "true" "true"; else _rec false "$d" "true" "false ($*)"; fi
}
check_false() {
    local d="$1"; shift
    if "$@" >/dev/null 2>&1; then _rec false "$d" "false" "true ($*)"; else _rec true "$d" "false" "false"; fi
}

# A scenario that cannot run here (a tool is missing) says so; it is neither a pass nor a failure.
sc_skip() {
    printf '%s\n' "$(jq -nc --arg s "$SC_NAME" --arg r "$1" '{scenario:$s,status:"skip",reason:$r}')" >> "$E2E_RESULTS"
    log "   skip: $1"
    SC_NAME=""
}

# A defect found by this harness and reported, not yet fixed: the check is kept, its
# failure is listed in the report under "Known bugs" and does not fail the run. The day
# the check passes the report says so, and the mark should go.
check_known() {  # bug-tag desc expected actual
    SC_CHECKS=$((SC_CHECKS + 1))
    if [ "$3" = "$4" ]; then
        printf '   ok   %s   (known bug "%s" no longer reproduces: drop the mark)\n' "$2" "$1" >&2
        SC_BUGS+=("$(jq -nc --arg b "$1" --arg d "$2" '{bug:$b,check:$d,fixed:true}')")
    else
        printf '   BUG  %s   [%s]\n        want: %s\n        got : %s\n' "$2" "$1" "$3" "$4" >&2
        SC_BUGS+=("$(jq -nc --arg b "$1" --arg d "$2" --arg w "$3" --arg g "$4" '{bug:$b,check:$d,want:$w,got:$g,fixed:false}')")
    fi
}

sc_end() {
    [ -n "$SC_NAME" ] || return 0
    local status=pass notes_json bugs_json
    bugs_json=$(printf '%s\n' "${SC_BUGS[@]:-}" | jq -cs 'map(select(. != null))' 2>/dev/null || echo '[]')
    [ "$(jq 'map(select(.fixed==false)) | length' <<<"$bugs_json")" -gt 0 ] && status=known
    [ "$SC_FAILS" -gt 0 ] && status=fail
    notes_json=$(printf '%s\n' "${SC_NOTES[@]:-}" | jq -cs 'map(select(. != null))' 2>/dev/null || echo '[]')
    jq -nc --arg s "$SC_NAME" --arg st "$status" --argjson c "$SC_CHECKS" --argjson f "$SC_FAILS" \
        --argjson n "$notes_json" --argjson b "$bugs_json" --argjson dur $(( $(date +%s) - SC_T0 )) \
        '{scenario:$s,status:$st,checks:$c,failed:$f,failures:$n,bugs:$b,seconds:$dur}' >> "$E2E_RESULTS"
    log "── $SC_NAME: $status ($SC_CHECKS checks, $SC_FAILS failed)"
    SC_NAME=""
}
