# Configuration

Shuush reads two files in `~/.config/voxtype/`, next to voxtype's own `config.toml`:

- `postprocess.env` — the settings below. Studio's **Settings** tab edits the common ones.
- `secrets.env` — API keys, one per line (`GROQ_API_KEY=...`), mode `600`. Studio's **API keys** popover writes it.

Changes apply to the next dictation; nothing needs restarting.

## Cleanup providers

| Setting | Default | What it does |
|---|---|---|
| `PP_ORDER` | `local groq` | Providers tried in order, first answer wins: `groq`, `groq2` (a second Groq key, `GROQ_API_KEY2`), `cerebras`, `openrouter`, `opencode`, `local` (Ollama). A provider that is down is skipped at once. Put `local` alone for a fully offline setup. |
| `PP_BUDGET_MS` | `6000` | Time for all providers together. When it runs out, your words are typed as heard (never lost). |
| `PP_GROQ_MODEL` | `qwen/qwen3.8-27b` | Model for `groq` / `groq2`. |
| `PP_CEREBRAS_MODEL` | `qwen-3.8-27b` | Model for `cerebras` (`CEREBRAS_API_KEY`). |
| `PP_OPENROUTER_MODELS` | two free Gemma models | Space-separated; OpenRouter moves to the next when one is busy (`OPENROUTER_API_KEY`). |
| `PP_OPENCODE_MODEL` | `deepseek-v4.1-flash` | OpenCode Zen model (`OPENCODE_API_KEY`). Zen's free models only answer inside the OpenCode app, so this needs Zen credits. |
| `PP_LOCAL_URL`, `PP_LOCAL_MODEL` | `http://127.0.0.1:11434`, `qwen3:4b-instruct-2507-q4_K_M` | Ollama for `local`. |
| `PP_STREAM_MIN_WORDS` | `60` | With the companion fork's streaming cleanup, dictations at least this long are typed sentence by sentence; shorter ones in one piece. |
| `PP_SPEAKER_BIO` | — | One line about you and what you dictate, given to the model as context. |

See [models.md](models.md) for which provider to pick.

## Context and privacy

| Setting | Default | What it does |
|---|---|---|
| `PP_APP_CONTEXT` | `1` | Use the focused app's name and window title to recognise project and file names. `0` sends no context from your desktop at all: none of the screen, field, selection or clipboard text below. |
| `PP_SCREEN_CONTEXT` | `1` | Read visible text, the text field you are typing into, and highlighted text (AT-SPI), so names on screen are spelled right. `0` turns off all screen, selection and clipboard context. |
| `PP_CLIPBOARD_CONTEXT` | `1` | Use text you copied within the last 15 seconds (needs the two settings above). A password manager's copy, or a copied word shaped like a password, is never used, and credentials in URLs are removed. |
| `PP_PRIVATE_GUARD` | `1` | Password fields, password managers, sign-in pages and `sudo` prompts: the take is typed exactly as heard, never sent to a model, not saved to history or the clipboard. |

## Learning

| Setting | Default | What it does |
|---|---|---|
| `PP_AUTO_LEARN` | `1` | A mishearing seen twice is learned automatically (with guards against ordinary English words). Seen once, it waits in Studio's **Learning Inbox**. |
| `PP_WATCH_EDITS` | `1` | For a few minutes after a take, a name you fix by hand in that field counts as a mishearing. Reads the field through AT-SPI, or through voxtype's input method when you run the [companion fork](voxtype-fork.md) with `read_field = true` or `preedit = true` under `[output]` (that also covers Electron apps). |

## Typing

| Setting | Default | What it does |
|---|---|---|
| `PP_SMART_JOIN` | `1` | A dictation right after text in the same field gets a leading space, and continues an unfinished sentence in lowercase. Needs `trim = false` under `[output.post_process]` in `config.toml`. |
| `PP_KEEP_ON_CLIPBOARD` | `1` | Also put each dictation on the clipboard, so it is never lost. `0` leaves your clipboard alone. |
| `PP_RELEASE_TAIL_MS` | `200` | Keep recording this long after you release push-to-talk, so the last syllable is not clipped. |

To press Enter after a dictation, end it with the word "submit" and set `smart_auto_submit = true` under `[text]` in `config.toml` (a voxtype feature).

## In `config.toml`

`shuush wire-config` (or `./install.sh --wire-config`) sets these up, after a timestamped backup:

```toml
[output.post_process]
command = "~/.local/share/shuush/libexec/voxtype-postprocess.sh"
timeout_ms = 20000
trim = false   # keep smart join's leading space; Shuush trims the model's own

[profiles.terminal]   # terminals and editors: commands and code kept verbatim
post_process_command = "~/.local/share/shuush/libexec/voxtype-postprocess.sh terminal"
# [profiles.chat] and [profiles.command] likewise
```

With the [companion fork](voxtype-fork.md), `streaming = true` under `[output.post_process]` types long dictations sentence by sentence as the cleanup streams (stock voxtype types the whole cleaned text at once).
