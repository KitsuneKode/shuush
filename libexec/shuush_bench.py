#!/usr/bin/env python3
"""shuush bench: offline dictation accuracy benchmark.

Subcommands:
  shuush bench prompts   List/export the curated read-aloud benchmark prompts
  shuush bench record    Interactively record prompts to ~/.local/share/shuush/bench/
  shuush bench import    Import an existing WAV (or batch manifest) with reference text
  shuush bench models    Show Hex / voxtype model candidates, 6 GB VRAM fit, fetch commands
  shuush bench run       Run offline voxtype transcribe across presets/models and score results
  shuush bench report    Print a saved report again
  shuush bench public    Fetch a small public LibriSpeech set to validate the pipeline
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
import threading
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
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    # Keep decimal points inside numbers (2.5) and dots in known tech names (next.js).
    s = re.sub(r"[-_/]+", " ", s)
    # Strip punctuation except internal apostrophes and internal dots between alnum chars.
    s = re.sub(r"(?<!\w)[.'\"`]+|[.'\"`]+(?!\w)", " ", s)
    s = re.sub(r"[^a-z0-9'.\s]", " ", s)
    return " ".join(s.split())


def normalize_words(text: str) -> list[str]:
    norm = normalize_text(text)
    return collapse_numbers(norm.split()) if norm else []


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


_NUM_TOKEN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _spoken_number_spans(tokens: list[str]) -> list[tuple[int, int, str]]:
    """(start, end, canonical digits) for each run of spoken number words in `tokens`."""
    spans: list[tuple[int, int, str]] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
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
                spans.append((i, j, _canonical_digit(val)))
                i = j
                continue
        i += 1
    return spans


def extract_numbers(text: str) -> list[str]:
    """Canonical numeric values from both digit forms ("404", "11,435") and spoken words."""
    tokens = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?|[a-zA-Z]+", text.lower())
    spans = {start: (end, val) for start, end, val in _spoken_number_spans(tokens)}
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if _NUM_TOKEN.fullmatch(tokens[i]):
            out.append(_canonical_digit(tokens[i]))
            i += 1
        elif i in spans:
            end, val = spans[i]
            out.append(val)
            i = end
        else:
            i += 1
    return out


def collapse_numbers(words: list[str]) -> list[str]:
    """Spoken numbers become their digits, so "forty five" and "45" are the same word."""
    spans = {start: (end, val) for start, end, val in _spoken_number_spans(words)}
    out: list[str] = []
    i = 0
    while i < len(words):
        if i in spans:
            end, val = spans[i]
            out.append(val)
            i = end
        else:
            out.append(_canonical_digit(words[i]) if _NUM_TOKEN.fullmatch(words[i]) else words[i])
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


def aggregate_scores(all_clips: list[dict]) -> dict:
    """Totals over the clips that were decoded. A clip whose decode failed (GPU out of memory, a crash)
    says nothing about accuracy, so it is counted in `errors` and left out of every rate."""
    ok = [r for r in all_clips if not r.get("error") or r.get("error") == "empty transcript"]
    out = _aggregate_ok(ok)
    out["errors"] = len(all_clips) - len(ok)
    if not ok:
        out["score_10"] = 0.0
    out["clips_total"] = len(all_clips)
    return out


def _aggregate_ok(clip_results: list[dict]) -> dict:
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

    walls = [r["wall_ms"] for r in clip_results if isinstance(r.get("wall_ms"), (int, float))]
    infers = [r["infer_ms"] for r in clip_results if isinstance(r.get("infer_ms"), (int, float))]
    audio_secs = sum(float(r.get("duration_secs") or 0.0) for r in clip_results)
    infer_secs = sum(infers) / 1000.0
    rtf = (infer_secs / audio_secs) if audio_secs > 0 and infers else None
    vrams = [r["vram_mb"] for r in clip_results if isinstance(r.get("vram_mb"), (int, float))]
    errors = 0
    empty = sum(1 for r in clip_results if r.get("error") == "empty transcript")

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
        "median_wall_ms": int(round(statistics.median(walls))) if walls else None,
        "median_infer_ms": int(round(statistics.median(infers))) if infers else None,
        "vram_peak_mb": max(vrams) if vrams else None,
        "errors": errors,
        "empty": empty,
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
    """One way of running one model: engine, model, and the options that matter."""

    name: str
    binary: Path
    engine: str
    model_path: str
    streaming_pipeline: bool = False   # fork cache-aware decode: final pass, pad, rescoring
    pad_secs: float = 0.5
    use_rescore: bool = False
    vocab_boost: float = 2.5
    segment_secs: float = 45.0
    gguf_arch: str = ""
    language: str = "en"
    initial_prompt: str = ""
    vram_est: str = ""
    note: str = ""


def _toml_str(val: str) -> str:
    return json.dumps(val)


def build_temp_config(
    spec: RunSpec,
    vocab_file: Path | None,
    corrections_file: Path | None,
) -> str:
    """A throwaway config: the model and decode options under test, nothing of the owner's.

    [text] is neutral so the score is the recogniser's: no replacements, no
    filler filtering, no spoken punctuation.
    """
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
        lines += ["[parakeet]", f"model = {_toml_str(spec.model_path)}"]
        if spec.model_path.endswith(".gguf"):
            lines.append('gguf_backend = "vulkan"')
        elif spec.streaming_pipeline:
            lines += [
                "streaming = true",
                "streaming_final_pass = true",
                f"pad_secs = {spec.pad_secs}",
                f"final_pass_segment_secs = {spec.segment_secs}",
                "idle_unload_secs = 0",
            ]
            if spec.use_rescore:
                if vocab_file and vocab_file.is_file():
                    lines.append(f"vocabulary_file = {_toml_str(str(vocab_file))}")
                if corrections_file and corrections_file.is_file():
                    lines.append(f"corrections_file = {_toml_str(str(corrections_file))}")
                lines.append(f"vocabulary_boost = {spec.vocab_boost}")
        else:
            lines += ["streaming = false", "idle_unload_secs = 0"]
    elif spec.engine == "whisper":
        lines += [
            "[whisper]",
            f"model = {_toml_str(spec.model_path)}",
            f"language = {_toml_str(spec.language or 'en')}",
            "translate = false",
            "flash_attention = true",
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
    """The transcript `voxtype transcribe <wav>` prints after its header lines."""
    if "No speech detected, skipping transcription." in stdout:
        return ""
    header_prefixes = (
        "Loading audio file:",
        "Audio format:",
        "Resampling from ",
        "Processing ",
        "VAD backend:",
        "VAD:",
    )
    lines = stdout.splitlines()
    last_header = -1
    for idx, line in enumerate(lines):
        if line.startswith(header_prefixes):
            last_header = idx
    return "\n".join(lines[last_header + 1:]).strip()


_LOADED_RE = re.compile(r"loaded in ([\d.]+)s")
_DONE_RE = re.compile(r"(?:transcription )?completed in ([\d.]+)s")


def _failure_line(log: str) -> str:
    """The line of a failed run that says why: an error line if there is one, else the last."""
    lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if re.search(r"(?i)\b(error|failed|not found|cannot|invalid|no such)\b", ln):
            return re.sub(r"^\S+\s+(?:ERROR|WARN)\s+", "", ln)[:200]
    return lines[-1][:200] if lines else ""


def _gpu_used_mb() -> int | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
        return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.split() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _gpu_total_mb() -> int | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
        return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.split() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _gpu_compute_apps() -> dict[int, int]:
    """pid -> MiB for every CUDA process on the GPU."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        r = subprocess.run(
            [exe, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    apps: dict[int, int] = {}
    for line in r.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            apps[int(parts[0])] = int(parts[1])
    return apps


def _descendants(pid: int) -> set[int]:
    """`pid` and every process below it (the GGUF worker is a child of voxtype)."""
    kids: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(entry))
    out, todo = {pid}, [pid]
    while todo:
        for kid in kids.get(todo.pop(), []):
            if kid not in out:
                out.add(kid)
                todo.append(kid)
    return out


