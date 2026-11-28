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


# 42 read-aloud prompts across 7 categories. Word, number, negation and
# question mix follows the owner's takes (35% carry a negation, 10% a digit,
# 11% are questions; median 32 words), minus anything private: only terms
# from the public part of vocabulary.txt (tools, products, public brands).
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
        text="I am comparing voxtype with Parakeet against Wispr Flow on long coding dictations.",
        terms=("voxtype", "Parakeet", "Wispr Flow"),
    ),
    BenchPrompt(
        id="names-03",
        category="names",
        text="Open Ghostty, attach kittymux, and launch T3 Code on Omarchy before I forget.",
        terms=("Ghostty", "kittymux", "T3 Code", "Omarchy"),
    ),
    BenchPrompt(
        id="names-04",
        category="names",
        text="Ask Devin and Codex to review the kuwai pull request, then tell Claude Code what they found.",
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
    # --- long (4): the owner's median take is 32 words; these run 30-45 ---
    BenchPrompt(
        id="long-01",
        category="long",
        text="The plan for tonight is to record every prompt once, run each model on the same clips, and then decide whether Parakeet is still the right default.",
        terms=("Parakeet",),
    ),
    BenchPrompt(
        id="long-02",
        category="long",
        text="I dictated a long paragraph yesterday and the last sentence lost its final word, and after I paused for a second the next one started without its first two words.",
        terms=(),
    ),
    BenchPrompt(
        id="long-03",
        category="long",
        text="Can you check whether the pull request touches the post process script? Another agent is changing it, so leave a comment instead of fixing it.",
        terms=(),
        is_question=True,
    ),
    BenchPrompt(
        id="long-04",
        category="long",
        text="The review is on Thursday afternoon, about 12 people are coming, the budget is 4500 dollars, and nobody should forward the invite until Codex finishes the checks.",
        terms=("Codex",),
    ),
)

PROMPT_CATEGORIES: tuple[str, ...] = (
    "names",
    "code_terms",
    "numbers",
    "negations",
    "last_word",
    "questions",
    "long",
)


@dataclass(frozen=True)
class ModelCandidate:
    """One ASR model: where it runs, what it costs on a 6 GB card, how to fetch it.

    Names, sizes and licences come from voxtype's own registries (`voxtype info
    models --json`, src/setup/model.rs); `hex_wer` is the figure Hex's catalog
    publishes (LibriSpeech test-clean unless `hex_note` says otherwise), shown
    for orientation only: it is not measured here and not on the owner's speech.
    """

    id: str
    display_name: str
    engine: str             # voxtype engine: parakeet | whisper | cohere | gguf | unsupported
    model_ref: str          # file or directory name inside the models dir
    runner: str             # "fork" (CUDA ONNX), "vulkan" (stock voxtype-vulkan + gguf worker), "none"
    disk_mb: int
    vram_est: str           # estimate; `bench run` measures the real peak for what it runs
    fits_6gb: str           # "yes" | "tight" | "no"
    vocab: str              # "rescore" (fork acoustic rescoring) | "prompt" (whisper initial_prompt) | "none"
    fetch_name: str         # the value for `voxtype setup --download --model`
    license: str = ""
    hex_wer: float | None = None
    hex_note: str = ""
    notes: str = ""
    gguf_arch: str = ""
    language: str = "en"
    streaming_pipeline: bool = False  # fork cache-aware streaming decode (final pass, pad, rescoring)


