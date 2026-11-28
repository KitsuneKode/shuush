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
    print("2026-11-28T00:00:00Z  INFO Parakeet model loaded in 1.50s", file=sys.stderr)
    print("2026-11-28T00:00:01Z  INFO Parakeet transcription completed in 0.25s (17 chars)", file=sys.stderr)
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
        limit = 34 if p.category == "long" else 20
        assert 5 <= len(p.text.split()) <= limit, f"prompt {p.id} length out of read-aloud range"
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
    assert by_id["parakeet-ultra-q8"]["runner_binary"] == str(vk_bin)
    assert by_id["parakeet-tdt-v2"]["runner_binary"] == str(fork_bin)
    assert by_id["whisper-large-v3-turbo"]["vocab"] == "prompt"
    assert by_id["granite-speech-5"]["engine_compiled"] is False
    assert doc["gpu_vram_budget_gb"] == 6

    # a fork binary without the GGUF engines cannot run Cohere: the report says so
    r2 = subprocess.run(
        [
            sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), "models", "--json",
            "--models-dir", str(models_dir), "--voxtype-bin", str(fork_bin), "--vulkan-bin", str(fork_bin),
        ],
        capture_output=True, text=True, check=True,
    )
    by_id2 = {c["id"]: c for c in json.loads(r2.stdout)["candidates"]}
    assert by_id2["cohere-transcribe-q4"]["engine_compiled"] is False
    assert "cohere" in by_id2["cohere-transcribe-q4"]["status_note"]


def test_catalog_is_consistent_and_fetch_commands_never_touch_the_daemon_config():
    ids = [m.id for m in data.MODEL_CATALOG]
    assert len(ids) == len(set(ids))
    for m in data.MODEL_CATALOG:
        assert m.runner in {"fork", "vulkan", "none"}
        assert m.vocab in {"rescore", "prompt", "none"}
        assert (m.runner == "none") == (m.engine == "unsupported")
        if m.runner != "none":
            assert m.model_ref and m.disk_mb > 0
            cmd = bench.fetch_command(m, Path("/opt/fork/voxtype"), Path("/usr/lib/voxtype/voxtype-vulkan"))
            assert cmd.startswith('XDG_CONFIG_HOME="$(mktemp -d)" ')
            assert f"--download --model {m.fetch_name}" in cmd or m.id.startswith("parakeet-unified")
        if m.engine == "gguf":
            assert m.gguf_arch
    assert data.MODEL_BY_ID["parakeet-unified-fp16"].streaming_pipeline


def test_every_prompt_term_is_in_the_public_vocabulary_seed():
    terms = {t.lower() for p in data.PROMPTS for t in p.terms}
    assert {"hyprland", "voxtype", "parakeet"} <= terms
    # nothing personal: no owner name, no client names
    private = {"manash", "kitsunekode", "barionene", "autotabi", "pulse", "skipper ui"}
    assert not private & terms


def test_numbers_are_compared_as_numbers_in_wer():
    assert bench.score_clip("We shipped forty five takes.", "We shipped 45 takes.")["wer"] == 0.0
    assert bench.score_clip("Port 11,435 is up.", "Port eleven thousand four hundred thirty five is up.")["wer"] == 0.0
    assert bench.score_clip("It returned 404.", "It returned four oh four.")["wer"] == 0.0
    assert bench.score_clip("It returned 404.", "It returned 405.")["wer"] > 0.0
    assert bench.score_clip("Set it to 8.5.", "Set it to eight point five.")["wer"] == 0.0


def test_parse_transcribe_stdout_keeps_multiline_text():
    out = (
        'Loading audio file: "/x.wav"\nAudio format: 16000 Hz, 1 channel(s), Int\n'
        "Processing 100 samples (0.01s)...\n\nFirst line.\n\nSecond paragraph.\n"
    )
    assert bench.parse_voxtype_transcribe_stdout(out) == "First line.\n\nSecond paragraph."
    assert bench.parse_voxtype_transcribe_stdout("VAD: 0.00s speech\nNo speech detected, skipping transcription.\n") == ""


