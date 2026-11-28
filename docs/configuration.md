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
| `PP_WATCH_EDITS` | `1` | For a few minutes after a take, a name you fix by hand in that field counts as a mishearing. Reads the field through AT-SPI, or through an input method (that also covers Electron apps): Shuush's own (`PP_IME=shuush`, below), or the [companion fork](voxtype-fork.md)'s with `read_field = true` or `preedit = true` under `[output]`. |

## Typing

| Setting | Default | What it does |
|---|---|---|
| `PP_SMART_JOIN` | `1` | A dictation right after text in the same field gets a leading space, and continues an unfinished sentence in lowercase. Needs `trim = false` under `[output.post_process]` in `config.toml`. |
| `PP_KEEP_ON_CLIPBOARD` | `1` | Also put each dictation on the clipboard, so it is never lost. `0` leaves your clipboard alone. |
| `PP_RELEASE_TAIL_MS` | `200` | Keep recording this long after you release push-to-talk, so the last syllable is not clipped. Skipped when the daemon has voxtype-fork's adaptive `release_tail_ms` (`shuush caps has release_tail_ms`) and it is set (in `[audio]` or as `VOXTYPE_RELEASE_TAIL_MS`), which does this natively. A daemon without it ignores both, so the tail applies. |

To press Enter after a dictation, end it with the word "submit" and set `smart_auto_submit = true` under `[text]` in `config.toml` (a voxtype feature; in `PP_DELIVERY=shuush` mode, use `PP_SMART_AUTO_SUBMIT=1` in `postprocess.env` instead).

## Shuush delivery (`PP_DELIVERY=shuush`, opt-in)

By default (`PP_DELIVERY=daemon`), `voxtype-postprocess.sh` prints the cleaned text back to `voxtype` and `voxtype` types or pastes it. Setting `PP_DELIVERY=shuush` hands each take to Shuush's ordered delivery service (`shuush-deliver.service`) and returns empty stdout so stock `voxtype` (with `fallback_on_empty = false`) goes idle immediately after recognition.

