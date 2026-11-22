# Shuush — Wayland-Native Voice Transcription Daemon

## Status

**ACTIVE — Phase 1 (eval set + benchmarks + learning track), since 2026-10-10.**
**Read `docs/learnings/2026-10-10-daily-driver.md` first** (proven facts, numbers and pitfalls — skip rediscovering them).
Then read `docs/handoff/2026-10-10-phase1-kickoff.md`:
it holds the decisions already made, the questions to ask the user next,
and the Phase 1 plan. The original spec (`docs/specs/2026-03-31-shuush-design.md`)
is background only, because several of its choices were superseded.
The current daily-driver setup (voxtype) is documented at `~/.config/voxtype/ARCHITECTURE.md`.
The pill-preview voxtype fork (KitsuneKode/voxtype, branch `kitsune/pill-preview`) lives at `~/Projects/voxtype-fork` (handoff in `.kitsune/`).

## What It Is

Hold a hotkey → speak → release → transcribed text streams into whatever app is focused. Like WhisperFlow but native Wayland, CUDA-accelerated, and designed to evolve into a Jarvis wake-word voice assistant.

## Architecture

Rust Cargo workspace:
- `shuushd` — daemon (audio capture, whisper-server client, text injection, GTK4 overlay)
- `shuush` — CLI (thin Unix socket client)
- `shuush-core` — shared types (IPC protocol, config, state enum)

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Language | Rust | Performance, gtk4-rs, cpal, cross-platform core |
| Transcription | `whisper-server` (already installed) | Model stays in VRAM, no build complexity |
| Audio | CPAL (PipeWire on Arch) | Cross-platform audio I/O |
| Text injection | ydotool → wtype → clipboard fallback | From hyprvoice pattern, avoids keyboard hijack |
| Overlay | GTK4 + gtk4-layer-shell | True Wayland-native |
| IPC | Unix socket, single-byte commands | From hyprvoice pattern |
| Config | TOML at `~/.config/shuush/` | Standard Arch ecosystem |
| Wake word (v2) | sherpa-onnx | Apache 2.0, native Rust bindings, no device limits |

## System Requirements

- `whisper.cpp-cuda` AUR package (provides `/usr/bin/whisper-server`)
- `ggml-large-v3.bin` model file
- `ydotool` + `ydotoold` (primary text injection)
- `gtk4`, `gtk4-layer-shell` (overlay)
- NVIDIA GPU + CUDA toolkit

## Reference Implementations

- [hyprvoice](https://github.com/leonardotrapani/hyprvoice) (Go) — daemon/CLI arch, IPC, injection fallback
- [hyprwhspr](https://github.com/goodroot/hyprwhspr) (Python) — XKB layout detection, audio patterns

## When to Start Building

Use `writing-plans` skill to generate bite-sized implementation plan from the spec. Build order:
1. `shuush-core` (types, IPC, config)
2. `shuush` CLI (socket client)
3. `shuushd` audio → transcribe → inject (no overlay)
4. GTK4 overlay last
5. Waybar module