def test_temp_config_only_sets_what_each_engine_can_use(tmp_path: Path):
    vocab = tmp_path / "v.txt"
    vocab.write_text("Kaliko\n")
    base = dict(binary=Path("/x"), engine="parakeet", model_path="/m/parakeet-unified-en-0.6b-fp16")
    daemon = bench.build_temp_config(
        bench.RunSpec(name="d", streaming_pipeline=True, use_rescore=True, vocab_boost=3.0, **base), vocab, None)
    assert "streaming = true" in daemon and f'vocabulary_file = "{vocab}"' in daemon
    assert "vocabulary_boost = 3.0" in daemon and "pad_secs = 0.5" in daemon
    bare = bench.build_temp_config(
        bench.RunSpec(name="b", streaming_pipeline=True, use_rescore=False, pad_secs=0.0, **base), vocab, None)
    assert "vocabulary_file" not in bare and "pad_secs = 0.0" in bare
    # the owner's [text] replacements never leak in
    assert "replacements = {}" in bare
    tdt = bench.build_temp_config(bench.RunSpec(name="t", **{**base, "model_path": "/m/parakeet-tdt-0.6b-v2"}), vocab, None)
    assert "streaming = false" in tdt and "vocabulary_file" not in tdt
    gguf = bench.build_temp_config(bench.RunSpec(name="g", **{**base, "model_path": "/m/parakeet-ultra-Q8_0.gguf"}), vocab, None)
    assert 'gguf_backend = "vulkan"' in gguf and "streaming" not in gguf
    cohere = bench.build_temp_config(
        bench.RunSpec(name="c", binary=Path("/x"), engine="cohere", model_path="/m/c.gguf"), None, None)
    assert "[cohere]" in cohere and 'gguf_backend = "vulkan"' in cohere
    qwen = bench.build_temp_config(
        bench.RunSpec(name="q", binary=Path("/x"), engine="gguf", model_path="/m/q.gguf", gguf_arch="qwen3_asr"), None, None)
    assert 'architecture = "qwen3_asr"' in qwen


def test_vocab_resolution_prefers_flag_then_bench_then_the_daemons_setting(tmp_path: Path):
    import argparse
    cfg = tmp_path / "voxtype"
    cfg.mkdir()
    (cfg / "vocabulary.txt").write_text("A\n")
    (cfg / "parakeet-vocabulary.txt").write_text("B\n")
    (cfg / "corrections.tsv").write_text("x\ty\n")
    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()
    ns = argparse.Namespace(vocab_file=None, corrections_file=None)
    # no [parakeet] setting: the shuush-managed list wins over the hand-edited one
    assert bench.resolve_vocab_files(ns, bench_dir, cfg)[0] == cfg / "parakeet-vocabulary.txt"
    (cfg / "config.toml").write_text(f'[parakeet]\nvocabulary_file = "{cfg}/vocabulary.txt"\n')
    assert bench.resolve_vocab_files(ns, bench_dir, cfg)[0] == cfg / "vocabulary.txt"
    (bench_dir / "vocabulary.txt").write_text("C\n")
    assert bench.resolve_vocab_files(ns, bench_dir, cfg)[0] == bench_dir / "vocabulary.txt"
    ns2 = argparse.Namespace(vocab_file=str(tmp_path / "mine.txt"), corrections_file=None)
    assert bench.resolve_vocab_files(ns2, bench_dir, cfg)[0] == tmp_path / "mine.txt"
    assert bench.resolve_vocab_files(ns, bench_dir, cfg)[1] == cfg / "corrections.tsv"


def _run_bench(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(LIBEXEC / "shuush_bench.py"), *args],
                          capture_output=True, text=True, **kw)


def _kaliko_bench(tmp_path: Path):
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
    common = ["--dir", str(bench_dir), "--models-dir", str(models_dir), "--voxtype-bin", str(fork_bin),
              "--vulkan-bin", str(vk_bin), "--config-dir", str(tmp_path / "no-config")]
    return bench_dir, common


def test_option_sets_isolate_rescoring_and_tail_padding(tmp_path: Path):
    bench_dir, common = _kaliko_bench(tmp_path)
    out_json = tmp_path / "out.json"
    r = _run_bench("run", *common, "--out", str(out_json), check=True)
    assert "parakeet-unified-fp16:daemon" in r.stdout
    assert "WER by category" in r.stdout
    report = json.loads(out_json.read_text())
    by = {x["name"]: x["summary"] for x in report["results"]}
    assert set(by) == {f"parakeet-unified-fp16:{p}" for p in bench.PARAKEET_PRESETS}
    d = by["parakeet-unified-fp16:daemon"]
    assert d["wer"] == 0.0 and d["term_recall"] == 1.0 and d["last_word_loss"] == 0.0
    assert by["parakeet-unified-fp16:no-rescore"]["term_recall"] == 0.5
    assert by["parakeet-unified-fp16:no-pad"]["last_word_loss"] == 0.5
    bare = by["parakeet-unified-fp16:bare"]
    assert bare["term_recall"] == 0.0 and bare["last_word_loss"] == 0.5
    # inference time comes from the binary's own log, not the wall clock
    assert d["median_infer_ms"] == 250 and d["median_wall_ms"] >= 0
    # the saved report prints again
    rep = _run_bench("report", str(out_json), check=True)
    assert "parakeet-unified-fp16:daemon" in rep.stdout
    # a boost sweep adds rescoring runs
    r2 = _run_bench("run", *common, "--presets", "daemon", "--vocab-boost", "1.5,4", "--no-save", "--json", check=True)
    names = [x["name"] for x in json.loads(r2.stdout)["results"]]
    assert names == ["parakeet-unified-fp16:daemon", "parakeet-unified-fp16:boost=1.5", "parakeet-unified-fp16:boost=4"]


