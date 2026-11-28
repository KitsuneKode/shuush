# Measuring dictation accuracy: `shuush bench`

One command family answers "is my dictation getting better?" with numbers from
your own voice: you read a fixed list of prompts once, and every later model or
option change is scored on the same recordings, offline.

```sh
shuush bench prompts                 # the 42 prompts, by category
shuush bench record                  # you read them aloud (interactive; ~10 minutes)
shuush bench run                     # the daemon's option sets on your clips
shuush bench run --models all        # every installed model, side by side
shuush bench models                  # which Hex models voxtype can run, size, VRAM, how to fetch
shuush bench public                  # 25 public LibriSpeech clips, to check the pipeline itself
shuush bench report -v               # the newest saved report again, with every differing clip
```

Everything lives under `~/.local/share/shuush/bench/` (clips, `manifest.jsonl`,
`results/bench-*.json`). Nothing is read from or written to the running daemon,
its runtime directory or `~/.config/voxtype` (cleanup, which is opt-in, works on
a scratch copy).

## The prompts

42 short sentences in seven categories, written from how the owner dictates
(takes carry a negation 35% of the time, a digit 10%, are questions 11%; median
32 words) using only public terms from `vocabulary.txt`:

| category | what it catches |
|---|---|
| names | Hyprland, Waybar, Quickshell, Wispr Flow, T3 Code, Groq ... spelled right |
| code_terms | Next.js, shadcn, ONNX, CUDA, TUI ... |
| numbers | `404`, `11435`, `8.5`, `4500`: read however you naturally read them; "forty five" and "45" score as the same word |
| negations | "don't", "never", "neither ... nor": a dropped or invented one flips the meaning |
| last_word | sentences ending in a name, to see the last word clipped when the key is released |
| questions | the question mark |
| long | 25-30 word takes, for pauses and sentence joins |

When you record, **stop right after the last word**, the way you release the
key; trailing silence hides exactly the tail loss the `last_word` prompts look for.

`shuush bench prompts --from-vocab` lists vocabulary terms no prompt covers;
add your own clip for any of them with
`shuush bench import clip.wav --ref "text" --terms "Term"`.

## What `run` does

For every clip and every config it runs `voxtype -c <scratch config> transcribe
<wav>` (the offline decoder, never the daemon) and scores the text:

