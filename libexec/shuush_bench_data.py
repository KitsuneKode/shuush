#!/usr/bin/env python3
"""Curated dictation accuracy benchmark prompts and Hex/voxtype model catalog.

Prompts contain zero personal or secret content: they exercise public desktop,
development, numeric, negation, trailing-word, and question patterns drawn from
the owner's tool stack and vocabulary categories.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchPrompt:
    id: str
    category: str
    text: str
    terms: tuple[str, ...] = ()
    is_question: bool = False


# 42 short read-aloud prompts across 6 categories (7 per category).
PROMPTS: tuple[BenchPrompt, ...] = (
    # --- names (7): desktop, compositor, tools, AI & web brands ---
    BenchPrompt(
        id="names-01",
        category="names",
        text="Restart Hyprland and check whether Waybar and Quickshell come back cleanly.",
        terms=("Hyprland", "Waybar", "Quickshell"),
    ),
    BenchPrompt(
        id="names-02",
        category="names",
        text="Compare voxtype with Parakeet against Wispr Flow on long coding dictations.",
        terms=("voxtype", "Parakeet", "Wispr Flow"),
    ),
    BenchPrompt(
        id="names-03",
        category="names",
        text="Open Ghostty and attach kittymux before launching T3 Code on Omarchy.",
        terms=("Ghostty", "kittymux", "T3 Code", "Omarchy"),
    ),
    BenchPrompt(
        id="names-04",
        category="names",
        text="Ask Devin, Codex, and Claude Code to review the kuwai pull request.",
        terms=("Devin", "Codex", "Claude Code", "kuwai"),
    ),
    BenchPrompt(
        id="names-05",
        category="names",
        text="Switch the cleanup failover from Groq to Ollama or OpenRouter when rate limited.",
        terms=("Groq", "Ollama", "OpenRouter"),
    ),
    BenchPrompt(
        id="names-06",
        category="names",
        text="PipeWire kept Cider paused while ZapZap notifications popped up over Wayland.",
        terms=("PipeWire", "Cider", "ZapZap", "Wayland"),
    ),
    BenchPrompt(
        id="names-07",
        category="names",
        text="Deploy the Shuush preview to Vercel and mirror the static docs on Netlify.",
        terms=("Shuush", "Vercel", "Netlify"),
    ),
    # --- code_terms (7): code vocabulary, frameworks, CLI & acronyms ---
    BenchPrompt(
        id="code-01",
        category="code_terms",
        text="Upgrade the Next.js and React components to use the latest shadcn dialog.",
        terms=("Next.js", "React", "shadcn"),
    ),
    BenchPrompt(
        id="code-02",
        category="code_terms",
        text="Run cargo test in the Rust repo before opening the GitHub PR.",
        terms=("cargo", "Rust", "repo", "GitHub", "PR"),
    ),
    BenchPrompt(
        id="code-03",
        category="code_terms",
        text="Check the TypeScript API types and polish the TUI and UX layout.",
        terms=("TypeScript", "API", "TUI", "UX"),
    ),
    BenchPrompt(
        id="code-04",
        category="code_terms",
        text="Enable CUDA on ONNX Runtime so the streaming final pass stays responsive.",
        terms=("CUDA", "ONNX", "streaming"),
    ),
    BenchPrompt(
        id="code-05",
        category="code_terms",
        text="AT-SPI reads focused draft text unless systemd restarts the accessibility bus.",
        terms=("AT-SPI", "systemd"),
    ),
    BenchPrompt(
        id="code-06",
        category="code_terms",
        text="Create isolated git worktrees so parallel agents never touch the same branch.",
        terms=("worktrees",),
    ),
    BenchPrompt(
        id="code-07",
        category="code_terms",
        text="Capture a screenshot of the Razorpay checkout UI and verify the CI checks.",
        terms=("screenshot", "Razorpay", "UI", "CI"),
    ),
    # --- numbers (7): HTTP codes, timings, VRAM, versions, counts ---
    BenchPrompt(
        id="num-01",
        category="numbers",
        text="The webhook returned 404 twice before succeeding with status 200.",
        terms=(),
    ),
    BenchPrompt(
        id="num-02",
        category="numbers",
        text="Keep VRAM under 6 GB because the laptop GPU leaves about 4.5 GB free.",
        terms=("VRAM",),
    ),
    BenchPrompt(
        id="num-03",
        category="numbers",
        text="Median cleanup latency dropped from 516 ms to 320 ms across 30 takes.",
        terms=(),
    ),
    BenchPrompt(
        id="num-04",
        category="numbers",
        text="Set release tail to 400 ms and paste anything over 300 characters.",
        terms=(),
    ),
    BenchPrompt(
        id="num-05",
        category="numbers",
        text="Port 11435 serves the local proxy while port 11434 talks to Ollama.",
        terms=("Ollama",),
    ),
    BenchPrompt(
        id="num-06",
        category="numbers",
        text="Bump the vocabulary boost from 2.5 to 3.2 and test 16 kHz mono audio.",
        terms=(),
    ),
    BenchPrompt(
        id="num-07",
        category="numbers",
        text="Our target is 8.5 out of 10 with under 2.5 percent word error rate.",
        terms=(),
    ),
    # --- negations (7): polarity preservation traps ---
    BenchPrompt(
        id="neg-01",
        category="negations",
        text="Do not restart the live voxtype service and never edit the production config.",
        terms=("voxtype",),
    ),
    BenchPrompt(
        id="neg-02",
        category="negations",
        text="I can't reproduce the lag when streaming is off and nothing else is running.",
        terms=("streaming",),
    ),
    BenchPrompt(
        id="neg-03",
        category="negations",
        text="This branch doesn't modify postprocess and won't break existing sandbox tests.",
        terms=(),
    ),
    BenchPrompt(
        id="neg-04",
        category="negations",
        text="We shouldn't download multi-gigabyte models without checking free disk space first.",
        terms=(),
    ),
    BenchPrompt(
        id="neg-05",
        category="negations",
        text="The toggle didn't fire because no audio device was active at startup.",
        terms=("toggle",),
    ),
    BenchPrompt(
        id="neg-06",
        category="negations",
        text="It isn't a Whisper bug; neither CUDA nor Vulkan dropped the final word.",
        terms=("Whisper", "CUDA", "Vulkan"),
    ),
    BenchPrompt(
        id="neg-07",
        category="negations",
        text="Please don't push the commit yet because I haven't reviewed the diff.",
        terms=(),
    ),
    # --- last_word (7): end-of-utterance truncation & clipping traps ---
    BenchPrompt(
        id="last-01",
        category="last_word",
        text="Check the component styling right after we upgrade shadcn.",
        terms=("shadcn",),
    ),
    BenchPrompt(
        id="last-02",
        category="last_word",
        text="Run the offline accuracy suite directly through voxtype.",
        terms=("voxtype",),
    ),
    BenchPrompt(
        id="last-03",
        category="last_word",
        text="Keep every parallel agent isolated inside separate git worktrees.",
        terms=("worktrees",),
    ),
    BenchPrompt(
        id="last-04",
        category="last_word",
        text="Reload the status pill whenever we tweak Quickshell.",
        terms=("Quickshell",),
    ),
    BenchPrompt(
        id="last-05",
        category="last_word",
        text="Attach the background session right inside kittymux.",
        terms=("kittymux",),
    ),
    BenchPrompt(
        id="last-06",
        category="last_word",
        text="Save the JSON summary as soon as we finish the benchmark.",
        terms=(),
    ),
    BenchPrompt(
        id="last-07",
        category="last_word",
        text="Verify that Cursor and Zed stay fast after the commit is rebased.",
        terms=("Cursor", "Zed"),
    ),
    # --- questions (7): interrogative syntax, punctuation, mixed difficulty ---
    BenchPrompt(
        id="q-01",
        category="questions",
        text="Does acoustic vocabulary rescoring actually fix Hyprland and Waybar?",
        terms=("Hyprland", "Waybar"),
        is_question=True,
    ),
    BenchPrompt(
        id="q-02",
        category="questions",
        text="Why did Parakeet drop the last word when padding was set to zero?",
        terms=("Parakeet",),
        is_question=True,
    ),
    BenchPrompt(
        id="q-03",
        category="questions",
        text="Can Cohere Transcribe fit inside 6 GB of VRAM alongside our desktop?",
        terms=("Cohere", "VRAM"),
        is_question=True,
    ),
    BenchPrompt(
        id="q-04",
        category="questions",
        text="Should we test Whisper large-v3-turbo with an initial prompt for code terms?",
        terms=("Whisper",),
        is_question=True,
    ),
    BenchPrompt(
        id="q-05",
        category="questions",
        text="Did the PRs in GitHub pass CI without touching the main checkout?",
        terms=("PRs", "GitHub", "CI"),
        is_question=True,
    ),
    BenchPrompt(
        id="q-06",
        category="questions",
        text="Where does Shuush save the recorded WAV clips and benchmark reports?",
        terms=("Shuush",),
        is_question=True,
    ),
    BenchPrompt(
        id="q-07",
        category="questions",
        text="What is the WER difference between fp16 and fp32 on 25 clips?",
        terms=("WER",),
        is_question=True,
    ),
)

PROMPT_CATEGORIES: tuple[str, ...] = (
    "names",
    "code_terms",
    "numbers",
    "negations",
    "last_word",
    "questions",
)


@dataclass(frozen=True)
class ModelCandidate:
    """One ASR model candidate from Hex / voxtype with 6 GB VRAM guidance."""

    id: str
    display_name: str
    hex_model: bool
    engine: str
    model_ref: str          # Model name/filename in ~/.local/share/voxtype/models/
    backend: str            # "onnx-cuda", "whisper-cuda", "whisper-vulkan", "gguf-vulkan", "unsupported"
    binary_kind: str        # "fork", "stock-vulkan", "both", "rebuild", "unsupported"
    disk_mb: int
    vram_gb: str            # Estimated GPU VRAM footprint (or "CPU / N/A")
    fits_6gb: str           # "yes", "tight", "no", "n/a"
    supports_streaming: bool
    supports_vocab_bias: str  # "rescore", "initial_prompt", "none"
    fetch_cmd: str
    notes: str
    gguf_arch: str = ""
    language: str = "en"


# Ordered by relevance for English dictation on a 6 GB NVIDIA RTX 4050 Laptop GPU.
MODEL_CATALOG: tuple[ModelCandidate, ...] = (
    ModelCandidate(
        id="parakeet-unified-fp16",
        display_name="Parakeet Unified EN 0.6B (fp16 ONNX)",
        hex_model=True,
        engine="parakeet",
        model_ref="parakeet-unified-en-0.6b-fp16",
        backend="onnx-cuda",
        binary_kind="fork",
        disk_mb=1255,
        vram_gb="~2.3 GB",
        fits_6gb="yes",
        supports_streaming=True,
        supports_vocab_bias="rescore",
        fetch_cmd="Already installed (~/.local/share/voxtype/models/parakeet-unified-en-0.6b-fp16)",
        notes="Current daily driver; streaming + final pass + fork acoustic vocabulary rescoring.",
    ),
    ModelCandidate(
        id="parakeet-unified-fp32",
        display_name="Parakeet Unified EN 0.6B (fp32 ONNX)",
        hex_model=True,
        engine="parakeet",
        model_ref="parakeet-unified-en-0.6b",
        backend="onnx-cuda",
        binary_kind="fork",
        disk_mb=2456,
        vram_gb="~3.3 GB",
        fits_6gb="yes",
        supports_streaming=True,
        supports_vocab_bias="rescore",
        fetch_cmd="voxtype setup --download --model parakeet-unified-en-0.6b",
        notes="Full-precision reference weights for Parakeet Unified; +1.0 GB VRAM over fp16.",
    ),
    ModelCandidate(
        id="parakeet-tdt-v2",
        display_name="Parakeet TDT 0.6B v2 (EN fp32 ONNX)",
        hex_model=True,
        engine="parakeet",
        model_ref="parakeet-tdt-0.6b-v2",
        backend="onnx-cuda",
        binary_kind="fork",
        disk_mb=2400,
        vram_gb="~3.2 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="voxtype setup --download --model parakeet-tdt-0.6b-v2",
        notes="Hex English-only TDT v2; offline batch only (streaming=false, no fork vocab rescoring).",
    ),
    ModelCandidate(
        id="parakeet-tdt-v2-int8",
        display_name="Parakeet TDT 0.6B v2 (EN int8 ONNX)",
        hex_model=True,
        engine="parakeet",
        model_ref="parakeet-tdt-0.6b-v2-int8",
        backend="onnx-cuda",
        binary_kind="fork",
        disk_mb=640,
        vram_gb="~1.2 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="voxtype setup --download --model parakeet-tdt-0.6b-v2-int8",
        notes="Quantized EN TDT v2; low disk/VRAM footprint; offline batch only.",
    ),
    ModelCandidate(
        id="parakeet-tdt-v3",
        display_name="Parakeet TDT 0.6B v3 (25 langs fp32 ONNX)",
        hex_model=True,
        engine="parakeet",
        model_ref="parakeet-tdt-0.6b-v3",
        backend="onnx-cuda",
        binary_kind="fork",
        disk_mb=2600,
        vram_gb="~3.4 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="voxtype setup --download --model parakeet-tdt-0.6b-v3",
        notes="Hex multilingual v3; can drift to non-English on short utterances (e.g. 'hello' -> 'Hallo').",
    ),
    ModelCandidate(
        id="parakeet-tdt-v3-int8",
        display_name="Parakeet TDT 0.6B v3 (25 langs int8 ONNX)",
        hex_model=True,
        engine="parakeet",
        model_ref="parakeet-tdt-0.6b-v3-int8",
        backend="onnx-cuda",
        binary_kind="fork",
        disk_mb=670,
        vram_gb="~1.3 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="voxtype setup --download --model parakeet-tdt-0.6b-v3-int8",
        notes="Quantized multilingual TDT v3; offline batch only.",
    ),
    ModelCandidate(
        id="parakeet-ultra-q8",
        display_name="Parakeet Ultra Q8_0 (GGUF Vulkan)",
        hex_model=True,
        engine="parakeet",
        model_ref="parakeet-ultra-Q8_0.gguf",
        backend="gguf-vulkan",
        binary_kind="stock-vulkan",
        disk_mb=740,
        vram_gb="~1.1 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="/usr/lib/voxtype/voxtype-vulkan setup --download --model parakeet-ultra-Q8_0.gguf",
        notes="25-language GGUF Parakeet via voxtype-gguf-worker-vulkan; offline only.",
    ),
    ModelCandidate(
        id="whisper-large-v3-turbo",
        display_name="Whisper large-v3-turbo",
        hex_model=True,
        engine="whisper",
        model_ref="ggml-large-v3-turbo.bin",
        backend="whisper-vulkan",
        binary_kind="both",
        disk_mb=1600,
        vram_gb="~2.2 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="initial_prompt",
        fetch_cmd="voxtype setup --download --model large-v3-turbo",
        notes="Hex pick; reads vocabulary via whisper.initial_prompt. Fast on /usr/lib/voxtype/voxtype-vulkan (fork binary lacks whisper CUDA).",
    ),
    ModelCandidate(
        id="cohere-transcribe-q4",
        display_name="Cohere Transcribe 03-2026 Q4_K_M (GGUF)",
        hex_model=True,
        engine="cohere",
        model_ref="cohere-transcribe-03-2026-Q4_K_M.gguf",
        backend="gguf-vulkan",
        binary_kind="stock-vulkan",
        disk_mb=1558,
        vram_gb="~2.3 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="/usr/lib/voxtype/voxtype-vulkan setup --download --model cohere-transcribe-03-2026-Q4_K_M.gguf",
        notes="Hex 'fewest wrong words' (2B encoder-decoder, 14 langs); runs via /usr/lib/voxtype/voxtype-vulkan + gguf-worker-vulkan.",
    ),
    ModelCandidate(
        id="cohere-transcribe-q8",
        display_name="Cohere Transcribe 03-2026 Q8_0 (GGUF)",
        hex_model=True,
        engine="cohere",
        model_ref="cohere-transcribe-03-2026-Q8_0.gguf",
        backend="gguf-vulkan",
        binary_kind="stock-vulkan",
        disk_mb=2411,
        vram_gb="~3.4 GB",
        fits_6gb="tight",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="/usr/lib/voxtype/voxtype-vulkan setup --download --model cohere-transcribe-03-2026-Q8_0.gguf",
        notes="Higher-precision Cohere Transcribe GGUF; fits 6 GB alone (~1.1 GB desktop headroom) but tight alongside Ollama.",
    ),
    ModelCandidate(
        id="qwen3-asr-0.6b",
        display_name="Qwen3-ASR 0.6B Q8_0 (GGUF)",
        hex_model=True,
        engine="gguf",
        model_ref="Qwen3-ASR-0.6B-Q8_0.gguf",
        backend="gguf-vulkan",
        binary_kind="stock-vulkan",
        disk_mb=850,
        vram_gb="~1.4 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="/usr/lib/voxtype/voxtype-vulkan setup --download --model Qwen3-ASR-0.6B-Q8_0.gguf",
        notes="Hex multilingual pick; voxtype gguf engine runs it offline (no vocab prompt wiring in voxtype yet).",
        gguf_arch="qwen3_asr",
    ),
    ModelCandidate(
        id="qwen3-asr-1.7b",
        display_name="Qwen3-ASR 1.7B Q8_0 (GGUF)",
        hex_model=False,
        engine="gguf",
        model_ref="Qwen3-ASR-1.7B-Q8_0.gguf",
        backend="gguf-vulkan",
        binary_kind="stock-vulkan",
        disk_mb=2185,
        vram_gb="~3.2 GB",
        fits_6gb="tight",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="/usr/lib/voxtype/voxtype-vulkan setup --download --model Qwen3-ASR-1.7B-Q8_0.gguf",
        notes="Strong Open ASR leaderboard score (5.75 avg WER); offline via stock Vulkan binary.",
        gguf_arch="qwen3_asr",
    ),
    ModelCandidate(
        id="nemotron-3.5-asr",
        display_name="Nemotron 3.5 ASR 0.6B Q8_0 (GGUF)",
        hex_model=True,
        engine="gguf",
        model_ref="nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
        backend="gguf-vulkan",
        binary_kind="stock-vulkan",
        disk_mb=751,
        vram_gb="~1.2 GB",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="/usr/lib/voxtype/voxtype-vulkan setup --download --model nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
        notes="Hex pick; Hex benchmarks put it behind Parakeet v3 (3.05% vs 1.95% LibriSpeech test-clean).",
        gguf_arch="parakeet",
        language="en-US",
    ),
    ModelCandidate(
        id="granite-speech-5.0",
        display_name="IBM Granite Speech 5.0 (470M TurboCTC / 2B)",
        hex_model=True,
        engine="unsupported",
        model_ref="",
        backend="unsupported",
        binary_kind="unsupported",
        disk_mb=500,
        vram_gb="~1.0 GB (470M) / ~4.5 GB (2B fp16)",
        fits_6gb="yes",
        supports_streaming=False,
        supports_vocab_bias="none",
        fetch_cmd="Not supported natively in voxtype (neither ONNX nor gguf-worker catalog); use remote OpenAI-compat ASR server",
        notes="Hex added 470M TurboCTC but noted Parakeet beat it on English accuracy & speed; 2B has no GGUF port yet.",
    ),
)

MODEL_BY_ID: dict[str, ModelCandidate] = {m.id: m for m in MODEL_CATALOG}