class VramSampler:
    """Peak GPU memory of one command: its own CUDA processes if the driver lists them
    (exact), else the whole-GPU rise above the starting level (a Vulkan worker, or noise
    from other GPU users: approximate)."""

    def __init__(self, pid: int, interval: float = 0.25) -> None:
        self.pid = pid
        self.interval = interval
        self.base = _gpu_used_mb()
        self.peak_gpu = self.base
        self.peak_own = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> None:
        used = _gpu_used_mb()
        if used is not None and (self.peak_gpu is None or used > self.peak_gpu):
            self.peak_gpu = used
        mine = _descendants(self.pid)
        own = sum(mb for pid, mb in _gpu_compute_apps().items() if pid in mine)
        self.peak_own = max(self.peak_own, own)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self.base is not None:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.base is not None:
            self._thread.join(timeout=3)

    @property
    def peak_mb(self) -> int | None:
        if self.base is None:
            return None
        if self.peak_own:
            return self.peak_own
        return max(0, (self.peak_gpu or self.base) - self.base)


def transcribe_wav_offline(
    spec: RunSpec,
    wav_path: Path,
    cfg_path: Path,
    tmp_dir: Path,
    timeout_secs: int = 120,
    measure_vram: bool = False,
) -> dict:
    """`<voxtype> -c <cfg> transcribe <wav>`: the offline decode, never the daemon.

    Runs against a scratch config/runtime/state dir, so it cannot see or touch
    the daemon's. Returns text, wall ms (model load included), the inference
    time the binary logs, peak VRAM delta, and an error string or None.
    """
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_dir / "cfg")
    env["XDG_RUNTIME_DIR"] = str(tmp_dir / "rt")
    env["XDG_STATE_HOME"] = str(tmp_dir / "state")
    env["NO_COLOR"] = "1"
    env.pop("VOXTYPE_CONFIG", None)
    for sub in ("cfg", "rt", "state"):
        (tmp_dir / sub).mkdir(parents=True, exist_ok=True)
    (tmp_dir / "rt").chmod(0o700)

    out = {"text": "", "wall_ms": 0, "load_ms": None, "infer_ms": None, "vram_mb": None, "error": None}
    t0 = time.monotonic()
    r = None
    try:
        proc = subprocess.Popen(
            [str(spec.binary), "-c", str(cfg_path), "transcribe", str(wav_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        out["error"] = str(exc)
        out["wall_ms"] = int((time.monotonic() - t0) * 1000)
        return out
    sampler = VramSampler(proc.pid) if measure_vram else None
    if sampler:
        sampler.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout_secs)
        r = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        out["error"] = f"timeout after {timeout_secs}s"
    finally:
        if sampler:
            sampler.stop()
            out["vram_mb"] = sampler.peak_mb
    out["wall_ms"] = int((time.monotonic() - t0) * 1000)
    if r is None:
        return out
    log = r.stderr or ""
    m = _LOADED_RE.search(log)
    if m:
        out["load_ms"] = int(float(m.group(1)) * 1000)
    m = _DONE_RE.search(log)
    if m:
        out["infer_ms"] = int(float(m.group(1)) * 1000)
    elif out["load_ms"] is not None:
        # the streaming transcriber logs only its load: decode time is what is left of the wall clock
        out["infer_ms"] = max(0, out["wall_ms"] - out["load_ms"])
    if r.returncode != 0:
        out["error"] = _failure_line(r.stderr or r.stdout) or f"exit {r.returncode}"
        return out
    out["text"] = parse_voxtype_transcribe_stdout(r.stdout)
    if not out["text"]:
        out["error"] = "empty transcript"
    return out


_TRANSIENT = re.compile(r"(?i)onnx runtime|cuda|out of memory|vulkan|device|alloc|timeout")


def transcribe_with_retry(spec: RunSpec, wav_path: Path, cfg_path: Path, tmp_dir: Path, *,
                          timeout_secs: int, measure_vram: bool, retries: int) -> dict:
    """A decode that failed on the GPU (another process took the memory) is tried again, a few seconds
    apart; the result says how many attempts it took. Anything else fails at once."""
    res = transcribe_wav_offline(spec, wav_path, cfg_path, tmp_dir, timeout_secs, measure_vram)
    res["attempts"] = 1
    while res["error"] and res["error"] != "empty transcript" and _TRANSIENT.search(res["error"]) \
            and res["attempts"] <= retries:
        time.sleep(3 * res["attempts"])
        again = transcribe_wav_offline(spec, wav_path, cfg_path, tmp_dir, timeout_secs, measure_vram)
        again["attempts"] = res["attempts"] + 1
        res = again
    return res


_CLEANUP_CONFIG_FILES = (
    "config.toml", "postprocess.env", "secrets.env", "vocabulary.txt",
    "parakeet-vocabulary.txt", "corrections.tsv",
)


class CleanupRunner:
    """shuush's cleanup (voxtype-postprocess.sh) on a copy of the owner's config.

    Only for `bench run --cleanup`: it calls the owner's configured providers
    (network, API keys from secrets.env) once per clip. The script runs against
    a scratch copy of the config files, a scratch runtime/state dir and stubbed
    desktop commands, so nothing in ~/.config/voxtype is written and no window,
    clipboard or compositor is read.
    """

    def __init__(self, script: Path, config_dir: Path, scratch: Path, mode: str = "default") -> None:
        self.script = script
        self.mode = mode
        home = scratch / "cleanup"
        cfg = home / "cfg" / "voxtype"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg.parent).chmod(0o700)
        for name in _CLEANUP_CONFIG_FILES:
            src = config_dir / name
            if src.is_file():
                shutil.copy2(src, cfg / name)
                if name == "secrets.env":
                    (cfg / name).chmod(0o600)
        for sub in ("snippets", "vocabulary.d"):
            if (config_dir / sub).is_dir():
                shutil.copytree(config_dir / sub, cfg / sub, dirs_exist_ok=True)
        shim = home / "shim"
        shim.mkdir(exist_ok=True)
        for cmd in ("wl-paste", "wl-copy", "hyprctl", "niri", "swaymsg", "notify-send", "kitten"):
            stub = shim / cmd
            stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            stub.chmod(0o755)
        (home / "rt" / "voxtype").mkdir(parents=True, exist_ok=True)
        (home / "rt").chmod(0o700)
        (home / "state").mkdir(exist_ok=True)
        env = dict(os.environ)
        env["PATH"] = f"{shim}:{env.get('PATH', '')}"
        env["XDG_CONFIG_HOME"] = str(cfg.parent)
        env["XDG_RUNTIME_DIR"] = str(home / "rt")
        env["XDG_STATE_HOME"] = str(home / "state")
        env["VOXTYPE_STREAM_OUTPUT"] = "0"
        env["PP_NO_HISTORY"] = "1"
        env["PP_WATCH_EDITS"] = "0"
        self.env = env

    def run(self, raw_text: str, timeout_secs: int = 30) -> tuple[str, int, str | None]:
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                ["bash", str(self.script), self.mode],
                input=raw_text, capture_output=True, text=True, env=self.env, timeout=timeout_secs,
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
    print("Controls: [Enter] start/stop, [s] skip, [q] quit.")
    print("Read each line once, naturally. Press Enter to stop right after the last word, as you would release the")
    print("dictation key: the clips are meant to catch a clipped last word. Quiet room, your usual microphone.\n")

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


def free_gb(path: Path) -> float | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / 1e9
    except OSError:
        return None


def _runner_for(
    cand: ModelCandidate,
    fork_bin: Path | None,
    fork_engines: set[str],
    vulkan_bin: Path | None,
    vulkan_engines: set[str],
) -> tuple[Path | None, str]:
    """The voxtype binary that can run `cand`, or (None, why not)."""
    if cand.runner == "none":
        return None, "voxtype has no engine for it"
    if cand.runner == "fork":
        if fork_bin and cand.engine in fork_engines:
            return fork_bin, ""
        return None, f"needs the fork binary with the '{cand.engine}' engine"
    if vulkan_bin and cand.engine in vulkan_engines:
        return vulkan_bin, ""
    return None, f"needs /usr/lib/voxtype/voxtype-vulkan with '{cand.engine}'"


def _candidate_status(
    cand: ModelCandidate,
    models_dir: Path,
    fork_bin: Path | None,
    fork_engines: set[str],
    vulkan_bin: Path | None,
    vulkan_engines: set[str],
) -> dict:
    installed_path = locate_model(cand, models_dir)
    runner, why = _runner_for(cand, fork_bin, fork_engines, vulkan_bin, vulkan_engines)
    return {
        **dataclasses.asdict(cand),
        "installed": installed_path is not None,
        "installed_path": str(installed_path) if installed_path else None,
        "runner_binary": str(runner) if runner else None,
        "engine_compiled": runner is not None,
        "status_note": why,
    }


def fetch_command(cand: ModelCandidate, fork_bin: Path | None, vulkan_bin: Path | None) -> str:
    """The download command. XDG_CONFIG_HOME is scratch so nothing can touch the daemon's config."""
    if cand.runner == "none":
        return cand.fetch_name
    binary = fork_bin if cand.runner == "fork" else (vulkan_bin or Path(DEFAULT_VULKAN_BIN))
    binary = binary or Path("voxtype")
    return f'XDG_CONFIG_HOME="$(mktemp -d)" {binary} setup --download --model {cand.fetch_name}'


def _disk_mb_label(mb: int) -> str:
    return "-" if mb <= 0 else (f"{mb} MB" if mb < 1000 else f"{mb / 1000:.2f} GB")


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
    free = free_gb(models_dir)
    if args.json:
        print(json.dumps({
            "models_dir": str(models_dir),
            "disk_free_gb": round(free, 1) if free is not None else None,
            "fork_binary": str(fork_bin) if fork_bin else None,
            "fork_engines": sorted(fork_engines),
            "vulkan_binary": str(vulkan_bin) if vulkan_bin else None,
            "vulkan_engines": sorted(vulkan_engines),
            "gpu_vram_budget_gb": 6,
            "candidates": rows,
        }, indent=2))
        return 0

    print("ASR model candidates (Hex's list, as far as voxtype can run it)")
    print(f"  models dir:  {models_dir}" + (f"   ({free:.1f} GB free)" if free is not None else ""))
    print(f"  fork binary: {fork_bin or 'not found'}  [{', '.join(sorted(fork_engines)) or '-'}]")
    print(f"  vulkan bin:  {vulkan_bin or 'not found'}  [{', '.join(sorted(vulkan_engines)) or '-'}]\n")

    headers = ("ID", "Engine", "Runs on", "Status", "Disk", "VRAM (est)", "6 GB", "Hex WER", "Vocabulary")
    table: list[tuple[str, ...]] = []
    for r in rows:
        if r["runner"] == "none":
            st = "not in voxtype"
        elif r["installed"] and r["engine_compiled"]:
            st = "ready"
        elif r["installed"]:
            st = "installed, no binary"
        elif r["engine_compiled"]:
            st = "fetch"
        else:
            st = "needs build"
        wer = f"{r['hex_wer']:.2f}%" if r["hex_wer"] is not None else "-"
        vocab = {"rescore": "acoustic rescoring", "prompt": "initial_prompt", "none": "none"}[r["vocab"]]
        table.append((r["id"], r["engine"], r["runner"], st, _disk_mb_label(r["disk_mb"]),
                      r["vram_est"], r["fits_6gb"], wer, vocab))
    widths = [max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in table:
        print(fmt.format(*row))
    print("\nHex WER is Hex's published LibriSpeech test-clean figure, for orientation; `shuush bench run` measures your own voice.")
    print("VRAM is an estimate on top of the ~1.2 GB the desktop holds; `bench run` records the real peak for what it runs.")

    todo = [(r, c) for r, c in zip(rows, MODEL_CATALOG) if not r["installed"] and r["runner"] != "none"]
    if todo:
        print("\nFetch (check `df -h /` first; downloading never selects the model, the daemon is untouched):")
        for r, c in todo:
            print(f"  {r['id']:<24} {_disk_mb_label(r['disk_mb']):>8}   {fetch_command(c, fork_bin, vulkan_bin)}")
        print("\nIf the models dir is on a full disk, add XDG_DATA_HOME=/path/with/space to the command and "
              "pass --models-dir /path/with/space/voxtype/models to `bench run`.")
    for r in rows:
        if r["runner"] == "none":
            print(f"\n{r['id']}: {r['notes']}")
    return 0


def live_parakeet_settings(config_dir: Path) -> dict:
    """[parakeet] from the live config.toml, read only."""
    path = config_dir / "config.toml"
    if not path.is_file():
        return {}
    try:
        import tomllib
        doc = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, ImportError):
        return {}
    return dict(doc.get("parakeet") or {})


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def resolve_vocab_files(args: argparse.Namespace, bench_dir: Path, config_dir: Path) -> tuple[Path | None, Path | None]:
    """Vocabulary and corrections for rescoring: flag, bench dir, the daemon's own setting, then the config dir."""
    live = live_parakeet_settings(config_dir)

    def pick(flag: str | None, name: str, live_key: str, extra: tuple[str, ...]) -> Path | None:
        if flag:
            return _expand(flag)
        if (bench_dir / name).is_file():
            return bench_dir / name
        if live.get(live_key):
            p = _expand(str(live[live_key]))
            if p.is_file():
                return p
        for alt in extra:
            if (config_dir / alt).is_file():
                return config_dir / alt
        return None

    vocab = pick(args.vocab_file, "vocabulary.txt", "vocabulary_file", ("parakeet-vocabulary.txt", "vocabulary.txt"))
    corr = pick(args.corrections_file, "corrections.tsv", "corrections_file", ("corrections.tsv",))
    return vocab, corr


def _parse_floats(raw: str | None) -> list[float]:
    out: list[float] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(float(part))
            except ValueError:
                raise SystemExit(f"--vocab-boost: '{part}' is not a number")
    return out


PARAKEET_PRESETS = {
    "daemon": "pad 0.5 s + acoustic rescoring: what the daemon's final pass does",
    "no-rescore": "pad 0.5 s, rescoring off",
    "no-pad": "rescoring on, no tail padding (isolates last-word clipping)",
    "bare": "no padding, no rescoring: the plain whole-recording decode",
}


def _parakeet_spec(cand: ModelCandidate, runner: Path, mpath: str, preset: str, boost: float,
                   vram: str) -> RunSpec:
    return RunSpec(
        name=f"{cand.id}:{preset}",
        binary=runner, engine="parakeet", model_path=mpath, streaming_pipeline=True,
        pad_secs=0.0 if preset in ("no-pad", "bare") else 0.5,
        use_rescore=preset in ("daemon", "no-pad") or preset.startswith("boost="),
        vocab_boost=boost, vram_est=vram, note=PARAKEET_PRESETS.get(preset, f"rescoring boost {boost}"),
    )


def _build_run_specs(
    args: argparse.Namespace,
    models_dir: Path,
    fork_bin: Path | None,
    vulkan_bin: Path | None,
    vocab_terms: list[str],
) -> tuple[list[RunSpec], list[dict]]:
    fork_engines = probe_compiled_engines(fork_bin)
    vulkan_engines = probe_compiled_engines(vulkan_bin)
    boosts = _parse_floats(args.vocab_boost)
    specs: list[RunSpec] = []
    skipped: list[dict] = []
    whisper_prompt = (", ".join(vocab_terms[:40]) + ".") if vocab_terms else ""

    def add_streaming_model(cand: ModelCandidate, runner: Path, mpath: str, presets: list[str]) -> None:
        for preset in presets:
            specs.append(_parakeet_spec(cand, runner, mpath, preset, 2.5, cand.vram_est))
        for b in boosts:
            specs.append(_parakeet_spec(cand, runner, mpath, f"boost={b:g}", b, cand.vram_est))

    if args.models is not None:
        if not args.models or args.models == ["all"]:
            candidates = list(MODEL_CATALOG)
        else:
            candidates = []
            for raw in args.models:
                for mid in raw.split(","):
                    mid = mid.strip()
                    if mid and mid not in MODEL_BY_ID:
                        raise SystemExit(f"Unknown model '{mid}'. `shuush bench models` lists the IDs.")
                    if mid:
                        candidates.append(MODEL_BY_ID[mid])
        for cand in candidates:
            st = _candidate_status(cand, models_dir, fork_bin, fork_engines, vulkan_bin, vulkan_engines)
            if not st["installed"] or not st["engine_compiled"]:
                skipped.append({
                    "id": cand.id,
                    "reason": st["status_note"] or ("not installed" if not st["installed"] else "not runnable"),
                    "disk_mb": cand.disk_mb, "vram_est": cand.vram_est, "fits_6gb": cand.fits_6gb,
                    "fetch_cmd": fetch_command(cand, fork_bin, vulkan_bin),
                })
                continue
            runner, mpath = Path(st["runner_binary"]), st["installed_path"]
            if cand.streaming_pipeline:
                add_streaming_model(cand, runner, mpath, ["daemon", "no-rescore"])
            elif cand.engine == "whisper":
                specs.append(RunSpec(name=f"{cand.id}:prompt", binary=runner, engine="whisper", model_path=mpath,
                                     initial_prompt=whisper_prompt, vram_est=cand.vram_est,
                                     note="vocabulary as initial_prompt"))
                specs.append(RunSpec(name=f"{cand.id}:plain", binary=runner, engine="whisper", model_path=mpath,
                                     vram_est=cand.vram_est))
            else:
                specs.append(RunSpec(name=cand.id, binary=runner, engine=cand.engine, model_path=mpath,
                                     gguf_arch=cand.gguf_arch, language=cand.language, vram_est=cand.vram_est))
        return specs, skipped

    if not fork_bin:
        raise SystemExit("No voxtype binary found. Pass --voxtype-bin or set SHUUSH_VOXTYPE_BIN.")
    primary = None
    for cid in ("parakeet-unified-fp16", "parakeet-unified-fp32"):
        cand = MODEL_BY_ID[cid]
        mpath = locate_model(cand, models_dir)
        if mpath:
            primary = (cand, mpath)
            break
    if primary is None:
        raise SystemExit(f"No Parakeet Unified model in {models_dir}. `shuush bench models` shows how to fetch one.")
    wanted = [p.strip() for p in (args.presets or "").split(",") if p.strip()] or list(PARAKEET_PRESETS)
    for w in wanted:
        if w not in PARAKEET_PRESETS:
            raise SystemExit(f"Unknown preset '{w}'. Choose from: {', '.join(PARAKEET_PRESETS)}")
    add_streaming_model(primary[0], fork_bin, str(primary[1]), wanted)
    return specs, skipped


def _fmt_pct(val: float | None) -> str:
    return "-" if val is None else f"{val * 100:.1f}%"


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*r))


