#!/usr/bin/env python3
"""shuush bench — Offline dictation accuracy benchmark harness.

Subcommands:
  shuush bench prompts   List/export the 42 curated read-aloud benchmark prompts
  shuush bench record    Interactively record prompts to ~/.local/share/shuush/bench/
  shuush bench import    Import an existing WAV (or batch manifest) with reference text
  shuush bench models    Show Hex / voxtype model candidates, 6 GB VRAM fit, fetch commands
  shuush bench run       Run offline voxtype transcribe across presets/models and score results
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shuush_bench_data import (  # noqa: E402
    MODEL_BY_ID,
    MODEL_CATALOG,
    PROMPT_CATEGORIES,
    PROMPTS,
    BenchPrompt,
    ModelCandidate,
)

DEFAULT_FORK_BIN = Path.home() / ".local" / "lib" / "voxtype-fork" / "voxtype"
DEFAULT_VULKAN_BIN = Path("/usr/lib/voxtype/voxtype-vulkan")
DEFAULT_MODELS_DIR = Path.home() / ".local" / "share" / "voxtype" / "models"

# Polarity words aligned with libexec/voxtype_pp_gate.py (apostrophes stripped).
NEGATION_KEYS = frozenset(
    "no not never none nothing nowhere neither nor nobody without cannot "
    "cant dont doesnt didnt isnt arent wasnt werent wont wouldnt couldnt "
    "shouldnt havent hasnt hadnt mustnt neednt aint".split()
)

_ONES = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_NUM_WORDS = frozenset(_ONES) | frozenset(_TENS) | frozenset(_SCALES) | frozenset({"point"})


def default_bench_dir() -> Path:
    env = os.environ.get("SHUUSH_BENCH_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "shuush" / "bench"


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "voxtype"


def default_models_dir() -> Path:
    env = os.environ.get("VOXTYPE_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "voxtype" / "models"


# ---------------------------------------------------------------------------
# Text normalization & scoring
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Normalize text for WER: lowercase, unify quotes, split hyphens, strip punctuation."""
    s = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.lower()
    # Keep decimal points inside numbers (2.5) and dots in known tech names (next.js).
    s = re.sub(r"[-_/]+", " ", s)
    # Strip punctuation except internal apostrophes and internal dots between alnum chars.
    s = re.sub(r"(?<!\w)[.'\"`]+|[.'\"`]+(?!\w)", " ", s)
    s = re.sub(r"[^a-z0-9'.\s]", " ", s)
    return " ".join(s.split())


def normalize_words(text: str) -> list[str]:
    norm = normalize_text(text)
    return norm.split() if norm else []


def word_edit_ops(ref_words: list[str], hyp_words: list[str]) -> tuple[int, int, int, int]:
    """Return (substitutions, deletions, insertions, ref_len) via dynamic programming."""
    n = len(ref_words)
    m = len(hyp_words)
    if n == 0:
        return (0, 0, m, 0)
    # dp[i][j] = (total_edits, S, D, I)
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        curr = [(i, 0, i, 0)]
        rw = ref_words[i - 1]
        for j in range(1, m + 1):
            if rw == hyp_words[j - 1]:
                curr.append(prev[j - 1])
            else:
                sub = prev[j - 1]
                dele = prev[j]
                ins = curr[j - 1]
                best = min(
                    (sub[0] + 1, sub[1] + 1, sub[2], sub[3]),
                    (dele[0] + 1, dele[1], dele[2] + 1, dele[3]),
                    (ins[0] + 1, ins[1], ins[2], ins[3] + 1),
                    key=lambda x: x[0],
                )
                curr.append(best)
        prev = curr
    _, s, d, ins = prev[m]
    return (s, d, ins, n)


def _parse_spoken_int(words: list[str]) -> str | None:
    """Parse spoken English integer or digit-by-digit sequence into canonical digit string."""
    if not words:
        return None
    # Digit-by-digit sequences like "four oh four" -> "404" or "one one four three five"
    if len(words) >= 2 and all(w in _ONES and _ONES[w] < 10 for w in words):
        if "oh" in words or len(words) >= 3:
            return "".join(str(_ONES[w]) for w in words)
    # Two-part "four oh" or "four zero four" handled above; now standard cardinal parser
    total = 0
    current = 0
    saw = False
    for w in words:
        if w == "and":
            continue
        if w in _ONES:
            val = _ONES[w]
            if w == "oh":
                return None
            current += val
            saw = True
        elif w in _TENS:
            current += _TENS[w]
            saw = True
        elif w == "hundred":
            current = (current or 1) * 100
            saw = True
        elif w in _SCALES:
            total += (current or 1) * _SCALES[w]
            current = 0
            saw = True
        else:
            return None
    if not saw:
        return None
    return str(total + current)


def _parse_spoken_number(words: list[str]) -> str | None:
    if "point" in words:
        idx = words.index("point")
        left = _parse_spoken_int(words[:idx])
        right_words = words[idx + 1:]
        if left is None or not right_words:
            return None
        if all(w in _ONES and _ONES[w] < 10 for w in right_words):
            frac = "".join(str(_ONES[w]) for w in right_words)
            return f"{left}.{frac}"
        return None
    return _parse_spoken_int(words)


def _canonical_digit(raw: str) -> str:
    raw = raw.replace(",", "")
    if "." in raw:
        left, right = raw.split(".", 1)
        left_i = str(int(left)) if left.isdigit() else left
        right_s = right.rstrip("0") or "0"
        return f"{left_i}.{right_s}" if right_s != "0" else left_i
    return str(int(raw)) if raw.isdigit() else raw


def extract_numbers(text: str) -> list[str]:
    """Extract canonical numeric values from both digit forms and spoken English words."""
    # Tokenize keeping digit strings (including decimals like 8.5, 404, 11,435) and words
    tokens = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?|[a-zA-Z]+", text.lower())
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?", tok):
            out.append(_canonical_digit(tok))
            i += 1
            continue
        if tok in _NUM_WORDS and tok not in ("oh", "point"):
            j = i + 1
            while j < n:
                nxt = tokens[j]
                if nxt in _NUM_WORDS:
                    j += 1
                elif (
                    nxt == "and"
                    and tokens[j - 1] in _SCALES
                    and j + 1 < n
                    and (tokens[j + 1] in _ONES or tokens[j + 1] in _TENS)
                ):
                    j += 1
                else:
                    break
            span = tokens[i:j]
            while span and span[-1] in ("and", "point"):
                span.pop()
                j -= 1
            val = _parse_spoken_number(span)
            if val is not None:
                out.append(_canonical_digit(val))
                i = j
                continue
        i += 1
    return out


def extract_negations(text: str) -> list[str]:
    """Extract normalized negation tokens (handling contractions like don't / do not)."""
    words = normalize_words(text)
    found: list[str] = []
    for w in words:
        key = w.replace("'", "")
        if key in NEGATION_KEYS:
            found.append(key)
    return found


def _term_pattern(term: str) -> re.Pattern[str]:
    """Build a flexible boundary regex for a domain name/code term."""
    norm = normalize_text(term)
    # Allow optional separators between multi-word/hyphen/dot pieces (e.g., Next.js / next js / nextjs)
    parts = [re.escape(p) for p in re.split(r"[\s.]+", norm) if p]
    if not parts:
        return re.compile(r"(?!)")
    joined = r"[\s.\-]*".join(parts)
    return re.compile(rf"(?<![a-z0-9]){joined}(?![a-z0-9])", re.I)


