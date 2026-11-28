#!/usr/bin/env python3
"""The kitty harness's focused program: a raw terminal that records what it is sent.

Typed keys arrive as bytes; a paste arrives between bracketed-paste markers, which
this program turns on, exactly as a shell or an editor does. Every chunk is one
JSON line in the log, so a scenario can tell typed text from pasted text and see
how many pieces a delivery came in (a streamed take lands sentence by sentence).

  term_target.py LOG
"""
import json
import os
import select
import signal
import sys
import termios
import time
import tty

PASTE_ON, PASTE_OFF = b"\x1b[200~", b"\x1b[201~"


def main(path: str) -> int:
    log = open(path, "a", buffering=1)

    def emit(kind: str, data: bytes) -> None:
        text = data.decode("utf-8", "replace").replace("\r", "\n")
        log.write(json.dumps({"t": time.time(), "kind": kind, "text": text}) + "\n")

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    sys.stdout.write("\x1b[?2004h")  # bracketed paste on
    sys.stdout.write("e2e target ready\r\n")
    sys.stdout.flush()
    emit("ready", b"")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    pasting, buf = False, b""
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            data = os.read(fd, 65536)
            if not data:
                break
            buf += data
            while buf:
                if pasting:
                    i = buf.find(PASTE_OFF)
                    if i < 0:
                        break  # wait for the closing marker
                    emit("paste", buf[:i])
                    buf, pasting = buf[i + len(PASTE_OFF):], False
                    continue
                i = buf.find(PASTE_ON)
                if i < 0:
                    emit("type", buf)
                    sys.stdout.write(buf.decode("utf-8", "replace").replace("\r", "\r\n"))
                    sys.stdout.flush()
                    buf = b""
                else:
                    if i:
                        emit("type", buf[:i])
                    buf, pasting = buf[i + len(PASTE_ON):], True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
