#!/usr/bin/env bash
# tests/e2e/run.sh — dictation, end to end, in a world of its own.
#
#   tests/e2e/run.sh                     every scenario, report on stdout
#   tests/e2e/run.sh short_take paste    only those (names or number prefixes)
#   tests/e2e/run.sh --list              what there is
#   tests/e2e/run.sh --keep              leave the world's directory for a look (path printed)
#   tests/e2e/run.sh --no-netns          do not enter user/net/pid namespaces
#   tests/e2e/run.sh --report DIR        where the report and results go
#                                        (default $XDG_STATE_HOME/shuush-e2e/reports)
#
# What it starts, all inside a private runtime dir, HOME and (when the kernel
# allows it) private user, network and pid namespaces: a headless sway, a
# PipeWire with one null microphone, the fork's voxtype (a copy), shuush
# installed into a scratch prefix, a fake speech-to-text and cleanup server on
# loopback, and a kitty / Chromium target for the text to land in. It never
# touches the owner's session, config, history, microphone, speakers, clipboard
# or media. Exit status: 0 when every scenario passed, 1 when any failed, 2
# when the world could not be built.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KEEP=0; NETNS=1; REPORT_DIR=""; LIST=0; ONLY=()
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --no-netns) NETNS=0 ;;
        --list) LIST=1 ;;
        --report) shift; REPORT_DIR="${1:-}" ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) ONLY+=("$1") ;;
    esac
    shift
done

scenario_files() {
    local f n want
    for f in "$HERE"/scenarios/[0-9]*.sh; do
        n="$(basename "$f" .sh)"
        if [ "${#ONLY[@]}" -eq 0 ]; then echo "$f"; continue; fi
        for want in "${ONLY[@]}"; do
            case "$n" in "$want"|"${want}_"*|*"_${want}"|*"_${want}_"*) echo "$f"; break ;; esac
        done
    done
}

if [ "$LIST" = 1 ]; then
    for f in "$HERE"/scenarios/[0-9]*.sh; do
        printf '%-28s %s\n' "$(basename "$f" .sh)" "$(sed -n 's/^# desc: //p' "$f" | head -1)"
    done
    exit 0
fi

# ---- shuush-ime, built before the namespaces close (cargo may need ~/.cargo, offline is enough) ----
prebuild_ime() {
    local cache="${E2E_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/shuush-e2e}" repo
    repo="$(cd "$HERE/../.." && pwd)"
    mkdir -p "$cache/bin"
    command -v cargo >/dev/null 2>&1 || return 0
    [ -f "$repo/ime/Cargo.toml" ] || return 0
    # rebuild only when the sources are newer than the binary
    if [ ! -x "$cache/bin/shuush-ime" ] || [ -n "$(find "$repo/ime/src" "$repo/ime/Cargo.lock" -newer "$cache/bin/shuush-ime" -print -quit)" ]; then
        echo "[e2e] building shuush-ime (offline, into $cache)" >&2
        CARGO_TARGET_DIR="$cache/ime-target" cargo build --release --locked --offline --manifest-path "$repo/ime/Cargo.toml" >"$cache/ime-build.log" 2>&1 \
            && cp "$cache/ime-target/release/shuush-ime" "$cache/bin/shuush-ime" \
            || echo "[e2e] shuush-ime did not build (see $cache/ime-build.log): the password-field scenario falls back to a marker" >&2
    fi
}

# ---- the namespaces --------------------------------------------------------------------
if [ -z "${E2E_INNER:-}" ]; then
    prebuild_ime
    if [ "$NETNS" = 1 ] && unshare --user --map-root-user --net --pid --mount --fork --kill-child --mount-proc true 2>/dev/null; then
        export E2E_INNER=1 E2E_ISOLATED=1 E2E_KEEP="$KEEP" E2E_REPORT_DIR="$REPORT_DIR"
        exec unshare --user --map-root-user --net --pid --mount --fork --kill-child --mount-proc \
            bash "$0" "${ONLY[@]}"
    fi
    echo "[e2e] no user/net/pid namespaces here: running without them (the cleanup model is still pinned to the fake endpoint, but nothing stops a stray network call)" >&2
    export E2E_INNER=1 E2E_ISOLATED=0
fi
KEEP="${E2E_KEEP:-$KEEP}"; REPORT_DIR="${E2E_REPORT_DIR:-$REPORT_DIR}"
[ -n "$REPORT_DIR" ] || REPORT_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/shuush-e2e/reports"

