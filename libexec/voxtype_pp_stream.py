#!/usr/bin/env python3
"""Stream one cleanup call and print the cleaned text as it is verified.

Called by voxtype-postprocess.sh when voxtype reads the command's output as
it arrives (VOXTYPE_STREAM_OUTPUT=1). Typing then starts with the model's
first sentence, not after its last one: on a long dictation the cleanup call
is 1-1.5 s, and the first sentence is back in ~0.25 s.

    voxtype_pp_stream.py URL BODY_JSON RAW_TXT FINAL_OUT
    (FINAL_OUT.printed is created as soon as any text has been printed)
    env: PP_KEY (optional bearer key), PP_FIRST_BYTE_S, PP_IDLE_S, PP_TOTAL_S

The whole transcript goes to the model in one request, so the cleanup has
the same context as the non-streaming call. What changes is that text is on
screen before the model has finished, so the checks the shell's sane() runs
on a whole reply have to run per sentence, before that sentence is printed:

- A sentence is released only once it is complete (., !, ?, or a line end)
  and its words line up with the transcript, in order, from where the last
  released sentence ended. A cleanup keeps most of the speaker's words; a
  reply to a dictated question, a preamble or an invented sentence does not.
- A sentence that does not line up (say the cleanup dropped a long
  self-correction) does not fail the call: releasing stops, the rest of the
  reply is collected and judged as one block against the rest of the
  transcript when the stream ends. The worst case is today's behaviour:
  the cleaned text arrives all at once at the end.
- The first sentence also passes the preamble check ("Sure", "Here is").
- The released text may not grow past what cleanup can produce (130% of
  the transcript's words, +3 for short ones).

Nothing is ever typed twice and nothing is lost:
- Fails before anything was printed (network, timeout, rejected first
  sentence): exit 10 (transport) or 12 (content), print nothing; the shell
  tries the next provider and finally the raw transcript.
- Fails after something was printed (stream drops, a later sentence is
  rejected, the reply is cut off at max_tokens): print the transcript from
  the point the released text covers, exit 11.
- Completes: exit 0.
FINAL_OUT always receives the full delivered text (for history/clipboard).
"""
import http.client
import json
import os
import re
import sys
import time
import urllib.parse

EXIT_OK, EXIT_TRANSPORT, EXIT_PARTIAL, EXIT_REJECTED = 0, 10, 11, 12

WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
# Sentence end: terminal punctuation (plus closing quotes/brackets) followed
# by whitespace, or a line break (list items, lead-ins ending in ":"). Not
# after a digit, so a list's "1." is not a sentence of its own.
BOUNDARY = re.compile(r"(?<!\d)[.!?…][\"')\]]*(?=\s)|\n")
PREAMBLE = re.compile(
    r"^(sure|here( is|'s)|i am (a|an) |i'm (a|an|sorry)|as an ai|i can(no|')t|"
    r"certainly|of course|okay, here)",
    re.I,
)
TAGS = re.compile(r"</?(dictation|previous|app|hints|screen|draft|selection|clipboard|now)>")
LIST_MARKER = re.compile(r"^\s*(\d+[.)]|[-*•])\s+")


def norm_words(text: str) -> list[str]:
    return WORD.findall(text.lower().replace("’", "'"))


def lcs_last(a: list[str], b: list[str]) -> tuple[int, int | None]:
    """Length of the longest common subsequence of `a` and `b`, and the index
    in `b` of the last word of one such alignment (None when nothing
    matches). Dictation sentences are short, so O(len(a) * len(b)) is fine."""
    n, m = len(a), len(b)
    if not n or not m:
        return 0, None
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai, row, nxt = a[i], dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if ai == b[j] else max(nxt[j], row[j + 1])
    i = j = 0
    last = None
    while i < n and j < m:
        if a[i] == b[j] and dp[i][j] == dp[i + 1][j + 1] + 1:
            last = j
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return dp[0][0], last