def score_clip(
    reference: str,
    hypothesis: str,
    terms: list[str] | tuple[str, ...] = (),
    is_question: bool | None = None,
) -> dict:
    """Compute WER, term recall, number/negation preservation, last-word loss, and question preservation."""
    ref_words = normalize_words(reference)
    hyp_words = normalize_words(hypothesis)
    s, d, ins, n = word_edit_ops(ref_words, hyp_words)
    wer = (s + d + ins) / n if n > 0 else (0.0 if not hyp_words else 1.0)

    # Term recall (case-insensitive normalized + exact spelling)
    term_total = len(terms)
    term_hits = 0
    term_exact = 0
    missed_terms: list[str] = []
    hyp_norm = normalize_text(hypothesis)
    for t in terms:
        if _term_pattern(t).search(hyp_norm):
            term_hits += 1
            if t in hypothesis:
                term_exact += 1
        else:
            missed_terms.append(t)

    # Number preservation
    ref_nums = collections.Counter(extract_numbers(reference))
    hyp_nums = collections.Counter(extract_numbers(hypothesis))
    num_total = sum(ref_nums.values())
    num_preserved = sum((ref_nums & hyp_nums).values())

    # Negation preservation (recall + spurious insertion check)
    ref_negs = extract_negations(reference)
    hyp_negs = extract_negations(hypothesis)
    neg_total = len(ref_negs)
    neg_matched = min(len(ref_negs), len(hyp_negs))
    neg_spurious = max(0, len(hyp_negs) - len(ref_negs))

    # Last-word loss
    last_word_lost = False
    ref_last = ref_words[-1] if ref_words else ""
    hyp_last = hyp_words[-1] if hyp_words else ""
    if ref_last:
        if not hyp_last:
            last_word_lost = True
        elif hyp_last == ref_last:
            last_word_lost = False
        else:
            # Allow numeric equivalence on final token (e.g. "zero" vs "0")
            rl_nums = extract_numbers(ref_last)
            hl_nums = extract_numbers(hyp_last)
            # Also allow split compound at end (e.g. "quickshell" -> "quick shell")
            joined_two = "".join(hyp_words[-2:]) if len(hyp_words) >= 2 else ""
            if rl_nums and hl_nums and rl_nums == hl_nums:
                last_word_lost = False
            elif joined_two == ref_last.replace(".", "").replace("'", ""):
                last_word_lost = False
            elif hyp_last.replace(".", "").replace("'", "") == ref_last.replace(".", "").replace("'", ""):
                last_word_lost = False
            else:
                last_word_lost = True

    # Question mark preservation
    q_expected = is_question if is_question is not None else reference.rstrip().endswith("?")
    q_preserved = hypothesis.rstrip().endswith("?") if q_expected else None

    return {
        "wer": wer,
        "substitutions": s,
        "deletions": d,
        "insertions": ins,
        "ref_words": n,
        "term_total": term_total,
        "term_hits": term_hits,
        "term_exact": term_exact,
        "missed_terms": missed_terms,
        "num_total": num_total,
        "num_preserved": num_preserved,
        "neg_total": neg_total,
        "neg_matched": neg_matched,
        "neg_spurious": neg_spurious,
        "last_word_lost": last_word_lost,
        "ref_last": ref_last,
        "hyp_last": hyp_last,
        "question_expected": bool(q_expected),
        "question_preserved": q_preserved,
    }


def aggregate_scores(clip_results: list[dict]) -> dict:
    """Aggregate clip-level metrics into dataset totals and a transparent /10 score."""
    n_clips = len(clip_results)
    tot_s = sum(r["substitutions"] for r in clip_results)
    tot_d = sum(r["deletions"] for r in clip_results)
    tot_i = sum(r["insertions"] for r in clip_results)
    tot_n = sum(r["ref_words"] for r in clip_results)
    wer = (tot_s + tot_d + tot_i) / tot_n if tot_n > 0 else 0.0

    term_total = sum(r["term_total"] for r in clip_results)
    term_hits = sum(r["term_hits"] for r in clip_results)
    term_exact = sum(r["term_exact"] for r in clip_results)
    term_recall = (term_hits / term_total) if term_total > 0 else None
    term_exact_rate = (term_exact / term_total) if term_total > 0 else None

    num_total = sum(r["num_total"] for r in clip_results)
    num_preserved = sum(r["num_preserved"] for r in clip_results)
    num_rate = (num_preserved / num_total) if num_total > 0 else None

    neg_total = sum(r["neg_total"] for r in clip_results)
    neg_matched = sum(r["neg_matched"] for r in clip_results)
    neg_spurious = sum(r["neg_spurious"] for r in clip_results)
    neg_rate = (max(0, neg_matched - neg_spurious) / neg_total) if neg_total > 0 else None

    last_lost = sum(1 for r in clip_results if r["last_word_lost"])
    last_loss_rate = (last_lost / n_clips) if n_clips > 0 else 0.0

    q_clips = [r for r in clip_results if r["question_expected"]]
    q_rate = (sum(1 for r in q_clips if r["question_preserved"]) / len(q_clips)) if q_clips else None

    # Composite /10 score (redistributes weights of absent metric categories)
    weighted: list[tuple[float, float]] = [
        (0.45, max(0.0, 1.0 - wer)),
        (0.10, max(0.0, 1.0 - last_loss_rate)),
    ]
    if term_recall is not None:
        weighted.append((0.25, term_recall))
    if num_rate is not None:
        weighted.append((0.10, num_rate))
    if neg_rate is not None:
        weighted.append((0.10, neg_rate))
    w_sum = sum(w for w, _ in weighted)
    score_10 = round(10.0 * sum(w * v for w, v in weighted) / w_sum, 2) if w_sum > 0 else 0.0

    latencies = [r["latency_ms"] for r in clip_results if isinstance(r.get("latency_ms"), (int, float))]
    audio_secs = sum(float(r.get("duration_secs") or 0.0) for r in clip_results)
    infer_secs = sum(float(r.get("latency_ms") or 0.0) / 1000.0 for r in clip_results)
    rtf = (infer_secs / audio_secs) if audio_secs > 0 else None

    return {
        "clips": n_clips,
        "ref_words": tot_n,
        "wer": round(wer, 4),
        "substitutions": tot_s,
        "deletions": tot_d,
        "insertions": tot_i,
        "term_total": term_total,
        "term_hits": term_hits,
        "term_exact": term_exact,
        "term_recall": round(term_recall, 4) if term_recall is not None else None,
        "term_exact_rate": round(term_exact_rate, 4) if term_exact_rate is not None else None,
        "num_total": num_total,
        "num_preserved": num_preserved,
        "num_preservation": round(num_rate, 4) if num_rate is not None else None,
        "neg_total": neg_total,
        "neg_matched": neg_matched,
        "neg_spurious": neg_spurious,
        "neg_preservation": round(neg_rate, 4) if neg_rate is not None else None,
        "last_word_lost": last_lost,
        "last_word_loss": round(last_loss_rate, 4),
        "question_total": len(q_clips),
        "question_preservation": round(q_rate, 4) if q_rate is not None else None,
        "score_10": score_10,
        "median_ms": int(round(statistics.median(latencies))) if latencies else None,
        "total_ms": int(round(sum(latencies))) if latencies else None,
        "audio_secs": round(audio_secs, 2),
        "rtf": round(rtf, 3) if rtf is not None else None,
    }


