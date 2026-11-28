# shuush-ime

Shuush's Wayland input method. While a take is live it binds as the seat's
input method (`zwp_input_method_v2`) and:

- shows the words heard so far as preedit in the focused text-input-v3 field:
  the last ~240 bytes, cut at a word with an ellipsis, moving about half back
  when full, so a long take does not slow the field down;
- commits the final text in place of them with `commit_string`: no synthesized
  keys, any keyboard layout;
- reads the field's text around the cursor into `field_text.json`, for smart
  join and learning from hand fixes, also in Electron apps;
- never shows or reads a password or PIN field: it publishes `sensitive_field`
  and marks the take private instead.

It binds only while a take is live, and for up to two minutes after on the
field the text went into. Then it exits, so at rest it costs nothing and never
holds the seat against fcitx5 or ibus. A seat they hold already means the take
is typed as before.

This is the input-method code of the companion voxtype fork
(`src/output/preedit/`, `[output] preedit` / `read_field`) moved out of the
daemon. MIT; see LICENSE for the copyright notices.

## How Shuush drives it

| When | What |
|---|---|
| record start (`voxtype-toggle.sh`) | `setpriv --pdeathsig TERM -- shuush-ime serve --take ID [--live auto]` from a subshell that waits for it |
| the cleanup delivers (`voxtype-postprocess.sh`) | `shuush-ime commit --take ID < text`; exit 1 or 3: `shuush-ime end`, then voxtype types it |
| the take is cancelled (`voxtype-cancel.sh`) | `shuush-ime release` |
| the raw text is typed at once (skip while polishing) | `shuush-ime end` |

All of it only with `PP_IME=shuush` (off by default) and `shuush caps has
shuush_ime` (installed, and the compositor offers input-method-v2). `shuush
wire` then turns the daemon's own `[output] preedit` and `read_field` off.

## Commands

```
shuush-ime serve [--take ID [--live PATH|auto] [--live-format text]] [--read]
shuush-ime begin --take ID [--live PATH|auto]
shuush-ime commit --take ID < text   0 in the field · 1 type it (preedit gone) · 3 no input method · 4 cancelled
shuush-ime clear | end [--take ID] | release [--take ID]
shuush-ime read-field [--wait MS]   {"before": …, "after": …}
shuush-ime status | probe | --version
```

A second `serve` hands its request to the running helper. Client commands exit
3 when no helper runs. The control socket is `$XDG_RUNTIME_DIR/shuush/ime.sock`
(0600 in a 0700 directory, and the peer's uid is checked): one JSON line per
request, the text after it, one JSON line back.

## Files it writes (`$XDG_RUNTIME_DIR/voxtype/`, the fork's names)

| File | While |
|---|---|
| `live_in_field` | the take's words show in the field (the pill leaves them out) |
| `sensitive_field` | a password or PIN field is focused |
| `field_text.json` | a readable field is focused (not for 5 minutes after `field_text.off`) |
| `takes/ID/pp_private` | the take's field turned out to be a password field |

All of them go when it exits.

## Live words

`src/live.rs` has the `LiveSource` trait. Today's source is a file holding the
whole live transcript, replaced atomically: the fork's
`streaming_preview_file = "auto"` (`voxtype/live_text`). Whatever the file holds
when the take begins is the last take's and is ignored until the file changes,
and the words stop following it once voxtype leaves `recording` / `streaming`
(the cleanup writes the raw transcript there for the pill). Stock voxtype's
preview file (upstream #1122 and the preview-file PR after it) is another
`--live PATH`, or another `Format` if it is not plain text.

## Building and testing

```sh
shuush build-ime            # cargo build --release --locked, installs ~/.local/bin/shuush-ime
cargo test                  # state machine, driver, core, socket end to end, and the real
                            # binary against a private in-process compositor (tests/wayland.rs)
cargo clippy --all-targets -- -D warnings
```

No test binds a real seat: `src/server.rs` has a fake compositor, and
`tests/wayland.rs` runs a Wayland server of its own on a socket in a
temporary runtime directory.
