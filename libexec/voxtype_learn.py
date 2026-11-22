#!/usr/bin/env python3
"""voxtype-learn: the dictation's correction memory.

~/.config/voxtype/corrections.tsv holds what the recognizer hears for this
speaker and what was meant, one pair per line:

    heard<TAB>meant<TAB>source<TAB>count

voxtype-postprocess.sh looks up the pairs whose heard words occur in a
dictation and hands only those to the cleanup model as <hints>, so the file
can grow to thousands of lines without making any dictation slower.

Pairs come from two places:
  user  you fixed a dictation: `voxtype-learn fix` (bound to a key) diffs
        the corrected text on the clipboard against the last dictation.
        `voxtype-learn add HEARD MEANT` adds one by hand.
  seed  mishearings picked out of history.jsonl by hand.

The cleanup model's own swaps are not a learning signal. A model diff
("hosts" -> "hooks", "things" -> "tasks") used to be offered as a hint and
then primed the next dictation. `voxtype-learn accept` still stores a pair
that is already waiting in learn-candidates.json. Function words and
anything shorter than three letters are refused, including a hand add: one
of those ("a" -> a clipboard command) was being attached to a third of all
dictations.

Usage: voxtype-learn [list | pending | add HEARD MEANT | accept HEARD | reject HEARD | forget HEARD | fix]
"""
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONF = Path.home() / ".config/voxtype"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "voxtype"
TSV = CONF / "corrections.tsv"
CANDIDATES = STATE / "learn-candidates.json"
REJECTED = STATE / "learn-rejected.json"
HISTORY = STATE / "history.jsonl"
MAX_SPAN = 4            # words per side; longer swaps are rewrites, not mishearings
# A heard phrase made only of these is a function word, not a mishearing.
# "a" -> "pkill dunst" matched every dictation that contained the article.
STOPWORDS = frozenset(
    "a an the in on to is it of and or for as at be by do if we he she me my "
    "so no not but from with that this was are were you your its i am be been "
    "being have has had does did can will just like than then them they their "
    "there here what when where who how into over also very really".split()
)
HEADER = ("# Learned mishearings: heard<TAB>meant<TAB>source<TAB>count.\n"
          "# Managed by ~/Scripts/voxtype_learn.py; hand edits are kept.\n")
VOCAB_FILE = CONF / "vocabulary.txt"
CONFIG_TOML = CONF / "config.toml"
PP_ENV = CONF / "postprocess.env"
AMBIGUOUS = frozenset({"swimming", "piers", "codecs", "clawed", "next year's"})
_COMMON: set[str] | None = None


_DICT_PATHS = (
    "/usr/share/dict/cracklib-small",
    "/usr/share/dict/words",
    "/usr/share/dict/american-english",
    "/usr/share/dict/british-english",
)
_FALLBACK_COMMON = frozenset(
    "about after again against all also always another any around back because been before "
    "being between both build call came change check clean code come could create data day "
    "different does done down each edit error even every example file files find first fix "
    "from get give good great group hand have help here high home hooks hosts house how "
    "idea into just keep know large last left less let life line lines list little live local "
    "long look made make many may mean might mode more most move much must name need never "
    "new next night note notes nothing now number off often old once only open order other "
    "our out over own page part pass path people place plan play point post print project "
    "pull push put quick quite read real really right room run same save say see seem send "
    "set setup shell should show side simple since small some something soon sort sound "
    "start state still stop story study stuff such sure system take talk task tasks tell "
    "test text than that their them then there these they thing things think this those "
    "though thought three through time today together told too tool tools top try turn two "
    "under until update use used user using very want watch way ways week well went were "
    "what when where which while who why will window with without word words work working "
    "world would write wrong year years your zero".split()
)


def _common_words() -> set[str]:
    global _COMMON
    if _COMMON is None:
        loaded: set[str] = set()
        for dp in _DICT_PATHS:
            p = Path(dp)
            if p.is_file():
                try:
                    loaded = {w.strip().lower() for w in p.read_text(errors="ignore").splitlines() if w.strip()}
                    if loaded:
                        break
                except OSError:
                    continue
        _COMMON = loaded if loaded else set(_FALLBACK_COMMON)
    return _COMMON