# ---------------------------------------------------------------------------
# Binary, model & WAV helpers
# ---------------------------------------------------------------------------

def resolve_fork_binary(override: str | None = None) -> Path | None:
    for candidate in (
        override,
        os.environ.get("SHUUSH_VOXTYPE_BIN"),
        str(DEFAULT_FORK_BIN),
        shutil.which("voxtype"),
    ):
        if candidate:
            p = Path(candidate).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return p
    return None


def resolve_vulkan_binary(override: str | None = None) -> Path | None:
    for candidate in (override, os.environ.get("SHUUSH_VULKAN_VOXTYPE_BIN"), str(DEFAULT_VULKAN_BIN)):
        if candidate:
            p = Path(candidate).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return p
    return None


def probe_compiled_engines(binary: Path | None) -> set[str]:
    """Query `voxtype info engines` in a throwaway config dir (never touches live config)."""
    if not binary or not binary.is_file():
        return set()
    with tempfile.TemporaryDirectory(prefix="shuush-bench-probe.") as td:
        env = dict(os.environ)
        env.update({"XDG_CONFIG_HOME": td, "XDG_RUNTIME_DIR": td, "NO_COLOR": "1"})
        env.pop("VOXTYPE_CONFIG", None)
        try:
            r = subprocess.run(
                [str(binary), "info", "engines"],
                capture_output=True, text=True, env=env, timeout=10, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
    engines: set[str] = set()
    for line in r.stdout.splitlines():
        m = re.match(r"^\s*compiled\s+([a-z0-9_-]+)", line)
        if m:
            engines.add(m.group(1))
    return engines


def locate_model(candidate: ModelCandidate, models_dir: Path) -> Path | None:
    """Return installed path for a catalog model if present on disk."""
    if not candidate.model_ref:
        return None
    direct = models_dir / candidate.model_ref
    if direct.exists():
        return direct
    # Whisper models sometimes live without ggml- prefix or as .bin directly
    if candidate.engine == "whisper" and candidate.model_ref.startswith("ggml-"):
        alt = models_dir / candidate.model_ref.removeprefix("ggml-")
        if alt.exists():
            return alt
    return None


def wav_duration_secs(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            fr = w.getframerate()
            return w.getnframes() / float(fr) if fr > 0 else 0.0
    except (OSError, wave.Error):
        return 0.0


def ensure_pcm16_16k_mono(src: Path, dst: Path) -> float:
    """Copy src WAV to dst as 16 kHz mono 16-bit PCM (converting via ffmpeg if needed)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    need_convert = True
    try:
        with wave.open(str(src), "rb") as w:
            if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000 and w.getcomptype() == "NONE":
                need_convert = False
    except (OSError, wave.Error):
        need_convert = True

    if not need_convert:
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        return wav_duration_secs(dst)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(f"ffmpeg is required to convert {src} to 16 kHz mono 16-bit PCM WAV")
    r = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(dst)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed for {src}: {r.stderr.strip()}")
    return wav_duration_secs(dst)


def load_manifest(bench_dir: Path, manifest_path: Path | None = None) -> list[dict]:
    mpath = manifest_path or (bench_dir / "manifest.jsonl")
    if not mpath.is_file():
        return []
    rows: list[dict] = []
    for line in mpath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        wav_val = row.get("wav") or ""
        wpath = Path(wav_val)
        if not wpath.is_absolute():
            wpath = (mpath.parent / wpath).resolve()
        row["_wav_path"] = wpath
        if "duration_secs" not in row:
            row["duration_secs"] = round(wav_duration_secs(wpath), 3)
        rows.append(row)
    return rows


def save_manifest(bench_dir: Path, rows: list[dict]) -> Path:
    bench_dir.mkdir(parents=True, exist_ok=True)
    mpath = bench_dir / "manifest.jsonl"
    tmp = mpath.with_name(f".{mpath.name}.{os.getpid()}.tmp")
    lines = []
    for r in rows:
        clean = {k: v for k, v in r.items() if not k.startswith("_")}
        lines.append(json.dumps(clean, ensure_ascii=False))
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, mpath)
    return mpath


def load_vocab_terms(path: Path | None) -> list[str]:
    if not path or not path.is_file():
        return []
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


# ---------------------------------------------------------------------------
# Offline voxtype transcribe & opt-in cleanup execution
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class RunSpec:
    name: str
    binary: Path
    engine: str
    model_path: str
    streaming: bool = True
    streaming_final_pass: bool = True
    pad_secs: float = 0.5
    use_rescore: bool = True
    vocab_boost: float = 2.5
    gguf_arch: str = ""
    language: str = "en"
    initial_prompt: str = ""
    vram_gb: str = ""
    note: str = ""


def _toml_str(val: str) -> str:
    return json.dumps(val)


def build_temp_config(
    spec: RunSpec,
    vocab_file: Path | None,
    corrections_file: Path | None,
) -> str:
    lines = [
        f"engine = {_toml_str(spec.engine)}",
        "",
        "[vad]",
        "enabled = false",
        "",
        "[text]",
        "spoken_punctuation = false",
        "filter_filler_words = false",
        "replacements = {}",
        "",
    ]
    if spec.engine == "parakeet":
        lines += [
            "[parakeet]",
            f"model = {_toml_str(spec.model_path)}",
        ]
        if spec.model_path.endswith(".gguf"):
            lines += [
                "streaming = false",
                'gguf_backend = "vulkan"',
            ]
        else:
            lines += [
                f"streaming = {'true' if spec.streaming else 'false'}",
                f"streaming_final_pass = {'true' if spec.streaming_final_pass else 'false'}",
                f"pad_secs = {spec.pad_secs}",
                "final_pass_segment_secs = 45.0",
                "idle_unload_secs = 0",
            ]
            if spec.streaming and spec.use_rescore:
                if vocab_file and vocab_file.is_file():
                    lines.append(f"vocabulary_file = {_toml_str(str(vocab_file))}")
                if corrections_file and corrections_file.is_file():
                    lines.append(f"corrections_file = {_toml_str(str(corrections_file))}")
                lines.append(f"vocabulary_boost = {spec.vocab_boost}")
    elif spec.engine == "whisper":
        lines += [
            "[whisper]",
            f"model = {_toml_str(spec.model_path)}",
            f"language = {_toml_str(spec.language or 'en')}",
            "translate = false",
            "flash_attention = true",
            "eager_processing = false",
            "streaming = false",
        ]
        if spec.initial_prompt:
            lines.append(f"initial_prompt = {_toml_str(spec.initial_prompt)}")
    elif spec.engine == "cohere":
        lines += [
            "[cohere]",
            f"model = {_toml_str(spec.model_path)}",
            f"language = {_toml_str(spec.language or 'en')}",
        ]
        if spec.model_path.endswith(".gguf"):
            lines.append('gguf_backend = "vulkan"')
    elif spec.engine == "gguf":
        lines += [
            "[gguf]",
            f"model = {_toml_str(spec.model_path)}",
            f"architecture = {_toml_str(spec.gguf_arch)}",
            f"language = {_toml_str(spec.language or 'auto')}",
            'gguf_backend = "vulkan"',
        ]
    return "\n".join(lines) + "\n"


def parse_voxtype_transcribe_stdout(stdout: str) -> str:
    """Extract the transcript printed by `voxtype transcribe <wav>`."""
    if "No speech detected, skipping transcription." in stdout:
        return ""
    # Standard output prints metadata header lines, then a blank line, then the transcript.
    header_prefixes = (
        "Loading audio file:",
        "Audio format:",
        "Resampling from ",
        "Processing ",
        "VAD backend:",
        "VAD:",
    )
    lines = stdout.splitlines()
    saw_header = any(l.startswith(header_prefixes) for l in lines)
    if saw_header:
        # Take everything after the last blank line following headers.
        blank_idx = -1
        for idx, line in enumerate(lines):
            if line.strip() == "":
                blank_idx = idx
        if blank_idx >= 0:
            return "\n".join(lines[blank_idx + 1:]).strip()
        tail = [l for l in lines if not l.startswith(header_prefixes)]
        return "\n".join(tail).strip()
    return stdout.strip()


def transcribe_wav_offline(
    spec: RunSpec,
    wav_path: Path,
    cfg_path: Path,
    tmp_dir: Path,
    timeout_secs: int = 120,
) -> tuple[str, int, str | None]:
    """Run `<voxtype> -c <tmp_cfg> transcribe <wav>` offline; returns (text, ms, error)."""
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_dir / "cfg")
    env["XDG_RUNTIME_DIR"] = str(tmp_dir / "rt")
    env["XDG_STATE_HOME"] = str(tmp_dir / "state")
    env["NO_COLOR"] = "1"
    env.pop("VOXTYPE_CONFIG", None)
    for sub in ("cfg", "rt", "state"):
        (tmp_dir / sub).mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [str(spec.binary), "-c", str(cfg_path), "transcribe", str(wav_path)],
            capture_output=True, text=True, env=env, timeout=timeout_secs, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ("", int((time.monotonic() - t0) * 1000), f"timeout after {timeout_secs}s")
    except OSError as exc:
        return ("", int((time.monotonic() - t0) * 1000), str(exc))
    ms = int((time.monotonic() - t0) * 1000)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()
        msg = err[-1] if err else f"exit {r.returncode}"
        return ("", ms, msg)
    return (parse_voxtype_transcribe_stdout(r.stdout), ms, None)


def run_shuush_cleanup(
    raw_text: str,
    postprocess_script: Path,
    mode: str = "default",
    config_dir: Path | None = None,
    timeout_secs: int = 30,
) -> tuple[str, int, str | None]:
    """Run shuush cleanup in an isolated temp runtime/state dir with stubbed desktop commands."""
    with tempfile.TemporaryDirectory(prefix="shuush-bench-pp.") as td:
        td_path = Path(td)
        shim = td_path / "shim"
        rt = td_path / "rt"
        state = td_path / "state"
        shim.mkdir()
        (rt / "voxtype").mkdir(parents=True)
        state.mkdir()
        for cmd in ("wl-paste", "wl-copy", "hyprctl", "niri", "swaymsg", "notify-send", "kitten"):
            p = shim / cmd
            p.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            p.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{shim}:{env.get('PATH', '')}"
        env["XDG_RUNTIME_DIR"] = str(rt)
        env["XDG_STATE_HOME"] = str(state)
        if config_dir is not None:
            env["XDG_CONFIG_HOME"] = str(config_dir.parent)
        env["VOXTYPE_STREAM_OUTPUT"] = "0"
        env["PP_NO_HISTORY"] = "1"
        env["PP_WATCH_EDITS"] = "0"
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                ["bash", str(postprocess_script), mode],
                input=raw_text, capture_output=True, text=True, env=env, timeout=timeout_secs,
            )
        except subprocess.TimeoutExpired:
            return (raw_text, int((time.monotonic() - t0) * 1000), f"cleanup timeout after {timeout_secs}s")
        except OSError as exc:
            return (raw_text, int((time.monotonic() - t0) * 1000), str(exc))
        ms = int((time.monotonic() - t0) * 1000)
        if r.returncode != 0:
            return (raw_text, ms, f"cleanup exit {r.returncode}: {r.stderr.strip()[:120]}")
        out = r.stdout.strip()
        return (out if out else raw_text, ms, None)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_prompts(args: argparse.Namespace) -> int:
    selected = list(PROMPTS)
    if args.category:
        cats = set(args.category)
        selected = [p for p in selected if p.category in cats]

    extra_terms: list[str] = []
    if args.from_vocab is not None:
        vpath = Path(args.from_vocab).expanduser() if args.from_vocab else (default_config_dir() / "vocabulary.txt")
        covered = {t.lower() for p in selected for t in p.terms}
        for term in load_vocab_terms(vpath):
            if term.lower() not in covered:
                extra_terms.append(term)

    if args.json:
        doc = {
            "count": len(selected),
            "categories": list(PROMPT_CATEGORIES),
            "prompts": [dataclasses.asdict(p) for p in selected],
        }
        if args.from_vocab is not None:
            doc["uncovered_local_vocab"] = extra_terms
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0

    print(f"Shuush Accuracy Benchmark Prompts ({len(selected)} prompts across {len({p.category for p in selected})} categories)")
    print("Read each prompt aloud naturally once during `shuush bench record`.\n")
    idx = 0
    for cat in PROMPT_CATEGORIES:
        cat_prompts = [p for p in selected if p.category == cat]
        if not cat_prompts:
            continue
        print(f"== {cat} ({len(cat_prompts)}) ==")
        for p in cat_prompts:
            idx += 1
            tags = []
            if p.terms:
                tags.append("terms: " + ", ".join(p.terms))
            if p.is_question:
                tags.append("question")
            meta = f"  [{'; '.join(tags)}]" if tags else ""
            print(f"  {idx:2d}. [{p.id}] {p.text}{meta}")
        print()

    if extra_terms:
        print(f"Local vocabulary terms not in default prompts ({len(extra_terms)}):")
        print("  " + ", ".join(extra_terms))
        print("  Tip: record custom clips with `shuush bench import <wav> --ref \"...\" --terms \"...\"`")
    return 0


def _choose_recorder(preferred: str | None) -> tuple[str, list[str]]:
    order = [preferred] if preferred else ["pw-record", "parecord", "arecord"]
    for name in order:
        if not name:
            continue
        exe = shutil.which(name)
        if not exe:
            continue
        if name == "pw-record":
            return (name, [exe, "--format=s16", "--rate=16000", "--channels=1"])
        if name == "parecord":
            return (name, [exe, "--file-format=wav", "--format=s16le", "--rate=16000", "--channels=1"])
        if name == "arecord":
            return (name, [exe, "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav"])
    raise RuntimeError("No audio recorder found (install pw-record, parecord, or arecord)")


def cmd_record(args: argparse.Namespace) -> int:
    bench_dir = Path(args.dir).expanduser() if args.dir else default_bench_dir()
    rec_name, rec_base = _choose_recorder(args.recorder)
    if args.device:
        if rec_name == "pw-record":
            rec_base += ["--target", args.device]
        elif rec_name == "parecord":
            rec_base += ["--device", args.device]
        elif rec_name == "arecord":
            rec_base += ["-D", args.device]

    existing = load_manifest(bench_dir)
    by_id = {r["id"]: r for r in existing}

    prompts = list(PROMPTS)
    if args.category:
        cats = set(args.category)
        prompts = [p for p in prompts if p.category in cats]
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        prompts = [p for p in prompts if p.id in want]
    if not args.re_record:
        prompts = [p for p in prompts if p.id not in by_id]

    if not prompts:
        print(f"All selected prompts are already recorded in {bench_dir} (pass --re-record to redo).")
        return 0

    bench_dir.mkdir(parents=True, exist_ok=True)
    print(f"Recording {len(prompts)} prompt(s) to {bench_dir} using {rec_name} (16 kHz mono WAV).")
    print("Controls: [Enter] start/stop, [s] skip, [q] quit.\n")

    recorded = 0
    for i, p in enumerate(prompts, 1):
        wav_name = f"{p.id}.wav"
        wav_path = bench_dir / wav_name
        while True:
            print(f"[{i}/{len(prompts)}] ({p.id} · {p.category})")
            print(f"  \"{p.text}\"")
            choice = input("  Press Enter to record (s=skip, q=quit): ").strip().lower()
            if choice == "q":
                save_manifest(bench_dir, list(by_id.values()))
                print(f"Saved {recorded} new recording(s) to {bench_dir / 'manifest.jsonl'}.")
                return 0
            if choice == "s":
                print("  Skipped.\n")
                break
            proc = subprocess.Popen(
                [*rec_base, str(wav_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                input("  ● Recording... press Enter to stop: ")
            finally:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=3)
            dur = wav_duration_secs(wav_path)
            if dur < 0.2:
                print(f"  Recording too short ({dur:.2f}s); let's retry.")
                wav_path.unlink(missing_ok=True)
                continue
            verdict = input(f"  Captured {dur:.2f}s -> {wav_name}. [Enter=keep, r=redo, s=skip]: ").strip().lower()
            if verdict == "r":
                wav_path.unlink(missing_ok=True)
                continue
            if verdict == "s":
                wav_path.unlink(missing_ok=True)
                print("  Skipped.\n")
                break
            by_id[p.id] = {
                "id": p.id,
                "wav": wav_name,
                "reference": p.text,
                "category": p.category,
                "terms": list(p.terms),
                "is_question": p.is_question,
                "duration_secs": round(dur, 3),
                "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            save_manifest(bench_dir, list(by_id.values()))
            recorded += 1
            print()
            break

    print(f"Done. Recorded {recorded} clip(s). Run `shuush bench run` to evaluate.")
    return 0


def _sanitize_id(stem: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-").lower()
    return s or "clip"


def cmd_import(args: argparse.Namespace) -> int:
    bench_dir = Path(args.dir).expanduser() if args.dir else default_bench_dir()
    existing = load_manifest(bench_dir)
    by_id = {r["id"]: r for r in existing}

    if args.manifest:
        src_manifest = Path(args.manifest).expanduser().resolve()
        rows = load_manifest(src_manifest.parent, src_manifest)
        imported = 0
        for row in rows:
            src_wav = row["_wav_path"]
            cid = row.get("id") or _sanitize_id(src_wav.stem)
            dst_name = f"{cid}.wav"
            dst_wav = bench_dir / dst_name
            dur = ensure_pcm16_16k_mono(src_wav, dst_wav)
            ref = str(row.get("reference") or "").strip()
            if not ref:
                raise SystemExit(f"Manifest entry {cid} is missing 'reference'")
            by_id[cid] = {
                "id": cid,
                "wav": dst_name,
                "reference": ref,
                "category": row.get("category") or args.category or "imported",
                "terms": list(row.get("terms") or []),
                "is_question": bool(row.get("is_question", ref.rstrip().endswith("?"))),
                "duration_secs": round(dur, 3),
                "imported_from": str(src_wav),
                "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            imported += 1
        mpath = save_manifest(bench_dir, list(by_id.values()))
        print(f"Imported {imported} clip(s) into {mpath}")
        return 0

    if not args.wav:
        print("shuush bench import: provide a WAV file path or --manifest JSONL", file=sys.stderr)
        return 2

    src_wav = Path(args.wav).expanduser().resolve()
    if not src_wav.is_file():
        print(f"shuush bench import: file not found: {src_wav}", file=sys.stderr)
        return 2

    ref = args.ref
    if not ref:
        ref = input(f"Reference transcript for {src_wav.name}: ").strip()
    if not ref:
        print("shuush bench import: reference text cannot be empty", file=sys.stderr)
        return 2

    base_id = args.id or _sanitize_id(src_wav.stem)
    cid = base_id
    if not args.overwrite:
        suffix = 2
        while cid in by_id:
            cid = f"{base_id}-{suffix}"
            suffix += 1

    dst_name = f"{cid}.wav"
    dst_wav = bench_dir / dst_name
    dur = ensure_pcm16_16k_mono(src_wav, dst_wav)
    terms = [t.strip() for t in (args.terms or "").split(",") if t.strip()]
    by_id[cid] = {
        "id": cid,
        "wav": dst_name,
        "reference": ref,
        "category": args.category or "imported",
        "terms": terms,
        "is_question": bool(args.question or ref.rstrip().endswith("?")),
        "duration_secs": round(dur, 3),
        "imported_from": str(src_wav),
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    mpath = save_manifest(bench_dir, list(by_id.values()))
    print(f"Imported {cid} ({dur:.2f}s) -> {dst_wav} (manifest: {mpath})")
    return 0


def _candidate_status(
    cand: ModelCandidate,
    models_dir: Path,
    fork_bin: Path | None,
    fork_engines: set[str],
    vulkan_bin: Path | None,
    vulkan_engines: set[str],
) -> dict:
    installed_path = locate_model(cand, models_dir)
    runner_bin: Path | None = None
    runnable_note = ""
    if cand.engine == "unsupported":
        runnable_note = "unsupported in voxtype"
    elif cand.binary_kind == "fork":
        if fork_bin and cand.engine in fork_engines:
            runner_bin = fork_bin
        else:
            runnable_note = f"needs fork binary with '{cand.engine}'"
    elif cand.binary_kind == "stock-vulkan":
        if vulkan_bin and cand.engine in vulkan_engines:
            runner_bin = vulkan_bin
        elif fork_bin and cand.engine in fork_engines:
            runner_bin = fork_bin
        else:
            runnable_note = f"needs voxtype-vulkan + gguf-worker ({cand.engine})"
    elif cand.binary_kind == "both":
        # Prefer Vulkan binary for Whisper (GPU acceleration) over CPU-only whisper in fork
        if vulkan_bin and cand.engine in vulkan_engines:
            runner_bin = vulkan_bin
        elif fork_bin and cand.engine in fork_engines:
            runner_bin = fork_bin
        else:
            runnable_note = f"needs binary with '{cand.engine}'"
    return {
        **dataclasses.asdict(cand),
        "installed": installed_path is not None,
        "installed_path": str(installed_path) if installed_path else None,
        "engine_compiled": runner_bin is not None,
        "runner_binary": str(runner_bin) if runner_bin else None,
        "status_note": runnable_note,
    }


def cmd_models(args: argparse.Namespace) -> int:
    models_dir = Path(args.models_dir).expanduser() if args.models_dir else default_models_dir()
    fork_bin = resolve_fork_binary(args.voxtype_bin)
    vulkan_bin = resolve_vulkan_binary(args.vulkan_bin)
    fork_engines = probe_compiled_engines(fork_bin)
    vulkan_engines = probe_compiled_engines(vulkan_bin)

    rows = [
        _candidate_status(c, models_dir, fork_bin, fork_engines, vulkan_bin, vulkan_engines)
        for c in MODEL_CATALOG
    ]
    if args.json:
        print(json.dumps({
            "models_dir": str(models_dir),
            "fork_binary": str(fork_bin) if fork_bin else None,
            "fork_engines": sorted(fork_engines),
            "vulkan_binary": str(vulkan_bin) if vulkan_bin else None,
            "vulkan_engines": sorted(vulkan_engines),
            "gpu_vram_budget_gb": 6,
            "candidates": rows,
        }, indent=2))
        return 0

    print("Hex & Voxtype ASR Model Candidates (6 GB VRAM budget)")
    print(f"  Models dir:    {models_dir}")
    print(f"  Fork binary:   {fork_bin or 'not found'} (engines: {', '.join(sorted(fork_engines)) or 'none'})")
    print(f"  Vulkan binary: {vulkan_bin or 'not found'} (engines: {', '.join(sorted(vulkan_engines)) or 'none'})\n")

    headers = ("ID", "Engine", "Status", "Disk", "VRAM (6GB)", "Vocab", "Binary")
    table: list[tuple[str, ...]] = []
    for r in rows:
        if r["engine"] == "unsupported":
            st = "unsupported"
        elif r["installed"] and r["engine_compiled"]:
            st = "ready"
        elif r["installed"]:
            st = "installed (no bin)"
        elif r["engine_compiled"]:
            st = "fetchable"
        else:
            st = "needs build"
        disk = f"{r['disk_mb']} MB" if r["disk_mb"] < 1000 else f"{r['disk_mb'] / 1000:.2f} GB"
        vram = f"{r['vram_gb']} ({r['fits_6gb']})"
        bin_lbl = Path(r["runner_binary"]).name if r["runner_binary"] else "-"
        table.append((r["id"], r["engine"], st, disk, vram, r["supports_vocab_bias"], bin_lbl))

    widths = [max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in table:
        print(fmt.format(*row))

    print("\nFetch commands (run `df -h /` first; do not download if disk headroom is low):")
    for r in rows:
        if not r["installed"] and r["engine"] != "unsupported":
            print(f"  [{r['id']}] ({r['disk_mb']} MB, {r['vram_gb']})")
            print(f"    {r['fetch_cmd']}")
    return 0


def _build_run_specs(
    args: argparse.Namespace,
    models_dir: Path,
    fork_bin: Path | None,
    vulkan_bin: Path | None,
    vocab_terms: list[str],
) -> tuple[list[RunSpec], list[dict]]:
    """Build the list of offline transcription runs and any skipped/uninstalled candidates."""
    fork_engines = probe_compiled_engines(fork_bin)
    vulkan_engines = probe_compiled_engines(vulkan_bin)
    specs: list[RunSpec] = []
    skipped: list[dict] = []

    whisper_prompt = ", ".join(vocab_terms[:40]) + ("." if vocab_terms else "")

    if args.models is not None:
        # Compare installed model candidates (or specific requested IDs)
        if args.models == [] or args.models == ["all"]:
            candidates = list(MODEL_CATALOG)
        else:
            candidates = []
            for raw in args.models:
                for mid in raw.split(","):
                    mid = mid.strip()
                    if not mid:
                        continue
                    if mid not in MODEL_BY_ID:
                        raise SystemExit(f"Unknown model ID '{mid}'. Run `shuush bench models` to list IDs.")
                    candidates.append(MODEL_BY_ID[mid])

        for cand in candidates:
            st = _candidate_status(cand, models_dir, fork_bin, fork_engines, vulkan_bin, vulkan_engines)
            if not st["installed"] or not st["engine_compiled"]:
                skipped.append({
                    "id": cand.id,
                    "display_name": cand.display_name,
                    "reason": "unsupported in voxtype" if cand.engine == "unsupported"
                    else ("not installed" if not st["installed"] else st["status_note"]),
                    "disk_mb": cand.disk_mb,
                    "vram_gb": cand.vram_gb,
                    "fits_6gb": cand.fits_6gb,
                    "fetch_cmd": cand.fetch_cmd,
                })
                continue
            runner = Path(st["runner_binary"])
            mpath = st["installed_path"]
            if cand.engine == "parakeet" and cand.supports_streaming:
                specs.append(RunSpec(
                    name=f"{cand.id}:rescore",
                    binary=runner,
                    engine="parakeet",
                    model_path=mpath,
                    streaming=True,
                    streaming_final_pass=True,
                    pad_secs=0.5,
                    use_rescore=True,
                    vocab_boost=args.vocab_boost,
                    vram_gb=cand.vram_gb,
                ))
                specs.append(RunSpec(
                    name=f"{cand.id}:base",
                    binary=runner,
                    engine="parakeet",
                    model_path=mpath,
                    streaming=True,
                    streaming_final_pass=True,
                    pad_secs=0.5,
                    use_rescore=False,
                    vram_gb=cand.vram_gb,
                ))
            elif cand.engine == "parakeet":
                specs.append(RunSpec(
                    name=cand.id,
                    binary=runner,
                    engine="parakeet",
                    model_path=mpath,
                    streaming=False,
                    streaming_final_pass=False,
                    pad_secs=0.0,
                    use_rescore=False,
                    vram_gb=cand.vram_gb,
                ))
            elif cand.engine == "whisper":
                specs.append(RunSpec(
                    name=f"{cand.id}:prompt",
                    binary=runner,
                    engine="whisper",
                    model_path=mpath,
                    initial_prompt=whisper_prompt,
                    language=cand.language,
                    vram_gb=cand.vram_gb,
                ))
                specs.append(RunSpec(
                    name=f"{cand.id}:plain",
                    binary=runner,
                    engine="whisper",
                    model_path=mpath,
                    initial_prompt="",
                    language=cand.language,
                    vram_gb=cand.vram_gb,
                ))
            elif cand.engine in ("cohere", "gguf"):
                specs.append(RunSpec(
                    name=cand.id,
                    binary=runner,
                    engine=cand.engine,
                    model_path=mpath,
                    gguf_arch=cand.gguf_arch,
                    language=cand.language,
                    vram_gb=cand.vram_gb,
                ))
        return specs, skipped

    # Default option-set matrix on the primary Parakeet Unified model (fork binary)
    if not fork_bin:
        raise SystemExit("No voxtype binary found. Pass --voxtype-bin or set SHUUSH_VOXTYPE_BIN.")

    fp16 = models_dir / "parakeet-unified-en-0.6b-fp16"
    fp32 = models_dir / "parakeet-unified-en-0.6b"
    primary_model = str(fp16 if fp16.exists() else (fp32 if fp32.exists() else "parakeet-unified-en-0.6b-fp16"))
    vram_label = "~2.3 GB" if fp16.exists() else "~3.3 GB"

    all_presets: dict[str, RunSpec] = {
        "final+rescore": RunSpec(
            name="parakeet:final+rescore",
            binary=fork_bin,
            engine="parakeet",
            model_path=primary_model,
            streaming=True,
            streaming_final_pass=True,
            pad_secs=0.5,
            use_rescore=True,
            vocab_boost=args.vocab_boost,
            vram_gb=vram_label,
            note="Final pass + pad=0.5s + acoustic vocabulary rescoring",
        ),
        "final+no-rescore": RunSpec(
            name="parakeet:final+no-rescore",
            binary=fork_bin,
            engine="parakeet",
            model_path=primary_model,
            streaming=True,
            streaming_final_pass=True,
            pad_secs=0.5,
            use_rescore=False,
            vram_gb=vram_label,
            note="Final pass + pad=0.5s, rescoring off",
        ),
        "final+no-pad": RunSpec(
            name="parakeet:final+no-pad",
            binary=fork_bin,
            engine="parakeet",
            model_path=primary_model,
            streaming=True,
            streaming_final_pass=True,
            pad_secs=0.0,
            use_rescore=True,
            vocab_boost=args.vocab_boost,
            vram_gb=vram_label,
            note="Final pass + rescoring, pad=0.0s (isolates last-word tail padding)",
        ),
        "no-final+no-rescore": RunSpec(
            name="parakeet:no-final+no-rescore",
            binary=fork_bin,
            engine="parakeet",
            model_path=primary_model,
            streaming=True,
            streaming_final_pass=False,
            pad_secs=0.0,
            use_rescore=False,
            vram_gb=vram_label,
            note="streaming_final_pass=false, pad=0.0s, rescoring off (note: `voxtype transcribe` runs offline decode)",
        ),
    }

    if args.presets:
        wanted = [p.strip() for p in args.presets.split(",") if p.strip()]
        for w in wanted:
            if w not in all_presets:
                raise SystemExit(f"Unknown preset '{w}'. Choose from: {', '.join(all_presets)}")
            specs.append(all_presets[w])
    else:
        specs = list(all_presets.values())

    return specs, skipped


def _fmt_pct(val: float | None) -> str:
    return "-" if val is None else f"{val * 100:.1f}%"


def print_summary_table(results: list[dict], skipped: list[dict]) -> None:
    headers = ("Config", "Score", "WER", "Terms", "Nums", "Negs", "TailLoss", "Q?", "MedMs", "RTF", "VRAM")
    rows: list[tuple[str, ...]] = []
    for res in results:
        agg = res["summary"]
        rows.append((
            res["name"],
            f"{agg['score_10']:.2f}/10",
            _fmt_pct(agg["wer"]),
            _fmt_pct(agg["term_recall"]),
            _fmt_pct(agg["num_preservation"]),
            _fmt_pct(agg["neg_preservation"]),
            _fmt_pct(agg["last_word_loss"]),
            _fmt_pct(agg["question_preservation"]),
            f"{agg['median_ms']}ms" if agg["median_ms"] is not None else "-",
            f"{agg['rtf']:.2f}x" if agg["rtf"] is not None else "-",
            res.get("vram_gb") or "-",
        ))

    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)] if rows else [len(h) for h in headers]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*r))

    if skipped:
        print("\nUninstalled / unavailable model candidates (not run):")
        for s in skipped:
            print(f"  - {s['id']} ({s['disk_mb']} MB, VRAM {s['vram_gb']}, 6GB={s['fits_6gb']}): {s['reason']}")
            print(f"      fetch: {s['fetch_cmd']}")


def cmd_run(args: argparse.Namespace) -> int:
    bench_dir = Path(args.dir).expanduser() if args.dir else default_bench_dir()
    manifest_path = Path(args.manifest).expanduser() if args.manifest else None
    clips = load_manifest(bench_dir, manifest_path)
    if not clips:
        loc = manifest_path or (bench_dir / "manifest.jsonl")
        print(
            f"No clips found at {loc}.\n"
            "Record prompts first with `shuush bench record`, or import WAVs with `shuush bench import <wav>`.",
            file=sys.stderr,
        )
        return 1

    if args.limit and args.limit > 0:
        clips = clips[: args.limit]

    models_dir = Path(args.models_dir).expanduser() if args.models_dir else default_models_dir()
    fork_bin = resolve_fork_binary(args.voxtype_bin)
    vulkan_bin = resolve_vulkan_binary(args.vulkan_bin)

    # Read-only lookup of vocabulary / corrections files
    vocab_file: Path | None = None
    if args.vocab_file:
        vocab_file = Path(args.vocab_file).expanduser()
    elif (bench_dir / "vocabulary.txt").is_file():
        vocab_file = bench_dir / "vocabulary.txt"
    elif (default_config_dir() / "vocabulary.txt").is_file():
        vocab_file = default_config_dir() / "vocabulary.txt"

    corrections_file: Path | None = None
    if args.corrections_file:
        corrections_file = Path(args.corrections_file).expanduser()
    elif (bench_dir / "corrections.tsv").is_file():
        corrections_file = bench_dir / "corrections.tsv"
    elif (default_config_dir() / "corrections.tsv").is_file():
        corrections_file = default_config_dir() / "corrections.tsv"

    vocab_terms = load_vocab_terms(vocab_file)
    specs, skipped = _build_run_specs(args, models_dir, fork_bin, vulkan_bin, vocab_terms)
    if not specs:
        print("No runnable model configurations found.", file=sys.stderr)
        if skipped:
            print_summary_table([], skipped)
        return 1

    postprocess_script = Path(args.postprocess_script).expanduser() if args.postprocess_script else (
        Path(__file__).resolve().parent / "voxtype-postprocess.sh"
    )
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else default_config_dir()

    all_results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="shuush-bench-run.") as td:
        td_path = Path(td)
        for spec in specs:
            cfg_path = td_path / f"{re.sub(r'[^a-zA-Z0-9_.-]', '_', spec.name)}.toml"
            cfg_path.write_text(build_temp_config(spec, vocab_file, corrections_file), encoding="utf-8")

            clip_rows: list[dict] = []
            cleanup_rows: list[dict] = []

            for clip in clips:
                wav_path: Path = clip["_wav_path"]
                ref = clip["reference"]
                terms = clip.get("terms") or []
                is_q = clip.get("is_question")
                hyp, ms, err = transcribe_wav_offline(spec, wav_path, cfg_path, td_path, timeout_secs=args.timeout)
                sc = score_clip(ref, hyp, terms=terms, is_question=is_q)
                clip_rows.append({
                    "id": clip["id"],
                    "category": clip.get("category", "unknown"),
                    "reference": ref,
                    "hypothesis": hyp,
                    "latency_ms": ms,
                    "duration_secs": clip.get("duration_secs", 0.0),
                    "error": err,
                    **sc,
                })

                if args.cleanup:
                    if args.cleanup_sleep > 0 and cleanup_rows:
                        time.sleep(args.cleanup_sleep)
                    cleaned, pp_ms, pp_err = run_shuush_cleanup(
                        hyp, postprocess_script, mode=args.cleanup_mode, config_dir=config_dir,
                    )
                    csc = score_clip(ref, cleaned, terms=terms, is_question=is_q)
                    cleanup_rows.append({
                        "id": clip["id"],
                        "category": clip.get("category", "unknown"),
                        "reference": ref,
                        "hypothesis": cleaned,
                        "raw_hypothesis": hyp,
                        "latency_ms": ms + pp_ms,
                        "cleanup_ms": pp_ms,
                        "duration_secs": clip.get("duration_secs", 0.0),
                        "error": err or pp_err,
                        **csc,
                    })

            all_results.append({
                "name": spec.name,
                "engine": spec.engine,
                "model": spec.model_path,
                "binary": str(spec.binary),
                "vram_gb": spec.vram_gb,
                "options": {
                    "streaming": spec.streaming,
                    "streaming_final_pass": spec.streaming_final_pass,
                    "pad_secs": spec.pad_secs,
                    "use_rescore": spec.use_rescore,
                    "vocab_boost": spec.vocab_boost,
                    "initial_prompt": bool(spec.initial_prompt),
                },
                "note": spec.note,
                "summary": aggregate_scores(clip_rows),
                "clips": clip_rows,
            })

            if args.cleanup:
                all_results.append({
                    "name": f"{spec.name}+cleanup",
                    "engine": f"{spec.engine}+shuush",
                    "model": spec.model_path,
                    "binary": str(spec.binary),
                    "vram_gb": spec.vram_gb,
                    "options": {"cleanup": True, "cleanup_mode": args.cleanup_mode},
                    "note": f"After shuush {args.cleanup_mode} cleanup",
                    "summary": aggregate_scores(cleanup_rows),
                    "clips": cleanup_rows,
                })

    report = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dataset": str(manifest_path or (bench_dir / "manifest.jsonl")),
        "clip_count": len(clips),
        "vocab_file": str(vocab_file) if vocab_file else None,
        "corrections_file": str(corrections_file) if corrections_file else None,
        "results": all_results,
        "skipped_models": skipped,
    }

    saved_path: Path | None = None
    if not args.no_save:
        if args.out:
            saved_path = Path(args.out).expanduser()
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            saved_path = bench_dir / "results" / f"bench-{ts}.json"
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        saved_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(f"Shuush Accuracy Benchmark ({len(clips)} clips from {report['dataset']})\n")
    print_summary_table(all_results, skipped)

    if args.verbose:
        print("\nPer-clip mismatches:")
        for res in all_results:
            diffs = [c for c in res["clips"] if c["wer"] > 0 or c["last_word_lost"] or c["missed_terms"]]
            print(f"\n[{res['name']}] ({len(diffs)}/{len(res['clips'])} clips with differences)")
            for c in diffs:
                flags = []
                if c["missed_terms"]:
                    flags.append(f"missed={','.join(c['missed_terms'])}")
                if c["last_word_lost"]:
                    flags.append(f"tail:{c['ref_last']}->{c['hyp_last'] or '<empty>'}")
                if c["error"]:
                    flags.append(f"err={c['error']}")
                flag_s = f" ({'; '.join(flags)})" if flags else ""
                print(f"  {c['id']} WER={c['wer']*100:.1f}%{flag_s}")
                print(f"    REF: {c['reference']}")
                print(f"    HYP: {c['hypothesis']}")

    if saved_path:
        print(f"\nSaved full results -> {saved_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shuush bench",
        description="Measure and compare offline dictation accuracy across voxtype models and options.",
    )
    sub = p.add_subparsers(dest="subcmd", required=True)

    sp = sub.add_parser("prompts", help="Show the 42 curated read-aloud prompts")
    sp.add_argument("--category", action="append", choices=PROMPT_CATEGORIES, help="Filter by category")
    sp.add_argument("--from-vocab", nargs="?", const="", default=None, help="Check coverage against vocabulary.txt")
    sp.add_argument("--json", action="store_true", help="Output JSON")
    sp.set_defaults(func=cmd_prompts)

    sr = sub.add_parser("record", help="Interactively record prompts to ~/.local/share/shuush/bench/")
    sr.add_argument("--dir", help="Bench data directory (default: ~/.local/share/shuush/bench)")
    sr.add_argument("--category", action="append", choices=PROMPT_CATEGORIES, help="Record only selected categories")
    sr.add_argument("--only", help="Comma-separated prompt IDs to record")
    sr.add_argument("--re-record", action="store_true", help="Re-record already captured prompts")
    sr.add_argument("--recorder", choices=("pw-record", "parecord", "arecord"), help="Audio capture backend")
    sr.add_argument("--device", help="Audio input device/target")
    sr.set_defaults(func=cmd_record)

    si = sub.add_parser("import", help="Import an existing WAV (or batch manifest) with reference text")
    si.add_argument("wav", nargs="?", help="Path to WAV/audio file")
    si.add_argument("--ref", help="Reference text (prompts interactively if omitted)")
    si.add_argument("--id", help="Clip ID (default: filename stem)")
    si.add_argument("--category", default="imported", help="Category label")
    si.add_argument("--terms", help="Comma-separated names/terms expected in this clip")
    si.add_argument("--question", action="store_true", help="Mark clip as a question")
    si.add_argument("--overwrite", action="store_true", help="Overwrite existing clip ID")
    si.add_argument("--manifest", help="Batch-import clips from a JSONL manifest")
    si.add_argument("--dir", help="Target bench directory (default: ~/.local/share/shuush/bench)")
    si.set_defaults(func=cmd_import)

    sm = sub.add_parser("models", help="List Hex & voxtype model candidates, 6 GB VRAM fit, and fetch commands")
    sm.add_argument("--models-dir", help="Models directory (default: ~/.local/share/voxtype/models)")
    sm.add_argument("--voxtype-bin", help="Path to fork voxtype binary")
    sm.add_argument("--vulkan-bin", help="Path to stock Vulkan voxtype binary")
    sm.add_argument("--json", action="store_true", help="Output JSON")
    sm.set_defaults(func=cmd_models)

    srun = sub.add_parser("run", help="Transcribe recorded clips offline and score accuracy")
    srun.add_argument("--dir", help="Bench data directory containing manifest.jsonl")
    srun.add_argument("--manifest", help="Explicit path to a manifest.jsonl dataset")
    srun.add_argument("--models", nargs="*", default=None, help="Compare installed model candidates (or specific IDs)")
    srun.add_argument("--presets", help="Comma-separated Parakeet option presets (final+rescore,final+no-rescore,final+no-pad,no-final+no-rescore)")
    srun.add_argument("--voxtype-bin", help="Path to fork voxtype binary")
    srun.add_argument("--vulkan-bin", help="Path to stock Vulkan voxtype binary")
    srun.add_argument("--models-dir", help="Models directory")
    srun.add_argument("--vocab-file", help="Vocabulary file for rescoring / Whisper initial_prompt")
    srun.add_argument("--corrections-file", help="Corrections TSV for Parakeet rescoring")
    srun.add_argument("--vocab-boost", type=float, default=2.5, help="Parakeet vocabulary_boost in nats (default: 2.5)")
    srun.add_argument("--cleanup", action="store_true", help="Also run shuush cleanup (uses configured LLM providers)")
    srun.add_argument("--cleanup-mode", default="default", choices=("default", "terminal", "chat"), help="Cleanup profile mode")
    srun.add_argument("--cleanup-sleep", type=float, default=0.0, help="Seconds to sleep between cleanup LLM calls")
    srun.add_argument("--postprocess-script", help=argparse.SUPPRESS)
    srun.add_argument("--config-dir", help=argparse.SUPPRESS)
    srun.add_argument("--limit", type=int, default=0, help="Limit to first N clips")
    srun.add_argument("--timeout", type=int, default=120, help="Per-clip transcribe timeout in seconds")
    srun.add_argument("--out", help="Output JSON report path")
    srun.add_argument("--no-save", action="store_true", help="Do not save JSON report to disk")
    srun.add_argument("--verbose", "-v", action="store_true", help="Show per-clip mismatches")
    srun.add_argument("--json", action="store_true", help="Print JSON report to stdout")
    srun.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