def print_summary_table(results: list[dict], skipped: list[dict]) -> None:
    rows: list[tuple[str, ...]] = []
    for res in results:
        agg = res["summary"]
        rows.append((
            res["name"],
            f"{agg['score_10']:.2f}",
            _fmt_pct(agg["wer"]),
            _fmt_pct(agg["term_recall"]),
            _fmt_pct(agg["num_preservation"]),
            _fmt_pct(agg["neg_preservation"]),
            _fmt_pct(agg["last_word_loss"]),
            _fmt_pct(agg["question_preservation"]),
            f"{agg['median_infer_ms']}" if agg.get("median_infer_ms") is not None else "-",
            f"{agg['median_wall_ms']}" if agg.get("median_wall_ms") is not None else "-",
            f"{agg['vram_peak_mb'] / 1000:.1f} GB" if agg.get("vram_peak_mb") else (res.get("vram_est") or "-"),
            str(agg["errors"]) if agg.get("errors") else "",
        ))
    _table(("Config", "Score", "WER", "Terms", "Nums", "Negs", "TailLoss", "Q mark", "Infer ms", "Wall ms", "VRAM", "Err"), rows)
    print("\nScore = 10 x weighted mean of (1-WER) .45, (1-tail loss) .10, term recall .25, number .10, negation .10"
          " (absent metrics drop out). Infer ms = the model's own decode, Wall ms adds the model load (each clip is a"
          " fresh process). VRAM is the measured peak (the process's own CUDA memory, else the GPU's rise) when it shows GB, else an estimate.")

    cats: list[str] = []
    for res in results:
        for c in res["clips"]:
            if c["category"] not in cats:
                cats.append(c["category"])
    if len(cats) > 1:
        print("\nWER by category")
        crow: list[tuple[str, ...]] = []
        for res in results:
            cells = [res["name"]]
            for cat in cats:
                sel = [c for c in res["clips"] if c["category"] == cat
                       and (not c.get("error") or c["error"] == "empty transcript")]
                n = sum(c["ref_words"] for c in sel)
                e = sum(c["substitutions"] + c["deletions"] + c["insertions"] for c in sel)
                cells.append(f"{e / n * 100:.1f}%" if n else "-")
            crow.append(tuple(cells))
        _table(("Config", *cats), crow)

    if skipped:
        print("\nNot run:")
        for s in skipped:
            print(f"  - {s['id']} ({s['reason']}); {s['fetch_cmd']}")