def test_models_comparison_runs_what_is_installed_and_lists_the_rest(tmp_path: Path):
    bench_dir, common = _kaliko_bench(tmp_path)
    r = _run_bench("run", *common, "--models", "parakeet-unified-fp16,whisper-large-v3-turbo,cohere-transcribe-q4",
                   "--no-save", "--json", check=True)
    doc = json.loads(r.stdout)
    names = [x["name"] for x in doc["results"]]
    assert names == ["parakeet-unified-fp16:daemon", "parakeet-unified-fp16:no-rescore",
                     "whisper-large-v3-turbo:prompt", "whisper-large-v3-turbo:plain"]
    skipped = {s["id"]: s for s in doc["skipped_models"]}
    assert "not installed" in skipped["cohere-transcribe-q4"]["reason"]
    assert "setup --download --model cohere-transcribe-03-2026-Q4_K_M.gguf" in skipped["cohere-transcribe-q4"]["fetch_cmd"]
    assert _run_bench("run", *common, "--models", "nope", "--no-save").returncode != 0


def test_cleanup_runs_on_a_scratch_copy_and_never_writes_the_owners_config(tmp_path: Path):
    bench_dir, common = _kaliko_bench(tmp_path)
    cfg = tmp_path / "owner-config"
    cfg.mkdir()
    (cfg / "config.toml").write_text("# owner\n")
    (cfg / "secrets.env").write_text("GROQ_API_KEY=sk-test\n")
    (cfg / "secrets.env").chmod(0o600)
    (cfg / "vocabulary.txt").write_text("Kaliko\n")
    common = common[:-1] + [str(cfg)]
    stub_pp = tmp_path / "stub-postprocess.sh"
    env_dump = tmp_path / "pp-env.txt"
    stub_pp.write_text(
        f"""#!/usr/bin/env bash
printf 'PP_NO_HISTORY=%s\\nPP_WATCH_EDITS=%s\\nVOXTYPE_STREAM_OUTPUT=%s\\nCFG=%s\\n' \
  "${{PP_NO_HISTORY:-}}" "${{PP_WATCH_EDITS:-}}" "${{VOXTYPE_STREAM_OUTPUT:-}}" "${{XDG_CONFIG_HOME:-}}" > "{env_dump}"
echo touched > "${{XDG_CONFIG_HOME}}/voxtype/touched"
ls -l "${{XDG_CONFIG_HOME}}/voxtype/secrets.env" | cut -c1-10 > "{tmp_path}/secrets-mode.txt"
sed 's/Kaligo/Kaliko/g'
""",
        encoding="utf-8",
    )
    stub_pp.chmod(0o755)
    r = _run_bench("run", *common, "--presets", "no-rescore", "--cleanup", "--postprocess-script", str(stub_pp),
                   "--no-save", "--json", check=True)
    by = {x["name"]: x["summary"] for x in json.loads(r.stdout)["results"]}
    assert by["parakeet-unified-fp16:no-rescore"]["term_recall"] == 0.5
    assert by["parakeet-unified-fp16:no-rescore+cleanup"]["term_recall"] == 1.0
    dumped = env_dump.read_text()
    assert "PP_NO_HISTORY=1" in dumped and "PP_WATCH_EDITS=0" in dumped and "VOXTYPE_STREAM_OUTPUT=0" in dumped
    assert str(cfg) not in dumped.split("CFG=")[1]
    assert not (cfg / "touched").exists()
    assert sorted(p.name for p in cfg.iterdir()) == ["config.toml", "secrets.env", "vocabulary.txt"]
    assert (tmp_path / "secrets-mode.txt").read_text().strip() == "-rw-------"


def test_cleanup_is_off_unless_asked(tmp_path: Path):
    bench_dir, common = _kaliko_bench(tmp_path)
    marker = tmp_path / "ran"
    stub_pp = tmp_path / "pp.sh"
    stub_pp.write_text(f"#!/usr/bin/env bash\ntouch {marker}\ncat\n")
    stub_pp.chmod(0o755)
    _run_bench("run", *common, "--presets", "daemon", "--postprocess-script", str(stub_pp), "--no-save", "--json", check=True)
    assert not marker.exists()


def test_run_without_a_unified_model_says_how_to_get_one(tmp_path: Path):
    bench_dir, common = _kaliko_bench(tmp_path)
    r = _run_bench("run", *common[:2], "--models-dir", str(tmp_path / "empty"), *common[4:], "--no-save")
    assert r.returncode != 0 and "bench models" in r.stderr