| Setting | Default | What it does |
|---|---|---|
| `PP_DELIVERY` | `daemon` | `daemon` (voxtype delivers) or `shuush` (Shuush's ordered queue delivers). Degrades to `daemon` automatically if the worker is unreachable or `fallback_on_empty` is not `false`. |
| `PP_PASTE_OVER_CHARS` | `300` | In `shuush` delivery mode, non-private takes longer than this many characters are pasted instead of typed (`0` = always type). Private takes never use the clipboard. |
| `PP_STREAM_TYPING` | `1` | Stream-type verified sentences as cleanup arrives when the target supports it (non-private, focused, `>= PP_STREAM_MIN_WORDS`, within `PP_PASTE_OVER_CHARS`). |
| `PP_PASTE_KEYS` | `auto` | Paste shortcut: `auto` (`ctrl+shift+v` for terminals, `ctrl+v` elsewhere), `shift+insert`, `ctrl+v`, `ctrl+shift+v`, or `super+v`. |
| `PP_AUTO_SUBMIT` | `0` | Press Enter after every non-empty delivered take (replaces `[output] auto_submit`). |
| `PP_SMART_AUTO_SUBMIT` | `0` | Strip a trailing "submit" and press Enter (replaces `[text] smart_auto_submit`). |
| `PP_APPEND_TEXT` | `""` | Append a suffix string to every delivered take (replaces `[output] append_text`). |

Switching on: set `PP_DELIVERY=shuush` in `~/.config/voxtype/postprocess.env`, run `shuush wire`, enable `systemctl --user enable --now shuush-deliver.service`, and restart `voxtype`. Switching off: set `PP_DELIVERY=daemon` and run `shuush wire` (which restores any `auto_submit`, `smart_auto_submit`, and `append_text` keys from `~/.config/shuush/wired.json`). `shuush doctor` shows which mode is active, and `shuush stats latency` prints per-stage p50/p90/p95 latency breakdowns.

### Shuush's input method (`PP_IME`, off by default)

`PP_IME=shuush` hands the focused field to `shuush-ime`, a small helper Shuush starts for each take (build it with `shuush build-ime`; check it with `shuush build-ime --check`). It needs a compositor with input-method-v2 (Hyprland, Sway, niri) and a field with text-input-v3; anywhere else the take is typed as before.

- It binds as the input method only while a take is live, and for up to two minutes after on the field the text went into (so smart join and learning from your fixes can read it), then exits. At rest it is not running, so fcitx5 or ibus keep the seat. A seat they already hold means typing, as without it.
- With the companion fork's live text (`streaming_preview_file = "auto"`), the words heard so far show underlined in the field, the last ~240 bytes of them; `live_in_field` tells the pill to leave them out. A password or PIN field never shows them: `sensitive_field` appears instead and the take is private.
- The final text goes into the field in one input-method commit, with no keys typed, when the daemon takes an empty answer as "nothing to type" (`fallback_on_empty = false`, which `shuush wire` sets). Text with newlines, a `terminal` / `chat` / `command` profile take and a private take are typed as before, after the live words are gone.
- It reads the field into `field_text.json`, like the fork's `read_field`, also in Electron apps.

Turn it on with `PP_IME=shuush` in `postprocess.env`, then `shuush wire` and `systemctl --user restart voxtype`: wire turns the daemon's own `[output] preedit` and `read_field` off (two input methods cannot share a seat) and records them; with `PP_IME` off again, `shuush wire` gives them back.

To press Enter after a dictation, end it with the word "submit" and set `smart_auto_submit = true` under `[text]` in `config.toml` (a voxtype feature).

## In `config.toml`

`shuush wire` (or `./install.sh --wire-config`) sets these up, after a timestamped backup. The keys are written with `voxtype config set` (validated, comments kept) and `shuush wire --dry-run` prints what it would set without writing anything. Shuush records the value each key had before, and the one it set, in `~/.config/shuush/wired.json`; `shuush unwire` puts every key back the way it was (a key you changed since stays yours unless you pass `--force`), and `shuush wire --status` and `shuush doctor` report one that changed. The profile lines are the one exception to `voxtype config set`, because voxtype has no settable key for a profile: they are edited in `config.toml` directly, as exact line changes the record can undo.

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

## The pill

How the KitsuneHud pill shows a take. A shell that draws the pill itself
(kuwai: Settings → Dictation words) says so in
`$XDG_RUNTIME_DIR/kuwai/voxtype-pill.json`; without one, set it in the OSD's
environment:

| `VOXTYPE_OSD_PILL` | While you speak |
|---|---|
| `ticker` (default) | The field shows the words; the pill sits on the line under the target window, reaching at most 8 px into it, with only the newest words on one line at every text size. A window with no room under it (maximised, fullscreen) gets it just inside its bottom-right corner, clear of a terminal's prompt and a centred composer. When the window moves, the pill glides with the host's ring on the same spring (`glide` in the host's file; none with reduced motion). Where the field can't show the words it opens to the whole take. Needs a host that knows the window; otherwise it stays at the bottom of the screen. |
| `field` | The field shows the words (voxtype's `[output] preedit = true`); the pill shows them only where the field can't, after a short wait for the field's marker. |
| `words` | Every word in the pill, the newest sliding in and the oldest fading out at the top left. Meant for `preedit = false`, so the words are in one place. |
| `status` | The pill never shows words: the level, the state and the time. The host shows a terminal's words in a caption on the window. |

`VOXTYPE_OSD_TEXT_SCALE` (1 to 2) enlarges the pill's text and its room with it.
Every style but `field` also lays the bars down into one breathing line between
words, glows behind them with your voice, and says "Too quiet · speak up" in
amber when three seconds pass with sound but no words.
