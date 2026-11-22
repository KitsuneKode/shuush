#!/usr/bin/env python3
"""Keep a cleanup's edits inside the repairs a dictation is allowed to make.

The model is asked to fix recognition errors. It also rewrites: "things"
becomes "tasks", "worktrees" becomes "branches". sane() only checks length,
so those swaps were typed. constrain() puts a span back unless the change is
one of:

  - the same words, with the model's casing and punctuation
  - a filler, a stutter, or the abandoned half of a self-correction
  - a sound-alike, including a letter-join ("light house" -> lighthouse)
  - a spoken number written as digits, or "number one" written as "1."
  - a missing article (a, an, the)
  - a pair from corrections.tsv, or the one title pair for this take

A pair is not forced. "swimming" stays "swimming" when the model keeps it,
and may become "streaming" when the model makes that change.

Sure names are a different step. hard_apply() rewrites a heard phrase that
is not an ordinary English word ("hyperlane" -> Hyprland) before the model
sees the text. Real English words that are sometimes right stay hints:
swimming, piers, codecs, clawed, and the phrase "next year's".

    voxtype_pp_gate.py strip                 stdin -> stdout
    voxtype_pp_gate.py hard                  stdin -> stdout
    voxtype_pp_gate.py hints                 stdin is the text the model sees
        prints the <hints> body, or nothing
    voxtype_pp_gate.py constrain RAW         stdin is the model's reply
    voxtype_pp_gate.py app CLASS TITLE       stdin is the heard text
        prints "<pair-or-empty>\\x1e<app block>"
    voxtype_pp_gate.py test

PP_TITLE_PAIR="heard<TAB>meant" adds one pair for this take only.
"""
import difflib
import os
import re
import sys
from pathlib import Path

CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxtype"
TSV = CONF / "corrections.tsv"

# Heard forms that are real English. A blind replace breaks the sentence
# where the word was actually meant ("I went swimming", "next year's plan").
AMBIGUOUS = frozenset({"swimming", "piers", "codecs", "clawed", "next year's"})

# Title tokens that are path clutter, not a name the speaker was reaching for.
GENERIC = frozenset(
    "readme src source index main test tests spec app lib dist node modules "
    "config json toml lock license changelog assets public static components "
    "pages styles image images docs doc file files code window home projects "
    "users desktop download downloads tmp var etc bin usr local md rs py js ts "
    "tsx jsx css html vue go rb lua sh txt yml yaml png jpg svg pdf woff map "
    "cursor code".split()
)

FILLER = frozenset("um uh er ah hmm mhm erm uhh umm uh-huh".split())
FILLER_PHRASES = (("you", "know"), ("so", "yeah"), ("kind", "of"), ("sort", "of"))
ARTICLES = frozenset({"a", "an", "the"})
# Expansions of contractions are stopwords on the model side ("it's" -> "it is").
STOP = frozenset(
    "a an the in on to is it of and or for as at be by do if we he she me my "
    "so no not but from with that this was are were you your its i am been "
    "being have has had does did can will just than then them they their "
    "there here what when where who how into over".split()
)
NUMBER = frozenset(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty "
    "thirty forty fifty sixty seventy eighty ninety hundred thousand million "
    "billion oh o point dot".split()
)
LIST_LEADS = frozenset({"number", "point", "bullet", "item"})
ORDINALS = frozenset(
    "one two three four five six seven eight nine ten first second third "
    "fourth fifth sixth seventh eighth ninth tenth".split()
)
# A marker inside a deleted span is the speaker taking the words back.
MARKERS = (
    ("no", "wait"),
    ("wait", "no"),
    ("i", "mean"),
    ("scratch", "that"),
    ("or", "rather"),
    ("let", "me", "rephrase"),
    ("actually",),
)
# A leading affix that flips the meaning. "in" + "visible" is not a mishearing
# the way "de" + "bounce" is. "de" is intentionally not in this list.
FLIP_PREFIX = ("un", "in", "im", "non", "dis", "ir", "il")
INFLECTION = frozenset({"s", "es", "ed", "d", "ing", "er", "ly", "n"})

TOKEN = re.compile(r"\d+(?:[.:]\d+)+|[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*")
# A whole sentence that is a whisper sign-off. "Thank you." and "Bye." are
# things a person says; "thanks for watching" is the model inventing an ending.
HALLUC = re.compile(
    r"(?i)(?:thanks?(?: you)?(?: (?:so|very) much)? for (?:watching|listening)"
    r"|you.?re welcome|the dictation is clear.*)"
)


class Tok:
    def __init__(self, surface: str, start: int, end: int) -> None:
        self.surface = surface
        self.start = start
        self.end = end
        self.key = re.sub(r"[^a-z0-9]", "", surface.lower())


def tokens(text: str) -> list[Tok]:
    return [Tok(m.group(0), m.start(), m.end()) for m in TOKEN.finditer(text)]


def norm_phrase(s: str) -> str:
    return " ".join(re.sub(r"[^\w'+#]", " ", s.lower()).split())


def load_pairs() -> list[tuple[str, str]]:
    pairs = []
    if not TSV.exists():
        return pairs
    for line in TSV.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 2 and f[0].strip():
            pairs.append((norm_phrase(f[0]), f[1].strip()))
    return pairs


def title_pair_from_env() -> tuple[str, str] | None:
    raw = os.environ.get("PP_TITLE_PAIR", "")
    if "\t" not in raw:
        return None
    heard, meant = raw.split("\t", 1)
    heard, meant = norm_phrase(heard), meant.strip()
    if not heard or not meant:
        return None
    return heard, meant


def pairs_for_call(pairs: list[tuple[str, str]] | None) -> list[tuple[str, str]]:
    out = list(load_pairs() if pairs is None else pairs)
    extra = title_pair_from_env()
    if extra and extra not in out:
        out.append(extra)
    return out


