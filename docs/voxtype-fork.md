# Stock voxtype or the companion fork

Shuush runs on stock [voxtype](https://github.com/peteonrails/voxtype) (1.3 or newer): install `voxtype-bin`, run `shuush wire`, and everything in the left column works. A few features need changes inside voxtype itself; they live in the [`kitsune/live`](https://github.com/KitsuneKode/voxtype/tree/kitsune/live) branch of a fork that tracks upstream, and are being offered upstream (see below). Shuush detects what it is running on: without the fork's extras it falls back on its own, and nothing breaks.

## What the daemon can do: `shuush caps`

`shuush caps` asks the daemon's own binary (found from its pid file, then `voxtype.service`'s `ExecStart`, then `PATH`; the `voxtype` on `PATH` can be another build) what it supports, from `voxtype config schema --json` (every key it can set), a few `--help` probes and the strings in the binary for files no schema lists. The answer is cached in `$XDG_RUNTIME_DIR/shuush/caps.json` by binary path, size and mtime, so it is asked again only when the binary changes. The probe runs with an empty throwaway config directory: it reads none of yours and writes nothing.

```sh
shuush caps                       # everything, as JSON
shuush caps list                  # one line per capability, on/off
shuush caps has queue_dictations  # exit 0 when on (what the scripts use)
shuush doctor                     # prints the same table for the running daemon
```

Every place that leans on a fork-only feature checks it and does what stock voxtype does when it is off: queued dictation (`queue_dictations`), the native release tail (`release_tail_ms`), skipping a cleanup that is running (`skip_post_process`: on stock the press is ignored), cleanup streaming (`post_process_streaming`), the input method's field marker (`read_field` / `preedit`), replay (`last_take`) and `shuush recover` (`record_recover`). The HUD reads voxtype's own `recording.json` and `outcome.json` and keeps the fork's `session_start` and `output_done` as a fallback.

The patched wtype (fast, exact typing) is built by Shuush now, not by the fork: `shuush build-wtype`, check it with `shuush build-wtype --check`.

| Works on stock voxtype | Needs the companion fork |
|---|---|
| LLM cleanup and the faithfulness gate | Live text in the focused field while you speak (`[output] preedit`) |
| Context from the app, screen, field, selection and clipboard | Learning from your hand fixes in Electron apps (VS Code, Slack, T3 Code…) that hide their text from AT-SPI (`read_field = true`) |
| Learning, snippets, privacy guard, Studio, history | Streamed cleanup: long dictations typed sentence by sentence (`streaming = true`) |
| Smart join (default profile) | Smart join in the `terminal` / `chat` profiles |
| The KitsuneHud pill | Full-context final pass, end padding and pause handling for Parakeet streaming |
| | Acoustic vocabulary rescoring (your `vocabulary.txt` and corrections at the recognition level) |
| | Media you paused or ducked always comes back at full volume |

## Upstream status

Fixes that stand on their own are open as pull requests against voxtype's `dev` branch; the larger features build on a deferred-commit streaming mode that upstream does not have yet, and are proposed in an issue first:

- [#1128](https://github.com/peteonrails/voxtype/pull/1128): restore ducked media when the player re-creates its stream
- [#1129](https://github.com/peteonrails/voxtype/pull/1129): profiles honour `[output.post_process] trim`
- [#1130](https://github.com/peteonrails/voxtype/pull/1130): skip post-processing for a take of only filler words
- [#1131](https://github.com/peteonrails/voxtype/issues/1131) (issue): deferred streaming commit with a full-context final pass, Parakeet final-pass accuracy, live preedit

When these land upstream, the right-hand column moves to the left and the fork is no longer needed.

## Installing the fork

It builds like voxtype (Rust, `clang`, `alsa-lib`); see voxtype's `CONTRIBUTING.md`.

```sh
git clone -b kitsune/live https://github.com/KitsuneKode/voxtype voxtype-fork
cd voxtype-fork
cargo build --release --features parakeet-cuda,parakeet-load-dynamic,onnx-load-dynamic,preedit   # NVIDIA
# CPU: --features parakeet,parakeet-load-dynamic,onnx-load-dynamic,preedit
```

The `*-load-dynamic` features load ONNX Runtime at run time, so install it as well (on Arch: `onnxruntime-cpu` or `onnxruntime-cuda`), or point `ORT_DYLIB_PATH` at a `libonnxruntime.so` (1.24 or newer).

Point your `voxtype.service` at `target/release/voxtype`, then add what you want to `config.toml`:

```toml
[output]
streaming_commit = "on_stop"   # preview while you speak, type once at the end
read_field = true              # read the focused field (smart join, learning from your fixes)
# preedit = true               # optional: also show the live text inside the field

[output.post_process]
streaming = true

[parakeet]
streaming = true
streaming_final_pass = true
vocabulary_file = "~/.config/voxtype/vocabulary.txt"
corrections_file = "~/.config/voxtype/corrections.tsv"
```
