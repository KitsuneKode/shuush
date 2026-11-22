---
title: "Adding Local Ollama, Groq & Screen Context Post-Processing to Voxtype (v1.3+)"
description: "A zero-ambiguity guide to wiring [output.post_process], 5-source Wayland screen context, anti-paraphrase cleanup, and air-gapped Ollama or Groq failover on stock voxtype-bin."
pubDate: "2026-11-22"
readTime: "5 min read"
tags: ["Guides", "Architecture", "Local AI", "Wayland", "voxtype"]
heroImage: "/assets/architecture.png"
---

Stock [`voxtype`](https://github.com/peteonrails/voxtype) ships a clean `[output.post_process]` hook in `~/.config/voxtype/config.toml`: it pipes the recognized transcript to your command on `stdin` and types whatever comes back on `stdout`.

Wiring a bare `ollama run` one-liner into that hook works for five minutes — then real coding dictations expose four concrete problems:

1. **Cold-start latency:** Spawning `ollama run` on stop pays model load time right while you are waiting for text to land.
2. **Silent paraphrasing & chatty replies:** Dictating a question (*"can you check why nginx is 502ing"*) makes a raw LLM answer the question instead of transcribing it.
3. **Blind to what is on screen:** Without your active window, AT-SPI visible text, highlighted selection, or fresh clipboard, the cleanup model cannot spell project symbols (`Waybar`, `Next.js`, `voxtype_pp_gate`) reliably.
4. **No privacy guard:** Dictating into a password manager or sign-in prompt should never touch a cloud LLM or history file.

**[Shuush](https://shuush.kitsunekode.in/)** solves all four as a MIT companion layer directly on top of stock `voxtype-bin` — no fork, no replaced binary, and full compatibility with **voxtype v1.2 and v1.3+** (`[history]`, `LARD`, `[text.replacements]`, and `[meeting]`).

## Exact ownership (zero overlap)

- **`voxtype`** owns PipeWire audio capture, CUDA/ROCm/Vulkan/CPU speech recognition (Parakeet, Whisper, Cohere GGUF, Moonshine), Wayland typing, and `v1.3+` meetings/history.
- **`shuush`** owns the `[output.post_process]` pipeline (`voxtype-postprocess.sh`), 5-source context capture, deterministic anti-paraphrase span gate (`voxtype_pp_gate.py`), warm keep-alive proxy + Ollama prewarming, phonetic self-learning, `KitsuneHud` Quickshell pill, and GTK4 Studio.
- **Your compositor** (Hyprland, Omarchy, Sway, Niri, or `kuwai`) owns only your keybindings.

## What `shuush wire-config` actually changes

Running the 1-line installer (or `shuush wire-config` after `yay -S shuush-git`) backs up `~/.config/voxtype/config.toml` to a timestamped `.bak` file, validates the updated TOML with Python's `tomllib` before saving, and wires only three areas:

```toml
[output.post_process]
command = "/home/you/.local/share/shuush/libexec/voxtype-postprocess.sh"
timeout_ms = 20000

[profiles.terminal]
post_process_command = "/home/you/.local/share/shuush/libexec/voxtype-postprocess.sh terminal"

[profiles.chat]
post_process_command = "/home/you/.local/share/shuush/libexec/voxtype-postprocess.sh chat"

[profiles.command]
post_process_command = "/home/you/.local/share/shuush/libexec/voxtype-postprocess.sh command"

[osd]
frontend = "auto"
style = "kitsune-hud"
```

Every existing `[whisper]`, `[parakeet]`, `[text]`, `[history]`, and `[meeting]` setting stays untouched. Leaving `frontend = "auto"` means users with Quickshell automatically get the 60fps `KitsuneHud` waveform pill, while users without Quickshell fall back cleanly to voxtype's built-in GTK4 or native OSD.

## Install on stock `voxtype` (v1.2 / v1.3+)

```bash
# 1. Verify Wayland, GPU, voxtype, GTK4, and AT-SPI readiness
curl -fsSL https://shuush.kitsunekode.in/doctor.sh | bash

# 2. Install Shuush on top of stock voxtype-bin
curl -fsSL https://shuush.kitsunekode.in/install.sh | bash

# 3. Enable user services and AT-SPI screen context
systemctl --user daemon-reload
systemctl --user enable --now voxtype.service shuush-llm-proxy.service shuush-live-osd.service
gsettings set org.gnome.desktop.interface toolkit-accessibility true

# 4. Verify all 14 checks and open Shuush Studio
shuush doctor && shuush studio
```

## Choosing Air-Gapped Ollama vs. Fast Cloud Failover

Open **Shuush Studio → Settings (`Alt+6`)** or edit `~/.config/voxtype/postprocess.env`:

- **100% Offline (Ollama only):** Flip **Air-gapped offline mode** in Studio (sets `PP_ORDER="local"`). While you hold your dictation hotkey, `shuush-live-osd.service` pre-warms `qwen3:4b` in Ollama (`127.0.0.1:11434`) with a 30-minute `keep_alive` so the model is already hot before you finish speaking.
- **Fast Failover (~516 ms P50):** Click **API keys** in Studio to store a Groq, OpenRouter, or Cerebras key in `~/.config/voxtype/secrets.env` (`chmod 600`, never passed on process argv). `shuush-llm-proxy.service` holds a warm TLS connection on `127.0.0.1:11435`, eliminating TCP+TLS handshake jitter on every dictation.
- **Optional speaker hint:** If you want the cleanup model to know your domain or accent, set `PP_SPEAKER_BIO="..."` in `~/.config/voxtype/postprocess.env` — Shuush ships with a neutral developer default out of the box.

## Upgrading `voxtype` later (e.g., `v1.3.1` → `v1.3.2+`)

Because Shuush never modifies `/usr/bin/voxtype`:

- Running `yay -Syu` or installing a new `voxtype` `.deb` / `.rpm` upgrades the daemon cleanly without overwriting `~/.config/voxtype/config.toml` or `~/.local/share/shuush`.
- If you enable `voxtype v1.3.1`'s built-in `voxtype history rule` (`[text.replacements]` sub-table) or keep inline `replacements = { ... }`, `shuush learn sync-toml` detects and preserves both formats with `tomllib` validation.
- AT-SPI password fields bypass the LLM, clipboard, and Shuush history automatically — even when cloud failover is enabled.