- **WER** (numbers compared as numbers), with a per-category table
- **Terms**: how many of the names/terms a clip must contain came out (any casing/spacing: `Next.js` = `next js`)
- **Nums / Negs**: numeric values and negations kept
- **TailLoss**: share of clips whose last word is missing or clipped
- **Q mark**: question marks kept
- **Score**: 10 x weighted mean of (1-WER) .45, (1-tail loss) .10, terms .25, numbers .10, negations .10. A proxy to rank configs, not a verdict; compare the columns
- **Infer ms / Wall ms**: decode time (voxtype's own figure, else wall time minus the model load) and the whole process, model load included
- **VRAM**: measured peak (the process's own CUDA memory when the driver lists it, else the rise of the whole GPU)

The scratch config has no `[text]` replacements, filler filter or spoken
punctuation: the score is the recogniser's. `bench run --cleanup` additionally
runs shuush's cleanup on each result and scores that (`...+cleanup` rows). It
calls your configured providers (network, API keys) once per clip and config,
so it is never on by default.

A clip whose decode failed (GPU out of memory because the daemon or another
program took the card, a crash) is retried three times, then left out of the rates
and counted in the `Err` column. Close the GPU's other users for a clean run.

### Option sets (the daemon's Parakeet Unified model)

`voxtype transcribe` always decodes the whole recording with the final-pass
pipeline, so the choices that exist offline are:

| preset | pad | rescoring |
|---|---|---|
| `daemon` | 0.5 s | on (the vocabulary the daemon uses) |
| `no-rescore` | 0.5 s | off |
| `no-pad` | 0 | on |
| `bare` | 0 | off |

`--vocab-boost 1.5,4` adds rescoring runs at other `vocabulary_boost` values
(the default is 2.5 nats). The *live preview* (the words in the pill while you
speak) is a separate chunked decode that `transcribe` cannot reach, so "final
pass off" cannot be measured offline; `streaming = false` with the Unified model
returns nothing (it needs the cache-aware path). The vocabulary is the one the
daemon is configured with (`[parakeet] vocabulary_file`), else
`parakeet-vocabulary.txt`, else `vocabulary.txt`; `--vocab-file` overrides.

## Models

`shuush bench models` is the live answer (what is installed, which binary runs
it, exact download commands). The candidates, from Hex's list and voxtype's
catalog; "Hex WER" is Hex's published LibriSpeech test-clean figure, not
measured here:

| id | runs on | disk | VRAM (est.) | Hex WER | notes |
|---|---|---|---|---|---|
| parakeet-unified-fp16 | fork, CUDA | 1.26 GB | 2.3 GB (measured) | 1.60 | daily driver; the only one with streaming preview and acoustic rescoring |
| parakeet-unified-fp32 | fork, CUDA | 2.66 GB | ~3.3 GB | 1.60 | same weights, full precision |
| parakeet-ultra-q8 / q4 | stock Vulkan | 740 / 486 MB | ~1.2 / 0.9 GB | 1.80 | batch only |
| cohere-transcribe-q4 / q8 | stock Vulkan | 1.56 / 2.41 GB | ~2.2 / 3.2 GB | 1.27 | Hex's lowest WER; no vocabulary hook |
| whisper-large-v3-turbo | stock Vulkan | 1.6 GB | ~2.0 GB | 2.01 | takes the vocabulary as `initial_prompt` |
| parakeet-tdt-v2 (+int8) / v3 (+int8) | fork, CUDA | 2.4 GB (640 MB) / 2.6 GB (670 MB) | ~3.2 / 3.4 GB | 1.69 / 1.94 | batch decode, no rescoring |
| nemotron-3.5-asr | stock Vulkan | 751 MB | ~1.2 GB | 3.05 | |
| qwen3-asr-0.6b / 1.7b | stock Vulkan | 850 MB / 2.19 GB | ~1.4 / 3.0 GB | n/a | multilingual |
| granite-speech-5 | none | | | 4.77 | voxtype has no Granite engine |

The fork build compiles only `whisper` and `parakeet`; Cohere, the GGUF
Parakeets, Qwen and Nemotron need the stock `/usr/lib/voxtype/voxtype-vulkan`
(1.3.1, which also ships the GGUF worker). The GPU is 6 GB and the desktop holds
about 1.2 GB, so anything over ~4.5 GB total cannot run beside it, and the
daemon's own model (~2.3 GB while loaded) shares the card with a bench run.

Fetching never selects the model and never touches `config.toml` (`--activate`
is off by default; the printed command also points `XDG_CONFIG_HOME` at a
scratch directory). Check `df -h /` first. Then:

```sh
shuush bench run --models parakeet-ultra-q8 cohere-transcribe-q4 parakeet-unified-fp16
```

A model that is not installed is listed under "Not run" with its fetch command.

## Public set

`shuush bench public` downloads 25 LibriSpeech dev-clean clips (CC BY 4.0, about
5 MB, from the Hugging Face datasets server, nothing sent about you) into
`<bench dir>/public`. It checks the pipeline and normalisation on clean read
speech with a known reference. It says nothing about your names or voice.

### Validation (2026-11-28, RTX 4050 6 GB, fork `35afd7e6`, Parakeet Unified fp16)

24 LibriSpeech dev-clean clips + the fork's `kaliko_hesitated.wav` fixture, vocabulary = the owner's public terms + `Kaliko`:

| config | WER (LibriSpeech) | fixture "Kaliko hesitated." | last word lost | VRAM | decode / wall per clip |
|---|---|---|---|---|---|
| daemon (pad 0.5 + rescoring) | 1.7% | exact | 0/25 | 2.3 GB | 1.3 s / 6.3 s |
| no-rescore | 1.7% | "Kaligo hesitated." | 0/25 | 2.3 GB | 0.8 s / 4.1 s |
| no-pad | 1.7% | exact | 0/25 | 2.3 GB | 0.8 s / 3.3 s |
| bare | 1.7% | "Kaligo hesitated." | 0/25 | 2.3 GB | 0.7 s / 2.5 s |

The remaining 1.7% is spelling and punctuation in LibriSpeech's own references ("realise"/"realize", "mc gregor"/"MacGregor", "board walks"/"boardwalks"), not misheard words. Hex publishes 1.60% for this model on test-clean. Rescoring changes nothing on ordinary speech and fixes a listed term; clean read speech with natural pauses does not show the tail padding, which is what the `last_word` prompts are for.