def print_clip_diffs(results: list[dict]) -> None:
    print("\nClips with differences:")
    for res in results:
        diffs = [c for c in res["clips"] if c["wer"] > 0 or c["last_word_lost"] or c["missed_terms"] or c.get("error")]
        print(f"\n[{res['name']}] {len(diffs)}/{len(res['clips'])} clips")
        for c in diffs:
            flags = []
            if c["missed_terms"]:
                flags.append("missed " + ",".join(c["missed_terms"]))
            if c["last_word_lost"]:
                flags.append(f"tail {c['ref_last']}->{c['hyp_last'] or '<none>'}")
            if c.get("error"):
                flags.append(f"error: {c['error']}")
            print(f"  {c['id']}  WER {c['wer'] * 100:.0f}%" + (f"  ({'; '.join(flags)})" if flags else ""))
            print(f"    REF {c['reference']}")
            print(f"    HYP {c['hypothesis']}")


def cmd_run(args: argparse.Namespace) -> int:
    bench_dir = Path(args.dir).expanduser() if args.dir else default_bench_dir()
    manifest_path = Path(args.manifest).expanduser() if args.manifest else None
    clips = load_manifest(bench_dir, manifest_path)
    if not clips:
        loc = manifest_path or (bench_dir / "manifest.jsonl")
        print(f"No clips at {loc}.\nRecord the prompts with `shuush bench record`, import a WAV with "
              "`shuush bench import`, or fetch the public set with `shuush bench public`.", file=sys.stderr)
        return 1
    if args.category:
        clips = [c for c in clips if c.get("category") in set(args.category)]
    if args.limit and args.limit > 0:
        clips = clips[: args.limit]
    if not clips:
        print("No clips match.", file=sys.stderr)
        return 1

    models_dir = Path(args.models_dir).expanduser() if args.models_dir else default_models_dir()
    fork_bin = resolve_fork_binary(args.voxtype_bin)
    vulkan_bin = resolve_vulkan_binary(args.vulkan_bin)
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else default_config_dir()
    vocab_file, corrections_file = resolve_vocab_files(args, bench_dir, config_dir)
    vocab_terms = load_vocab_terms(vocab_file)
    specs, skipped = _build_run_specs(args, models_dir, fork_bin, vulkan_bin, vocab_terms)
    if not specs:
        print("Nothing runnable.", file=sys.stderr)
        print_summary_table([], skipped)
        return 1

    postprocess_script = Path(args.postprocess_script).expanduser() if args.postprocess_script else (
        Path(__file__).resolve().parent / "voxtype-postprocess.sh"
    )
    log = (lambda *a: None) if args.json else (lambda *a: print(*a, file=sys.stderr))
    used, total = _gpu_used_mb(), _gpu_total_mb()
    if used is not None and total is not None and total - used < 4000 and any(sp.binary == fork_bin for sp in specs):
        busy = ", ".join(f"pid {pid} {mb} MB" for pid, mb in _gpu_compute_apps().items()) or "see nvidia-smi"
        log(f"warning: only {total - used} MB of GPU memory is free ({busy}). Parakeet needs ~2.5-3.6 GB; clips that "
            "hit out-of-memory are retried and, if they keep failing, left out of the scores (Err column).")
    log(f"{len(clips)} clips x {len(specs)} configs; vocabulary {vocab_file or 'none'}"
        f" ({len(vocab_terms)} terms), corrections {corrections_file or 'none'}")

    all_results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="shuush-bench-run.") as td:
        td_path = Path(td)
        cleaner = CleanupRunner(postprocess_script, config_dir, td_path, args.cleanup_mode) if args.cleanup else None
        for si, spec in enumerate(specs, 1):
            cfg_path = td_path / f"{re.sub(r'[^a-zA-Z0-9_.-]', '_', spec.name)}.toml"
            cfg_path.write_text(build_temp_config(spec, vocab_file, corrections_file), encoding="utf-8")
            log(f"[{si}/{len(specs)}] {spec.name}")
            clip_rows: list[dict] = []
            cleanup_rows: list[dict] = []
            for clip in clips:
                ref, terms, is_q = clip["reference"], clip.get("terms") or [], clip.get("is_question")
                tr = transcribe_with_retry(spec, clip["_wav_path"], cfg_path, td_path, timeout_secs=args.timeout,
                                           measure_vram=not args.no_vram, retries=args.retries)
                sc = score_clip(ref, tr["text"], terms=terms, is_question=is_q)
                base = {
                    "id": clip["id"], "category": clip.get("category", "unknown"), "reference": ref,
                    "duration_secs": clip.get("duration_secs", 0.0),
                }
                clip_rows.append({
                    **base, "hypothesis": tr["text"], "latency_ms": tr["wall_ms"], "wall_ms": tr["wall_ms"],
                    "infer_ms": tr["infer_ms"], "load_ms": tr["load_ms"], "vram_mb": tr["vram_mb"],
                    "attempts": tr["attempts"], "error": tr["error"], **sc,
                })
                if cleaner:
                    if args.cleanup_sleep > 0 and cleanup_rows:
                        time.sleep(args.cleanup_sleep)
                    cleaned, pp_ms, pp_err = cleaner.run(tr["text"])
                    csc = score_clip(ref, cleaned, terms=terms, is_question=is_q)
                    cleanup_rows.append({
                        **base, "hypothesis": cleaned, "raw_hypothesis": tr["text"],
                        "latency_ms": tr["wall_ms"] + pp_ms, "wall_ms": tr["wall_ms"] + pp_ms,
                        "infer_ms": tr["infer_ms"], "cleanup_ms": pp_ms, "vram_mb": tr["vram_mb"],
                        "error": tr["error"] or pp_err, **csc,
                    })
            all_results.append({
                "name": spec.name, "engine": spec.engine, "model": spec.model_path, "binary": str(spec.binary),
                "vram_est": spec.vram_est, "note": spec.note,
                "options": {
                    "streaming_pipeline": spec.streaming_pipeline, "pad_secs": spec.pad_secs,
                    "rescoring": spec.use_rescore, "vocab_boost": spec.vocab_boost if spec.use_rescore else None,
                    "initial_prompt": bool(spec.initial_prompt),
                },
                "summary": aggregate_scores(clip_rows), "clips": clip_rows,
            })
            if cleaner:
                all_results.append({
                    "name": f"{spec.name}+cleanup", "engine": f"{spec.engine}+shuush", "model": spec.model_path,
                    "binary": str(spec.binary), "vram_est": spec.vram_est,
                    "note": f"after shuush '{args.cleanup_mode}' cleanup (owner's providers)",
                    "options": {"cleanup": True, "cleanup_mode": args.cleanup_mode},
                    "summary": aggregate_scores(cleanup_rows), "clips": cleanup_rows,
                })

    report = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dataset": str(manifest_path or (bench_dir / "manifest.jsonl")),
        "clip_count": len(clips),
        "vocab_file": str(vocab_file) if vocab_file else None,
        "vocab_terms": len(vocab_terms),
        "corrections_file": str(corrections_file) if corrections_file else None,
        "binaries": {"fork": str(fork_bin) if fork_bin else None, "vulkan": str(vulkan_bin) if vulkan_bin else None},
        "results": all_results,
        "skipped_models": skipped,
    }
    saved_path: Path | None = None
    if not args.no_save:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        saved_path = Path(args.out).expanduser() if args.out else bench_dir / "results" / f"bench-{ts}.json"
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        saved_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"Shuush accuracy benchmark: {len(clips)} clips from {report['dataset']}\n")
    print_summary_table(all_results, skipped)
    if args.verbose:
        print_clip_diffs(all_results)
    if saved_path:
        print(f"\nSaved {saved_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    bench_dir = Path(args.dir).expanduser() if args.dir else default_bench_dir()
    path = Path(args.file).expanduser() if args.file else None
    if path is None:
        found = sorted((bench_dir / "results").glob("bench-*.json"))
        if not found:
            print(f"No saved results in {bench_dir / 'results'}.", file=sys.stderr)
            return 1
        path = found[-1]
    report = json.loads(path.read_text(encoding="utf-8"))
    print(f"{path}  ({report.get('timestamp', '?')}, {report.get('clip_count', '?')} clips)\n")
    print_summary_table(report["results"], report.get("skipped_models") or [])
    if args.verbose:
        print_clip_diffs(report["results"])
    return 0


PUBLIC_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows?dataset=openslr%2Flibrispeech_asr"
    "&config=clean&split=validation&offset=0&length=100"
)
PUBLIC_AUDIO_HOST = "datasets-server.huggingface.co"