def _vocab_map() -> dict[str, str]:
    out: dict[str, str] = {}
    if not VOCAB_FILE.exists():
        return out
    for line in VOCAB_FILE.read_text(errors="replace").splitlines():
        t = line.split("#", 1)[0].strip()
        if t:
            out[re.sub(r"[^a-z0-9]", "", t.lower())] = t
    return out


def _auto_learn_enabled() -> bool:
    if not PP_ENV.exists():
        return True
    val = "1"
    for line in PP_ENV.read_text(errors="replace").splitlines():
        m = re.match(r"^PP_AUTO_LEARN=([^\s#]+)", line.strip())
        if m:
            val = m.group(1).strip("\"'")
    return val == "1"


_SPOKEN_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "plus": "+", "dot": ".", "dash": "-", "slash": "/",
}


def _phonetic_key(s: str) -> str:
    """Fold spoken digits and common English phonetic twins for ASR matching."""
    toks = [_SPOKEN_DIGITS.get(t, t) for t in words(norm(s))]
    joined = re.sub(r"[^a-z0-9+]", "", "".join(toks))
    joined = joined.replace("ph", "f").replace("ck", "k").replace("qu", "kw")
    joined = joined.replace("c", "k").replace("w", "v").replace("y", "i")
    return re.sub(r"(.)\1+", r"\1", joined)