def strip_hallucinations(text: str) -> str:
    """Drop a whisper sign-off sentence. Keep a real "Thank you." or "Bye."."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    kept = []
    for p in parts:
        p = p.strip()
        if not p or HALLUC.fullmatch(p.rstrip(".!? ")):
            continue
        kept.append(p)
    return " ".join(kept)


def hard_apply(text: str, pairs: list[tuple[str, str]] | None = None) -> str:
    """Rewrite sure names before the model. Ambiguous English stays put.

    The one title pair is not applied here. It is a hint: "kite eye" is not
    always "kaitai", and the span gate only keeps the change when the model
    makes it.
    """
    rows = list(load_pairs() if pairs is None else pairs)
    rows.sort(key=lambda hm: (-len(hm[0].split()), -len(hm[0])))
    for heard, meant in rows:
        if heard in AMBIGUOUS or not heard or not meant:
            continue
        parts = [re.escape(w) for w in heard.split()]
        pat = re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])", re.I)
        text = pat.sub(lambda _m, meant=meant: meant, text)
    return text


# Same idea as voxtype_learn.unsafe_heard: a function-word phrase matches
# almost every dictation, so it is not a hint.
HINT_STOP = frozenset(
    "a an the in on to is it of and or for as at be by do if we he she me my "
    "so no not but from with that this was are were you your its i am been "
    "being have has had does did can will just like than then them they their "
    "there here what when where who how into over also very really".split()
)


def hint_unsafe(heard: str) -> bool:
    toks = heard.split()
    if not toks or len("".join(toks)) < 3:
        return True
    return all(t in HINT_STOP for t in toks)


def hint_line(text: str, limit: int = 12) -> str:
    """The <hints> body for text the model is about to see.

    Call this after hard_apply. A sure name is already rewritten, so it does
    not spend a slot. What remains is a real English word the model has to
    judge, or a sure name the rewrite missed. Ambiguous pairs are listed
    first. A shorter phrase that only occurs inside a longer one already
    listed is left out ("sat cn" inside "sat cn or").
    """
    padded = " " + norm_phrase(text) + " "
    amb: list[tuple[str, str]] = []
    other: list[tuple[str, str]] = []
    kept: list[str] = []
    rows = sorted(load_pairs(), key=lambda hm: (-len(hm[0].split()), -len(hm[0])))
    for heard, meant in rows:
        if not heard or not meant or hint_unsafe(heard):
            continue
        if f" {heard} " not in padded:
            continue
        if any(f" {heard} " in f" {longer} " for longer in kept):
            continue
        kept.append(heard)
        (amb if heard in AMBIGUOUS else other).append((heard, meant))
    chosen = (amb + other)[:limit]
    return "; ".join(f'"{heard}" -> {meant}' for heard, meant in chosen)


def _filler_word(key: str) -> bool:
    if key in FILLER:
        return True
    return bool(re.fullmatch(r"u+h+|h+u+|u+m+|e+r+|a+h+|h+m+", key))


def _contains_marker(keys: list[str]) -> bool:
    for marker in MARKERS:
        n = len(marker)
        for i in range(len(keys) - n + 1):
            if keys[i:i + n] != list(marker):
                continue
            # "actually" by itself is a word. It counts only inside a longer cut.
            if marker == ("actually",) and keys == ["actually"]:
                return False
            return True
    return False


def delete_ok(keys: list[str], i1: int, i2: int) -> bool:
    span = keys[i1:i2]
    if not span:
        return True
    if all(_filler_word(k) for k in span):
        return True
    if tuple(span) in FILLER_PHRASES:
        return True
    if _contains_marker(span):
        return True
    # A stutter is this word next to the copy that stays. The aligner may
    # drop either the first or the second copy.
    if len(span) <= 2 and (
        (i2 < len(keys) and all(k == keys[i2] for k in span))
        or (i1 > 0 and all(k == keys[i1 - 1] for k in span))
    ):
        return True
    return False


def insert_ok(keys: list[str]) -> bool:
    return bool(keys) and all(k in ARTICLES for k in keys)


def _meaning_flip(a: str, b: str) -> bool:
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return any(long_ == p + short and len(p) < len(long_) for p in FLIP_PREFIX)


def _same_stem_rewrite(a: str, b: str) -> bool:
    """play+ing vs play+back. An inflection of the same stem is not this."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    if n < 4:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if long_.startswith(short) and long_[len(short):] in INFLECTION:
        return False
    # "you're" loses the apostrophe and becomes "youre". That extra e is the
    # contraction, not a second word the way "playback" is.
    if b == a + "e":
        return False
    return True


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(a=a, b=b).ratio()


def _sound_ok(raw_keys: list[str], model_keys: list[str]) -> bool:
    a, b = "".join(raw_keys), "".join(model_keys)
    if not a or not b or _meaning_flip(a, b):
        return False
    if len(raw_keys) == 1 and len(model_keys) == 1 and _same_stem_rewrite(a, b):
        return False
    r = _ratio(a, b)
    if r >= 0.8:
        return True
    # film/form and pool/pull sit at 0.5. Same onset keeps those and rejects
    # "ensure" vs "make sure" (0.71, different onset).
    return r >= 0.5 and a[0] == b[0]


def _number_ok(raw_keys: list[str], model_keys: list[str]) -> bool:
    if not raw_keys or not model_keys:
        return False
    if not any(k in NUMBER for k in raw_keys):
        return False
    if not all(k in NUMBER or k == "and" for k in raw_keys):
        return False
    return all(re.fullmatch(r"\d+", k) for k in model_keys)


def _list_ok(raw_keys: list[str], model_keys: list[str]) -> bool:
    if len(model_keys) != 1 or not re.fullmatch(r"\d+", model_keys[0]):
        return False
    return len(raw_keys) == 2 and raw_keys[0] in LIST_LEADS and raw_keys[1] in ORDINALS


