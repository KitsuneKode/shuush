# Handoff — Phase 1 kickoff (from the voxtype stabilization session, 2026-10-10)

Read this first. It replaces "PARKED" in CLAUDE.md. The 2026-03-31 spec in
`docs/specs/` is a starting point, not the plan: it predates the decisions below
(it is CUDA-only, uses fixed whole-file transcription, and has no live preview).

## Where we are

- **Daily driver today:** voxtype 1.1.0 with a personal layer on top. The full
  state, the lessons learned the hard way, and how to verify it are in
  `~/.config/voxtype/ARCHITECTURE.md`. Read its "Phase 0 changes" and
  "Hard-won failure modes" sections.
- **Hardware:** RTX 4050 Laptop (6 GB VRAM), AVX2 CPU (no AVX-512), Arch +
  Hyprland (HyDE), Waybar. Display is 2560x1440 at 1.6 scale. The CUDA 13.4
  toolkit is installed. **cuDNN is not installed yet**: run `sudo pacman -S cudnn`
  (lowercase). The ONNX CUDA variant needs `libcudnn.so.9`.
- **Models on disk:** Parakeet TDT 0.6B v3 is the **active engine** (CPU ONNX
  for now, through a systemd drop-in). The whisper large-v3-turbo file was
  deleted in a disk cleanup, so re-download it for the Phase 1 baseline.
  The user dictates in **English only** (decided 2026-10-10), so question 1
  below is answered.

## Decisions already made (don't re-ask)

| Topic | Decision |
|---|---|
| Goal | The user's daily driver first, built as open source for the Omarchy/Linux community (aim: sponsorable). Monetization last. |
| Fork vs new | **New Rust project.** Borrow from voxtype (MIT, with attribution) for text injection, hotkeys and compositor glue. |
| Hardware scope | Never require CUDA. Support CUDA, Vulkan and CPU, so AMD/Intel Omarchy laptops work. The 4050 is the fast path. |
| Streaming UX | Live draft in the OSD only (a display-only preview pass). At stop, run a full-context final pass, clean it up, and insert it once. Never insert preview text. |
| Cleanup | Hybrid: a local small LLM by default, cloud bring-your-own-key (Groq or any OpenAI-compatible API) as an opt-in, and deterministic rules as the fallback so text is never lost. |
| Cleanup UX | The diff animation (fillers struck out, punctuation popping in) must never add latency. Cleaned text is the default. Offer an "undo cleanup" hotkey and pill click instead of choosing raw vs cleaned up front. History keeps both versions. |
| v1 scope | Core dictation, command mode, long-form/notes, and context awareness. The persistent island is optional (draggable, or embeddable in Waybar/Quickshell) and built **last**. The user has their own vision for its placement in upcoming dots. |
| Phase order | 0 stabilize voxtype (done) → **1 eval set + benchmarks** → 2 Rust core → 3 cleanup/command/context → 4 long-form → 5 island/UI |

## Phase 1 goal

Build a personal benchmark and use it to learn inference properly, with
senior-engineer depth. Every later engine or model choice is made from these
numbers, not from leaderboards or vibes.

**Harness (sketch; confirm with the user before building):**
- **Dataset:** 30–50 of the user's own clips, each with a hand-checked
  transcript. Categories: short commands, technical jargon (Hyprland,
  Quickshell, kittymux, …), long-form (2–10 min), pauses and silence,
  self-corrections ("no wait, actually…"), noisy room, and Hinglish if the user
  speaks it.
- **Metrics:**
  - WER (word error rate)
  - Accuracy on the user's own vocabulary terms
  - Hallucination rate: text produced on silence-only clips
  - Latency (p50/p95, both stop→final and preview cadence)
  - Peak VRAM, and CPU usage
