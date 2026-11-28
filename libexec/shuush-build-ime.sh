#!/usr/bin/env bash
# shuush-build-ime.sh — build shuush-ime, Shuush's Wayland input method helper.
#
#   shuush build-ime                build and install to ~/.local/bin/shuush-ime
#   shuush build-ime --check        is shuush-ime installed, current, and usable here? (exit 0 yes, 1 no)
#   shuush build-ime --dry-run      say what a build would do; nothing built or written
#   options: --prefix DIR   install into DIR instead of ~/.local/bin (SHUUSH_BIN_DIR)
#            --src DIR      the crate to build (default: the ime/ next to Shuush's libexec)
#            --bin PATH     the shuush-ime to --check (default: the one Shuush would use)
#
# What it is: the input-method code of the voxtype fork (`[output] preedit`,
# `read_field`) as a helper Shuush starts for a take. It shows the words heard
# so far as preedit in the focused field, commits the final text in one
# `commit_string` (no synthesized keys, any layout), and reads the field for
# smart join and edit learning. At rest it is not running, so it never holds
# the seat against fcitx5 or ibus. Turn it on with PP_IME=shuush in
# postprocess.env, then `shuush wire` (it turns the daemon's own preedit off).
#
# Needs cargo (rustup or pacman -S rust); the crate's only dependencies are
# libc, serde_json and the wayland-rs client crates. The build directory is
# ~/.cache/shuush/ime-target (SHUUSH_IME_TARGET).
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
src="$here/../ime"
prefix=${SHUUSH_BIN_DIR:-$HOME/.local/bin}
target=${SHUUSH_IME_TARGET:-${XDG_CACHE_HOME:-$HOME/.cache}/shuush/ime-target}
mode=build
bin=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) mode=check ;;
    --dry-run) mode=dry ;;
    --prefix) prefix=${2:?--prefix needs a directory}; shift ;;
    --src) src=${2:?--src needs a directory}; shift ;;
    --bin) bin=${2:?--bin needs a path}; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
    *) echo "shuush build-ime: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

crate_ok() { [ -f "$src/Cargo.toml" ] && [ -f "$src/Cargo.lock" ] && grep -q '^name = "shuush-ime"' "$src/Cargo.toml"; }
crate_version() { sed -n 's/^version = "\(.*\)"/\1/p' "$src/Cargo.toml" | head -1; }

case "$mode" in
  check)
    bin=${bin:-${SHUUSH_IME_BIN:-$(command -v shuush-ime || { [ -x "$HOME/.local/bin/shuush-ime" ] && echo "$HOME/.local/bin/shuush-ime"; } || true)}}
    if [ -z "$bin" ] || [ ! -x "$bin" ]; then
      echo "[warn] shuush-ime is not installed: shuush build-ime"
      exit 1
    fi
    rc=0
    have=$("$bin" --version 2>/dev/null | awk '{print $2}')
    if crate_ok && [ -n "$have" ] && [ "$have" != "$(crate_version)" ]; then
      echo "[warn] $bin is $have, the source is $(crate_version): shuush build-ime"
      rc=1
    else
      echo "[ok]   $bin ${have:-(no version)}"
    fi
    # Reads the compositor's registry; binds nothing.
    probe_rc=0
    "$bin" probe >/dev/null 2>&1 || probe_rc=$?
    case "$probe_rc" in
      0) echo "[ok]   the compositor offers input-method-v2" ;;
      1) echo "[warn] the compositor lacks input-method-v2: Shuush types instead"; rc=1 ;;
      *) echo "[info] no Wayland session to ask" ;;
    esac
    conf="${XDG_CONFIG_HOME:-$HOME/.config}/voxtype/postprocess.env"
    on=${PP_IME:-$(sed -n 's/^PP_IME=\([a-z]*\).*/\1/p' "$conf" 2>/dev/null | tail -1 || true)}
    if [ "$on" = shuush ]; then
      echo "[ok]   PP_IME=shuush: Shuush starts it for each take"
    else
      echo "[info] PP_IME is off: set PP_IME=shuush in postprocess.env, then shuush wire"
    fi
    exit "$rc"
    ;;
  dry)
    echo "shuush build-ime --dry-run: nothing is built or installed"
    if crate_ok; then echo "  source   $src (shuush-ime $(crate_version))"; else echo "  source   MISSING or not the shuush-ime crate: $src"; fi
    echo "  build    cargo build --release --locked (target $target)"
    echo "  install  $prefix/shuush-ime"
    if ! command -v cargo >/dev/null 2>&1; then echo "  missing  cargo (rustup, or pacman -S rust)"; exit 1; fi
    crate_ok || exit 1
    echo "  tools    all present"
    exit 0
    ;;
esac

crate_ok || { echo "shuush build-ime: $src is not the shuush-ime crate" >&2; exit 1; }
command -v cargo >/dev/null 2>&1 || { echo "shuush build-ime: missing: cargo (rustup, or pacman -S rust)" >&2; exit 1; }
mkdir -p "$target"
cargo build --release --locked --manifest-path "$src/Cargo.toml" --target-dir "$target"
install -Dm755 "$target/release/shuush-ime" "$prefix/shuush-ime"
echo "installed $prefix/shuush-ime ($("$prefix/shuush-ime" --version))"
echo "Turn it on: PP_IME=shuush in postprocess.env, then shuush wire and systemctl --user restart voxtype"