def _undoes_pair(raw_keys: list[str], model_keys: list[str], pairs: list[tuple[str, str]]) -> bool:
    """True when the raw span is the meant form and the model went back to heard."""
    heard = " ".join(model_keys)
    raw_flat = "".join(raw_keys)
    for h, meant in pairs:
        if h != heard:
            continue
        meant_keys = [t.key for t in tokens(meant)]
        if meant_keys and "".join(meant_keys) == raw_flat:
            return True
    return False


def _dict_ok(raw_keys: list[str], model_keys: list[str], pairs: list[tuple[str, str]]) -> bool:
    heard = " ".join(raw_keys)
    model_flat = "".join(model_keys)
    model_phrase = " ".join(model_keys)
    for h, meant in pairs:
        if h != heard:
            continue
        meant_keys = [t.key for t in tokens(meant)]
        if "".join(meant_keys) == model_flat or " ".join(meant_keys) == model_phrase:
            return True
    return False


def _contraction_expansion(raw_keys: list[str], model_keys: list[str]) -> bool:
    """it's -> it is, we're -> we are. The prompt keeps the contraction."""
    return len(raw_keys) == 1 and len(model_keys) >= 2 and all(k in STOP for k in model_keys)


def screen_terms_from_env() -> frozenset[str]:
    raw = os.environ.get("PP_SCREEN_TERMS", "")
    terms = set()
    for part in re.split(r"[^A-Za-z0-9]+", raw):
        k = part.lower()
        if len(k) >= 3 and k not in STOP and k not in GENERIC:
            terms.add(k)
    for t in vocab_terms():
        k = re.sub(r"[^a-z0-9]", "", t.lower())
        if len(k) >= 3 and k not in STOP:
            terms.add(k)
    return frozenset(terms)


def extract_screen_terms(text: str, limit: int = 80) -> str:
    seen: list[str] = []
    seen_set: set[str] = set()
    for part in re.split(r"[^A-Za-z0-9_.-]+", text):
        for sub in re.split(r"[_.-]+", part):
            k = re.sub(r"[^a-z0-9]", "", sub.lower())
            if len(k) >= 3 and k not in STOP and k not in GENERIC and k not in seen_set and not k.isdigit():
                seen_set.add(k)
                seen.append(k)
        whole = re.sub(r"[^a-z0-9]", "", part.lower())
        if len(whole) >= 4 and whole not in STOP and whole not in GENERIC and whole not in seen_set and not whole.isdigit():
            seen_set.add(whole)
            seen.append(whole)
    return " ".join(seen[-limit:])


def _screen_or_vocab_ok(raw_keys: list[str], model_keys: list[str]) -> bool:
    a, b = "".join(raw_keys), "".join(model_keys)
    if not a or not b or len(b) < 3 or _meaning_flip(a, b):
        return False
    if b not in screen_terms_from_env():
        return False
    return _ratio(a, b) >= 0.55


def replace_ok(raw_keys: list[str], model_keys: list[str], pairs: list[tuple[str, str]]) -> bool:
    if not raw_keys or not model_keys:
        return False
    # "gonna" stays "gonna" even when the aligner bundles it with a neighbor
    # ("we're gonna" -> "we are going").
    if "gonna" in raw_keys and "gonna" not in model_keys:
        return False
    # The model is not allowed to put a sure name back to the mishearing.
    if _undoes_pair(raw_keys, model_keys, pairs):
        return False
    if "".join(raw_keys) == "".join(model_keys):
        return True
    if _contraction_expansion(raw_keys, model_keys):
        return False
    if _dict_ok(raw_keys, model_keys, pairs):
        return True
    if _number_ok(raw_keys, model_keys) or _list_ok(raw_keys, model_keys):
        return True
    if _screen_or_vocab_ok(raw_keys, model_keys):
        return True
    return _sound_ok(raw_keys, model_keys)


def _piece_ok(raw_keys: list[str], model_keys: list[str], pairs: list[tuple[str, str]],
              raw_all: list[str], abs_i: int) -> bool:
    if not model_keys:
        return delete_ok(raw_all, abs_i, abs_i + len(raw_keys))
    if not raw_keys:
        return insert_ok(model_keys)
    return replace_ok(raw_keys, model_keys, pairs)


