#!/usr/bin/env bash
# shuush-build-wtype.sh — build the wtype that voxtype's type mode should use.
#
#   shuush build-wtype                build and install to ~/.local/bin/wtype
#   shuush build-wtype --check        is the wtype in use patched? (exit 0 yes, 1 no)
#   shuush build-wtype --dry-run      say what a build would do; no network, nothing written
#   options: --prefix DIR   install into DIR instead of ~/.local/bin (SHUUSH_BIN_DIR)
#            --src DIR      patch and build a wtype checkout you already have (no clone)
#            --rev REV      wtype revision to build (WTYPE_REV, default 4b9cc29)
#            --bin PATH     the wtype to --check (default: the one on PATH)
#
# Why not the packaged wtype 0.4:
# - It drops apostrophes in Chromium/Electron apps ("It's" -> "Its"); fixed
#   upstream after 0.4 (printable keycodes for literal text, 82bca82).
# - It sleeps a hard-coded 2 ms after every key press and release, capping
#   typing at ~150-240 chars/s (a 3-minute dictation took 16 s to type).
#   wtype-key-gap.patch (next to this script) makes that gap WTYPE_KEY_GAP_US
#   (default 2000, so other callers behave as before). Set it in voxtype's
#   service environment: Environment=WTYPE_KEY_GAP_US=200
# - Explicit keys (-k Return, -P Shift_L) got logical code N as their evdev
#   code, so voxtype's Shift+Enter reached Chromium as code=Escape. The patch
#   puts named keys on their real positions (Enter is code=Enter now).
#
# Measured (2189 chars, nested Hyprland, Electron 43 textarea and a
# contenteditable doing work per keystroke, kitty): exact at every gap
# down to 0; 2000us = 240 c/s, 200us = ~2100 c/s, 0 = ~28000 c/s.
#
# This script came out of the voxtype fork (scripts/build-wtype.sh): it is not
# daemon code, so it lives with Shuush.
set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
patch="$here/wtype-key-gap.patch"
rev=${WTYPE_REV:-4b9cc29}
prefix=${SHUUSH_BIN_DIR:-$HOME/.local/bin}
mode=build
src=""
bin=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) mode=check ;;
    --dry-run) mode=dry ;;
    --prefix) prefix=${2:?--prefix needs a directory}; shift ;;
    --src) src=${2:?--src needs a directory}; shift ;;
    --rev) rev=${2:?--rev needs a revision}; shift ;;
    --bin) bin=${2:?--bin needs a path}; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
    *) echo "shuush build-wtype: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

patch_ok() { [ -f "$patch" ] && git apply --numstat "$patch" >/dev/null 2>&1; }

missing_tools() {
  local t out=""
  for t in git gcc wayland-scanner pkg-config; do command -v "$t" >/dev/null 2>&1 || out="$out $t"; done
  if command -v pkg-config >/dev/null 2>&1; then
    pkg-config --exists xkbcommon wayland-client 2>/dev/null || out="$out xkbcommon wayland (dev files)"
  fi
  printf '%s' "${out# }"
}

case "$mode" in
  check)
    bin=${bin:-$(command -v wtype || true)}
    rc=0
    if ! patch_ok; then echo "[warn] $patch is missing or is not a patch"; rc=1; else echo "[ok]   key-gap patch present"; fi
    if [ -z "$bin" ] || [ ! -x "$bin" ]; then
      echo "[warn] no wtype found: pacman -S wtype, then shuush build-wtype"
      exit 1
    fi
    if grep -qa WTYPE_KEY_GAP_US "$bin" 2>/dev/null; then
      echo "[ok]   $bin has the key-gap patch (WTYPE_KEY_GAP_US)"
    else
      echo "[warn] $bin is stock wtype: slow typing (~240 chars/s), explicit keys on the wrong codes: shuush build-wtype"
      rc=1
    fi
    gap=$(systemctl --user show -p Environment voxtype.service 2>/dev/null | grep -o 'WTYPE_KEY_GAP_US=[0-9]*' || true)
    if [ -n "$gap" ]; then
      echo "[ok]   voxtype.service sets $gap"
    else
      echo "[info] voxtype.service does not set WTYPE_KEY_GAP_US: the default 2000us gap applies (add Environment=WTYPE_KEY_GAP_US=200 to a drop-in for ~10x faster typing)"
    fi
    exit "$rc"
    ;;
  dry)
    echo "shuush build-wtype --dry-run: nothing is cloned, built or installed"
    if patch_ok; then echo "  patch    $patch"; else echo "  patch    MISSING or not a patch: $patch"; fi
    if [ -n "$src" ]; then echo "  source   $src (your checkout; the patch is applied to a copy)"; else echo "  source   git clone https://github.com/atx/wtype at $rev"; fi
    echo "  build    wayland-scanner + gcc -O2 against xkbcommon and wayland-client"
    echo "  install  $prefix/wtype"
    m=$(missing_tools)
    if [ -n "$m" ]; then echo "  missing  $m"; exit 1; fi
    echo "  tools    all present"
    exit 0
    ;;
esac

patch_ok || { echo "shuush build-wtype: $patch is missing or not a patch" >&2; exit 1; }
m=$(missing_tools)
[ -z "$m" ] || { echo "shuush build-wtype: missing: $m (pacman -S git gcc wayland pkgconf libxkbcommon)" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -n "$src" ]; then
  [ -f "$src/main.c" ] || { echo "shuush build-wtype: $src is not a wtype checkout (no main.c)" >&2; exit 1; }
  cp -a "$src" "$work/wtype"
  cd "$work/wtype"
  rm -rf .git b
else
  git clone -q https://github.com/atx/wtype "$work/wtype"
  cd "$work/wtype"
  git checkout -q "$rev"
fi
git apply "$patch"
mkdir -p b
proto=protocol/virtual-keyboard-unstable-v1.xml
wayland-scanner client-header "$proto" b/virtual-keyboard-unstable-v1-client-protocol.h
wayland-scanner private-code "$proto" b/virtual-keyboard-unstable-v1-protocol.c
# shellcheck disable=SC2046
gcc -O2 -Ib -o b/wtype main.c b/virtual-keyboard-unstable-v1-protocol.c \
    $(pkg-config --cflags --libs xkbcommon wayland-client)
install -Dm755 b/wtype "$prefix/wtype"
echo "installed $prefix/wtype (wtype ${src:+checkout $src}${src:-$rev} + key-gap patch)"
echo "Set Environment=WTYPE_KEY_GAP_US=200 in voxtype's service to type ~10x faster; check with: shuush build-wtype --check"