def _http_get(url: str, limit: int) -> bytes:
    import urllib.parse
    import urllib.request
    host = urllib.parse.urlparse(url).hostname
    if urllib.parse.urlparse(url).scheme != "https" or host != PUBLIC_AUDIO_HOST:
        raise RuntimeError(f"refusing to fetch from {host}")
    req = urllib.request.Request(url, headers={"User-Agent": "shuush-bench"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("download larger than expected")
    return data


def cmd_public(args: argparse.Namespace) -> int:
    """A small public set (LibriSpeech dev-clean, CC BY 4.0) to check the pipeline on audio anyone can hear."""
    bench_dir = Path(args.dir).expanduser() if args.dir else default_bench_dir() / "public"
    free = free_gb(bench_dir)
    if free is not None and free < 5.0:
        print(f"Only {free:.1f} GB free; not downloading.", file=sys.stderr)
        return 1
    try:
        rows = json.loads(_http_get(PUBLIC_ROWS_URL, 5_000_000))["rows"]
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"shuush bench public: could not list clips: {exc}", file=sys.stderr)
        return 1
    usable = [r["row"] for r in rows if r.get("row") and r["row"].get("audio") and r["row"].get("text")]
    step = max(1, len(usable) // max(1, args.count))
    picked = usable[::step][: args.count]
    existing = {r["id"]: r for r in load_manifest(bench_dir)}
    bench_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    for row in picked:
        cid = str(row["id"])
        if cid in existing and not args.refresh:
            continue
        flac = None
        for attempt in range(3):
            try:
                flac = _http_get(row["audio"][0]["src"], 3_000_000)
                break
            except (RuntimeError, OSError) as exc:
                if attempt == 2:
                    print(f"  skip {cid}: {exc}", file=sys.stderr)
                else:
                    time.sleep(1.5 * (attempt + 1))
        if flac is None:
            continue
        src = bench_dir / f".{cid}.flac"
        src.write_bytes(flac)
        try:
            dur = ensure_pcm16_16k_mono(src, bench_dir / f"{cid}.wav")
        finally:
            src.unlink(missing_ok=True)
        if dur > args.max_secs:
            (bench_dir / f"{cid}.wav").unlink(missing_ok=True)
            continue
        existing[cid] = {
            "id": cid, "wav": f"{cid}.wav", "reference": str(row["text"]).capitalize(),
            "category": "librispeech", "terms": [], "is_question": False,
            "duration_secs": round(dur, 3), "source": "LibriSpeech dev-clean (CC BY 4.0), via Hugging Face datasets-server",
        }
        added += 1
    mpath = save_manifest(bench_dir, list(existing.values()))
    print(f"{added} new clip(s), {len(existing)} in {mpath}")
    print(f"Run:  shuush bench run --dir {bench_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shuush bench",
        description="Measure dictation accuracy offline: your own recordings through voxtype's offline decoder, "
        "under each model and option set, scored for WER, names, numbers, negations and lost last words.",
    )
    sub = p.add_subparsers(dest="subcmd", required=True)

    sp = sub.add_parser("prompts", help="Show the read-aloud prompts")
    sp.add_argument("--category", action="append", choices=PROMPT_CATEGORIES, help="Only this category")
    sp.add_argument("--from-vocab", nargs="?", const="", default=None, metavar="FILE",
                    help="Also list vocabulary terms no prompt covers (default file: ~/.config/voxtype/vocabulary.txt)")
    sp.add_argument("--json", action="store_true", help="Output JSON")
    sp.set_defaults(func=cmd_prompts)

    sr = sub.add_parser("record", help="Read each prompt aloud once; saves WAVs + references to the bench dir")
    sr.add_argument("--dir", help="Bench data directory (default: ~/.local/share/shuush/bench)")
    sr.add_argument("--category", action="append", choices=PROMPT_CATEGORIES, help="Only this category")
    sr.add_argument("--only", help="Comma-separated prompt IDs")
    sr.add_argument("--re-record", action="store_true", help="Record already captured prompts again")
    sr.add_argument("--recorder", choices=("pw-record", "parecord", "arecord"), help="Capture backend")
    sr.add_argument("--device", help="Audio input device/target")
    sr.set_defaults(func=cmd_record)

    si = sub.add_parser("import", help="Add an existing audio file (or a JSONL manifest) with its typed reference")
    si.add_argument("wav", nargs="?", help="Path to a WAV (or any ffmpeg-readable audio)")
    si.add_argument("--ref", help="Reference text (asked for if omitted)")
    si.add_argument("--id", help="Clip ID (default: file name)")
    si.add_argument("--category", default="imported", help="Category label")
    si.add_argument("--terms", help="Comma-separated names/terms this clip must contain")
    si.add_argument("--question", action="store_true", help="The clip is a question")
    si.add_argument("--overwrite", action="store_true", help="Replace an existing clip with this ID")
    si.add_argument("--manifest", help="Import every clip of a JSONL manifest")
    si.add_argument("--dir", help="Bench directory (default: ~/.local/share/shuush/bench)")
    si.set_defaults(func=cmd_import)

    sm = sub.add_parser("models", help="Hex/voxtype model candidates: runnable here?, size, VRAM, how to fetch")
    sm.add_argument("--models-dir", help="Models directory (default: ~/.local/share/voxtype/models)")
    sm.add_argument("--voxtype-bin", help="The fork voxtype binary")
    sm.add_argument("--vulkan-bin", help="The stock voxtype-vulkan binary")
    sm.add_argument("--json", action="store_true", help="Output JSON")
    sm.set_defaults(func=cmd_models)

    srun = sub.add_parser("run", help="Transcribe the clips offline under each config and score them")
    srun.add_argument("--dir", help="Bench directory holding manifest.jsonl (default: ~/.local/share/shuush/bench)")
    srun.add_argument("--manifest", help="Another manifest.jsonl")
    srun.add_argument("--models", nargs="*", default=None, metavar="ID",
                      help="Compare models (IDs from `bench models`, or none/'all' for every installed one) "
                      "instead of the daemon's option sets")
    srun.add_argument("--presets", help="Option sets for the Parakeet Unified daemon model, comma-separated: "
                      + ", ".join(PARAKEET_PRESETS) + " (default: all)")
    srun.add_argument("--vocab-boost", metavar="N[,N...]", help="Also run rescoring with these vocabulary_boost values (nats)")
    srun.add_argument("--category", action="append", help="Only clips of this category")
    srun.add_argument("--voxtype-bin", help="The fork voxtype binary")
    srun.add_argument("--vulkan-bin", help="The stock voxtype-vulkan binary")
    srun.add_argument("--models-dir", help="Models directory")
    srun.add_argument("--vocab-file", help="Vocabulary for rescoring and the Whisper prompt "
                      "(default: the daemon's own [parakeet] vocabulary_file)")
    srun.add_argument("--corrections-file", help="corrections.tsv for rescoring (default: the daemon's)")
    srun.add_argument("--cleanup", action="store_true",
                      help="Also score the text after shuush's cleanup. Calls your configured providers (network, "
                      "API keys) once per clip and config")
    srun.add_argument("--cleanup-mode", default="default", choices=("default", "terminal", "chat"))
    srun.add_argument("--cleanup-sleep", type=float, default=0.0, help="Seconds between cleanup calls (rate limits)")
    srun.add_argument("--postprocess-script", help=argparse.SUPPRESS)
    srun.add_argument("--config-dir", help=argparse.SUPPRESS)
    srun.add_argument("--limit", type=int, default=0, help="Only the first N clips")
    srun.add_argument("--timeout", type=int, default=120, help="Per-clip timeout, seconds")
    srun.add_argument("--retries", type=int, default=3,
                      help="Retries of a clip that failed on the GPU, e.g. out of memory (default 3)")
    srun.add_argument("--no-vram", action="store_true", help="Do not sample nvidia-smi for peak VRAM")
    srun.add_argument("--out", help="Write the JSON report here")
    srun.add_argument("--no-save", action="store_true", help="Do not save a JSON report")
    srun.add_argument("--verbose", "-v", action="store_true", help="Show every clip that differs from its reference")
    srun.add_argument("--json", action="store_true", help="Print the JSON report instead of the tables")
    srun.set_defaults(func=cmd_run)

    srep = sub.add_parser("report", help="Print a saved report again (newest by default)")
    srep.add_argument("file", nargs="?", help="Report JSON")
    srep.add_argument("--dir", help="Bench directory")
    srep.add_argument("--verbose", "-v", action="store_true")
    srep.set_defaults(func=cmd_report)

    spub = sub.add_parser("public", help="Fetch a small public LibriSpeech set to validate the pipeline")
    spub.add_argument("--count", type=int, default=25, help="Clips to fetch (default 25)")
    spub.add_argument("--max-secs", type=float, default=15.0, help="Skip clips longer than this")
    spub.add_argument("--dir", help="Where to put them (default: <bench dir>/public)")
    spub.add_argument("--refresh", action="store_true", help="Fetch clips already present again")
    spub.set_defaults(func=cmd_public)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
