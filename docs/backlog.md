# Backlog — what the daily driver taught us (seeded 2026-10-10)

Each item names the real limit or failure it came from. The voxtype-based
daily driver is the prototype; this project is where these become first-class.

## Core pipeline (Phase 2)

- **Our own state and event channel.** Voxtype exposes only a one-word state
  file (torn reads gave us fake new sessions), raw PCM, and the journal
  (scraping it orphaned 12 processes). Our daemon publishes typed events
  (`session_started`, `partial`, `final_raw`, `final_clean`, `inserted`,
  `error`) over a socket, and the UI and Waybar subscribe to them.
- **Two-pass ASR.** A display-only live preview while speaking, plus one
  full-context final pass at stop. Voxtype's eager mode stitched preview
  chunks into the output; never do that.
- **Overlay appears in under 100ms.** Voxtype's overlay shows about 300ms
  after the hotkey. Keep the surface alive and hidden, and show it on the
  state event.
- **Exit and "done" moment.** Voxtype hides the overlay the instant it goes
  idle. Ours shows the final text with a ✓ for about 600ms, then leaves the
  way it entered.
- **Visible errors.** "Cleanup offline, typed raw text" or "Mic unavailable"
  in the pill, never silent fallbacks.
- **Per-app output.** Type short text; paste long text (`wtype` crawls on
  long-form). Terminals need Ctrl+Shift+V. Restore the clipboard afterwards.
- **GPU-first with honest fallback.** CUDA, then Vulkan, then CPU. Detect a
  kernel/userspace driver mismatch (it happened on 2026-10-10 after a cuDNN
  install pulled in a driver update) and say "reboot to re-enable GPU"
  instead of failing.
- **VRAM budget manager.** Speech model and cleanup LLM share 6 GB with the
  desktop. Decide residency (keep_alive), unload when gaming, and report it.

## Cleanup and intelligence (Phase 3)

- **Local-first cleanup.** Small instruct LLM on GPU (candidate:
  `qwen3:4b-instruct-2507-q4_K_M`; fast fallback `qwen3:1.7b`), pre-warmed
  on recording start, with prompt-prefix caching. Cloud is opt-in.
- **Skip the LLM when the text is already clean.** This is already in the
  daily driver (`via=skip`, 22ms vs 0.3–1.1s). Next: replace the regex
  heuristic with a measured classifier.
- **Cleanup diff animation:** fillers struck through and collapsed,
  punctuation popping in. It plays after insertion and never delays text.
- **Undo-cleanup hotkey** that replaces the just-inserted text with the raw
  version (needs selection and replace in the target app).
- **Command mode:** select text, then speak an instruction ("make this a
  list").
- **Context awareness:** focused app, selected text and on-screen terms bias
  vocabulary and tone. Parakeet has no prompt biasing, so this lives in
  cleanup.
- **Personal dictionary learning:** words misheard often (from
  `voxtype-test` diffs) are promoted automatically.

## Long-form (Phase 4)

- 15+ minute sessions, chunked final passes at real pauses, crash-safe
  recording, and notes saved as markdown.
- Never cut off: the daily driver's 900s cap is a band-aid.

## UI (Phase 5)

- Island that is draggable and embeddable (Waybar/Quickshell dots), with
  multi-monitor placement that follows the focused output.
- Real reduced-motion support (read a system/Hyprland setting, not just an
  env var).
- Settings UI, model manager, AUR packaging, Omarchy-friendly defaults.

## Later

- Android (keyboard IME or companion app).
- Wake word (sherpa-onnx KWS).
- Benchmark CI: the eval set runs on every engine or prompt change.
