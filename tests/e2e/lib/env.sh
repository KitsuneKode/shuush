#!/usr/bin/env bash
# lib/env.sh — the private world the harness runs in: its own runtime dir, HOME,
# D-Bus, PipeWire (a null microphone and nothing else), headless sway, the
# fake endpoints, a copy of the fork's voxtype and shuush installed into a
# scratch prefix. Nothing here reaches the owner's session: every child starts
# from `env -i` with this world's variables, and run.sh puts the whole tree in
# its own user, network and pid namespaces when the kernel allows it.
#
# Sourced by run.sh; the functions below read the E2E_* variables set by e2e_init.

E2E_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$E2E_LIB/.." && pwd)"
E2E_REPO="$(cd "$E2E_DIR/../.." && pwd)"
E2E_CACHE="${E2E_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/shuush-e2e}"
E2E_SWAY_PREFIX="$E2E_CACHE/sway-prefix"

log() { printf '[e2e %s] %s\n' "$(date +%T)" "$*" >&2; }
die() { log "FATAL: $*"; exit 2; }

# ---------------------------------------------------------------------------
# a compositor we start ourselves (headless sway), fetched on first use
# ---------------------------------------------------------------------------
e2e_fetch_sway() {
    [ -x "$E2E_SWAY_PREFIX/usr/bin/sway" ] && return 0
    command -v sway >/dev/null 2>&1 && { E2E_SWAY_PREFIX=""; return 0; }
    command -v pacman >/dev/null 2>&1 || die "no sway and no pacman to fetch one with: put sway on PATH or set E2E_SWAY_PREFIX"
    log "fetching sway into $E2E_SWAY_PREFIX (once; nothing is installed system-wide)"
    local dl="$E2E_CACHE/dl" u f n exp got
    mkdir -p "$dl" "$E2E_SWAY_PREFIX"
    for u in $(pacman -Sp --needed --noconfirm sway 2>/dev/null); do
        n=$(basename "$u"); n=${n//%3A/:}
        [ -s "$dl/$n" ] || curl -fsSL -o "$dl/$n" "$u" || die "download failed: $u"
        exp=$(bsdtar -xOf /var/lib/pacman/sync/extra.db --include '*/desc' 2>/dev/null \
            | awk -v f="$n" '/^%FILENAME%/{getline; cur=($0==f)} cur&&/^%SHA256SUM%/{getline; print; exit}')
        got=$(sha256sum "$dl/$n" | cut -d' ' -f1)
        [ -n "$exp" ] && [ "$exp" = "$got" ] || die "checksum mismatch for $n (db: $exp, file: $got)"
        bsdtar -xf "$dl/$n" -C "$E2E_SWAY_PREFIX" --exclude='.*' || die "cannot unpack $n"
    done
    [ -x "$E2E_SWAY_PREFIX/usr/bin/sway" ] || die "sway did not unpack"
}

# ---------------------------------------------------------------------------
# the world
# ---------------------------------------------------------------------------
e2e_init() {
    # Short path: a Wayland socket path may not pass 108 bytes.
    E2E_ROOT="$(mktemp -d /tmp/se2e.XXXXXX)"
    export E2E_ROOT
    E2E_RUN="$E2E_ROOT/run"
    E2E_HOME="$E2E_ROOT/home"
    E2E_BIN="$E2E_ROOT/bin"
    E2E_LOGS="$E2E_ROOT/logs"
    E2E_PREFIX="$E2E_ROOT/shuush"
    E2E_RESULTS="${E2E_RESULTS:-$E2E_ROOT/results.jsonl}"
    mkdir -p -m 700 "$E2E_RUN"
    mkdir -p "$E2E_HOME/.config" "$E2E_HOME/.local/share" "$E2E_HOME/.local/state" "$E2E_HOME/.cache" \
        "$E2E_BIN" "$E2E_LOGS" "$E2E_ROOT/targets" "$E2E_ROOT/wav"
    : > "$E2E_RESULTS"
    E2E_PIDS="$E2E_ROOT/pids"
    : > "$E2E_PIDS"
    E2E_RT_VOX="$E2E_RUN/voxtype"      # voxtype's runtime dir inside the private runtime dir
    export E2E_RT_VOX
    # The patched wtype is the one the owner's stack uses; fall back to the system one.
    E2E_WTYPE="$(command -v wtype || true)"
    E2E_VOXTYPE_SRC="${E2E_VOXTYPE_BIN:-$HOME/.local/lib/voxtype-fork/voxtype}"
    [ -x "$E2E_VOXTYPE_SRC" ] || die "no voxtype to copy: $E2E_VOXTYPE_SRC (set E2E_VOXTYPE_BIN)"
}

# The world's environment, rebuilt whenever a port or socket becomes known.
# Nothing from the caller's environment passes except what is named here.
e2e_mkenv() {
    E2E_ENV=(env -i
        "PATH=$E2E_BIN:/usr/bin:/bin"
        "HOME=$E2E_HOME"
        "XDG_RUNTIME_DIR=$E2E_RUN"
        "XDG_CONFIG_HOME=$E2E_HOME/.config"
        "XDG_DATA_HOME=$E2E_HOME/.local/share"
        "XDG_STATE_HOME=$E2E_HOME/.local/state"
        "XDG_CACHE_HOME=$E2E_HOME/.cache"
        "XDG_SESSION_TYPE=wayland"
        "XDG_CURRENT_DESKTOP=sway"
        "WAYLAND_DISPLAY=wayland-1"
        ${E2E_SWAYSOCK:+"SWAYSOCK=$E2E_SWAYSOCK"}
        "DBUS_SESSION_BUS_ADDRESS=unix:path=$E2E_RUN/bus"
        "PULSE_SERVER=unix:$E2E_RUN/pulse/native"
        "LANG=C.UTF-8" "LC_ALL=C.UTF-8" "TERM=xterm-256color"
        "LIBGL_ALWAYS_SOFTWARE=1"
        "SHUUSH_PREFIX=$E2E_PREFIX" "SHUUSH_BIN_DIR=$E2E_BIN"
        "E2E_FAKE=http://127.0.0.1:${E2E_PORT:-0}"
        "E2E_ROOT=$E2E_ROOT"
        "${E2E_EXTRA_ENV[@]}")
}
E2E_EXTRA_ENV=("E2E=1")

e2e_env() { "${E2E_ENV[@]}" "$@"; }

# Start a long-running process in the world; remember its pid for teardown.
e2e_spawn() {  # name cmd...
    local name="$1"; shift
    setsid "${E2E_ENV[@]}" "$@" > "$E2E_LOGS/$name.log" 2>&1 < /dev/null &
    echo "$! $name" >> "$E2E_PIDS"
}

e2e_wait() {  # description timeout cmd...   (poll until cmd succeeds)
    local what="$1" t="$2"; shift 2
    local i=0 max=$((t * 20))
    while ! "$@" >/dev/null 2>&1; do
        i=$((i + 1))
        [ "$i" -ge "$max" ] && { log "timed out waiting for $what"; return 1; }
        sleep 0.05
    done
    return 0
}

e2e_down() {
    [ -n "${E2E_PIDS:-}" ] && [ -f "$E2E_PIDS" ] || return 0
    local pid name
    # newest first
    tac "$E2E_PIDS" | while read -r pid name; do
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 0.4
    tac "$E2E_PIDS" | while read -r pid name; do
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    done
    : > "$E2E_PIDS"
}

e2e_up() {
    e2e_fetch_sway
    local lib=""
    [ -n "$E2E_SWAY_PREFIX" ] && lib="$E2E_SWAY_PREFIX/usr/lib"

    # --- the tools the world sees -------------------------------------------------
    cp "$E2E_VOXTYPE_SRC" "$E2E_BIN/voxtype"          # our own copy, never the installed one
    # wtype builds a one-off keymap per call, and headless sway lets the first key(s)
    # of a fresh virtual keyboard overtake the client's keymap switch: kitty and Chromium
    # then lose the first character. Hyprland (the owner's compositor) was not seen to.
    # `pre_type_delay_ms` cannot help (see scenarios/14_first_key.sh), so the world's
    # wtype settles first. E2E_WTYPE_SETTLE_MS=0 takes the shim away.
    if [ -n "$E2E_WTYPE" ]; then
        cat > "$E2E_BIN/wtype" <<SHIM
#!/bin/sh
# argv of every delivery, for scenarios that check how the daemon calls wtype
printf '%s\\n' "\$*" >> "$E2E_LOGS/wtype.argv"
ms="\${E2E_WTYPE_SETTLE_MS:-${E2E_WTYPE_SETTLE_MS:-100}}"
if [ "\$ms" -gt 0 ] 2>/dev/null; then exec "$E2E_WTYPE" -s "\$ms" "\$@"; fi
exec "$E2E_WTYPE" "\$@"
SHIM
        chmod +x "$E2E_BIN/wtype"
    fi
    [ -x "$E2E_CACHE/bin/shuush-ime" ] && cp "$E2E_CACHE/bin/shuush-ime" "$E2E_BIN/shuush-ime"
    local t
    for t in kitty kitten google-chrome-stable chromium; do
        command -v "$t" >/dev/null 2>&1 && ln -sf "$(command -v "$t")" "$E2E_BIN/$t"
    done
    if [ -n "$E2E_SWAY_PREFIX" ]; then
        ln -sf "$E2E_SWAY_PREFIX/usr/bin/swaymsg" "$E2E_BIN/swaymsg"
    fi
    # No window manager or desktop helper of the owner's can answer in here: say so
    # loudly instead of letting a missing helper look like a pass.
    for t in notify-send; do
        printf '#!/bin/sh\necho "$@" >> "%s/notify.log"\nexit 0\n' "$E2E_LOGS" > "$E2E_BIN/$t"; chmod +x "$E2E_BIN/$t"
    done

    # --- loopback + D-Bus --------------------------------------------------------
    e2e_mkenv
    ip link set lo up >/dev/null 2>&1 || true
    e2e_spawn dbus dbus-daemon --session --address="unix:path=$E2E_RUN/bus" --nofork --nopidfile >/dev/null
    e2e_wait "dbus" 5 test -S "$E2E_RUN/bus" || die "private dbus did not start"

    # --- the accessibility bus (AT-SPI): without it libatspi aborts the interpreter, so it is
    # started for real; voxtype_screen_ctx.py also checks for it and says "unknown" without it.
    if [ -x /usr/lib/at-spi-bus-launcher ]; then
        e2e_spawn atspi /usr/lib/at-spi-bus-launcher --launch-immediately
        e2e_wait "the a11y bus" 8 bash -c "ls $E2E_RUN/at-spi/bus* >/dev/null 2>&1 || ls $E2E_RUN/at-spi >/dev/null 2>&1" || log "no a11y bus: the screen context says unknown"
    fi

    # --- the fake endpoints ------------------------------------------------------
    mkdir -p "$E2E_ROOT/pages"
    cp "$E2E_DIR/apps/"* "$E2E_ROOT/pages/" 2>/dev/null || true
    e2e_spawn fake python3 -I "$E2E_DIR/fake_services.py" --port-file "$E2E_ROOT/port" --pages "$E2E_ROOT/pages"
    e2e_wait "fake services" 5 test -s "$E2E_ROOT/port" || die "fake services did not start"
    E2E_PORT="$(cat "$E2E_ROOT/port")"
    export E2E_PORT
    e2e_mkenv
    FAKE="http://127.0.0.1:$E2E_PORT"
    export FAKE

    # --- audio: a null microphone, no hardware ---------------------------------------
    mkdir -p "$E2E_HOME/.config/wireplumber/wireplumber.conf.d"
    cat > "$E2E_HOME/.config/wireplumber/wireplumber.conf.d/90-e2e.conf" <<'EOF'
wireplumber.profiles = {
  main = {
    monitor.alsa = disabled
    monitor.alsa-midi = disabled
    monitor.bluez = disabled
    monitor.bluez-midi = disabled
    monitor.v4l2 = disabled
    monitor.libcamera = disabled
  }
}
EOF
    e2e_spawn pipewire pipewire
    e2e_wait "pipewire" 8 test -S "$E2E_RUN/pipewire-0" || die "private pipewire did not start"
    e2e_spawn wireplumber wireplumber
    e2e_spawn pipewire-pulse pipewire-pulse
    e2e_wait "pulse" 8 test -S "$E2E_RUN/pulse/native" || die "private pipewire-pulse did not start"
    e2e_pactl load-module module-null-sink sink_name=e2e_mic "sink_properties=device.description=E2EMic" >/dev/null \
        || die "cannot create the null microphone"
    e2e_pactl set-default-sink e2e_mic
    e2e_pactl set-default-source e2e_mic.monitor
    # Anything but our null device means the private pipewire can see the owner's hardware.
    local others
    others=$(e2e_pactl list short sinks | grep -vc 'e2e_mic' || true)
    [ "$others" = 0 ] || die "the private audio server can see hardware sinks; refusing to go on:$(e2e_pactl list short sinks)"

    # --- the compositor ---------------------------------------------------------------
    cat > "$E2E_ROOT/sway.conf" <<'EOF'
default_border pixel 1
focus_follows_mouse no
xwayland disable
output HEADLESS-1 resolution 1280x800
EOF
    e2e_spawn sway env WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman \
        ${lib:+LD_LIBRARY_PATH=$lib} "${E2E_SWAY_PREFIX:+$E2E_SWAY_PREFIX/usr/bin/}sway" -c "$E2E_ROOT/sway.conf"
    e2e_wait "sway" 8 bash -c "ls $E2E_RUN/sway-ipc.*.sock" || { cat "$E2E_LOGS/sway.log" >&2; die "headless sway did not start"; }
    E2E_SWAYSOCK="$(ls "$E2E_RUN"/sway-ipc.*.sock | head -1)"
    export E2E_SWAYSOCK
    e2e_mkenv
    e2e_wait "sway ipc" 8 e2e_env swaymsg -t get_version || die "sway ipc did not answer"

    # --- shuush, installed into the scratch prefix --------------------------------------
    e2e_env bash "$E2E_REPO/install.sh" > "$E2E_LOGS/install.log" 2>&1 || { cat "$E2E_LOGS/install.log" >&2; die "install.sh failed in the scratch prefix"; }
    [ -x "$E2E_PREFIX/bin/shuush" ] || die "shuush did not install into $E2E_PREFIX"
    ln -sf "$E2E_PREFIX/bin/shuush" "$E2E_BIN/shuush"
    log "world is up: $E2E_ROOT (wayland-1, sway, null mic, fake endpoints on $E2E_PORT)"
}

e2e_pactl() { e2e_env pactl "$@"; }
e2e_sh()    { e2e_env bash -c "$@"; }
sway()      { e2e_env swaymsg "$@"; }
