# Dictation end to end

`tests/e2e/run.sh` records, transcribes, cleans and delivers real takes through the
real scripts and the fork's real daemon, into a real terminal and a real browser,
inside a world that is not the desktop's.

```sh
tests/e2e/run.sh                    # everything (about 5 minutes), report on stdout
tests/e2e/run.sh --list             # scenarios
tests/e2e/run.sh private_take       # by name (a scenario that runs under both deliveries has both)
E2E_DELIVERY=shuush tests/e2e/run.sh   # only the shuush-delivery variants (daemon | shuush | both)
tests/e2e/run.sh --keep             # leave /tmp/se2e.XXXXXX (logs/, run/, home/) to look at
```

Reports and results: `${XDG_STATE_HOME:-~/.local/state}/shuush-e2e/reports/{latest.md,report-*.md,results-*.jsonl}`.
Exit status: 0 all pass (documented known bugs do not fail the run), 1 a scenario failed, 2 the world could not be built.

Nightly (not enabled by anything here): `tests/e2e/install-timer.sh`, then
`systemctl --user enable --now shuush-e2e.timer`. `nightly.sh` runs the suite and sends one
notification only when something failed.

## The world

| piece | what it is |
|---|---|
| namespaces | user + net + pid (`unshare`): no route off the machine, everything dies with the run |
| runtime dir, HOME, XDG_* | `/tmp/se2e.XXXXXX/{run,home}`, every child started with `env -i` and only the world's variables |
| compositor | headless **sway**, fetched once into `~/.cache/shuush-e2e/sway-prefix` (checksum-verified against pacman's sync db, nothing installed system-wide) |
| audio | its own PipeWire + WirePlumber with hardware monitors disabled, one null sink `e2e_mic`; takes are espeak WAVs played into it with `paplay`; the run refuses to go on if any other sink is visible |
| voxtype | a **copy** of `~/.local/lib/voxtype-fork/voxtype`, `[whisper] mode = "remote"` pointed at the fake server; settings of the live daemon: `queue_dictations`, `warm_mic_secs = 3`, `release_tail_ms = 400` |
| shuush | `install.sh` into `/tmp/se2e.XXXXXX/shuush`, config written by `shuush wire` |
| fake server | `fake_services.py` on loopback: speech-to-text (`/v1/audio/transcriptions`, answers with the queued transcript, records the audio's length and loudness), cleanup model (`/v1/chat/completions` streamed, `/api/chat`; modes echo, fail500, reset, hang), and the browser page's event log |
| targets | kitty running `apps/term_target.py` (logs every typed byte and bracketed paste) and Chromium on `apps/page.html` (textarea, contenteditable, password input) |
| input method | `shuush-ime`, built offline once into the cache, for the password-field scenario |

Takes are driven the way the key binds do: `shuush toggle|hold|release|cancel|recover`.

## Scenarios

`scenarios/NN_name.sh` defines `scn_config` (optional daemon knobs) and `scn_run`. A first comment line
`# desc:` is the one-liner for `--list`; `# deliveries: daemon shuush` runs it under both delivery modes.
Checks are `check_eq`, `check_true`, `check_match`...; `check_known TAG ...` marks a defect the harness
found and reported: it shows under "Known bugs" in the report without failing the run, and says so the day it passes.

## Quirks of the world (not of dictation)

- Headless sway lets wtype's first key overtake the client's keymap switch. The world's `wtype` shim settles for
  100 ms (`E2E_WTYPE_SETTLE_MS=0` turns it off; `first_key` does).
- The fake cleanup model cleans with one documented rule (`clean_text`): filler sounds out, capital first letter, full
  stop at the end. It never rewrites a word.
- The Hyprland-only helpers (`voxtype-outline.sh`, hyprctl) do nothing here.

## What it cannot cover

Real speech recognition
(a fake transcriber answers), Hyprland itself, Electron apps with their own IME quirks, real AT-SPI password
detection, GPU/CUDA paths, the Quickshell pill, media ducking, and any cloud provider's behaviour.