def load_rejected() -> set[str]:
    if not REJECTED.exists():
        return set()
    try:
        data = json.loads(REJECTED.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except ValueError:
        return set()


def save_rejected(keys: set[str]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = REJECTED.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(keys), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(REJECTED)


def reject_pair(heard: str, meant: str) -> str:
    """Permanently silence a (heard -> meant) suggestion so it never reappears."""
    key = f"{norm(heard)}\t{norm(meant)}"
    rej = load_rejected()
    rej.add(key)
    save_rejected(rej)
    cands = load_candidates()
    removed = False
    for k in [k for k in cands if norm(k.split("\t", 1)[0]) == norm(heard)]:
        cands.pop(k, None)
        removed = True
    if removed:
        save_candidates(cands)
    return key


def unreject_pair(key: str) -> None:
    rej = load_rejected()
    if key in rej:
        rej.discard(key)
        save_rejected(rej)


def is_rejected(heard: str, meant: str, rejected: set[str] | None = None) -> bool:
    rej = load_rejected() if rejected is None else rejected
    return f"{norm(heard)}\t{norm(meant)}" in rej


def _add_vocab_if_distinctive(term: str) -> bool:
    t = term.strip()
    if not t or len(t) < 2:
        return False
    if not (any(c.isupper() for c in t[1:]) or "." in t or "-" in t or "+" in t or t.isupper()):
        return False
    existing = _vocab_map()
    key = re.sub(r"[^a-z0-9]", "", t.lower())
    if not key or key in existing:
        return False
    VOCAB_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = VOCAB_FILE.read_text(errors="replace") if VOCAB_FILE.exists() else "# Vocabulary terms\n"
    if text and not text.endswith("\n"):
        text += "\n"
    tmp = VOCAB_FILE.with_suffix(".tmp")
    tmp.write_text(text + t + "\n", encoding="utf-8")
    tmp.replace(VOCAB_FILE)
    return True


def is_safe_auto_candidate(heard: str, meant: str, screen_terms: str = "") -> str | None:
    """Return canonical meant form if (heard -> meant) is safe to track/auto-learn.

    Non-poisoning guards:
      1. Must pass is_mishearing (short, phonetic match, not inflection/stopword)
         and not be user-rejected.
      2. `heard` must NOT be an ordinary English dictionary word (blocks hosts -> hooks).
      3. `meant` must be grounded in vocabulary.txt, visible <screen>/<draft>, or
         have distinctive proper-noun/code casing or punctuation.
    """
    h = norm(heard)
    if not h or h in AMBIGUOUS or is_rejected(h, meant) or not is_mishearing(h, meant):
        return None
    a = re.sub(r"[^a-z0-9]", "", h)
    b = re.sub(r"[^a-z0-9]", "", meant.lower())
    pa, pb = _phonetic_key(h), _phonetic_key(meant)
    raw_sim = difflib.SequenceMatcher(a=a, b=b).ratio() if (a and b) else 0.0
    phon_sim = difflib.SequenceMatcher(a=pa, b=pb).ratio() if (pa and pb) else 0.0
    if max(raw_sim, phon_sim) < 0.55:
        return None
    htoks = words(h)
    mtoks = words(norm(meant))
    if len(htoks) > 3 or len(mtoks) > 3:
        return None
    if htoks[0] in STOPWORDS or htoks[-1] in STOPWORDS:
        return None
    common = _common_words()
    if len(htoks) == 1 and htoks[0] in common:
        return None
    if len(htoks) >= 2 and all(t in common for t in htoks) and len(a) < 8 and phon_sim < 0.85:
        return None
    vmap = _vocab_map()
    if b in vmap:
        return vmap[b]
    sterms = {
        re.sub(r"[^a-z0-9]", "", p.lower())
        for p in re.split(r"[^A-Za-z0-9]+", screen_terms or "")
        if len(p) >= 3 or (len(p) == 2 and (p.isupper() or any(c.isdigit() for c in p)))
    }
    if b in sterms:
        return meant.strip()
    if (
        any(c.isupper() for c in meant[1:])
        or "." in meant
        or "-" in meant
        or (any(c.isalpha() for c in meant) and any(c.isdigit() for c in meant))
    ):
        return meant.strip()
    return None


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w'+#]", " ", s.lower()).split())


def words(s: str) -> list[str]:
    return s.split()


def load() -> dict[str, list]:
    pairs = {}
    if TSV.exists():
        for line in TSV.read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) >= 2 and f[0].strip():
                pairs[norm(f[0])] = [f[1].strip(), (f[2:3] or ["user"])[0], int((f[3:4] or ["1"])[0] or 1)]
    return pairs


def save(pairs: dict[str, list]) -> None:
    TSV.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{h}\t{m}\t{src}\t{n}\n" for h, (m, src, n) in sorted(pairs.items()))
    tmp = TSV.with_suffix(".tmp")
    tmp.write_text(HEADER + body)
    tmp.replace(TSV)


def unsafe_heard(heard: str) -> str | None:
    """Why this heard phrase must not become a hint, or None when it may."""
    h = norm(heard)
    toks = words(h)
    if not toks or len("".join(toks)) < 3:
        return "shorter than 3 letters"
    if all(t in STOPWORDS for t in toks):
        return "only function words"
    return None


def sounds_alike(heard: str, meant: str) -> bool:
    a, b = re.sub(r"[^a-z0-9]", "", heard), re.sub(r"[^a-z0-9]", "", meant.lower())
    pa, pb = _phonetic_key(heard), _phonetic_key(meant)
    raw_sim = difflib.SequenceMatcher(a=a, b=b).ratio() if (a and b) else 0.0
    phon_sim = difflib.SequenceMatcher(a=pa, b=pb).ratio() if (pa and pb) else 0.0
    return max(raw_sim, phon_sim) >= 0.5


def is_mishearing(heard: str, meant: str) -> bool:
    """A swap worth remembering: short, sounds alike, and not just grammar
    (plural, tense, contraction, hyphenation or spacing of the same word)."""
    h, m = norm(heard), norm(meant)
    if unsafe_heard(h) or not m or h == m:
        return False
    if len(words(h)) > MAX_SPAN or len(words(m)) > MAX_SPAN:
        return False
    if "'" in h + m:
        return False
    flat_h, flat_m = re.sub(r"[\s-]", "", h), re.sub(r"[\s-]", "", m)
    if flat_h == flat_m:
        return False
    short, long_ = sorted((flat_h, flat_m), key=len)
    if long_.startswith(short) and long_[len(short):] in {"s", "es", "ed", "d", "ing", "er", "ly"}:
        return False                       # agent/agents, play/playing
    return sounds_alike(h, m)


def remember(heard: str, meant: str, source: str) -> str | None:
    """Store one pair. Returns None on success, otherwise why it was refused."""
    if (why := unsafe_heard(heard)):
        return why
    pairs = load()
    key = norm(heard)
    old = pairs.get(key)
    n = old[2] + 1 if old and old[0].lower() == meant.lower() else 1
    # A user fix always wins over what the model guessed.
    if old and old[1] == "user" and source != "user" and old[0].lower() != meant.lower():
        return "a hand-added pair already has this"
    pairs[key] = [meant, source, n]
    save(pairs)
    return None


def _cand_view(v) -> tuple[int, bool]:
    if isinstance(v, dict):
        try:
            return int(v.get("n") or 0), bool(v.get("asked"))
        except (TypeError, ValueError):
            return 0, False
    try:
        return int(v), False
    except (TypeError, ValueError):
        return 0, False


def load_candidates() -> dict:
    if not CANDIDATES.exists():
        return {}
    try:
        data = json.loads(CANDIDATES.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_candidates(cands: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text(json.dumps(cands, ensure_ascii=False, indent=2) + "\n")


def tally(swaps: list[str], screen_terms: str = "") -> list[tuple[str, str]]:
    """Track only grounded, non-English mishearing swaps in learn-candidates.json.

    At 1 occurrence: surfaces in Voxtype Studio's Learning Inbox for 1-click approval.
    At >= 2 occurrences (when PP_AUTO_LEARN=1): auto-promotes to corrections.tsv
    and notifies via kuwai-notify so the user always sees what was learned.
    """
    existing = load()
    cands = load_candidates()
    changed = False
    promoted: list[tuple[str, str]] = []
    auto_on = _auto_learn_enabled()
    for swap in swaps:
        if " -> " not in swap:
            continue
        raw_h, raw_m = swap.split(" -> ", 1)
        canonical = is_safe_auto_candidate(raw_h, raw_m, screen_terms)
        if not canonical:
            continue
        h = norm(raw_h)
        if h in existing:
            continue
        key = f"{h}\t{canonical}"
        prev_n, asked = _cand_view(cands.get(key))
        n = prev_n + 1
        changed = True
        if auto_on and n >= 2:
            if not remember(h, canonical, "auto"):
                cands.pop(key, None)
                promoted.append((h, canonical))
        else:
            cands[key] = {"n": n, "asked": asked}
    if changed:
        save_candidates(cands)
    if promoted:
        for _h, m in promoted:
            _add_vocab_if_distinctive(m)
        sync_replacements()
        notify("Auto-learned dictation fix", "\n".join(f"{h} → {m}" for h, m in promoted))
    return promoted


def sync_replacements() -> int:
    """Sync non-ambiguous sure-name corrections into config.toml [text] replacements."""
    if not CONFIG_TOML.exists():
        return 0
    text = CONFIG_TOML.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^(replacements\s*=\s*\{)([^}]*)(\})", text, re.M)
    if not m:
        return 0
    current: dict[str, str] = {}
    for km in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"', m.group(2)):
        current[km.group(1).lower()] = km.group(2)
    common = _common_words()
    added = 0
    for h, (meant, _src, _n) in sorted(load().items()):
        if h in AMBIGUOUS or not h or not meant:
            continue
        toks = words(h)
        if len(toks) == 1 and toks[0] in common:
            continue
        if h not in current:
            added += 1
        current[h] = meant
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    inner = ", ".join(f'"{esc(k)}" = "{esc(v)}"' for k, v in sorted(current.items()))
    new_line = f"{m.group(1)} {inner} {m.group(3)}"
    updated = text[:m.start()] + new_line + text[m.end():]
    if updated != text:
        tmp = CONFIG_TOML.with_suffix(".tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(CONFIG_TOML)
    return added


def last_dictation() -> dict | None:
    if not HISTORY.exists():
        return None
    for line in reversed(HISTORY.read_text().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def heard_for(raw: list[str], clean: list[str], j1: int, j2: int) -> list[str]:
    """The raw (recognized) words behind clean[j1:j2], via the raw->clean
    alignment, so a fix teaches what the recognizer heard, not what the
    cleanup model guessed from it."""
    idx: list[int | None] = [None] * len(clean)
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(a=raw, b=clean, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(b2 - b1):
                idx[b1 + k] = a1 + k
        elif op == "replace":
            for k in range(b1, b2):
                idx[k] = a1 if k == b1 else a2 - 1
    hits = [i for i in idx[j1:j2] if i is not None]
    return raw[min(hits):max(hits) + 1] if hits else clean[j1:j2]


def fix() -> list[tuple[str, str]]:
    last = last_dictation()
    fixed = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True).stdout
    if not last or not fixed.strip():
        return []
    raw, clean, new = (words(norm(s)) for s in (last["raw"], last["clean"], fixed))
    # Words are compared lower-cased, but MEANT keeps the clipboard's
    # spelling ("Next.js", "Groq") by reading it from the corrected text.
    cased = fixed.split()
    learned = []
    for op, j1, j2, k1, k2 in difflib.SequenceMatcher(a=clean, b=new, autojunk=False).get_opcodes():
        if op != "replace" or j2 - j1 > MAX_SPAN or k2 - k1 > MAX_SPAN:
            continue
        heard = " ".join(heard_for(raw, clean, j1, j2))
        meant = " ".join(cased[k1:k2]) if len(cased) == len(new) else " ".join(new[k1:k2])
        meant = meant.strip(".,;:!?")
        if norm(heard) != norm(meant):
            why = remember(heard, meant, "user")
            if why:
                learned.append((heard, f"(skipped, {why})"))
            else:
                _add_vocab_if_distinctive(meant)
                learned.append((heard, meant))
    if any(not m.startswith("(skipped") for _, m in learned):
        sync_replacements()
    return learned


def notify(title: str, body: str) -> None:
    nbin = next(
        (str(p) for p in (
            Path(os.environ.get("KUWAI_ROOT", "")) / "bin/kuwai-notify",
            Path.home() / ".local/share/kuwai/bin/kuwai-notify",
            Path.home() / "Projects/rice/bin/kuwai-notify",
        ) if p.is_file() and os.access(p, os.X_OK)),
        "notify-send",
    )
    try:
        subprocess.run([nbin, "-a", "Dictation", "-t", "5000",
                        "-i", "audio-input-microphone", title, body],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        for h, (m, src, n) in sorted(load().items(), key=lambda x: (x[1][1], x[0])):
            print(f"{src:5} {n:3}  {h} -> {m}")
    elif cmd == "pending":
        rows = []
        for k, v in load_candidates().items():
            heard, _, meant = k.partition("\t")
            n, _asked = _cand_view(v)
            rows.append((n, heard, meant))
        if not rows:
            print("nothing waiting")
        for n, heard, meant in sorted(rows, key=lambda r: (-r[0], r[1])):
            print(f"{n:3}  {heard} -> {meant}")
    elif cmd == "add" and len(argv) == 4:
        why = remember(argv[2], argv[3], "user")
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return 1
        print(f"learned: {norm(argv[2])} -> {argv[3]}")
    elif cmd == "accept" and len(argv) == 3:
        want = norm(argv[2])
        cands = load_candidates()
        hit = [k for k in cands if k.split("\t", 1)[0] == want]
        if not hit:
            print("not found", file=sys.stderr)
            return 1
        heard, _, meant = hit[0].partition("\t")
        why = remember(heard, meant, "auto")
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return 1
        for k in hit:
            del cands[k]
        save_candidates(cands)
        print(f"learned: {heard} -> {meant}")
    elif cmd == "reject" and len(argv) == 3:
        want = norm(argv[2])
        cands = load_candidates()
        hit = [k for k in cands if k.split("\t", 1)[0] == want]
        if not hit:
            print("not found", file=sys.stderr)
            return 1
        for k in hit:
            h, _, m = k.partition("\t")
            reject_pair(h, m)
        print(f"rejected: {want}")
    elif cmd == "forget" and len(argv) == 3:
        pairs = load()
        if pairs.pop(norm(argv[2]), None) is None:
            print("not found", file=sys.stderr)
            return 1
        save(pairs)
    elif cmd == "sync-toml":
        n = sync_replacements()
        print(f"synced {n} new sure-name replacement(s) to {CONFIG_TOML}")
    elif cmd == "fix":
        learned = fix()
        if learned:
            notify("Learned from your fix", "\n".join(f"{h} → {m}" for h, m in learned))
        else:
            notify("Nothing to learn",
                   "Copy the corrected text of your last dictation, then press the key again.")
        for h, m in learned:
            print(f"learned: {h} -> {m}")
    else:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
