# Handoff — streaming preview spike (approved 2026-10-10)

**Type: SPIKE.** The output is *answers and numbers*, plus a runner that may
become the seed of the Phase 2 core. Read this, confirm the plan in 2–3
sentences, get a nod, then build. The daily-driver setup in
`~/.config/voxtype` must keep working throughout: **do not modify voxtype's
config, scripts or services.** The spike only *writes* `live_text`.

## The question
Can **`parakeet-unified-en-0.6b`** (NVIDIA cache-aware streaming
FastConformer, English, trained for both streaming and offline use) deliver:
1. live words in the dictation pill about 0.2–0.4s behind speech,
2. with offline accuracy close enough to `parakeet-tdt-0.6b-v2` (the current
   final model) that **one model could do both jobs** in the Phase 2 core,
3. at an acceptable CPU (spike) and GPU (target) cost?

## Why this model (decided, don't re-litigate)
- Target design: **one capture → one streaming model → both preview and
  final**. Streaming partials go to the pill. At stop, an offline re-pass
  with the same weights over the whole buffer → incremental cleanup per
  committed sentence → a single insert. No duplicated transcription.
  Preview and final come from the same model, so they agree.
- Rejected alternatives:
  - Whisper: not a streaming model; it invents text in pauses.
  - Kyutai STT: too big for 6 GB next to anything else.
  - sherpa-onnx Zipformer / Moonshine: kept only as the fallback preview
    model if unified-en is too heavy on CPU.
- Rejected: voxtype's own `[parakeet] streaming = true`. It types partials
  into the focused app with backspace corrections, which is risky in
  terminals and agent prompts, and not the chosen experience (live words in
  the pill only).

## Facts already established
- Machine: RTX 4050 6 GB, i5-13450HX (AVX2, no AVX-512), 23 GB RAM, CUDA
  13.4, cuDNN 9.27, Arch + Hyprland.
- Voxtype daemon: Parakeet v2 on CUDA holds **3.3 GB of VRAM**. Two such
  models don't fit, so **the spike runs unified-en on CPU** (or int8) first.
- **`audio.sock` carries no audio:** 16-byte level frames only (seq u32 +
  peak/rms/vad), ~32/s. The spike captures the mic itself (PipeWire allows a
  second capture client: `pw-record`, or `cpal` in Rust).
- The pill (`~/.config/voxtype/osd/kitsune-hud/KitsuneHud.qml`) already shows
  `$XDG_RUNTIME_DIR/voxtype/live_text` whenever it is non-empty, including
  during "Listening". Write partials there (throttle to ~10 Hz). The watcher
  clears the file at session start and at idle; post-process overwrites it
  with the final raw text during "Polishing".
- Session state: `$XDG_RUNTIME_DIR/voxtype/state` (idle | recording |
  transcribing). Treat empty reads as "no change" (torn writes). Start
  capture on idle→recording; stop on leaving recording.
- Download the model with
  `/usr/lib/voxtype/voxtype-onnx-avx2 setup --download --model parakeet-unified-en-0.6b`
  (the Vulkan `/usr/bin/voxtype` binary can't download Parakeet). It lands in
  `~/.local/share/voxtype/models/`. About 40 GB of disk is free.
- Voxtype's CUDA ONNX Runtime libs are in `/usr/lib/voxtype/cuda-13/`; they
  may help if the runner later needs the CUDA execution provider.

## Plan
1. **Runner in Rust** with `parakeet-rs` (the library voxtype uses, so the
   downloaded model files load as-is), `cpal` for capture, CPU execution
   first. Put it in `shussh/spikes/streaming-preview/` (a Cargo project).
   If `parakeet-rs` can't stream this model, say so and fall back to Python
   with whatever runtime can (onnxruntime / sherpa-onnx), still CPU.
2. Run it as a foreground process or a transient user unit (not wired into
   voxtype's units). It should follow the state file and write `live_text`.
3. **Measure:**
   - **Lag:** time from a word spoken to its appearance. Use TTS clips
     played through a loopback, plus the user's own voice.
   - **Stability:** how often already-shown words change (flicker), and
     how far back.
   - **Accuracy:** WER of the unified-en streaming transcript, and of its
     offline pass, vs v2 on the read-aloud scripts in
     `~/.config/voxtype/tests/scripts/`. Score with `voxtype-test`'s WER
     logic; v2's text is in `~/.local/state/voxtype/history.jsonl` `raw`.
   - **Cost:** CPU % on the AVX2 CPU (and VRAM if a GPU run is tried with
     voxtype temporarily on CPU via `voxtype-engine.sh cpu`; switch back
     with `voxtype-engine.sh gpu`).
4. **Report:** a table of numbers plus a go/no-go on "unified-en for
   everything", recorded in `shussh/docs/` and as an entry in
   `~/Projects/kaitai-inference/docs/journal.md` (build-in-public).

## Verification habits (from the daily-driver work)
- Visual: timed `grim` screenshots of the pill. Never `voxtype record stop`
  from a terminal (it types into it); use `record cancel`, or simulate states.
- After any QML change (none planned), grep the voxtype journal for
  `WARN scene|failed to load custom QML`.
- `voxtype-test doctor` must stay green before and after.

## Experiment A results (2026-10-10, voxtype file-mode streaming, isolated test daemon)
Setup: a test daemon (`-c` copy of the config) on a PipeWire virtual mic
(`module-null-sink` + `module-remap-source`, captured with
`PULSE_SOURCE=vt_mic`), fed espeak clips; the real service was paused.
- `parakeet-unified-en-0.6b` loads on CUDA in 1.6–2.4s. **This voxtype's
  default streaming context is invalid** ("left_context_secs must map to a
  mel-frame count divisible by 8"). It works with `streaming_chunk_secs = 0.32`,
  `streaming_left_context_secs = 5.6`, `streaming_right_context_secs = 0.32`.
  (Upstream bug report candidate.)
- **File mode writes the streamed transcript once, at stop** (~90–110ms
  after stop). No partials are exposed anywhere: not in the file, and not in
  the log even at `-vv`. So voxtype cannot feed a pill-only live preview
  without a code change. **Plan A (config + scripts) is not viable.**
- No `post_process` calls happen in streaming file mode, so cleanup is
  fully the caller's.
- `record stop --wait` did not remember the `--file` path; pass
  `--wait-file`. It also reported "empty" before the file landed (race).
  (Upstream bug report candidate.)
- **Accuracy (60s, 178-word clip): v2 offline 0.0% WER; unified-en
  STREAMING 1.1%** ("land"→"end" ×2). Streaming is close to offline, so the
  single-model design is promising. Must be confirmed with real voice.
