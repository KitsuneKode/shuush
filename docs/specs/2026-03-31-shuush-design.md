# Shuush — Wayland-Native Voice Transcription Tool

**Status:** Designed. PARKED until income/hackathon window.
**Date:** 2026-03-31
**Project location:** `~/Projects/shuush/` (to be created)
**Feeds into:** Jarvis voice assistant (wake-word layer)

---

## What It Is

A Wayland-native background transcription daemon for Arch Linux / Hyprland. Hold a hotkey → speak → release → text streams into whatever app is focused. Like WhisperFlow but:

- Native Wayland (no Electron)
- CUDA-accelerated via local `whisper-server` (already installed)
- Deterministic text cleaning built in
- Designed to evolve into the Jarvis wake-word foundation
- Target: publishable to AUR

**Name:** Shuush (the sound of asking for quiet)
**Language:** Rust (Cargo workspace)

---

## Reference Implementations

| Repo | Lang | What We Steal | What We Avoid |
|------|------|--------------|---------------|
| [hyprvoice](https://github.com/leonardotrapani/hyprvoice) | Go | Daemon+CLI arch, Unix socket single-byte IPC, ydotool→wtype→clipboard fallback chain, state machine, TOML hot-reload config | Nothing major |
| [hyprwhspr](https://github.com/goodroot/hyprwhspr) | Python | XKB keymap layout detection, USB keepalive stream pattern | `evdev grab_keys=True` (keyboard hijacking bug), no text cleaning |

**The keybinding hijack bug** comes from `evdev grab_keys` stealing exclusive input. Our Unix socket IPC (hyprland.conf keybinds → shuush CLI → socket → daemon) avoids this entirely.

---

## System Package

Already installed: `whisper.cpp-cuda 1.8.4`

Provides:
- `/usr/bin/whisper-server` — HTTP server, keeps model in VRAM ✅ use this
- `/usr/bin/whisper-cli` — one-shot CLI (model reloads each call ❌ too slow)
- `/usr/lib/libwhisper.so` — shared library (optional future direct linking)

**Model:** `ggml-large-v3.bin` — user downloads to `~/.local/share/shuush/`

---

## Architecture

```
Hyprland keybind
  └─ shuush start/stop/toggle/cancel
         │
         ▼ Unix socket ($XDG_RUNTIME_DIR/shuush.sock)
         │
    ┌────▼──────────────────────────────────────────┐
    │              shuushd (Rust daemon)             │
    │                                               │
    │  state: Idle → Recording → Transcribing       │
    │          → Injecting → Idle                   │
    │                                               │
    │  ┌─────────┐  ┌───────────────┐  ┌────────┐  │
    │  │  CPAL   │  │whisper-server │  │ wtype  │  │
    │  │(PipeWire│  │localhost:8178 │  │ydotool │  │
    │  │ 16kHz)  │  │(CUDA, large-v3│  │clip fb │  │
    │  └────┬────┘  └──────┬────────┘  └───┬────┘  │
    │       │WAV            │text           │stream  │
    │       └──────────────►│               │       │
    │                       └───────────────►       │
    │                                               │
    │  GTK4 layer-shell overlay (recording pill)    │
    └───────────────────────────────────────────────┘
```

---

## State Machine

```
         [start]              [stop/release]      [done]          [done]
Idle ──────────────▶ Recording ───────────▶ Transcribing ──────▶ Injecting ──▶ Idle
  │                     │                       │                    │
  │                  [cancel]                [error]             [error]
  └─────────────────────┴───────────────────────┴────────────────────┘
                                                        (show 3s error, then Idle)
```

---

## IPC Protocol (from hyprvoice pattern)

Unix socket: `$XDG_RUNTIME_DIR/shuush.sock`

Single-byte commands:
| Byte | Command | Description |
|------|---------|-------------|
| `s` | start | Begin recording (hold-to-record mode) |
| `p` | stop | Stop recording, trigger transcription |
| `t` | toggle | Toggle start/stop (toggle mode) |
| `c` | cancel | Abort, discard recording |
| `?` | status | Return JSON status line |
| `q` | quit | Shut down daemon |

**Hyprland keybinds (`~/.config/hypr/hyprland.conf`):**
```ini
bind  = SUPER, V, exec, shuush start      # press down
bindr = SUPER, V, exec, shuush stop       # release (hold-to-record)
bind  = SUPER SHIFT, V, exec, shuush toggle
bind  = SUPER, Escape, exec, shuush cancel
```

---

## Audio Pipeline

1. **Capture**: `cpal` crate → PipeWire backend → 16kHz mono f32 PCM
   - Why 16kHz: whisper.cpp resamples to 16kHz internally; capture at native rate reduces buffer
   - Ring buffer in memory during recording (no disk I/O until stop)

2. **Encode**: `hound` crate → `/tmp/shuush-{timestamp}.wav`
   - 16-bit PCM, 16kHz, mono
   - Typical file size: ~480KB for 15s speech

3. **Transcribe**: HTTP POST to `whisper-server`
   ```
   POST http://127.0.0.1:8178/inference
   Content-Type: multipart/form-data

   file=<wav bytes>
   temperature=0.0
   language=auto
   ```
   Response: `{ "text": "transcribed text here" }`

4. **Latency budget** (CUDA + large-v3, ~5s speech):
   - WAV write: ~5ms
   - HTTP overhead: ~2ms
   - CUDA inference: ~400-800ms
   - **Total: 0.5-1s** ✅

### Whisper-server lifecycle

`shuushd` spawns `whisper-server` as a managed child process at startup:
```
shuushd starts
  └─ spawn: whisper-server --model <path> --host 127.0.0.1 --port 8178 --gpu-device 0
  └─ poll GET /health until 200 (max 10s)
  └─ on crash: re-spawn after 2s, overlay shows "Reloading model..."
```

---

## Text Cleaning Pipeline

Deterministic, <5ms, fully offline. No LLM in v1.

```
raw whisper output
    │
    ├─ Stage 1: Artifact removal
    │    "[BLANK_AUDIO]" "[Music]" "(Music)" "[inaudible]" → ""
    │
    ├─ Stage 2: Filler word reduction (configurable)
    │    isolated " um ", " uh ", " you know ", " like " → " "
    │    double spaces → single space
    │
    └─ Stage 3: Normalization
         First character → uppercase
         No trailing punctuation → append "."
         "i " (isolated) → "I "
         "i'm", "i've", "i'd", "i'll" → capitalize
```

**v2 optional**: LLM post-processing via local ollama or OpenAI API (config flag `[llm_cleanup] enabled = true`).

---

## Text Injection Fallback Chain

From hyprvoice pattern:

```
1. Try ydotool (uinput-based, most compatible across apps)
     └─ requires: ydotoold running, /dev/uinput accessible
     └─ fail → try next

2. Try wtype (Wayland virtual keyboard protocol)
     └─ requires: compositor supports wlr-virtual-pointer protocol
     └─ fail → try next

3. wl-clipboard + wtype paste
     └─ wl-copy <text> && wtype -k ctrl+v
     └─ last resort, may overwrite clipboard
```

**Streaming**: each word injected with configurable delay (default 8ms = ~125 wpm, natural feel).
`stream_delay_ms = 0` disables streaming (dumps all at once).

---

## GTK4 Layer-Shell Overlay

Floating pill rendered above all windows. Uses `gtk4-layer-shell`.

| State | Visual | Color | Animation |
|-------|--------|-------|-----------|
| Idle | Hidden | — | — |
| Recording | Dot + elapsed time "0.4s" | Red | Pulse (1Hz) |
| Transcribing | Spinner + "Processing..." | Amber | Rotate |
| Injecting | Text preview (first 30 chars...) | Green | Fade in |
| Error | Short message | Red | Flash, 3s fade out |

Position: configurable (`top-right`, `top-left`, `bottom-right`, `bottom-left`).

**Cross-platform abstraction** (future):
```rust
trait OverlayBackend: Send + Sync {
    fn show_state(&self, state: DaemonState, text_preview: Option<&str>);
    fn hide(&self);
}

struct Gtk4LayerShellOverlay;     // v1 — Wayland
struct MacOSOverlay;               // v3 — AppKit NSPanel
```

---

## Waybar Module

```json
"custom/shuush": {
    "exec": "shuush status --json",
    "return-type": "json",
    "interval": 1,
    "format": "{icon} {text}",
    "format-icons": {
        "idle": "󰍬",
        "recording": "󰑊",
        "transcribing": "󰑓",
        "injecting": "󰆒"
    },
    "on-click": "shuush toggle",
    "on-right-click": "shuush cancel"
}
```

---

## Project Structure

```
~/Projects/shuush/
├── Cargo.toml                      # workspace
├── crates/
│   ├── shuushd/                    # daemon binary
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── main.rs             # startup, IPC socket listener
│   │       ├── state.rs            # DaemonState enum + transitions
│   │       ├── audio.rs            # CPAL recording, ring buffer, WAV write
│   │       ├── transcribe.rs       # whisper-server HTTP client
│   │       ├── clean.rs            # text cleaning pipeline
│   │       ├── inject.rs           # ydotool/wtype/clipboard injection
│   │       ├── overlay/
│   │       │   ├── mod.rs          # OverlayBackend trait
│   │       │   └── gtk4.rs         # GTK4 layer-shell impl
│   │       ├── waybar.rs           # JSON status serialization
│   │       └── whisper_proc.rs     # whisper-server subprocess manager
│   │
│   ├── shuush/                     # CLI binary (thin client)
│   │   ├── Cargo.toml
│   │   └── src/main.rs             # parse args, send socket command, print response
│   │
│   └── shuush-core/                # shared: no platform deps
│       ├── Cargo.toml
│       └── src/
│           ├── ipc.rs              # IpcCommand, IpcResponse enums
│           ├── config.rs           # Config struct (serde + toml)
│           └── state.rs            # DaemonState enum
│
├── config/default.toml
├── systemd/
│   └── shuush.service              # [Unit] [Service] [Install] systemd user unit
├── waybar/
│   └── shuush.jsonc                # example waybar module config
├── PKGBUILD                        # AUR package
└── README.md
```

---

## Key Cargo Dependencies

```toml
[workspace.dependencies]
# Audio
cpal       = "0.15"
hound      = "3.5"

# HTTP (whisper-server client)
reqwest    = { version = "0.12", features = ["multipart", "blocking"] }

# GTK4 overlay
gtk4              = "0.9"
gtk4-layer-shell  = "0.8"

# Shared
serde      = { version = "1", features = ["derive"] }
serde_json = "1"
toml       = "0.8"
dirs       = "5"

# Async
tokio      = { version = "1", features = ["full"] }

# Logging
tracing           = "0.1"
tracing-subscriber = "0.3"
```

---

## Config (`~/.config/shuush/config.toml`)

```toml
[whisper]
model_path   = "~/.local/share/shuush/ggml-large-v3.bin"
server_port  = 8178
language     = "auto"     # "auto" or ISO 639-1 code e.g. "en"

[recording]
mode         = "hold"     # "hold" (default) or "toggle"

[cleaning]
remove_fillers   = true
auto_capitalize  = true
add_punctuation  = true

[overlay]
position  = "top-right"   # top-right | top-left | bottom-right | bottom-left
opacity   = 0.92
margin_px = 12

[inject]
stream_delay_ms = 8       # 0 = instant dump

[llm_cleanup]             # v2 — disabled in v1
enabled  = false
provider = "ollama"
model    = "llama3.2"
endpoint = "http://localhost:11434"
```

---

## Jarvis Extension Point

Wake word detection slots in as a new `ActivationMode` variant:

```rust
// crates/shuush-core/src/state.rs
pub enum ActivationMode {
    Hotkey,               // v1 — bind in hyprland.conf
    WakeWord {            // v2 — "hey kuma"
        keyword: String,
        silence_timeout_ms: u64,
    },
}
```

**Wake word stack (designed, not built):**
- **sherpa-onnx** — Apache 2.0, native Rust bindings (`sherpa-onnx` crate), keyword spotting via ONNX runtime, CUDA-accelerated, ~3-5% CPU idle. Custom wake word: record 10-20 samples of "hey kuma", train with sherpa-onnx keyword spotting tooling, export to ONNX. No usage limits, no device restrictions. See: https://github.com/k2-fsa/sherpa-onnx
  - Why not Picovoice Porcupine: free tier has device limits and commercial restrictions
  - Why not openWakeWord: Python-native, adds subprocess + Python runtime overhead to a lean Rust daemon
- **Auto-stop**: silence detection after 3s pause using `whisper-vad-speech-segments` (`/usr/bin/whisper-vad-speech-segments`, already installed).
- **Jarvis routing**: daemon gets `RouteTo::JarvisAgent` config option — instead of injecting text into focused window, sends transcription to Jarvis HTTP API for intent parsing + action.

---

## Cross-Platform Expansion Path

| Phase | Platform | Overlay impl | Injection impl | Audio backend |
|-------|----------|-------------|----------------|---------------|
| **v1** | Wayland (Hyprland, Sway) | `gtk4-layer-shell` | ydotool → wtype | CPAL/PipeWire |
| v2 | X11 | X11 override-redirect window | xdotool | CPAL/ALSA |
| v3 | macOS | AppKit `NSPanel` (floating) | CGEvent | CPAL/CoreAudio |
| v4 | Windows | Win32 `WS_EX_TOPMOST` | `SendInput` | CPAL/WASAPI |

Only `overlay/` and `inject.rs` are platform-specific. `audio.rs`, `transcribe.rs`, `clean.rs`, `state.rs`, `ipc.rs` are identical across all platforms.

---

## AUR Package (PKGBUILD outline)

```bash
depends=('gtk4' 'gtk4-layer-shell' 'ydotool' 'whisper.cpp-cuda')
optdepends=('wtype: Wayland text injection fallback'
            'wl-clipboard: clipboard injection fallback')
```

Post-install message: instructions to add hyprland.conf keybinds + optionally enable systemd user service.

---

## Spec Self-Review

- [x] No TBDs or placeholder sections
- [x] Architecture diagram matches component breakdown
- [x] Whisper package confirmed installed (`whisper.cpp-cuda 1.8.4`, `/usr/bin/whisper-server`)
- [x] IPC protocol is concrete (single-byte commands from hyprvoice pattern)
- [x] Injection fallback chain is explicit (ydotool → wtype → clipboard)
- [x] Cross-platform path designed but not over-engineered
- [x] Jarvis extension point defined without coupling it to v1
- [x] Scope is clear: v1 = Wayland + hold-to-record + whisper-server + deterministic cleaning

---

## Implementation Notes for When Build Starts

1. Start with `shuush-core` crate (types, IPC protocol, config) — no platform deps, testable anywhere
2. Then `shuush` CLI — thin, just socket communication
3. Then `shuushd` audio → transcribe → inject (no overlay first, test via CLI)
4. Overlay last — GTK4 layer-shell is the hardest part
5. Waybar module is just JSON output from `shuush status --json`

Use `writing-plans` skill when ready to build to get the bite-sized implementation plan.