- **Speech-to-text candidates:**
  - whisper large-v3-turbo through whisper.cpp: CUDA vs Vulkan, f16/q8/q5
  - Parakeet TDT 0.6B v2 (English) and v3, through ONNX on CUDA, and on CPU at int8
  - `parakeet-unified-en-0.6b` (check whether it streams)
  - Cohere Transcribe (voxtype's README claims it is #1 on the Open ASR leaderboard; verify)
  - Moonshine and Kyutai STT, both streaming-native
- **Cleanup LLM candidates:** small instruct models (about 1.5–4B) through
  llama.cpp with CUDA. Measure faithfulness (it must not change the meaning),
  latency with and without prompt-prefix caching, and VRAM co-residency with
  the speech model on a 6 GB card.

**Learning track: read readable code first, then the fast code.**
- `openai/whisper` (Python/PyTorch, about 2k lines: `audio.py`, `model.py`,
  `decoding.py`, `transcribe.py`). Learn log-mel features, the encoder/decoder,
  beam search, and **why Whisper hallucinates**: an autoregressive decoder plus
  `condition_on_previous_text`, guarded by the `no_speech_prob` and
  compression-ratio thresholds.
- `whisper.cpp`: map its ggml graph back onto `model.py`, then compare the
  CUDA and Vulkan backends.
- NeMo Parakeet (TDT transducer): why a transducer emits blanks instead of
  inventing sentences.
- Karpathy `llama2.c` (`run.c`, about 1k lines of C) or nanoGPT: KV cache,
  sampling, quantization. Then llama.cpp: offloading layers to the GPU (`-ngl`),
  flash attention, quantization types, slots and prompt caching.
- `transformers.js` / whisper-web (TypeScript): a readable end-to-end pipeline,
  if TypeScript reads more easily for the user.
- **Exercises to propose:** implement log-mel in numpy and diff it against
  Whisper's; reproduce the "Thank you." hallucination on a silence clip, then
  kill it with VAD gating; measure how VRAM and latency move with quantization.

### Local inference track (the user runs local models; wants to master this from the ground up)

> **Lives in `~/Projects/kaitai-inference` (ROADMAP M1–M6).** Do the
> learning and measurement there; link the results here. The list below is
> the shuush-specific slice.

Learn with the daily driver as the lab: every concept gets measured on the
user's own 4050.

1. **Ollama, then llama.cpp.** Run the cleanup model through Ollama first
   (`ollama-cuda`, `qwen3:4b-instruct-2507-q4_K_M`). Then run the same GGUF
   in raw `llama-server` to see what Ollama hides: `-ngl` GPU layer offload,
   context size, flash attention, slots, prompt cache.
2. **GGUF and quantization.** What Q4_K_M, Q8_0 and F16 actually store.
   Measure quality (cleanup faithfulness on the eval set), size, and speed
   for each.
3. **Prefill vs decode.** Cleanup is input-heavy, so prefill (prompt
   processing) dominates. Measure tokens/s for both. Learn why the system
   prompt prefix cache helps, and why it is compute-bound vs
   memory-bandwidth-bound.
4. **KV-cache math.** Bytes per token = 2 × layers × kv_heads × head_dim ×
   dtype size. Compute it for the chosen model, then verify against
   `nvidia-smi`.
5. **VRAM budgeting.** Parakeet (ONNX/CUDA) + LLM + KV + desktop must fit in
   6 GB. Measure, then decide keep_alive/residency policy.
6. **ONNX Runtime and execution providers.** CUDA EP vs TensorRT EP vs CPU;
   what cuDNN provides; why a driver mismatch kills CUDA until reboot (a real
   incident on 2026-10-10).
7. **Profiling.** `nsight-systems` (the user tried to install it; the package
   is `nsight-systems`, and `nsight-compute` for kernels) to see where time
   goes in one dictation, end to end.
8. **Server-class concepts, read but don't deploy.** vLLM/SGLang paged
   attention, continuous batching and speculative decoding, so the user
   knows what scales beyond a laptop.

Readable code for each: `llama2.c` (`run.c`) → llama.cpp `examples/simple`
→ `llama-server`; `openai/whisper` → `whisper.cpp`; NeMo Parakeet → its ONNX
export as run by onnxruntime.

See also `docs/backlog.md`: the later-phase list, each item tied to the
failure it came from.

## Questions to ask the user (one at a time, before building)

1. **Languages:** English only, or Hinglish/Hindi too? This decides Whisper
   (99 languages, prompt biasing) vs Parakeet (no Hindi, no prompt biasing,
   but far fewer hallucinations and much faster).
2. **Android:** v1, after Linux v1, or later? And in what form: a keyboard
   IME (like FUTO Voice) or a companion app?
3. **Name:** keep "Shuush"? The folder is `shussh` and the spec says `shuush`,
   so settle the spelling.
4. **Learning mode:** does the user write the code while the agent reviews
   and explains, or does the agent scaffold while the user explains it back
   and extends it? How many hours a week?
5. **Reading language:** Python, TypeScript or C for the reference code?
   (The user asked for Python/TS reimplementations to make llama.cpp-style
   code easier to follow.)
6. **Eval data:** OK to record and keep personal audio clips locally? Where?
   (Suggestion: `~/Storage/Datasets/dictation-eval/`.)
7. **Activation:** hold-to-talk, toggle, or both? Does the user want a wake
   word (sherpa-onnx KWS, from the old spec) in v1?

## Things the user can do daily (to find out what to build)

- **Friction log:** whenever dictation does something wrong, add one line to
  `docs/friction-log.md`: date, app, what was said, what came out, and how it
  felt. Review it weekly; it becomes the backlog.
- **Check history:** `~/.local/state/voxtype/history.jsonl` has `{raw, clean}`
  for every dictation. Diffing them shows exactly where cleanup helps, and
  where it rewrites meaning.
- **Notice misheard words:** every term that comes out wrong goes into
  `replacements` in voxtype's config now, and into the eval vocabulary list
  later.