def _cover(raw_keys: list[str], model_keys: list[str], pairs: list[tuple[str, str]],
           raw_all: list[str], i0: int, j0: int) -> list[tuple] | None:
    """Split one rejected replace into smaller repairs that each pass.

    "number two Grog" -> "2. Groq" is one opcode. The list marker and the
    name are each fine; together they match no rule, so the name was put
    back. A cover is used only when every piece is allowed. A paraphrase
    that cannot be split that way is still put back whole.
    """
    n, m = len(raw_keys), len(model_keys)
    if n > 6 or m > 6:
        return None
    dp: list[list[list | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = []
    for i in range(n + 1):
        for k in range(m + 1):
            if dp[i][k] is None:
                continue
            for di in range(0, n - i + 1):
                for dk in range(0, m - k + 1):
                    if di == 0 and dk == 0:
                        continue
                    if i == 0 and k == 0 and di == n and dk == m:
                        continue
                    if not _piece_ok(raw_keys[i:i + di], model_keys[k:k + dk], pairs, raw_all, i0 + i):
                        continue
                    if dp[i + di][k + dk] is None:
                        dp[i + di][k + dk] = dp[i][k] + [(i, di, k, dk)]
    steps = dp[n][m]
    if not steps:
        return None
    pieces = []
    for i, di, k, dk in steps:
        if dk:
            pieces.append(("m", j0 + k, j0 + k + dk))
    return pieces


def _glue(prev: str, nxt: str) -> str:
    if not prev:
        return nxt
    if not nxt:
        return prev
    if prev[-1].isspace() or nxt[0].isspace():
        return prev + nxt
    # The gap after a word is ":\n" or ". ", already attached to the next span.
    if nxt[0] in ".,!?;:)]}" or prev[-1] in "([{\"'":
        return prev + nxt
    return prev + " " + nxt


def _stitch(raw: str, rw: list[Tok], model: str, mw: list[Tok], pieces: list[tuple]) -> str:
    out = ""
    restored_lead = False
    trim_next_punct = False
    for idx, (kind, a, b) in enumerate(pieces):
        if kind == "m":
            if a >= b:
                continue
            left = 0 if a == 0 else mw[a - 1].end
            right = len(model) if b == len(mw) else mw[b - 1].end
            # A span that ends before the last token must not swallow the next
            # token. right is the end of this span's last word; the gap after
            # it belongs to the next span (its leading gap). When this span
            # runs to the end, include the closing punctuation.
            if b != len(mw):
                right = mw[b - 1].end
            chunk = model[left:right]
            if trim_next_punct:
                trim_next_punct = False
                chunk = re.sub(r"^[.!?…]+\s*", "", chunk)
                if re.match(r"[A-Z][a-z]", chunk) and chunk.split()[0] != "I" \
                        and re.search(r"(?<![A-Za-z])" + re.escape(chunk.split()[0].lower()) + r"(?![A-Za-z])", raw):
                    chunk = chunk[0].lower() + chunk[1:]
        else:
            if a >= b:
                continue
            # A restored span keeps its words, not its filler sounds: the
            # model cut "um so", the gate put "so" back, and "um" came too
            # (2026-11-17).
            keep = [t for t in rw[a:b] if not _filler_word(re.sub(r"[^a-z0-9']", "", t.surface.lower()))]
            if not keep:
                continue
            chunk = raw[keep[0].start:keep[-1].end] if len(keep) == b - a else " ".join(t.surface for t in keep)
            restored_lead = restored_lead or not out
            # If the next model chunk starts with sentence-ending punctuation
            # (e.g. model dropped "and" in "do this and also" -> "do this. Also"),
            # keep the period before the restored word instead of after it.
            if out and idx + 1 < len(pieces) and pieces[idx + 1][0] == "m":
                na, nb = pieces[idx + 1][1], pieces[idx + 1][2]
                if na > 0 and na < nb:
                    gap = model[mw[na - 1].end:mw[na].start]
                    pm = re.match(r"^([.!?…]+)\s+", gap)
                    if pm and not re.search(r"[.!?…]$", out.rstrip()):
                        out = out.rstrip() + pm.group(1)
                        if chunk[:1].islower():
                            chunk = chunk[0].upper() + chunk[1:]
                        trim_next_punct = True
        # The model's sentence start, now after restored words mid-sentence:
        # its capital was only the sentence's ("um so We need").
        if kind == "m" and a == 0 and out and not re.search(r"[.!?…:]\s*$", out) \
                and re.match(r"[A-Z][a-z]", chunk) and chunk.split()[0] != "I" \
                and re.search(r"(?<![A-Za-z])" + re.escape(chunk.split()[0].lower()) + r"(?![A-Za-z])", raw):
            chunk = chunk[0].lower() + chunk[1:]
        out = _glue(out, chunk)
    if pieces and pieces[-1][0] == "r" and mw:
        tail = model[mw[-1].end:]
        punct = tail.strip()
        if punct and re.fullmatch(r"[.!?…]+", punct) and not re.search(r"[.!?…]$", out.rstrip()):
            out = out.rstrip() + punct
    if model[:1] and not model[:1].isspace():
        out = out.lstrip()
    # Restored words at the very start begin the sentence.
    if restored_lead and out[:1].islower():
        out = out[0].upper() + out[1:]
    return out


def constrain(raw: str, model: str, pairs: list[tuple[str, str]] | None = None) -> str:
    """Model text, with any span that is not a real repair put back."""
    if not (model or "").strip():
        return raw
    if not (raw or "").strip():
        return model
    rw, mw = tokens(raw), tokens(model)
    if not rw or not mw:
        return model
    pair_rows = pairs_for_call(pairs)
    raw_keys = [t.key for t in rw]
    pieces: list[tuple] = []
    sm = difflib.SequenceMatcher(a=raw_keys, b=[t.key for t in mw], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            pieces.append(("m", j1, j2))
        elif op == "delete":
            if not delete_ok(raw_keys, i1, i2):
                pieces.append(("r", i1, i2))
        elif op == "insert":
            keys = [t.key for t in mw[j1:j2]]
            if insert_ok(keys):
                pieces.append(("m", j1, j2))
        else:
            rk, mk = raw_keys[i1:i2], [t.key for t in mw[j1:j2]]
            if replace_ok(rk, mk, pair_rows):
                pieces.append(("m", j1, j2))
            else:
                covered = _cover(rk, mk, pair_rows, raw_keys, i1, j1)
                if covered is None:
                    pieces.append(("r", i1, i2))
                else:
                    pieces.extend(covered)
    if not pieces:
        return raw
    return _stitch(raw, rw, model, mw, pieces)


def title_hint(raw: str, title: str) -> tuple[str, str] | None:
    """One heard-phrase -> title-token pair, or None.

    The path stays on this machine. A token that already appears in the
    dictation is not a hint, and neither is src/readme/rs.
    """
    if not title:
        return None
    keys = [t.key for t in tokens(raw)]
    best = None
    seen = []
    for part in re.split(r"[^A-Za-z0-9]+", title):
        tok = part.lower()
        if len(tok) < 4 or tok in GENERIC or tok in keys or tok in seen:
            continue
        seen.append(tok)
        for n in (1, 2, 3):
            for i in range(len(keys) - n + 1):
                span_keys = keys[i:i + n]
                # "in the readme" starts with the same letters as "inference"
                # and used to be offered as a hint. Function words do not count
                # toward the sound match. The words that do are the heard phrase.
                content = [k for k in span_keys if k not in STOP]
                if not content:
                    continue
                span = "".join(content)
                ratio = _ratio(span, tok)
                if ratio < 0.45 or (span[0] != tok[0] and ratio < 0.6):
                    continue
                cand = (abs(len(span) - len(tok)), -ratio, -len(content), " ".join(content), tok)
                if best is None or cand < best:
                    best = cand
    if best is None:
        return None
    return best[3], best[4]


def app_fields(raw: str, cls: str, title: str) -> tuple[str, str]:
    """(PP_TITLE_PAIR value, text that goes inside <app>)."""
    hint = title_hint(raw, title)
    pair = f"{hint[0]}\t{hint[1]}" if hint else ""
    lines = []
    if cls.strip():
        lines.append(cls.strip())
    if hint:
        lines.append(f'"{hint[0]}" -> {hint[1]}')
    return pair, "\n".join(lines)


def _read_stdin() -> str:
    return sys.stdin.read()


# ── After the model (2026-11-17) ─────────────────────────────────────────
# The prompt asks for these spellings; this makes them so. A vocabulary term
# heard in another case or with its parts split ("github", "git hub",
# "kitty mux") is written the way vocabulary.txt spells it. Only exact
# letter matches (case, spaces, hyphens aside): a sound-alike stays with the
# hints and the model, where the context decides. Also runs on the raw text
# when no model answered.

VOCAB_FILE = str(CONF / "vocabulary.txt")


def vocab_terms() -> list[str]:
    try:
        lines = open(VOCAB_FILE, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        t = line.split("#", 1)[0].strip()
        if t and len(re.sub(r"[^0-9A-Za-z]", "", t)) >= 3:
            out.append(t)
    return out


_COMMON: set[str] | None = None


def _common_words() -> set[str]:
    global _COMMON
    if _COMMON is None:
        _COMMON = set()
        for path in (
            "/usr/share/dict/cracklib-small",
            "/usr/share/dict/words",
            "/usr/share/dict/american-english",
            "/usr/share/dict/british-english",
        ):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    words_set = {w.strip().lower() for w in f if w.strip()}
                if words_set:
                    _COMMON = words_set
                    break
            except OSError:
                continue
    return _COMMON


def finish(text: str, terms: list[str] | None = None) -> str:
    terms = vocab_terms() if terms is None else terms
    for term in sorted(terms, key=len, reverse=True):
        letters = re.sub(r"[^0-9A-Za-z]", "", term)
        if not letters:
            continue
        # The term's letters, with an optional space or hyphen between any two.
        body = r"[\s-]?".join(re.escape(c) for c in letters)
        pat = re.compile(r"(?<![0-9A-Za-z])" + body + r"(?![0-9A-Za-z])", re.I)

        def fix(m):
            got = m.group(0)
            if got == term:
                return got
            if got.replace(" ", "").replace("-", "") == got:      # same letters, case differs
                # An ordinary word that is also a name ("pulse" / Pulse)
                # keeps the speaker's case; and nothing loses its capital
                # (a sentence starting "Voxtype").
                if got.lower() in _common_words() or (got[:1].isupper() and term[:1].islower()):
                    return got
            return term
        text = pat.sub(fix, text)
    return text


# When every model failed, the raw transcript is typed: take out the sounds
# that are never words (T3 Code's list), tidy the spaces, capitalise.
_FILLER = re.compile(r"(?:,\s*)?(?<![\w'])(?:u+h+m*|u+m+|e+r+m+|e+h+m*|a+h+m+|h+m+|m{2,})(?![\w'])[,.]?", re.I)


def fallback_clean(text: str) -> str:
    t = _FILLER.sub("", text)
    t = re.sub(r"[^\S\n]{2,}", " ", t)
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r"^[\s,.]+", "", t)
    t = t.strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return finish(t)


# The terminal the text goes into: its last lines (the prompt being written),
# as context for names. It leaves the machine (the cleanup model), so secrets
# are taken out first, box drawing is dropped, and it is kept short.
_SECRET = [
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "[redacted]"),
    (re.compile(r"(?i)\b(password|passwd|passphrase|secret|token|api[_-]?key|authorization)(\s*[:=]\s*|\s+)\S+"), r"\1\2[redacted]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[redacted]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "[redacted]"),
]


def screen_block(text: str, lines: int = 12, limit: int = 1200) -> str:
    keep = []
    for line in text.splitlines():
        line = re.sub(r"[\u2500-\u259f\u25a0-\u25ff\u2800-\u28ff\ue000-\uf8ff]", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            keep.append(line)
    out = "\n".join(keep[-lines:])
    for pat, rep in _SECRET:
        out = pat.sub(rep, out)
    return out[-limit:]


# A take that must stay on this machine: no cleanup model, no clipboard, no
# history. Password managers and system prompts by window class; a browser
# page by its title (sign-in, one-time codes); a terminal by the line its
# cursor sits on (sudo, ssh, gpg asking for a password). Editors and
# terminals are not judged by title: a file named login.ts is not a prompt.
# (AT-SPI would see a password field directly, but Zen and Electron apps only
# expose their trees with accessibility forced on, which costs every app.)
_PRIVATE_CLASS = re.compile(r"(?i)keepassxc|bitwarden|1password|enpass|proton[ ._-]?pass|seahorse|gcr-prompter"
                            r"|pinentry|polkit|authentication-agent|kwalletd|ksecretd|hyprlock|gnome-keyring")
_PRIVATE_TITLE = re.compile(r"(?i)\b(password|passphrase|passcode|sign[ -]?in|log[ -]?in|authenticat\w*|unlock"
                            r"|2fa|two[- ]factor|one[- ]time (?:code|password)|verification code|security code|otp)\b")
_PRIVATE_PROMPT = re.compile(r"(?i)(\[sudo\].*|.*\b(?:password|passphrase|passcode|pin)\b.{0,60}|"
                             r".*\benter (?:the )?(?:code|token)\b.{0,30}):\s*$")
_TEXT_TOOLS = re.compile(r"(?i)^(kitty|ghostty|alacritty|foot|wezterm|konsole|xterm|.*terminal.*|neovide|cursor"
                         r"|code|code-oss|zed|dev\.zed.*|jetbrains-.*|obsidian|com\.t3tools\.t3code|devin-desktop.*)$")


def private_reason(cls: str, title: str, screen: str = "") -> str:
    if _PRIVATE_CLASS.search(cls or ""):
        return f"app {cls}"
    if title and not _TEXT_TOOLS.match(cls or "") and _PRIVATE_TITLE.search(title):
        return "sign-in page"
    lines = [l.strip() for l in (screen or "").splitlines() if l.strip()]
    if lines and _PRIVATE_PROMPT.match(lines[-1][-200:]):
        return "password prompt"
    return ""


# Snippets: say the trigger on its own and its text is typed instead, with no
# cleanup model in between. ~/.config/voxtype/snippets, one "trigger: text"
# a line; "\n" in the text is a new line. "insert", "type", "paste" or
# "snippet" in front of the trigger, and "please" after it, are allowed.
SNIPPETS = Path(os.environ.get("VOXTYPE_SNIPPETS", CONF / "snippets"))


def _snip_key(s: str) -> str:
    k = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    k = re.sub(r"\s+", " ", k).strip()
    k = re.sub(r"^(?:(?:insert|type|paste|snippet|my snippet)\s+)", "", k)
    return re.sub(r"\s+please$", "", k)


def load_snippets(path: Path | None = None) -> dict[str, str]:
    out = {}
    try:
        lines = (path or SNIPPETS).read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        trig, _, text = line.partition(":")
        key = _snip_key(trig)
        if key and text.strip():
            out[key] = text.strip().replace("\\n", "\n")
    return out


def snippet(text: str, table: dict[str, str] | None = None) -> str | None:
    table = load_snippets() if table is None else table
    words = _snip_key(text)
    return table.get(words) if words and len(words.split()) <= 8 else None


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "strip":
        sys.stdout.write(strip_hallucinations(_read_stdin()))
    elif cmd == "hard":
        sys.stdout.write(hard_apply(_read_stdin()))
    elif cmd == "hints":
        sys.stdout.write(hint_line(_read_stdin()))
    elif cmd == "constrain" and len(argv) == 3:
        sys.stdout.write(constrain(argv[2], _read_stdin()))
    elif cmd == "app" and len(argv) == 4:
        pair, block = app_fields(_read_stdin(), argv[2], argv[3])
        sys.stdout.write(pair + "\x1e" + block)
    elif cmd == "finish":
        sys.stdout.write(finish(_read_stdin()))
    elif cmd == "fallback":
        sys.stdout.write(fallback_clean(_read_stdin()))
    elif cmd == "screen":
        sys.stdout.write(screen_block(_read_stdin()))
    elif cmd == "terms":
        sys.stdout.write(extract_screen_terms(_read_stdin()))
    elif cmd == "private" and len(argv) == 4:
        why = private_reason(argv[2], argv[3], _read_stdin())
        sys.stdout.write(why)
        return 0 if why else 1
    elif cmd == "snippet":
        hit = snippet(_read_stdin())
        if hit is None:
            return 1
        sys.stdout.write(hit)
    elif cmd == "test":
        return _selftest()
    else:
        print(__doc__.strip().splitlines()[-4], file=sys.stderr)
        return 2
    return 0


def _expect(name: str, got: str, pred) -> str | None:
    if pred(got):
        return None
    return f"{name}: {got!r}"


def _selftest() -> int:
    fails = []

    def check(name, got, pred):
        err = _expect(name, got, pred)
        if err:
            fails.append(err)

    T = ["GitHub", "Hyprland", "kittymux", "Kitsunekode"]
    check("vocab case", finish("push to github and reload hyprland", T), lambda s: s == "push to GitHub and reload Hyprland")
    check("vocab split", finish("open kitty mux and git hub", T), lambda s: s == "open kittymux and GitHub")
    check("vocab no partial", finish("the kit is mine", T), lambda s: s == "the kit is mine")
    check("vocab inside word kept", finish("githubbing", T), lambda s: s == "githubbing")
    check("fallback fillers", fallback_clean("um so uh we need to, hmm, fix the bounce"), lambda s: s == "So we need to fix the bounce")
    check("fallback keeps words", fallback_clean("umbrella and hummus"), lambda s: s == "Umbrella and hummus")
    check("screen redacts", screen_block("export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuv\n│ > fix the hero section │"),
          lambda s: "sk-abc" not in s and "fix the hero section" in s and "│" not in s)
    check("screen redacts tokens", screen_block("token: ghp_abcdefghijklmnopqrstuvwxyz123456"), lambda s: "ghp_" not in s)
    check("screen last lines", screen_block("\n".join(str(i) for i in range(30)), lines=3), lambda s: s == "27\n28\n29")
    RAWQ = "um so we need to uh fix the capsule"
    check("restored span drops filler", constrain(RAWQ, "We need to fix the capsule."), lambda s: s.startswith("So we need") and "um" not in s.split())
    check("model start kept", constrain("so we need to fix it", "So we need to fix it."), lambda s: s == "So we need to fix it.")
    check("common word keeps case", finish("check the pulse audio", ["Pulse", "Hyprland"]), lambda s: s == "check the pulse audio")
    check("never lowercases a capital", finish("Voxtype is fast", ["voxtype"]), lambda s: s == "Voxtype is fast")
    check("sign-off kept", strip_hallucinations("Thank you."), lambda s: "thank you" in s.lower())
    check("bye kept", strip_hallucinations("Bye."), lambda s: s.lower().startswith("bye"))
    check("watching dropped", strip_hallucinations("Thanks for watching."), lambda s: s == "")
    check("welcome dropped", strip_hallucinations("You're welcome."), lambda s: s == "")
    check("real plus sign-off", strip_hallucinations("Ship it. Thank you."),
          lambda s: "ship it" in s.lower() and "thank you" in s.lower())
    check("watching sentence dropped", strip_hallucinations("Thanks for watching. Ship the fix."),
          lambda s: s.lower() == "ship the fix.")

    check("hyperlane", hard_apply("set up the hyperlane config"),
          lambda s: "Hyprland" in s and "hyperlane" not in s.lower())
    check("omarchi and hyperlane", hard_apply("set up the omarchi theme with the hyperlane config"),
          lambda s: "Omarchy" in s and "Hyprland" in s and "omarchi" not in s.lower())
    check("groc", hard_apply("can we use groc instead"),
          lambda s: "Groq" in s and "groc" not in s.lower())
    check("model cannot undo groq",
          constrain("can we use Groq instead", "Can we use groc instead."),
          lambda s: "Groq" in s and "groc" not in s.lower())
    check("swimming stays", hard_apply("I went swimming yesterday"),
          lambda s: "swimming" in s.lower() and "streaming" not in s.lower())
    check("pipeline swimming stays for the model", hard_apply("the swimming pipeline drops frames"),
          lambda s: "swimming" in s.lower())
    check("next year's stays", hard_apply("next year's plan is the redesign"),
          lambda s: "next year's" in s.lower())

    check("synonym restore",
          constrain("we need to fix things in the worktrees",
                    "We need to fix tasks in the branches."),
          lambda s: "things" in s and "worktrees" in s and "tasks" not in s and "branches" not in s)
    check("erase raise",
          constrain("Erase a PR right now", "Raise a PR right now."),
          lambda s: "raise a pr" in s.lower() and "erase" not in s.lower())
    check("love wider span",
          constrain("that part also is love wider client",
                    "That part also is loved by the client."),
          lambda s: "loved by" in s.lower() and "wider" not in s.lower())
    check("film form",
          constrain("the agency that I try to film", "The agency that I'm trying to form."),
          lambda s: "form" in s.lower() and "trying" in s.lower() and "film" not in s.lower())
    check("contraction kept",
          constrain("I think it's fine but we're gonna need to check",
                    "I think it is fine but we are going to need to check."),
          lambda s: "it's" in s.lower() and "we're" in s.lower() and "gonna" in s.lower()
          and "it is" not in s.lower() and "we are" not in s.lower())
    check("numbers",
          constrain("set the timeout to three hundred milliseconds and version nineteen point two",
                    "Set the timeout to 300 milliseconds and version 19.2."),
          lambda s: "300" in s and "19.2" in s and "three hundred" not in s)
    check("list",
          constrain("things to do number one call the bank number two book tickets",
                    "Things to do:\n1. Call the bank.\n2. Book tickets."),
          lambda s: "1." in s and "2." in s and "number one" not in s.lower())
    check("self correction",
          constrain("Let's book a meeting at 4. No, actually let's do it at 3.30",
                    "Let's do it at 3:30."),
          lambda s: "3:30" in s and "meeting" not in s.lower())
    check("stutter",
          constrain("what I need need from you", "What I need from you."),
          lambda s: s.lower().count("need") == 1)
    check("fillers",
          constrain("um can you uh tell me", "Can you tell me?"),
          lambda s: "um" not in s.lower() and "uh" not in s.lower() and "tell me" in s.lower())
    check("homophones",
          constrain("their going to push there changes and its going to break",
                    "They're going to push their changes and it's going to break."),
          lambda s: "they're" in s.lower() and "their changes" in s.lower() and "it's" in s.lower())
    check("joins",
          constrain("we use type script and next js and the end point",
                    "We use TypeScript and Next.js and the endpoint."),
          lambda s: "typescript" in s.lower() and "next.js" in s.lower() and "endpoint" in s.lower())
    check("bolt bold",
          constrain("the heading font to be more bolt and the light house score",
                    "The heading font to be more bold and the Lighthouse score."),
          lambda s: "bold" in s.lower() and "lighthouse" in s.lower() and "bolt" not in s.lower())
    check("swimming allowed when the model does it",
          constrain("the swimming pipeline drops frames", "The streaming pipeline drops frames."),
          lambda s: "streaming" in s.lower())
    check("swimming kept when the model keeps it",
          constrain("I went swimming yesterday and the pool was cold",
                    "I went swimming yesterday and the pool was cold."),
          lambda s: "swimming" in s.lower() and "streaming" not in s.lower())
    check("title pair allows kaitai",
          constrain("the kite eye scheduler keeps dropping requests",
                    "The Kaitai scheduler keeps dropping requests.",
                    pairs=[("kite eye", "kaitai")]),
          lambda s: "kaitai" in s.lower() and "kite" not in s.lower())
    check("invisible not flipped",
          constrain("the button is invisible", "The button is visible."),
          lambda s: "invisible" in s.lower())
    check("playback not invented",
          constrain("check that things are being played", "Check that playback is working."),
          lambda s: "played" in s.lower() and "playback" not in s.lower())

    hint = title_hint(
        "the kite eye inference scheduler keeps dropping requests under load",
        "kaitai-inference/src/scheduler.rs",
    )
    check("title hint is kite eye", str(hint), lambda s: s == "('kite eye', 'kaitai')")
    none = title_hint("Fix the typo in the readme heading", "Cursor — kaitai-inference/README.md")
    # readme is generic; kaitai is in the title but nothing in the raw sounds like it
    # "readme" is generic so it is skipped. "kaitai" might still match a window.
    check("readme title does not invent a pair",
          str(title_hint("Fix the typo in the readme heading", "kaitai-inference/README.md")),
          lambda s: s == "None")
    check("exact readme is not a hint",
          str(title_hint("Fix the typo in the readme heading", "README.md")),
          lambda s: s == "None")
    pair, block = app_fields(
        "the kite eye inference scheduler keeps dropping requests",
        "Cursor",
        "kaitai-inference/src/scheduler.rs",
    )
    check("app block has the pair", block, lambda s: '"kite eye" -> kaitai' in s and "Cursor" in s)
    check("app block hides the path", block, lambda s: "scheduler.rs" not in s and "src" not in s)
    check("pair value", pair, lambda s: s == "kite eye\tkaitai")
    # silence the unused name when the kaitai-in-title case is asserted above
    _ = none

    check("clawed is a hint, not a force",
          hard_apply("ask clawed to review and the cat clawed the curtain"),
          lambda s: s.lower().count("clawed") == 2 and "claude" not in s.lower())
    check("clawed may become claude when the model does it",
          constrain("ask clawed to review the diff", "Ask Claude to review the diff.",
                    pairs=[("clawed", "claude")]),
          lambda s: "claude" in s.lower() and "clawed" not in s.lower())
    check("cat clawed stays when the model keeps it",
          constrain("the cat clawed the curtain", "The cat clawed the curtain.",
                    pairs=[("clawed", "claude")]),
          lambda s: "clawed" in s.lower() and "claude" not in s.lower())
    check("thank you restored when the model drops it",
          constrain("Thank you. Ship the fix.", "Ship the fix."),
          lambda s: "thank you" in s.lower() and "ship" in s.lower())
    swim = hint_line("the swimming pipeline and can we use groc")
    check("ambiguous hint is listed before a sure name still in the text", swim,
          lambda s: "swimming" in s and "groc" in s and s.find("swimming") < s.find("groc"))
    check("sure name is not hinted again after it is applied",
          hint_line(hard_apply("can we use groc instead")),
          lambda s: "groc" not in s.lower() and "groq" not in s.lower())
    check("swimming hint survives the rewrite",
          hint_line(hard_apply("the swimming pipeline drops frames")),
          lambda s: '"swimming" -> streaming' in s)
    both = hint_line("use sat cn or for the buttons")
    check("shorter hint is dropped when the longer phrase is the one said", both,
          lambda s: '"sat cn or" ->' in s and '"sat cn" ->' not in s)
    check("accepted names are applied",
          hard_apply("hyperlan kitty max and the saddle bar toogle was spunning"),
          lambda s: "Hyprland" in s and "kittymux" in s and "Waybar" in s
          and "toggle" in s and "spinning" in s
          and "hyperlan" not in s.lower() and "toogle" not in s.lower())
    check("your becomes you're",
          constrain("make sure your on the latest branch before you rebase",
                    "Make sure you're on the latest branch before you rebase."),
          lambda s: "you're" in s.lower() and "your on" not in s.lower())
    check("list marker and a name are both kept",
          constrain("Number one, group port API number two Grog users number three cursor",
                    "1. Group port API\n2. Groq users\n3. Cursor"),
          lambda s: "groq" in s.lower() and "grog" not in s.lower() and "\n2." in s and "\n3." in s)
    check("a bad word does not ride along with a list marker",
          constrain("number two things to fix", "2. tasks to fix."),
          lambda s: "things" in s.lower() and "tasks" not in s.lower())
    check("playback still blocked beside an allowed name",
          constrain("check grog playback", "Check Groq playback is working."),
          lambda s: "groq" in s.lower() and "playback is working" not in s.lower())

    for cls, title, screen, want in [
        ("org.keepassxc.KeePassXC", "Passwords.kdbx", "", True),
        ("zen", "Sign in - Google Accounts — Zen Browser", "", True),
        ("zen", "Enter verification code | GitHub", "", True),
        ("zen", "Hyprland wiki — Zen Browser", "", False),
        ("cursor", "login.ts - rice - Cursor", "", False),
        ("kitty", "zsh", "$ sudo pacman -Syu\n[sudo] password for me: ", True),
        ("kitty", "ssh", "me@host's password:", True),
        ("kitty", "gpg", "Enter passphrase for key:", True),
        ("kitty", "nvim", "the password field is broken: fix it\n-- INSERT --", False),
        ("kitty", "claude", "> tell me about the login page\n  ? for shortcuts", False),
    ]:
        got = private_reason(cls, title, screen)
        check(f"private {cls} {title[:20]!r}", got, lambda g, w=want: bool(g) == w)
    table = {"my email": "me@example.com", "sign off": "Thanks,\nM"}
    check("snippet said alone", snippet("My email.", table) or "", lambda s: s == "me@example.com")
    check("snippet with insert and please", snippet("Insert sign off, please.", table) or "",
          lambda s: s == "Thanks,\nM")
    check("snippet inside a sentence is not a trigger", snippet("send it to my email today", table) or "",
          lambda s: s == "")
    check("t3code login thread not private", private_reason("com.t3tools.T3Code", "Fix login password bug", ""),
          lambda s: s == "")
    check("restored word keeps period before it",
          constrain("to do this and also think about it", "to do this. Also think about it."),
          lambda s: s == "to do this. And also think about it.")
    old_st = os.environ.get("PP_SCREEN_TERMS")
    try:
        os.environ["PP_SCREEN_TERMS"] = "kuwai Autotabi"
        check("screen term allows kuwai",
              constrain("fix the huai config", "Fix the kuwai config."),
              lambda s: "kuwai" in s and "huai" not in s)
    finally:
        if old_st is None:
            os.environ.pop("PP_SCREEN_TERMS", None)
        else:
            os.environ["PP_SCREEN_TERMS"] = old_st

    if fails:
        print("\n".join(fails))
        return 1
    print(f"gate selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