# Ordered by how likely each is to beat the daily driver for English dictation
# on a 6 GB NVIDIA laptop GPU. The desktop itself holds ~1.2 GB of the 6.
MODEL_CATALOG: tuple[ModelCandidate, ...] = (
    ModelCandidate(
        id="parakeet-unified-fp16",
        display_name="Parakeet Unified EN 0.6B (fp16 ONNX, CUDA)",
        engine="parakeet", model_ref="parakeet-unified-en-0.6b-fp16", runner="fork",
        disk_mb=1255, vram_est="~2.3 GB", fits_6gb="yes", vocab="rescore",
        fetch_name="(installed; fp16 conversion of parakeet-unified-en-0.6b)",
        hex_wer=1.60,
        notes="The daily driver: streaming preview, whole-recording final pass, pad, acoustic vocabulary rescoring.",
        streaming_pipeline=True,
    ),
    ModelCandidate(
        id="parakeet-unified-fp32",
        display_name="Parakeet Unified EN 0.6B (fp32 ONNX, CUDA)",
        engine="parakeet", model_ref="parakeet-unified-en-0.6b", runner="fork",
        disk_mb=2660, vram_est="~3.3 GB", fits_6gb="yes", vocab="rescore",
        fetch_name="parakeet-unified-en-0.6b",
        hex_wer=1.60,
        notes="Reference precision for the same model; shows whether fp16 costs accuracy.",
        streaming_pipeline=True,
    ),
    ModelCandidate(
        id="parakeet-ultra-q8",
        display_name="Parakeet Ultra Q8_0 (GGUF, Vulkan)",
        engine="parakeet", model_ref="parakeet-ultra-Q8_0.gguf", runner="vulkan",
        disk_mb=740, vram_est="~1.2 GB", fits_6gb="yes", vocab="none",
        fetch_name="parakeet-ultra-Q8_0.gguf",
        license="CC-BY-4.0", hex_wer=1.80,
        notes="Batch only (no streaming preview, no rescoring); half the VRAM of the daily driver.",
    ),
    ModelCandidate(
        id="parakeet-ultra-q4",
        display_name="Parakeet Ultra Q4_K_M (GGUF, Vulkan)",
        engine="parakeet", model_ref="parakeet-ultra-Q4_K_M.gguf", runner="vulkan",
        disk_mb=486, vram_est="~0.9 GB", fits_6gb="yes", vocab="none",
        fetch_name="parakeet-ultra-Q4_K_M.gguf",
        license="CC-BY-4.0", hex_wer=1.80, hex_note="Hex lists the Q8_0 file",
        notes="Smallest Parakeet Ultra; check the quantisation cost against Q8_0.",
    ),
    ModelCandidate(
        id="cohere-transcribe-q4",
        display_name="Cohere Transcribe 03-2026 Q4_K_M (GGUF, Vulkan)",
        engine="cohere", model_ref="cohere-transcribe-03-2026-Q4_K_M.gguf", runner="vulkan",
        disk_mb=1558, vram_est="~2.2 GB", fits_6gb="yes", vocab="none",
        fetch_name="cohere-transcribe-03-2026-Q4_K_M.gguf",
        license="Apache-2.0", hex_wer=1.27,
        notes="Hex's lowest English WER; encoder-decoder, no vocabulary hook; 14 languages.",
    ),
    ModelCandidate(
        id="cohere-transcribe-q8",
        display_name="Cohere Transcribe 03-2026 Q8_0 (GGUF, Vulkan)",
        engine="cohere", model_ref="cohere-transcribe-03-2026-Q8_0.gguf", runner="vulkan",
        disk_mb=2411, vram_est="~3.2 GB", fits_6gb="tight", vocab="none",
        fetch_name="cohere-transcribe-03-2026-Q8_0.gguf",
        license="Apache-2.0", hex_wer=1.27,
        notes="Larger quantisation; ~1.5 GB left beside the desktop, tight next to a running daemon.",
    ),
    ModelCandidate(
        id="whisper-large-v3-turbo",
        display_name="Whisper large-v3-turbo (ggml, Vulkan)",
        engine="whisper", model_ref="ggml-large-v3-turbo.bin", runner="vulkan",
        disk_mb=1600, vram_est="~2.0 GB", fits_6gb="yes", vocab="prompt",
        fetch_name="large-v3-turbo",
        license="MIT", hex_wer=2.01,
        notes="The only candidate that reads the vocabulary at decode time (whisper.initial_prompt); drops words more than Parakeet on short takes.",
    ),
    ModelCandidate(
        id="parakeet-tdt-v2",
        display_name="Parakeet TDT 0.6B v2 (English, fp32 ONNX, CUDA)",
        engine="parakeet", model_ref="parakeet-tdt-0.6b-v2", runner="fork",
        disk_mb=2400, vram_est="~3.2 GB", fits_6gb="yes", vocab="none",
        fetch_name="parakeet-tdt-0.6b-v2",
        license="CC-BY-4.0", hex_wer=1.69,
        notes="Batch decode with the fork's non-streaming transcriber; no rescoring.",
    ),
    ModelCandidate(
        id="parakeet-tdt-v2-int8",
        display_name="Parakeet TDT 0.6B v2 (English, int8 ONNX)",
        engine="parakeet", model_ref="parakeet-tdt-0.6b-v2-int8", runner="fork",
        disk_mb=640, vram_est="~1.2 GB", fits_6gb="yes", vocab="none",
        fetch_name="parakeet-tdt-0.6b-v2-int8",
        license="CC-BY-4.0", hex_wer=1.69, hex_note="Hex lists the fp32 export",
        notes="Cheapest download of the v2 weights.",
    ),
    ModelCandidate(
        id="parakeet-tdt-v3",
        display_name="Parakeet TDT 0.6B v3 (25 languages, fp32 ONNX)",
        engine="parakeet", model_ref="parakeet-tdt-0.6b-v3", runner="fork",
        disk_mb=2600, vram_est="~3.4 GB", fits_6gb="yes", vocab="none",
        fetch_name="parakeet-tdt-0.6b-v3",
        license="CC-BY-4.0", hex_wer=1.94,
        notes="Multilingual; can drift into another language on very short takes.",
    ),
    ModelCandidate(
        id="nemotron-3.5-asr",
        display_name="Nemotron 3.5 ASR 0.6B Q8_0 (GGUF, Vulkan)",
        engine="gguf", model_ref="nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf", runner="vulkan",
        disk_mb=751, vram_est="~1.2 GB", fits_6gb="yes", vocab="none",
        fetch_name="nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf",
        license="OpenMDW-1.1", hex_wer=3.05,
        notes="Hex's own run; behind Parakeet on English.",
        gguf_arch="parakeet", language="en-US",
    ),
    ModelCandidate(
        id="qwen3-asr-0.6b",
        display_name="Qwen3-ASR 0.6B Q8_0 (GGUF, Vulkan)",
        engine="gguf", model_ref="Qwen3-ASR-0.6B-Q8_0.gguf", runner="vulkan",
        disk_mb=850, vram_est="~1.4 GB", fits_6gb="yes", vocab="none",
        fetch_name="Qwen3-ASR-0.6B-Q8_0.gguf",
        license="Apache-2.0", hex_wer=None, hex_note="Hex publishes only a Mandarin CER (FLEURS)",
        notes="Multilingual; no English figure from Hex.",
        gguf_arch="qwen3_asr",
    ),
    ModelCandidate(
        id="qwen3-asr-1.7b",
        display_name="Qwen3-ASR 1.7B Q8_0 (GGUF, Vulkan)",
        engine="gguf", model_ref="Qwen3-ASR-1.7B-Q8_0.gguf", runner="vulkan",
        disk_mb=2185, vram_est="~3.0 GB", fits_6gb="tight", vocab="none",
        fetch_name="Qwen3-ASR-1.7B-Q8_0.gguf",
        license="Apache-2.0",
        notes="Not in Hex's picker; in voxtype's.",
        gguf_arch="qwen3_asr",
    ),
    ModelCandidate(
        id="granite-speech-5",
        display_name="IBM Granite Speech 5.0",
        engine="unsupported", model_ref="", runner="none",
        disk_mb=0, vram_est="n/a", fits_6gb="n/a", vocab="none",
        fetch_name="(not in voxtype)",
        license="", hex_wer=4.77,
        notes="Hex ships it; voxtype has no Granite engine or GGUF. Only reachable through a remote OpenAI-style server (whisper.mode = remote).",
    ),
)

MODEL_BY_ID: dict[str, ModelCandidate] = {m.id: m for m in MODEL_CATALOG}
