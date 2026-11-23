#!/usr/bin/env bash
# tests/bench_models.sh — compare cleanup models on your own recent dictations.
#
#   bash tests/bench_models.sh [N] [provider:model ...]
#
# Builds the exact request voxtype-postprocess.sh would send for the last N
# (default 20) dictations of 8+ words in history.jsonl (no screen, clipboard
# or window context: those are stubbed out), sends it to each model, and runs
# the reply through the same gate the live path uses. Writes one JSONL per
# model under $OUT and prints latency, rejections and the outputs side by side.
# Providers: groq, zen (OpenCode Zen), openrouter. Keys come from secrets.env.
# BENCH_SLEEP=<s> paces the calls (Groq's free tier limits input tokens per
# minute, and each request is ~2k tokens). Zen's free models answer only
# inside the OpenCode app, not over this API.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype"
HIST="${XDG_STATE_HOME:-$HOME/.local/state}/voxtype/history.jsonl"
N="${1:-20}"; shift || true
MODELS=("$@")
[ ${#MODELS[@]} -gt 0 ] || MODELS=(groq:qwen/qwen3.8-27b groq:openai/gpt-oss-20b)
OUT="${OUT:-$(mktemp -d)}"
secret() { sed -n "s/^$1\\s*=\\s*\"\\{0,1\\}\\([^\"]*\\)\"\\{0,1\\}\\s*$/\\1/p" "$CONF/secrets.env" | head -1; }

# Stubs: no live clipboard, window or notification side effects.
SHIM="$OUT/shim"; mkdir -p "$SHIM" "$OUT/rt/voxtype" "$OUT/state"
for c in wl-paste wl-copy hyprctl niri swaymsg notify-send kitten; do
  printf '#!/bin/sh\nexit 1\n' > "$SHIM/$c"; chmod +x "$SHIM/$c"
done

jq -c 'select(.mode != "command" and ((.raw // "") | split(" ") | length) >= 8) | {raw, mode}' "$HIST" \
  | tail -n "$N" > "$OUT/takes.jsonl"
i=0
while IFS= read -r take; do
  i=$((i + 1))
  raw=$(jq -r .raw <<<"$take"); mode=$(jq -r .mode <<<"$take")
  printf '%s' "$raw" | PATH="$SHIM:$PATH" XDG_RUNTIME_DIR="$OUT/rt" XDG_STATE_HOME="$OUT/state" \
    PP_DUMP_MESSAGES="$OUT/msg.$i.json" VOXTYPE_STREAM_OUTPUT=0 \
    bash "$ROOT/libexec/voxtype-postprocess.sh" "$mode" >/dev/null 2>&1
  printf '%s' "$raw" > "$OUT/raw.$i.txt"
done < "$OUT/takes.jsonl"

for spec in "${MODELS[@]}"; do
  prov=${spec%%:*}; model=${spec#*:}
  case "$prov" in
    groq) url=https://api.groq.com/openai/v1/chat/completions; key=$(secret GROQ_API_KEY)
          extra='{"reasoning_effort":"none"}'; [[ "$model" == openai/* ]] && extra='{"reasoning_effort":"low"}' ;;
    zen)  url=https://opencode.ai/zen/v1/chat/completions; key=$(secret OPENCODE_API_KEY); extra='{}' ;;
    openrouter) url=https://openrouter.ai/api/v1/chat/completions; key=$(secret OPENROUTER_API_KEY); extra='{}' ;;
    *) echo "unknown provider $prov"; continue ;;
  esac
  file="$OUT/$(tr '/:' '__' <<<"$spec").jsonl"; : > "$file"
  for ((k = 1; k <= i; k++)); do
    [ -f "$OUT/msg.$k.json" ] || continue
    body=$(jq -c --arg m "$model" --argjson x "$extra" '{model:$m, messages:., temperature:0.1, max_tokens:1024} + $x' "$OUT/msg.$k.json")
    sleep "${BENCH_SLEEP:-0}"   # Groq's free tier caps input tokens per minute
    t0=$(date +%s%3N)
    resp=$(curl -s -m 30 "$url" -H "Authorization: Bearer $key" -H 'Content-Type: application/json' -d "$body")
    ms=$(( $(date +%s%3N) - t0 ))
    text=$(jq -r '.choices[0].message.content // empty' <<<"$resp" 2>/dev/null | sed 's/<think>.*<\/think>//g')
    err=$(jq -r '.error.message // .error // empty' <<<"$resp" 2>/dev/null | head -c 160)
    gated=$(printf '%s' "$text" | python3 -I "$ROOT/libexec/voxtype_pp_gate.py" constrain-check "$(cat "$OUT/raw.$k.txt")" 2>/dev/null); grc=$?
    jq -nc --argjson k "$k" --argjson ms "$ms" --arg text "$text" --arg gated "$gated" --argjson grc "$grc" --arg err "$err" \
      '{k:$k, ms:$ms, text:$text, gated:$gated, rejected:($grc != 0), err:$err}' >> "$file"
  done
  jq -s --arg s "$spec" '{model:$s, n:length, errors:(map(select(.text=="")) | length),
     rejected:(map(select(.rejected)) | length),
     p50_ms:(map(.ms) | sort | .[length/2|floor]), max_ms:(map(.ms) | max),
     restored:(map(select(.text != "" and .gated != .text and (.rejected|not))) | length)}' "$file"
done
echo "outputs: $OUT"