class Aligner:
    """Tracks how far into the transcript the released text has reached."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        # (word, start offset in raw) for every transcript word
        self.words = [(m.group(0), m.start()) for m in WORD.finditer(raw.lower().replace("’", "'"))]
        self.cursor = 0          # index of the first transcript word not yet covered
        self.released_words = 0

    def accepts(self, sentence: str, whole_rest: bool = False) -> bool:
        """True (and advance) when `sentence` reads as a cleanup of the next
        stretch of the transcript. `whole_rest` widens the window to the rest
        of the transcript (for judging a held-back block at the end)."""
        out = norm_words(LIST_MARKER.sub("", sentence))
        if not out:
            return True  # punctuation / list marker only
        limit = len(self.words)
        budget = limit * 13 // 10 + 3
        if self.released_words + len(out) > budget:
            return False
        # In-order alignment (longest common subsequence) against a window
        # ahead of the cursor. The window allows for dropped fillers and
        # self-corrections (transcript words the cleanup leaves out); LCS,
        # unlike a greedy scan, is not thrown off by an added word ("and")
        # that also occurs further ahead.
        window_end = limit if whole_rest else min(limit, self.cursor + 3 * len(out) + 40)
        hits, last = lcs_last(out, [w for w, _ in self.words[self.cursor:window_end]])
        if last is not None:
            last += self.cursor
        # A one-word sentence must be the speaker's word ("Yes."); longer ones
        # may carry repairs of misheard words and digits for spoken numbers.
        need = 1.0 if len(out) == 1 else 0.5
        if hits < need * len(out):
            return False
        if last is not None:
            self.cursor = last + 1
        self.released_words += len(out)
        return True

    def remainder(self) -> str:
        """Transcript text not covered by what was released."""
        if self.cursor >= len(self.words):
            return ""
        return self.raw[self.words[self.cursor][1]:].strip()

    def uncovered_words(self) -> int:
        return len(self.words) - self.cursor

    def covered_text(self, before: int, after: int) -> str:
        """The transcript slice this sentence was aligned to."""
        if after <= before or not self.words:
            return ""
        before = max(0, before)
        after = min(len(self.words), after)
        if after <= before:
            return ""
        start = self.words[before][1]
        last, last_at = self.words[after - 1]
        return self.raw[start:last_at + len(last)]


def stream_deltas(url: str, body: dict, key: str, first_byte_s: float, idle_s: float, total_s: float):
    """Yield (content_delta, finish_reason) from an OpenAI-compatible SSE stream."""
    u = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port, timeout=first_byte_s)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
               "User-Agent": "voxtype-postprocess/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    path = u.path + (f"?{u.query}" if u.query else "")
    deadline = time.monotonic() + total_s
    conn.request("POST", path, json.dumps(body), headers)
    resp = conn.getresponse()
    if resp.status != 200:
        raise ConnectionError(f"HTTP {resp.status}: {resp.read(300).decode(errors='replace')}")
    if conn.sock is not None:
        conn.sock.settimeout(idle_s)
    buf = b""
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("total timeout")
        data = resp.read1(65536)
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                return
            msg = json.loads(payload)
            choice = (msg.get("choices") or [{}])[0]
            delta = (choice.get("delta") or {}).get("content") or ""
            yield delta, choice.get("finish_reason")


# Milliseconds from start to the first printed text, for the log line.
FIRST_TEXT_MS: list[int] = []
_T0 = time.monotonic()


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import voxtype_pp_gate as _gate
except Exception:
    _gate = None                        # then nothing is released ungated (see release)
_TERMS: list = []
if _gate is not None:
    try:                                # the spelling pass; never the reason a dictation fails
        _TERMS = _gate.vocab_terms()
    except Exception:
        _TERMS = []


class Emitter:
    def __init__(self, marker: str | None = None) -> None:
        self.parts: list[str] = []
        self.marker = marker

    def emit(self, text: str) -> None:
        if not text:
            return
        if _gate is not None and _TERMS:          # vocabulary spelled exactly (2026-11-17)
            try:
                text = _gate.finish(text, _TERMS)
            except Exception:
                pass
        if _gate is not None:
            try:
                text = _gate.expand_inline(text)
            except Exception:
                pass
        if not self.parts:
            text = text.lstrip()
            if not text:
                return
        elif not self.parts[-1][-1].isspace() and not text[0].isspace() and text[0] not in ".,!?;:)]}":
            text = " " + text
        if not self.parts:
            FIRST_TEXT_MS.append(int((time.monotonic() - _T0) * 1000))
        if not self.parts and self.marker:
            # Tells the shell text is on screen, even if this process dies:
            # it must then never print the transcript itself.
            open(self.marker, "w").close()
        self.parts.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()

    @property
    def started(self) -> bool:
        return bool(self.parts)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def run(url: str, body: dict, raw: str, key: str, marker: str | None = None) -> tuple[int, str]:
    first_byte_s = float(os.environ.get("PP_FIRST_BYTE_S", "2.5"))
    idle_s = float(os.environ.get("PP_IDLE_S", "2.0"))
    # 4s when the shell forgot to pass a budget. The old 15s default was a
    # stalled socket sitting through the whole daemon timeout.
    total_s = float(os.environ.get("PP_TOTAL_S", "4"))

    body = dict(body)
    body["stream"] = True
    # Room for the whole cleanup: ~4 chars per token, plus list markers and
    # margin. A fixed 1024 cut off dictations longer than ~5 minutes.
    body["max_tokens"] = max(int(body.get("max_tokens") or 0), 1024, len(raw) // 2)

    aligner = Aligner(raw)
    out = Emitter(marker)
    pending = ""          # generated, not yet released
    in_think = False
    finish = None

    def fall_back() -> tuple[int, str]:
        if not out.started:
            return EXIT_REJECTED, ""
        rest = aligner.remainder()
        if rest:
            out.emit(("" if out.text[-1:].isspace() else " ") + rest)
        return EXIT_PARTIAL, out.text

    def release(chunk: str, whole_rest: bool = False, last: bool = False) -> bool:
        # Typing cannot be undone: without the gate, nothing goes out as the
        # model wrote it; the rest is typed as heard (fall_back).
        if _gate is None:
            return False
        if not out.started:
            head = TAGS.sub("", chunk).lstrip()
            if PREAMBLE.match(head):
                return False
        cursor_before = aligner.cursor
        released_before = aligner.released_words
        if not aligner.accepts(chunk, whole_rest):
            return False
        # The 50% overlap check still lets a synonym, negation flip, or
        # invented first-person reply through. Run the span gate + invariants
        # before the words are typed; typing cannot be undone.
        try:
            # The last sentence answers for everything left in the
            # transcript: "use port five" -> "Use port 5." stopped the
            # alignment before "five", which was then lost.
            end = len(aligner.words) if last else aligner.cursor
            covered = aligner.covered_text(cursor_before, end)
            if last:
                aligner.cursor = len(aligner.words)
            if covered.strip():
                stripped = chunk.lstrip()
                lead = chunk[:len(chunk) - len(stripped)]
                gated = _gate.constrain(covered, stripped)
                if gated:
                    if not _gate.output_invariants_ok(covered, gated):
                        aligner.cursor = cursor_before
                        aligner.released_words = released_before
                        return False
                    # A sentence the gate re-opened with a restored word
                    # must still be spaced from the one before it.
                    if not lead and out.text and not out.text[-1:].isspace() and gated[:1].isalnum():
                        lead = " "
                    chunk = lead + gated
        except Exception:
            aligner.cursor = cursor_before
            aligner.released_words = released_before
            return False
        out.emit(chunk)
        return True

    held = None           # set once a sentence fails: the rest is judged at the end
    carry = ""            # whitespace between sentences (a list's line breaks)
    lag = None            # the newest sentence, released when the next one arrives

    try:
        for delta, fin in stream_deltas(url, body, key, first_byte_s, idle_s, total_s):
            finish = fin or finish
            if not delta:
                continue
            pending += delta
            # Reasoning blocks never reach the screen.
            if in_think or "<think>" in pending:
                if "</think>" in pending:
                    pending = pending.split("</think>", 1)[1]
                    in_think = False
                else:
                    in_think = True
                    continue
            pending = TAGS.sub("", pending)
            while True:
                m = None
                for m in BOUNDARY.finditer(pending):
                    break
                if m is None:
                    break
                cut = m.end()
                chunk, pending = pending[:cut], pending[cut:]
                if held is not None:
                    held += chunk
                    continue
                if not chunk.strip():
                    carry += chunk       # goes out with the next sentence
                    continue
                chunk, carry = carry + chunk, ""
                # One sentence behind, so the last one is only judged once
                # the stream has ended, against all of the remaining words.
                prev, lag = lag, chunk
                if prev is not None and not release(prev):
                    held, lag = prev + lag, None
    except Exception as e:  # noqa: BLE001 - any failure: fall back, never crash mid-text
        print(f"voxtype_pp_stream: {type(e).__name__}: {e}", file=sys.stderr)
        if not out.started:
            return EXIT_TRANSPORT, ""
        return fall_back()

    tail = ((lag or "") + carry + TAGS.sub("", pending)).rstrip()
    if held is not None:
        block = (held + tail).rstrip()
        if not release(block, whole_rest=True, last=True):
            return fall_back()
    elif tail.strip() and not release(tail, last=True):
        if not release(tail, whole_rest=True, last=True):
            return fall_back()
    if not out.started:
        return EXIT_REJECTED, ""
    # Cut off at max_tokens, or stopped while a long stretch of the
    # transcript is still uncovered: the model dropped the end of the
    # dictation. Deliver the rest as heard rather than lose it.
    if finish == "length" or (aligner.uncovered_words() > 25
                              and aligner.uncovered_words() * 10 > len(aligner.words) * 3):
        return fall_back()
    return EXIT_OK, out.text


def main() -> int:
    url, body_path, raw_path, final_path = sys.argv[1:5]
    with open(body_path) as f:
        body = json.load(f)
    with open(raw_path) as f:
        raw = f.read()
    t0 = time.monotonic()
    code, text = run(url, body, raw, os.environ.get("PP_KEY", ""), final_path + ".printed")
    # One line per call on stderr (the shell appends it to /tmp/voxtype-pp.log):
    # which route, how it ended, and when text started and finished.
    host = urllib.parse.urlsplit(url).netloc
    first = f"{FIRST_TEXT_MS[0]}ms" if FIRST_TEXT_MS else "-"
    print(f"{time.strftime('%T')}  stream  {host}  exit={code}  first_text={first}  "
          f"total={int((time.monotonic() - t0) * 1000)}ms  in={len(raw)}B out={len(text)}B",
          file=sys.stderr)
    with open(final_path, "w") as f:
        f.write(text)
    return code


def _selftest() -> int:
    """Drive run() with canned model deltas (no network)."""
    import io
    global stream_deltas
    cases = [
        # (raw transcript, model sentences, what must be on screen)
        ("send it today but I do not approve",
         ["Send it today."], lambda t: "not approve" in t),
        ("we will use port five for the test server and then restart it",
         ["We will use port 5 for the test server", " and then restart it."],
         lambda t: "5" in t and "restart it" in t),
        ("use port five",
         ["Use port 5."], lambda t: t.rstrip(".").endswith(("5", "five"))),
        ("we do not approve the release today and we will wait",
         ["We do not approve the release today.", " We will wait."],
         lambda t: "do not approve" in t and "wait" in t),
    ]
    real, bad = stream_deltas, 0
    for raw, deltas, ok in cases:
        stream_deltas = lambda *a, d=deltas, **k: iter([(x, None) for x in d] + [("", "stop")])  # noqa: E731
        saved, sys.stdout = sys.stdout, io.StringIO()
        try:
            code, text = run("http://fake", {}, raw, "")
        finally:
            sys.stdout = saved
        if not ok(text):
            bad += 1
            print(f"FAIL {raw!r}: exit={code} typed={text!r}")
    stream_deltas = real
    print("stream selftest ok" if not bad else f"{bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        sys.exit(_selftest())
    sys.exit(main())