def test_public_set_downloads_only_from_the_public_host(tmp_path: Path, monkeypatch):
    import argparse
    rows = {"rows": [
        {"row": {"id": f"100-{i}", "text": f"SOME WORDS NUMBER {i}", "audio": [{"src": f"https://{bench.PUBLIC_AUDIO_HOST}/a/{i}.flac"}]}}
        for i in range(10)
    ]}
    wav = _write_silence_wav(tmp_path / "src.wav", secs=1.0)

    def fake_get(url: str, limit: int) -> bytes:
        if url == bench.PUBLIC_ROWS_URL:
            return json.dumps(rows).encode()
        return wav.read_bytes()

    monkeypatch.setattr(bench, "_http_get", fake_get)
    monkeypatch.setattr(bench, "free_gb", lambda p: 50.0)
    out = tmp_path / "public"
    rc = bench.cmd_public(argparse.Namespace(dir=str(out), count=5, max_secs=15.0, refresh=False))
    assert rc == 0
    clips = bench.load_manifest(out)
    assert len(clips) == 5 and clips[0]["category"] == "librispeech"
    assert clips[0]["reference"] == "Some words number 0"
    assert all(c["_wav_path"].is_file() for c in clips)
    monkeypatch.setattr(bench, "free_gb", lambda p: 3.0)
    assert bench.cmd_public(argparse.Namespace(dir=str(tmp_path / "x"), count=5, max_secs=15.0, refresh=False)) == 1
    monkeypatch.undo()
    for bad in ("http://datasets-server.huggingface.co/x", "https://example.com/x.flac"):
        try:
            bench._http_get(bad, 10)
        except RuntimeError as exc:
            assert "refusing" in str(exc)
        else:
            raise AssertionError(bad)


def test_vram_sampler_prefers_the_commands_own_cuda_memory_and_falls_back_to_the_gpu_rise(monkeypatch):
    import os as _os
    levels = iter([1000, 1000, 2300, 3300, 2000, 1500])
    monkeypatch.setattr(bench, "_gpu_used_mb", lambda: next(levels, 1500))
    monkeypatch.setattr(bench, "_gpu_compute_apps", lambda: {})
    s = bench.VramSampler(_os.getpid(), interval=0.001)
    s.start()
    import time as _t
    _t.sleep(0.05)
    s.stop()
    assert s.peak_mb == 2300

    monkeypatch.setattr(bench, "_gpu_used_mb", lambda: 5000)
    monkeypatch.setattr(bench, "_gpu_compute_apps", lambda: {_os.getpid(): 2400, 1: 900})
    s2 = bench.VramSampler(_os.getpid(), interval=0.001)
    s2.start()
    _t.sleep(0.05)
    s2.stop()
    assert s2.peak_mb == 2400


def test_gpu_failures_are_retried_and_then_left_out_of_the_scores(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    def flaky(spec, wav, cfg, tmp, timeout_secs=120, measure_vram=False):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"text": "", "error": "ONNX Runtime error: cuda_call.cc out of memory", "wall_ms": 1,
                    "infer_ms": None, "load_ms": None, "vram_mb": None}
        return {"text": "ok", "error": None, "wall_ms": 1, "infer_ms": 1, "load_ms": 1, "vram_mb": 100}

    monkeypatch.setattr(bench, "transcribe_wav_offline", flaky)
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)
    spec = bench.RunSpec(name="x", binary=Path("/x"), engine="parakeet", model_path="/m")
    res = bench.transcribe_with_retry(spec, tmp_path / "a.wav", tmp_path / "c", tmp_path, timeout_secs=5,
                                      measure_vram=False, retries=2)
    assert res["error"] is None and res["attempts"] == 3
    # a model that is simply missing is not retried
    calls["n"] = 0
    monkeypatch.setattr(bench, "transcribe_wav_offline",
                        lambda *a, **k: {"text": "", "error": "Error: Model not found: /x", "wall_ms": 1,
                                         "infer_ms": None, "load_ms": None, "vram_mb": None})
    res = bench.transcribe_with_retry(spec, tmp_path / "a.wav", tmp_path / "c", tmp_path, timeout_secs=5,
                                      measure_vram=False, retries=2)
    assert res["attempts"] == 1

    good = bench.score_clip("hello world", "hello world")
    failed = {**bench.score_clip("hello world", ""), "error": "ONNX Runtime error"}
    agg = bench.aggregate_scores([good, failed])
    assert agg["errors"] == 1 and agg["clips"] == 1 and agg["wer"] == 0.0
    assert bench.aggregate_scores([failed])["score_10"] == 0.0
