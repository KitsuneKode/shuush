# Stock voxtype or the companion fork

Shuush runs on stock [voxtype](https://github.com/peteonrails/voxtype) (1.3 or newer): install `voxtype-bin`, run `shuush wire-config`, and everything in the left column works. A few features need changes inside voxtype itself; they live in the [`kitsune/live`](https://github.com/KitsuneKode/voxtype/tree/kitsune/live) branch of a fork that tracks upstream, and are being offered upstream (see below). Shuush detects what it is running on: without the fork's extras it falls back on its own, and nothing breaks.

| Works on stock voxtype | Needs the companion fork |
|---|---|
| LLM cleanup and the faithfulness gate | Live text in the focused field while you speak (`[output] preedit`) |
| Context from the app, screen, field, selection and clipboard | Learning from your hand fixes in Electron apps (VS Code, Slack, T3 Code…) that hide their text from AT-SPI |
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
preedit = true                 # live text in the focused field

[output.post_process]
streaming = true

[parakeet]
streaming = true
streaming_final_pass = true
vocabulary_file = "~/.config/voxtype/vocabulary.txt"
corrections_file = "~/.config/voxtype/corrections.tsv"
```