# ---- libraries -------------------------------------------------------------------------
. "$HERE/lib/env.sh"
. "$HERE/lib/daemon.sh"
. "$HERE/lib/targets.sh"
. "$HERE/lib/take.sh"
. "$HERE/lib/assert.sh"
. "$HERE/lib/scenario.sh"

e2e_init
TS="$(date +%Y%m%dT%H%M%S)"
finish() {
    local rc=$?
    e2e_down
    if [ "$KEEP" = 1 ]; then log "kept: $E2E_ROOT"; else rm -rf "$E2E_ROOT"; fi
    exit "$rc"
}
trap finish EXIT

e2e_up || exit 2
mapfile -t FILES < <(scenario_files)
[ "${#FILES[@]}" -gt 0 ] || { log "no scenario matches: ${ONLY[*]}"; exit 2; }

for f in "${FILES[@]}"; do
    base="$(basename "$f" .sh)"; base="${base#[0-9]*_}"
    deliveries="$(sed -n 's/^# deliveries: //p' "$f" | head -1)"
    for d in ${deliveries:-daemon}; do
        name="$base"; [ "$d" = daemon ] || name="$base@$d"
        [ -n "${E2E_DELIVERY:-}" ] && [ "$E2E_DELIVERY" != "$d" ] && [ "$E2E_DELIVERY" != both ] && continue
        ( run_scenario "$name" "$f" "$d" ) || true
        # A scenario that fails once is run once more: a host under load (this machine builds
        # things) can starve the world. It counts as passed only if the second run passes, and the
        # report says it needed one.
        if jq -e --arg n "$name" 'select(.scenario==$n and .status=="fail")' "$E2E_RESULTS" >/dev/null 2>&1 && [ "${E2E_RETRIES:-1}" -gt 0 ]; then
            log "── $name failed: running it once more"
            jq -c --arg n "$name" 'select(.scenario==$n and .status=="fail") | .status="first_try_failed"' "$E2E_RESULTS" > "$E2E_ROOT/first.jsonl"
            jq -c --arg n "$name" 'select(.scenario!=$n)' "$E2E_RESULTS" > "$E2E_ROOT/rest.jsonl"
            cat "$E2E_ROOT/rest.jsonl" > "$E2E_RESULTS"
            ( run_scenario "$name" "$f" "$d" ) || true
            if jq -e --arg n "$name" 'select(.scenario==$n and .status!="fail")' "$E2E_RESULTS" >/dev/null 2>&1; then
                jq -c --arg n "$name" 'select(.scenario==$n) | .retried=true' "$E2E_RESULTS" > "$E2E_ROOT/second.jsonl"
                jq -c --arg n "$name" 'select(.scenario!=$n)' "$E2E_RESULTS" > "$E2E_ROOT/rest.jsonl"
                cat "$E2E_ROOT/rest.jsonl" "$E2E_ROOT/second.jsonl" > "$E2E_RESULTS"
            else
                cat "$E2E_ROOT/first.jsonl" | jq -c '.status="fail"' >> "$E2E_RESULTS"
            fi
        fi
        if ! jq -e --arg n "$name" 'select(.scenario==$n)' "$E2E_RESULTS" >/dev/null 2>&1; then
            jq -nc --arg s "$name" '{scenario:$s,status:"fail",checks:0,failed:1,failures:[{check:"the scenario finished",want:"a result",got:"it crashed or hit its time limit"}],bugs:[],seconds:0}' >> "$E2E_RESULTS"
            log "── $name: crashed or timed out"
        fi
    done
done

# ---- the report --------------------------------------------------------------------------
mkdir -p "$REPORT_DIR"
cp "$E2E_RESULTS" "$REPORT_DIR/results-$TS.jsonl"
collect_stats > "$E2E_ROOT/stats.json" 2>/dev/null || echo '{}' > "$E2E_ROOT/stats.json"
python3 -I "$HERE/report.py" "$E2E_RESULTS" --stats "$E2E_ROOT/stats.json" --isolated "${E2E_ISOLATED:-0}" \
    --voxtype "$("$E2E_BIN/voxtype" --version 2>/dev/null | head -1)" \
    --shuush "$(git -C "$E2E_REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    --out "$REPORT_DIR/report-$TS.md" --latest "$REPORT_DIR/latest.md" || true
cat "$REPORT_DIR/report-$TS.md"
fails=$(jq -s '[.[] | select(.status=="fail")] | length' "$E2E_RESULTS")
[ "$fails" -eq 0 ]
