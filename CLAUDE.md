# Shuush — Local GPU Voice Dictation Companion Layer on voxtype

## Architecture

Shuush is an MIT-licensed Linux/Wayland intelligence, context, safety, and UI layer built on top of `voxtype` (and optionally enhanced by the companion `~/Projects/voxtype-fork` build).

### Repository Layout
- `bin/shuush` — User CLI (`studio`, `doctor`, `caps`, `wire` / `unwire`, `recover`, `build-wtype`, `build-ime`, `toggle`, `learn`, `bench`, `services`, `uninstall`)
- `ime/` — `shuush-ime`, the only compiled part (Rust; libc, serde_json, wayland-rs): the fork's input method as a helper the toggle starts for a take (`PP_IME=shuush`, off by default). Live words as preedit from a live-text source, the final text in one commit, `field_text.json`, `live_in_field` / `sensitive_field`. Control socket `$XDG_RUNTIME_DIR/shuush/ime.sock`; exits when idle. Test with `cargo test` (fakes and a private in-process compositor, never the live seat) and `cargo clippy --all-targets`
- `install.sh` — Zero-sudo installer & live-sync script (`~/.local/share/shuush`, `~/.local/bin/shuush`, systemd user services, KitsuneHud OSD)
- `libexec/`
  - `shuush_caps.py` / `shuush-caps` — Capability probe: what the daemon's own voxtype binary supports (`config schema --json`, `--help` probes), cached per binary path + mtime in `$XDG_RUNTIME_DIR/shuush/caps.json`. Every use of a fork-only feature asks it (`shuush-caps has CAP`) and degrades on stock
  - `shuush_wire.py` — `shuush wire` / `unwire` / `wire --status`: writes Shuush's keys with `voxtype config set`, records old and new values in `~/.config/shuush/wired.json`, restores them exactly. Never hand-edit config.toml (profiles are the one exception: voxtype has no settable profile key)
  - `shuush-build-wtype.sh` + `wtype-key-gap.patch` — build/check the patched wtype
  - `shuush-build-ime.sh` — build/check `shuush-ime` (`cargo build --release --locked`, into `~/.local/bin`)
  - `shuush_bench.py` / `shuush_bench_data.py` — `shuush bench`: read-aloud prompts, `record`/`import`/`public` clips, `run` (offline `voxtype transcribe` per model and option set, scored for WER, names, numbers, negations, last-word loss, peak VRAM; `--cleanup` only on request), `models` (Hex/voxtype candidates). Docs: `docs/accuracy-bench.md`. Never runs the daemon, never writes `~/.config/voxtype`
  - `voxtype-toggle.sh` — Push-to-talk / tap-for-handsfree / command-mode launcher; captures target window, primary selection, clipboard baseline, and background AT-SPI/kitty screen+draft context at record start
  - `voxtype-postprocess.sh` — Cleanup pipeline (`[output.post_process]`), DATA-framed prompt, failover chain (`local`, `groq`, `groq2`, `cerebras`, `opencode`, `openrouter`), budget control, streaming support, clipboard leak guard, and history/review dispatch
  - `voxtype_pp_gate.py` — Single-call deterministic `prepare` batch (privacy guard, hallucination strip, snippets, sure-name `hard_apply`, secret redaction, XML tag neutralization, screen terms, hints), anti-paraphrase span gate (`constrain` / `constrain-check` with negation, digit, numeric-value, and first-person invariants), vocabulary `finish`, and `fallback_clean`
  - `voxtype_pp_stream.py` — Sentence-level SSE streaming aligner + span gate + invariant guard
  - `voxtype_screen_ctx.py` — Bounded AT-SPI2 reader for focused draft text, text before the caret (smart join), visible screen text, the focused field (edit watcher), and `password-text` privacy detection
  - `voxtype-llm-proxy.py` — Persistent warm-TLS HTTP proxy with multi-key 429 failover
  - `voxtype_learn.py` — Grounded phonetic self-learning (`corrections.tsv`, `learn-candidates.json`, `learn-rejected.json`, `config.toml` `[text]` replacement sync)
  - `voxtype-review.py` — History writer (`history.jsonl`), swap tally, starts the edit watcher, review notifications
  - `voxtype_edit_watch.py` — Re-reads the target field for 5 min after a take; names you fix by hand go to `voxtype_learn.tally` (`PP_WATCH_EDITS`)
  - `voxtype-studio.py` — Native GTK4 + Libadwaita Studio (History + overlay Inspector, Insights, Learning Inbox, Vocabulary, Snippets, Settings)
  - `voxtype-live-osd.sh` / `voxtype-outline.sh` / `voxtype-waybar.sh` / `voxtype-cancel.sh` — OSD watcher, Hyprland border indicator, Waybar widget, cancel helper
- `osd/kitsune-hud/` — Quickshell HUD pill (`KitsuneHud.qml`, `voxtype-osd.toml`)
- `config/` — `postprocess.env.example` and starter config templates
- `tests/test_sandbox.sh` — Isolated multi-environment sandbox test suite (never touches live `~/.config/voxtype`)
- `packaging/aur/shuush-git/` — AUR `PKGBUILD`, `.SRCINFO`, install script
- `web/` — Astro marketing & documentation site (`https://shuush.kitsunekode.in`, deployed on Cloudflare Pages)

### Companion Fork (`~/Projects/voxtype-fork`)
Branch `kitsune/pill-preview` adds the live pill preview (`streaming_commit = "on_stop"`), the Parakeet full-context final pass, CUDA idle VRAM unload, and non-spinning ONNX threads. `kitsune/hex-asr` builds on it with the Hex-derived ASR work (final-pass padding, pause limiting in the live stream, acoustic vocabulary rescoring from `vocabulary.txt` + `corrections.tsv`); `kitsune/live` adds the input-method-v2 live preedit (`[output] preedit`, `preedit` cargo feature) and media restore that survives players re-creating their stream; the installed build comes from `kitsune/live`. Build the GPU variant with `CARGO_TARGET_DIR=target/cpu` too when adding the `preedit` feature (the GPU `target/` would rebuild whisper-rs-sys, which needs libclang). Build with the cached `target/` dirs only (no libclang here) and `ORT_DYLIB_PATH` pointing at `~/.local/lib/voxtype-fork/libonnxruntime.so` for tests.

### Testing
Run the full isolated test suite before committing:
```bash
bash tests/test_sandbox.sh
python3 -m pytest -q tests
```
Nothing in the tests runs the real voxtype: `tests/fixtures/fake-voxtype` answers from the real stock and fork `config schema --json` keys (`tests/fixtures/schema-*.json`), and the scripts reach it through `SHUUSH_VOXTYPE_BIN`.
