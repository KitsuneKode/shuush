"""Tests for `shuush bench` (prompts, scoring, import, model catalog, offline runner).

Never runs the real voxtype binary, never touches ~/.config/voxtype or
~/.local/share/shuush, and never opens an audio input/output device.
"""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = ROOT / "libexec"
sys.path.insert(0, str(LIBEXEC))

import shuush_bench as bench  # noqa: E402
import shuush_bench_data as data  # noqa: E402


def _write_silence_wav(path: Path, secs: float = 0.5, rate: int = 16000) -> Path:
    n_frames = int(secs * rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * n_frames)
    return path


def _make_fake_voxtype(tmp_path: Path, name: str = "voxtype", engines: tuple[str, ...] = ("whisper", "parakeet")) -> Path:
    """Build a fake voxtype script that answers `info engines` and `transcribe <wav>`."""
    script = tmp_path / name
    eng_lines = "\n".join(f"  compiled  {e}" for e in engines)
    script.write_text(
        f"""#!/usr/bin/env python3
import os, re, sys, tomllib
from pathlib import Path

args = sys.argv[1:]
cfg_path = None
i = 0
rest = []
while i < len(args):
    if args[i] in ("-c", "--config") and i + 1 < len(args):
        cfg_path = Path(args[i + 1])
        i += 2
    else:
        rest.append(args[i])
        i += 1

if rest[:2] == ["info", "engines"]:
    print("Transcription engines")
    print({eng_lines!r})
    sys.exit(0)

if rest[:1] == ["transcribe"] and len(rest) >= 2:
    wav = Path(rest[1])
    cfg = tomllib.loads(cfg_path.read_text()) if cfg_path and cfg_path.is_file() else {{}}
    eng = cfg.get("engine", "parakeet")
    pk = cfg.get("parakeet", {{}})
    has_vocab = bool(pk.get("vocabulary_file"))
    pad = float(pk.get("pad_secs", 0.5))
    print(f'Loading audio file: "{{wav}}"')
    print("Audio format: 16000 Hz, 1 channel(s), Int")
    print("Processing 8000 samples (0.50s)...")
    print("")
    stem = wav.stem
    if stem == "kaliko":
        if has_vocab:
            print("Kaliko hesitated.")
        else:
            print("Kaligo hesitated.")
    elif stem == "tail":
        if pad > 0:
            print("Upgrade the component in shadcn.")
        else:
            print("Upgrade the component in shad")
    elif eng == "whisper":
        wh = cfg.get("whisper", {{}})
        if wh.get("initial_prompt"):
            print("Restart Hyprland and check 404 without errors?")
        else:
            print("Restart hyper land and check 404 without errors?")
    else:
        print("Restart Hyprland and check 404 without errors?")
    sys.exit(0)

print(f"unexpected args: {{args}}", file=sys.stderr)
sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


# ---------------------------------------------------------------------------
# 1. Prompts & privacy checks
# ---------------------------------------------------------------------------

def test_prompts_cover_all_categories_and_have_no_secrets():
    assert 38 <= len(data.PROMPTS) <= 50
    cats = {p.category for p in data.PROMPTS}
    assert cats == set(data.PROMPT_CATEGORIES)
    ids = [p.id for p in data.PROMPTS]
    assert len(ids) == len(set(ids)), "duplicate prompt IDs"

    # Guard against accidental secrets, keys, emails, or home paths
    forbidden = re.compile(r"(sk-[a-zA-Z0-9]{10,}|gsk_[a-zA-Z0-9]{10,}|@[a-z0-9.-]+\.[a-z]{2,}|/home/)")
    for p in data.PROMPTS:
        assert 5 <= len(p.text.split()) <= 20, f"prompt {p.id} length out of read-aloud range"
        assert not forbidden.search(p.text), f"forbidden pattern in {p.id}"


def test_prompts_cli_json_and_from_vocab(tmp_path: Path):
    vocab = tmp_path / "vocabulary.txt"
    vocab.write_text("# comment\nHyprland\nCustomToolXYZ\n", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), "prompts", "--json", "--from-vocab", str(vocab)],
        capture_output=True, text=True, check=True,
    )
    doc = json.loads(r.stdout)
    assert doc["count"] == len(data.PROMPTS)
    assert "CustomToolXYZ" in doc["uncovered_local_vocab"]
    assert "Hyprland" not in doc["uncovered_local_vocab"]


# ---------------------------------------------------------------------------
# 2. Scoring math
# ---------------------------------------------------------------------------

def test_wer_and_edit_ops():
    assert bench.word_edit_ops(["a", "b", "c"], ["a", "b", "c"]) == (0, 0, 0, 3)
    s, d, i, n = bench.word_edit_ops(["restart", "hyprland", "now"], ["restart", "hyper", "land"])
    assert n == 3
    assert s + d + i == 2

    sc = bench.score_clip("Kaliko hesitated.", "Kaligo hesitated.", terms=["Kaliko"])
    assert abs(sc["wer"] - 0.5) < 1e-6
    assert sc["term_hits"] == 0 and sc["term_total"] == 1
    assert not sc["last_word_lost"]

    sc_ok = bench.score_clip("Kaliko hesitated.", "Kaliko hesitated.", terms=["Kaliko"])
    assert sc_ok["wer"] == 0.0
    assert sc_ok["term_hits"] == 1 and sc_ok["term_exact"] == 1


def test_number_preservation_handles_spoken_and_digits():
    assert bench.extract_numbers("The webhook returned 404 and 200.") == ["404", "200"]
    assert bench.extract_numbers("The webhook returned four oh four and two hundred.") == ["404", "200"]
    assert bench.extract_numbers("Two hundred and five takes with four and six errors.") == ["205", "4", "6"]
    assert bench.extract_numbers("Target is eight point five out of ten.") == ["8.5", "10"]

    sc_good = bench.score_clip(
        "The webhook returned 404 before status 200.",
        "The webhook returned four oh four before status two hundred.",
    )
    assert sc_good["num_total"] == 2
    assert sc_good["num_preserved"] == 2

    sc_bad = bench.score_clip(
        "The webhook returned 404 before status 200.",
        "The webhook returned 500 before status 200.",
    )
    assert sc_bad["num_total"] == 2
    assert sc_bad["num_preserved"] == 1


def test_negation_preservation_catches_drops_and_flips():
    sc_ok = bench.score_clip(
        "Do not restart the service and never edit config.",
        "Don't restart the service and never edit config.",
    )
    assert sc_ok["neg_total"] == 2
    assert sc_ok["neg_matched"] == 2
    assert sc_ok["neg_spurious"] == 0

    sc_drop = bench.score_clip(
        "Do not restart the service.",
        "Restart the service.",
    )
    assert sc_drop["neg_total"] == 1
    assert sc_drop["neg_matched"] == 0

    sc_flip = bench.score_clip(
        "Restart the service now.",
        "Do not restart the service now.",
    )
    assert sc_flip["neg_total"] == 0
    assert sc_flip["neg_spurious"] == 1


def test_last_word_loss_catches_dropped_and_clipped_tails():
    assert bench.score_clip("Upgrade shadcn.", "Upgrade shad")["last_word_lost"] is True
    assert bench.score_clip("Upgrade shadcn.", "Upgrade")["last_word_lost"] is True
    assert bench.score_clip("Upgrade shadcn.", "")["last_word_lost"] is True
    assert bench.score_clip("Upgrade shadcn.", "Upgrade shadcn.")["last_word_lost"] is False
    assert bench.score_clip("Reload Quickshell.", "Reload quick shell.")["last_word_lost"] is False


def test_question_preservation_and_aggregate():
    c1 = bench.score_clip("Does Hyprland work?", "Does Hyprland work?", terms=["Hyprland"])
    c2 = bench.score_clip("Does Waybar work?", "Does Waybar work.", terms=["Waybar"])
    agg = bench.aggregate_scores([c1, c2])
    assert agg["wer"] == 0.0
    assert agg["term_recall"] == 1.0
    assert agg["question_preservation"] == 0.5
    assert agg["score_10"] == 10.0


# ---------------------------------------------------------------------------
# 3. Import, models catalog, offline run, and opt-in cleanup isolation
# ---------------------------------------------------------------------------

def test_import_wav_and_batch_manifest(tmp_path: Path):
    src_wav = _write_silence_wav(tmp_path / "source" / "sample_take.wav", secs=0.8)
    bench_dir = tmp_path / "bench"

    r = subprocess.run(
        [
            sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), "import",
            str(src_wav), "--dir", str(bench_dir),
            "--ref", "Restart Hyprland cleanly.", "--terms", "Hyprland", "--category", "names",
        ],
        capture_output=True, text=True, check=True,
    )
    assert "Imported sample_take" in r.stdout
    rows = bench.load_manifest(bench_dir)
    assert len(rows) == 1
    assert rows[0]["id"] == "sample_take"
    assert rows[0]["reference"] == "Restart Hyprland cleanly."
    assert rows[0]["terms"] == ["Hyprland"]
    assert (bench_dir / "sample_take.wav").is_file()


def test_models_catalog_lists_hex_candidates_and_vram(tmp_path: Path):
    fork_bin = _make_fake_voxtype(tmp_path, "voxtype-fork", engines=("whisper", "parakeet"))
    vk_bin = _make_fake_voxtype(tmp_path, "voxtype-vulkan", engines=("whisper", "parakeet", "cohere", "gguf"))
    models_dir = tmp_path / "models"
    (models_dir / "parakeet-unified-en-0.6b-fp16").mkdir(parents=True)

    r = subprocess.run(
        [
            sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), "models",
            "--json", "--models-dir", str(models_dir),
            "--voxtype-bin", str(fork_bin), "--vulkan-bin", str(vk_bin),
        ],
        capture_output=True, text=True, check=True,
    )
    doc = json.loads(r.stdout)
    by_id = {c["id"]: c for c in doc["candidates"]}
    assert by_id["parakeet-unified-fp16"]["installed"] is True
    assert by_id["parakeet-unified-fp16"]["engine_compiled"] is True
    assert by_id["cohere-transcribe-q4"]["engine_compiled"] is True
    assert by_id["cohere-transcribe-q4"]["installed"] is False
    assert by_id["qwen3-asr-0.6b"]["engine_compiled"] is True
    assert by_id["nemotron-3.5-asr"]["engine_compiled"] is True
    assert by_id["whisper-large-v3-turbo"]["supports_vocab_bias"] == "initial_prompt"
    assert by_id["granite-speech-5.0"]["engine"] == "unsupported"
    assert doc["gpu_vram_budget_gb"] == 6


def test_offline_run_presets_models_and_isolated_cleanup(tmp_path: Path):
    fork_bin = _make_fake_voxtype(tmp_path, "voxtype-fork", engines=("whisper", "parakeet"))
    vk_bin = _make_fake_voxtype(tmp_path, "voxtype-vulkan", engines=("whisper", "parakeet", "cohere", "gguf"))
    models_dir = tmp_path / "models"
    (models_dir / "parakeet-unified-en-0.6b-fp16").mkdir(parents=True)
    (models_dir / "ggml-large-v3-turbo.bin").write_bytes(b"fake")

    bench_dir = tmp_path / "bench"
    _write_silence_wav(bench_dir / "kaliko.wav")
    _write_silence_wav(bench_dir / "tail.wav")
    (bench_dir / "vocabulary.txt").write_text("Kaliko\nHyprland\nshadcn\n", encoding="utf-8")
    bench.save_manifest(bench_dir, [
        {"id": "kaliko", "wav": "kaliko.wav", "reference": "Kaliko hesitated.", "category": "names", "terms": ["Kaliko"]},
        {"id": "tail", "wav": "tail.wav", "reference": "Upgrade the component in shadcn.", "category": "last_word", "terms": ["shadcn"]},
    ])

    # 1. Option-set matrix (final+rescore vs final+no-rescore vs final+no-pad)
    out_json = tmp_path / "out.json"
    r = subprocess.run(
        [
            sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), "run",
            "--dir", str(bench_dir),
            "--models-dir", str(models_dir),
            "--voxtype-bin", str(fork_bin),
            "--vulkan-bin", str(vk_bin),
            "--presets", "final+rescore,final+no-rescore,final+no-pad",
            "--out", str(out_json),
        ],
        capture_output=True, text=True, check=True,
    )
    assert "parakeet:final+rescore" in r.stdout
    report = json.loads(out_json.read_text())
    by_name = {x["name"]: x["summary"] for x in report["results"]}
    # Rescore fixes Kaliko, pad=0.5 prevents tail clipping
    assert by_name["parakeet:final+rescore"]["wer"] == 0.0
    assert by_name["parakeet:final+rescore"]["term_recall"] == 1.0
    assert by_name["parakeet:final+rescore"]["last_word_loss"] == 0.0
    # Without rescore, Kaliko is missed
    assert by_name["parakeet:final+no-rescore"]["term_recall"] == 0.5
    # Without padding, last word is clipped
    assert by_name["parakeet:final+no-pad"]["last_word_loss"] == 0.5

    # 2. `--models` comparison across installed models + skipped uninstalled ones
    r2 = subprocess.run(
        [
            sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), "run",
            "--dir", str(bench_dir),
            "--models-dir", str(models_dir),
            "--voxtype-bin", str(fork_bin),
            "--vulkan-bin", str(vk_bin),
            "--models", "parakeet-unified-fp16,whisper-large-v3-turbo,cohere-transcribe-q4",
            "--no-save", "--json",
        ],
        capture_output=True, text=True, check=True,
    )
    doc2 = json.loads(r2.stdout)
    names2 = [x["name"] for x in doc2["results"]]
    assert "parakeet-unified-fp16:rescore" in names2
    assert "whisper-large-v3-turbo:prompt" in names2
    assert any(s["id"] == "cohere-transcribe-q4" for s in doc2["skipped_models"])

    # 3. Opt-in `--cleanup` uses isolated temp env with PP_NO_HISTORY=1
    stub_pp = tmp_path / "stub-postprocess.sh"
    env_dump = tmp_path / "pp-env.txt"
    stub_pp.write_text(
        f"""#!/usr/bin/env bash
printf 'PP_NO_HISTORY=%s\\nPP_WATCH_EDITS=%s\\nVOXTYPE_STREAM_OUTPUT=%s\\n' \
  "${{PP_NO_HISTORY:-}}" "${{PP_WATCH_EDITS:-}}" "${{VOXTYPE_STREAM_OUTPUT:-}}" > "{env_dump}"
sed 's/Kaligo/Kaliko/g'
""",
        encoding="utf-8",
    )
    stub_pp.chmod(0o755)
    r3 = subprocess.run(
        [
            sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), "run",
            "--dir", str(bench_dir),
            "--models-dir", str(models_dir),
            "--voxtype-bin", str(fork_bin),
            "--presets", "final+no-rescore",
            "--cleanup", "--postprocess-script", str(stub_pp),
            "--no-save", "--json",
        ],
        capture_output=True, text=True, check=True,
    )
    doc3 = json.loads(r3.stdout)
    by_name3 = {x["name"]: x["summary"] for x in doc3["results"]}
    assert by_name3["parakeet:final+no-rescore"]["term_recall"] == 0.5
    assert by_name3["parakeet:final+no-rescore+cleanup"]["term_recall"] == 1.0
    dumped = env_dump.read_text()
    assert "PP_NO_HISTORY=1" in dumped
    assert "PP_WATCH_EDITS=0" in dumped
    assert "VOXTYPE_STREAM_OUTPUT=0" in dumped


def test_shuush_cli_dispatches_bench():
    r = subprocess.run(
        [str(ROOT / "bin" / "shuush"), "bench", "prompts", "--json"],
        capture_output=True, text=True, check=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    doc = json.loads(r.stdout)
    assert doc["count"] == len(data.PROMPTS)
