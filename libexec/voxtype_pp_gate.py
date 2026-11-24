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
# Spoken glue between sentences. The model may drop one where it starts a new
# sentence ("…this and also think" -> "…this. Also, think"); restoring it gave
# "this. And Also, think".
DISCOURSE = frozenset("and so like okay ok yeah alright basically".split())
# Only these may go at the very end of a take: "it works well" keeps "well".
TAGS = frozenset("okay ok yeah alright".split())
# A leading "so" is glue only before a new clause ("so we need"), never an
# intensifier ("so much better").
CLAUSE_STARTS = frozenset(
    "i we you they he she it the this that there what how now then if basically "
    "let lets can maybe anyway yeah okay my our".split()
)
# Doubled on purpose ("I had had enough", "that that", "hello hello", "no no"):
# never collapsed as a stutter.
DOUBLES_KEEP = frozenset(
    "had that no yes yeah hello hi hey bye very really so okay ok please wait stop "
    "go now come knock ha haha why far many much more".split()
)
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
# Polarity words: a span or whole output must not flip negation count.
# Keys are lowercased with apostrophes stripped (matching Tok.key).
NEGATIONS = frozenset(
    "no not never none nothing nowhere neither nor nobody without cannot "
    "cant dont doesnt didnt isnt arent wasnt werent wont wouldnt couldnt "
    "shouldnt havent hasnt hadnt mustnt neednt aint".split()
)
FIRST_PERSON = frozenset("i me my mine myself im ive ill id lemme gimme".split())
_RESERVED_TAG_RE = re.compile(
    r"<(/?(?:dictation|previous|app|hints|screen|draft|selection|clipboard|now|instruction)\b)",
    re.I,
)
_ONES_VAL = {
    "zero": 0, "oh": 0, "o": 0,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS_VAL = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALE_VAL = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_ORDINAL_VAL = {
    "one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3,
    "four": 4, "fourth": 4, "five": 5, "fifth": 5, "six": 6, "sixth": 6,
    "seven": 7, "seventh": 7, "eight": 8, "eighth": 8, "nine": 9, "ninth": 9,
    "ten": 10, "tenth": 10,
}
LIST_LEADS = frozenset({"number", "point", "bullet", "item"})
ORDINALS = frozenset(_ORDINAL_VAL)
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

TOKEN = re.compile(r"(?<![\w-])-?\d+(?:[.:]\d+)+|(?<![\w-])-\d+|[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*")
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
        # A number keeps its sign and the separators between its digits:
        # "1.25" is not "125", "-5" is not "5", "3:30" is not "330".
        k = re.sub(r"(?<=\d)[.:-](?=\d)", lambda m: "\x00" + m.group(0), surface.lower())
        k = re.sub(r"^-(?=\d)", "\x00-", k)
        self.key = re.sub(r"\x00(.)|[^a-z0-9]", lambda m: m.group(1) or "", k)


def tokens(text: str) -> list[Tok]:
    return [Tok(m.group(0), m.start(), m.end()) for m in TOKEN.finditer(text)]


def norm_phrase(s: str) -> str:
    return " ".join(re.sub(r"[^\w'+#]", " ", s.lower()).split())


def load_pairs(rewrite: bool = False) -> list[tuple[str, str]]:
    """(heard, meant) from corrections.tsv. `rewrite`: only pairs that may be
    rewritten blindly; a "hint" pair (an English heard word) is left to the
    model."""
    pairs = []
    if not TSV.exists():
        return pairs
    for line in TSV.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 2 and f[0].strip():
            if rewrite and len(f) >= 3 and f[2].strip() == "hint":
                continue
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
    rows = list(load_pairs(rewrite=True) if pairs is None else pairs)
    rows.sort(key=lambda hm: (-len(hm[0].split()), -len(hm[0])))
    low = text.lower()
    for heard, meant in rows:
        if heard in AMBIGUOUS or not heard or not meant:
            continue
        # Compiling a pattern costs ~0.4 ms; most pairs are not in the text.
        if not all(w.lower() in low for w in heard.split()):
            continue
        parts = [re.escape(w) for w in heard.split()]
        pat = re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])", re.I)
        text = pat.sub(lambda _m, meant=meant: meant, text)
        low = text.lower()
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


def _contains_marker(keys: list[str], strict: bool = False) -> bool:
    for marker in MARKERS:
        if strict and marker == ("actually",):
            continue
        n = len(marker)
        for i in range(len(keys) - n + 1):
            if keys[i:i + n] != list(marker):
                continue
            # "actually" by itself is a word. It counts only inside a longer cut.
            if marker == ("actually",) and keys == ["actually"]:
                return False
            return True
    return False


def _stutter_at(keys: list[str], p: int) -> bool:
    """keys[p] is part of an accidental run ("fast fast fast fetch"), not a
    word people repeat on purpose (DOUBLES_KEEP: "had had", "no no no")."""
    k = keys[p]
    if k in DOUBLES_KEEP:
        return False
    lo = p
    while lo > 0 and keys[lo - 1] == k:
        lo -= 1
    hi = p
    while hi + 1 < len(keys) and keys[hi + 1] == k:
        hi += 1
    return hi > lo


def delete_ok(keys: list[str], i1: int, i2: int, surfaces: list[str] | None = None) -> bool:
    span = keys[i1:i2]
    if not span:
        return True
    if all(_filler_word(k) for k in span):
        return True
    if tuple(span) in FILLER_PHRASES:
        return True
    # Every cut word is a stutter next to its own copy: "shown [shown also] also".
    if len(span) <= 4 and all(_stutter_at(keys, p) for p in range(i1, i2)):
        return True
    # A take-back marker may cut a negation only when the speaker restarts
    # right after it ("I don't want, scratch that, I want ..."): "we should
    # not scratch that or move it" is an instruction, not a correction.
    negated = any(k in NEGATIONS and k != "no" for k in span)
    if _contains_marker(span, strict=negated) and (
            not negated or (i2 < len(keys) and keys[i2] == span[0])):
        return True
    # A false start: the speaker restarts with the same word ("we have we
    # need to fix", "build an build a", "before that before starting that").
    # Never across a negation: "I don't I do" is a reversal, not a restart.
    # The aligner may keep the first copy instead: "we [have we] need". A
    # restart may open with spoken glue: "okay before that before starting".
    core = span
    while len(core) > 1 and core[0] in DISCOURSE:
        core = core[1:]
    # At most two words: "fix the header fix the footer" and "send to Alice
    # then send to Bob" are two items, not a restart.
    # Besides the restarted word, only function words go: "we [have] we need",
    # "build [an] build a"; "buy milk buy eggs" is two items.
    light = STOP | {"that", "this"}
    if len(core) == 2 and not any(k in NEGATIONS for k in span) and (
            (i2 < len(keys) and keys[i2] == core[0] and all(k in light for k in core[1:]))
            or (i1 > 0 and keys[i1 - 1] == core[-1] and len(core) >= 2
                and all(k in light for k in core[:-1]))):
        return True
    # A stutter is this word next to the copy that stays. The aligner may
    # drop either the first or the second copy.
    if len(span) == 1 and _stutter_at(keys, i1):
        return True
    return False


def _discourse_cut(span: list[str], model: str, mw: list, j: int,
                   raw_keys: list[str] | None = None, i2: int = 0) -> bool:
    """One or two words of spoken glue, dropped where the model's sentence
    ends or begins: "and" before "Also,", "okay" after "fix it."."""
    if not span or len(span) > 2 or not all(k in DISCOURSE for k in span):
        return False
    raw_keys = raw_keys or []
    if span[-1] == "so" and not (i2 < len(raw_keys) and raw_keys[i2] in CLAUSE_STARTS):
        return False
    # "that's okay", "oh yeah": in a short take the tag is the message.
    if j == len(mw) and len(raw_keys) <= 3:
        return False
    gap = model[mw[j - 1].end:mw[j].start if j < len(mw) else len(model)] if j > 0 else ""
    ends_before = j == 0 or re.search(r"[.!?…:]", gap) is not None
    starts_after = j < len(mw) and mw[j].surface[:1].isupper()
    return ends_before and (starts_after or (j == len(mw) and all(k in TAGS for k in span)))


_CONTRACTION = re.compile(r"['’](d|ll|re|ve|m|s|t)$", re.I)


def _lost_apostrophe(raw: "Tok", model: "Tok") -> bool:
    return bool(_CONTRACTION.search(raw.surface)) and not re.search(r"['’]", model.surface)


def _drops_contraction(raw: list, model: list) -> bool:
    """A contraction the speaker said is gone from the model's words:
    "I'd like" -> "I like", "I'll check" -> "I check"."""
    said = {t.surface.lower().replace("’", "'") for t in raw if _CONTRACTION.search(t.surface)}
    kept = {t.surface.lower().replace("’", "'") for t in model}
    return bool(said - kept)


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


def _negation_count(keys: list[str]) -> int:
    return sum(1 for k in keys if k in NEGATIONS)


def _digits_in(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _parse_cardinal(words_list: list[str]) -> int | None:
    """Parse an English cardinal phrase ('three hundred twenty five' -> 325)."""
    toks = [w for w in words_list if w != "and"]
    if not toks:
        return None
    total = 0
    current = 0
    saw_any = False
    # "zero"/"oh" is a digit, not a quantity: "four zero" is 40 (digit by
    # digit, below), never 4.
    if len(toks) > 1 and any(w in ("zero", "oh", "o") for w in toks):
        return None
    for w in toks:
        if w in _ONES_VAL:
            if current % 10 != 0 and current < 100 and w != "zero":
                return None
            if current >= 10 and current < 20:
                return None
            current += _ONES_VAL[w]
            saw_any = True
        elif w in _TENS_VAL:
            if current % 100 != 0:
                return None
            current += _TENS_VAL[w]
            saw_any = True
        elif w == "hundred":
            if current == 0:
                current = 100
            elif current < 100:
                current *= 100
            else:
                return None
            saw_any = True
        elif w in _SCALE_VAL:
            scale = _SCALE_VAL[w]
            if current == 0:
                current = 1
            total += current * scale
            current = 0
            saw_any = True
        else:
            return None
    return total + current if saw_any else None


def _parse_grouped_digits(words_list: list[str]) -> str | None:
    """Parse spoken digit/pair runs ('three four three' -> '343', 'nineteen eighty four' -> '1984')."""
    toks = [w for w in words_list if w != "and"]
    if not toks:
        return None
    parts: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i]
        if w in _TENS_VAL and i + 1 < len(toks) and toks[i + 1] in _ONES_VAL and 1 <= _ONES_VAL[toks[i + 1]] <= 9:
            parts.append(str(_TENS_VAL[w] + _ONES_VAL[toks[i + 1]]))
            i += 2
        elif w in _TENS_VAL:
            parts.append(str(_TENS_VAL[w]))
            i += 1
        elif w in _ONES_VAL:
            parts.append(str(_ONES_VAL[w]))
            i += 1
        else:
            return None
    # "two fifty" is 250, 2.50 or 2:50 depending on what is meant: a single
    # digit followed by a two-digit group stays in words. Digit by digit
    # ("three four three") and year-like ("nineteen eighty four") are clear.
    if len(parts) > 1 and len(parts[0]) == 1 and any(len(x) > 1 for x in parts[1:]):
        return None
    return "".join(parts) if parts else None


def _spoken_number_forms(raw_keys: list[str]) -> set[str]:
    """Return all exact digit-string forms a spoken number phrase legitimately represents."""
    if not raw_keys:
        return set()
    sep_idx = [i for i, k in enumerate(raw_keys) if k in ("point", "dot")]
    if sep_idx:
        if len(sep_idx) > 3:
            return set()
        segs: list[list[str]] = []
        prev = 0
        for idx in sep_idx:
            segs.append(raw_keys[prev:idx])
            prev = idx + 1
        segs.append(raw_keys[prev:])
        if any(not s for s in segs):
            return set()
        combos: set[str] | None = None
        for seg in segs:
            seg_forms = _spoken_number_forms(seg)
            if not seg_forms:
                return set()
            # "one point five" is 1.5, never 15.
            combos = seg_forms if combos is None else {c + "." + f for c in combos for f in seg_forms}
        return combos or set()
    forms: set[str] = set()
    val = _parse_cardinal(raw_keys)
    if val is not None:
        forms.add(str(val))
    grp = _parse_grouped_digits(raw_keys)
    if grp is not None:
        forms.add(grp)
    return forms


def _num_shape(keys: list[str]) -> str:
    return "".join(c for k in keys for c in k if c.isdigit() or c in ".:-")


def _shape_changed(raw_keys: list[str], model_keys: list[str]) -> bool:
    """A sign or separator went in, out or moved: 1.25 -> 125, -5 -> 5."""
    a, b = _num_shape(raw_keys), _num_shape(model_keys)
    # "3.30" written as a clock time, "3:30", is the same time.
    a = re.sub(r"(?<!\d)(\d{1,2})\.([0-5]\d)(?!\d)", r"\1:\2", a) if ":" in b else a
    return any(c in ".:-" for c in a + b) and a != b


def _sound_ok(raw_keys: list[str], model_keys: list[str]) -> bool:
    a, b = "".join(raw_keys), "".join(model_keys)
    if not a or not b or _meaning_flip(a, b) or _shape_changed(raw_keys, model_keys):
        return False
    # Never let fuzzy sound similarity change digits ("15" -> "16"); spoken
    # digits count on the raw side ("e c two" -> "EC2", "gpt four" -> "GPT-4").
    da = "".join(k if k.isdigit() else str(_ONES_VAL[k]) if k in _ONES_VAL and k != "o" else _digits_in(k)
                 for k in raw_keys)
    db = _digits_in(b)
    if (da or db) and da != db:
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
    if not all(re.fullmatch(r"\d+(?:[.:]\d+)*", k) for k in model_keys):
        return False
    want = "".join(model_keys)
    forms = _spoken_number_forms(raw_keys)
    return want in forms


def _list_ok(raw_keys: list[str], model_keys: list[str]) -> bool:
    if len(model_keys) != 1 or not re.fullmatch(r"\d+", model_keys[0]):
        return False
    if not (len(raw_keys) == 2 and raw_keys[0] in LIST_LEADS and raw_keys[1] in ORDINALS):
        return False
    return str(_ORDINAL_VAL[raw_keys[1]]) == model_keys[0]


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
    da, db = _digits_in(a), _digits_in(b)
    if (da and db and da != db) or _shape_changed(raw_keys, model_keys):
        return False
    if b not in screen_terms_from_env():
        return False
    return _ratio(a, b) >= 0.55


def replace_ok(raw_keys: list[str], model_keys: list[str], pairs: list[tuple[str, str]]) -> bool:
    if not raw_keys or not model_keys:
        return False
    if "".join(raw_keys) == "".join(model_keys):
        return not _undoes_pair(raw_keys, model_keys, pairs)
    # Never let a replacement flip polarity ("can't" -> "can", "don't" -> "do", "isn't" -> "is").
    if _negation_count(raw_keys) != _negation_count(model_keys):
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


def neutralize_tags(text: str) -> str:
    """Prevent reserved XML prompt tags inside screen/draft/clipboard/dictation from breaking framing."""
    if not text or "<" not in text:
        return text
    return _RESERVED_TAG_RE.sub(r"‹\1", text)


def is_trivial(text: str, hints: str = "", title_pair: str = "") -> bool:
    """Two words with nothing for a model to fix: no filler, glue word,
    stutter or known mishearing. "Apple Music" and "New thread" went to the
    LLM and came back unchanged (~300 ms each). Three-word commands still go:
    "erase a PR" -> "raise a PR" needs it."""
    keys = [t.key for t in tokens(text)]
    if not keys or len(keys) > 2 or hints or title_pair:
        return False
    if any(_filler_word(k) or k in DISCOURSE for k in keys) or _contains_marker(keys):
        return False
    return all(a != b for a, b in zip(keys, keys[1:]))


def is_numbers_only(text: str) -> bool:
    """True when text contains digits/punctuation only (no letters), so an LLM rewrite only risks corruption."""
    s = (text or "").strip()
    return bool(s) and any(c.isdigit() for c in s) and not any(c.isalpha() for c in s)


def output_invariants_ok(raw: str, out: str) -> bool:
    """Whole-output faithfulness guards borrowed from Hex's apple_cleanup invariants.

    Checks:
      1. Negation count cannot change unless raw contains a self-correction marker
         or an adjacent negation stutter ("no no").
      2. Model cannot invent new first-person pronouns ("Sure, I can help..." / persona drift).
    """
    rk = [t.key for t in tokens(raw)]
    ok = [t.key for t in tokens(out)]
    if not rk or not ok:
        return bool(ok)
    raw_neg = _negation_count(rk)
    out_neg = _negation_count(ok)
    if raw_neg != out_neg:
        has_stutter = any(rk[i] in NEGATIONS and rk[i] == rk[i + 1] for i in range(len(rk) - 1))
        # "no actually ..." may drop the "no"; "not actually broken" may not drop the "not".
        polar_same = _negation_count([k for k in rk if k != "no"]) == _negation_count([k for k in ok if k != "no"])
        if not (_contains_marker(rk, strict=True) or (_contains_marker(rk) and polar_same)
                or (has_stutter and out_neg < raw_neg)):
            return False
    raw_fp = sum(1 for k in rk if k in FIRST_PERSON)
    out_fp = sum(1 for k in ok if k in FIRST_PERSON)
    if out_fp > raw_fp:
        return False
    return True


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
    trim_next_comma = False
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
            # Words restored after the model's last word: its closing
            # punctuation goes after them (added at the end), not before.
            if b == len(mw) and idx + 1 < len(pieces):
                chunk = re.sub(r"[.!?…]+\s*$", "", chunk)
            if trim_next_comma:
                trim_next_comma = False
                chunk = re.sub(r"^[,;]\s*", " ", chunk)
            if trim_next_punct:
                trim_next_punct = False
                chunk = re.sub(r"^[.!?…]+\s*", "", chunk)
                if re.match(r"[A-Z][a-z]", chunk) and chunk.split()[0] != "I" \
                        and re.search(r"(?<![A-Za-z])" + re.escape(chunk.split()[0].lower().strip(".,;:!?")) + r"(?![A-Za-z])", raw):
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
            # The model's comma where the word was: a restored conjunction
            # takes its place ("Apples, oranges" -> "Apples and oranges"); any
            # other restored word goes after it.
            if out and idx + 1 < len(pieces) and pieces[idx + 1][0] == "m":
                na = pieces[idx + 1][1]
                if 0 < na < len(mw) and re.match(r"^\s*[,;]", model[mw[na - 1].end:mw[na].start]):
                    if keep[-1].key not in ("and", "or", "but", "nor"):
                        out = out.rstrip() + model[mw[na - 1].end:mw[na].start].strip()[0]
                    trim_next_comma = True
            # If the next model chunk starts with sentence-ending punctuation
            # (e.g. model dropped "and" in "do this and also" -> "do this. Also"),
            # keep the period before the restored word instead of after it.
            if kind == "d" and out and idx + 1 < len(pieces) and pieces[idx + 1][0] == "m":
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
                and re.search(r"(?<![A-Za-z])" + re.escape(chunk.split()[0].lower().strip(".,;:!?")) + r"(?![A-Za-z])", raw):
            chunk = chunk[0].lower() + chunk[1:]
        out = _glue(out, chunk)
    if pieces and pieces[-1][0] in ("r", "d") and mw:
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
    raw_surfaces = [t.surface for t in rw]
    pieces: list[tuple] = []
    sm = difflib.SequenceMatcher(a=raw_keys, b=[t.key for t in mw], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            # Same letters, lost apostrophe: "we'll" -> "Well", "we're" -> "were".
            start = j1
            for k in range(i2 - i1):
                if _lost_apostrophe(rw[i1 + k], mw[j1 + k]):
                    if start < j1 + k:
                        pieces.append(("m", start, j1 + k))
                    pieces.append(("r", i1 + k, i1 + k + 1))
                    start = j1 + k + 1
            if start < j2:
                pieces.append(("m", start, j2))
        elif op == "delete":
            if not delete_ok(raw_keys, i1, i2, raw_surfaces) and not _discourse_cut(raw_keys[i1:i2], model, mw, j1, raw_keys, i2):
                pieces.append(("d", i1, i2))
        elif op == "insert":
            keys = [t.key for t in mw[j1:j2]]
            if insert_ok(keys):
                pieces.append(("m", j1, j2))
        else:
            rk, mk = raw_keys[i1:i2], [t.key for t in mw[j1:j2]]
            if _drops_contraction(rw[i1:i2], mw[j1:j2]):
                pieces.append(("r", i1, i2))
            elif replace_ok(rk, mk, pair_rows):
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
    # A term can only match where its letters run in order once spaces and
    # hyphens are gone; skip the rest before compiling a pattern for each.
    squashed = re.sub(r"[\s-]", "", text).lower()
    for term in sorted(terms, key=len, reverse=True):
        letters = re.sub(r"[^0-9A-Za-z]", "", term)
        if not letters or letters.lower() not in squashed:
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
    (re.compile(r"://[^\s:/@]+:[^\s/@]+@"), "://[redacted]@"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[redacted]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "[redacted]"),
]


def looks_like_password(text: str) -> bool:
    """A copied password: one token mixing three of lower, upper, digits and
    symbols. Identifiers (`useDictationShortcut`, `kitsune/live`) mix fewer,
    and a URL is kept (its credentials are redacted instead)."""
    t = text.strip()
    if not 8 <= len(t) <= 128 or any(c.isspace() for c in t) or "://" in t:
        return False
    kinds = (
        any(c.islower() for c in t),
        any(c.isupper() for c in t),
        any(c.isdigit() for c in t),
        any(not c.isalnum() for c in t),
    )
    return sum(kinds) >= 3


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
    return neutralize_tags(out[-limit:])


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


def prepare_payload(req: dict) -> dict:
    """Run all pre-LLM deterministic steps in a single Python invocation.

    Replaces ~9 separate python3 subprocess startups per dictation with one call,
    cutting ~350-450 ms of wall-clock latency while keeping identical semantics.
    """
    raw = req.get("raw", "") or ""
    mode = req.get("mode", "default") or "default"
    cls = req.get("cls", "") or ""
    title = req.get("title", "") or ""
    screen_raw = req.get("screen_raw", "") or ""
    draft_raw = req.get("draft_raw", "") or ""
    sel_raw = req.get("sel_raw", "") or ""
    clip_raw = req.get("clip_raw", "") or ""
    app_ctx = bool(req.get("app_ctx", False))
    screen_ctx = bool(req.get("screen_ctx", True))
    private_guard = bool(req.get("private_guard", True))
    atspi_password = bool(req.get("atspi_password", False))

    private_why = ""
    if private_guard:
        if atspi_password:
            private_why = "password field (AT-SPI)"
        else:
            private_why = private_reason(cls, title, screen_raw)
    if private_why:
        return {"private_why": private_why}

    stripped = strip_hallucinations(raw)
    if not stripped.strip():
        return {"private_why": "", "stripped": stripped, "empty": True}

    if mode != "command":
        snip = snippet(stripped)
        if snip is not None:
            return {"private_why": "", "stripped": stripped, "snippet": snip}

    heard = stripped
    hard = hard_apply(heard)

    title_pair = ""
    app_str = ""
    screen_str = ""
    draft_str = ""
    sel_str = ""
    clip_str = ""
    screen_terms = ""
    if app_ctx:
        title_pair, app_str = app_fields(heard, cls, title)
        if screen_ctx:
            if screen_raw.strip():
                screen_str = screen_block(screen_raw)
            if draft_raw.strip():
                draft_str = screen_block(draft_raw)
            if sel_raw.strip() and not looks_like_password(sel_raw):
                sel_str = screen_block(sel_raw)[:450]
            if clip_raw.strip() and not looks_like_password(clip_raw):
                clip_str = screen_block(clip_raw)[:500]
            combined = "\n".join(x for x in (title, screen_str, draft_str, sel_str, clip_str) if x)
            if combined:
                screen_terms = extract_screen_terms(combined)

    hints = hint_line(hard)
    return {
        "private_why": "",
        "stripped": stripped,
        "snippet": None,
        "numbers_only": is_numbers_only(hard) if mode != "command" else False,
        "trivial": is_trivial(hard, hints, title_pair) if mode != "command" else False,
        "heard": heard,
        "hard": hard,
        "hard_framed": neutralize_tags(hard),
        "title_pair": title_pair,
        "app": neutralize_tags(app_str),
        "screen": screen_str,
        "draft": draft_str,
        "selection": sel_str,
        "clipboard": clip_str,
        "screen_terms": screen_terms,
        "hints": neutralize_tags(hints),
    }


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "prepare":
        import json
        req = json.loads(_read_stdin() or "{}")
        sys.stdout.write(json.dumps(prepare_payload(req), ensure_ascii=False))
    elif cmd == "strip":
        sys.stdout.write(strip_hallucinations(_read_stdin()))
    elif cmd == "hard":
        sys.stdout.write(hard_apply(_read_stdin()))
    elif cmd == "hints":
        sys.stdout.write(hint_line(_read_stdin()))
    elif cmd == "constrain" and len(argv) == 3:
        sys.stdout.write(constrain(argv[2], _read_stdin()))
    elif cmd == "constrain-finish" and len(argv) == 3:
        # constrain-check, then finish, in one process: the model's answer
        # passed or not, and when it passed it is typed after finish anyway.
        heard = os.environ.get("PP_HEARD", "") if argv[2] == "-" else argv[2]
        out = constrain(heard, _read_stdin())
        if not output_invariants_ok(heard, out):
            return 1
        sys.stdout.write(finish(out))
    elif cmd == "constrain-check" and len(argv) == 3:
        # "-": the transcript is in PP_HEARD, kept off the command line.
        heard = os.environ.get("PP_HEARD", "") if argv[2] == "-" else argv[2]
        out = constrain(heard, _read_stdin())
        if not output_invariants_ok(heard, out):
            return 1
        sys.stdout.write(out)
    elif cmd == "check" and len(argv) == 3:
        return 0 if output_invariants_ok(argv[2], _read_stdin()) else 1
    elif cmd == "neutralize":
        sys.stdout.write(neutralize_tags(_read_stdin()))
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
    # The repo's own corrections, not whoever runs this: the checks below
    # name pairs ("hyperlane" -> Hyprland) a fresh install does not have.
    global TSV
    TSV = Path(__file__).resolve().parent.parent / "tests/fixtures/corrections.tsv"
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
    check("a copied password is not context",
          [looks_like_password(x) for x in ("Tr0ub4dor&3", "hunter2!X", "useDictationShortcut", "kitsune/live", "voxtype_pp_gate.py", "https://a.b/c?d=1", "two words")],
          lambda r: r == [True, True, False, False, False, False, False])
    check("URL credentials are redacted", screen_block("see https://me:s3cret@git.example.com/repo"),
          lambda r: "s3cret" not in r and "git.example.com/repo" in r)
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
    check("sentence-initial and may go where the model starts a sentence",
          constrain("to do this and also think about it", "to do this. Also think about it."),
          lambda s: s == "to do this. Also think about it.")
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

    prep = prepare_payload({
        "raw": "open the kite eye scheduler in hyprland",
        "mode": "terminal",
        "cls": "Cursor",
        "title": "kaitai-inference/src/scheduler.rs",
        "screen_raw": "export API_KEY=sk-abcdefghijklmnopqrstuv\nfn dispatch_waybar()",
        "app_ctx": True,
        "screen_ctx": True,
        "private_guard": True,
    })
    check("prepare single-call batch", str(prep), lambda _: (
        prep["private_why"] == ""
        and prep["title_pair"] == "kite eye\tkaitai"
        and "sk-abc" not in prep["screen"]
        and "dispatchwaybar" in prep["screen_terms"]
    ))

    # Tier 1 regression cases: negation flips, digit flips, spoken number value mismatch, tag neutralization
    check("negation cant->can blocked",
          constrain("I can't make it on Friday", "I can make it on Friday."),
          lambda s: "can't" in s.lower())
    check("negation dont->do blocked",
          constrain("we don't need the cache", "We do need the cache."),
          lambda s: "don't" in s.lower())
    check("negation isnt->is blocked",
          constrain("it isn't working", "It is working."),
          lambda s: "isn't" in s.lower())
    check("number fifteen->50 blocked",
          constrain("fifteen seconds", "50 seconds"),
          lambda s: "fifteen" in s.lower() and "50" not in s)
    check("number fifteen->15 allowed",
          constrain("fifteen seconds", "15 seconds"),
          lambda s: "15" in s and "fifteen" not in s.lower())
    check("digit 15->16 blocked",
          constrain("15 seconds", "16 seconds"),
          lambda s: "15" in s and "16" not in s)
    check("whole-output negation invariant",
          str(output_invariants_ok("I can't make it", "I can make it.")),
          lambda s: s == "False")
    check("whole-output self-correction negation allowed",
          str(output_invariants_ok("I don't think no wait I do think it works", "I do think it works.")),
          lambda s: s == "True")
    check("whole-output invented first-person blocked",
          str(output_invariants_ok("Fix the build error", "I fixed the build error.")),
          lambda s: s == "False")
    check("tag neutralization escapes reserved only",
          neutralize_tags("<div>keep</div> </screen> <dictation>x</dictation> Vec<T>"),
          lambda s: "<div>keep</div>" in s and "Vec<T>" in s and "</screen>" not in s and "<dictation>" not in s)
    check("rejected replacement keeps the period after it",
          constrain("we need the tasks then we deploy", "We need the things. Then we deploy."),
          lambda s: s == "We need the tasks. Then we deploy.")
    check("spoken digits inside names allowed",
          constrain("deploy to e c two then ping me", "Deploy to EC2. Then ping me."),
          lambda s: s == "Deploy to EC2. Then ping me.")
    check("not actually cut blocked",
          str(output_invariants_ok("it's not actually broken", "It's broken.")
              or "not" not in constrain("it's not actually broken", "It's broken.")),
          lambda s: s == "False")
    check("no actually self-correction allowed",
          str(output_invariants_ok("no actually use the cache", "Use the cache.")),
          lambda s: s == "True")
    check("lemme is first person",
          str(output_invariants_ok("lemme check the logs", "Let me check the logs.")),
          lambda s: s == "True")
    check("with out respelled",
          constrain("ship it with out tests", "Ship it without tests."),
          lambda s: "without" in s)
    check("sentence-initial and dropped cleanly",
          constrain("you can do this and also think about it", "You can do this. Also, think about it."),
          lambda s: s == "You can do this. Also, think about it.")
    check("two items are not a false start",
          constrain("fix the header fix the footer", "Fix the footer."),
          lambda s: "header" in s)
    check("trailing well kept, trailing okay dropped",
          constrain("it works well", "It works.") + " | " + constrain("we need to fix it okay", "We need to fix it."),
          lambda s: s == "It works well. | We need to fix it.")
    check("like before an article kept",
          constrain("make it like the old one", "Make it the old one."),
          lambda s: "like the" in s)
    check("interleaved stutter dropped",
          constrain("only one disk is being shown shown also also there is one", "Only one disk is being shown. Also, there is one."),
          lambda s: s == "Only one disk is being shown. Also, there is one.")
    check("like stays unless it glues a new sentence",
          constrain("we are going to completely like improve it", "We are going to completely improve it.")
          + " | " + constrain("it looks like a bug", "It looks a bug.")
          + " | " + constrain("I like the red one", "I the red one.")
          + " | " + constrain("we ship it like we fix the docs", "We ship it. We fix the docs."),
          lambda s: s == "We are going to completely like improve it. | It looks like a bug. | I like the red one."
                       " | We ship it. We fix the docs.")
    check("false start opening with okay dropped",
          constrain("fix it okay before that before starting that I need things",
                    "Fix it. Before starting that, I need things."),
          lambda s: s == "Fix it. Before starting that, I need things.")
    check("false start dropped",
          constrain("fix all the things we have we need to fix", "Fix all the things we need to fix."),
          lambda s: s == "Fix all the things we need to fix.")
    check("reversal across a negation kept",
          constrain("I don't I do want it", "I do want it."),
          lambda s: "don't" in s)
    check("mid-sentence and still restored",
          constrain("apples and oranges", "Apples, oranges."),
          lambda s: "and" in s)
    check("number values keep sign and separators",
          " | ".join(constrain(r, m) for r, m in [
              ("set it to 1.25 volts now", "Set it to 125 volts now."),
              ("it was -5 degrees outside", "It was 5 degrees outside."),
              ("use one point five volts", "Use 15 volts."),
              ("use one point five volts", "Use 1.5 volts."),
              ("open port four zero", "Open port 4."),
              ("meet at three thirty", "Meet at 3:30."),
              ("about two fifty for lunch", "About 250 for lunch."),
              ("in nineteen eighty four", "In 1984."),
          ]),
          lambda s: s == "Set it to 1.25 volts now. | It was -5 degrees outside. | Use one point five volts."
                       " | Use 1.5 volts. | Open port four zero. | Meet at three thirty."
                       " | About two fifty for lunch. | In 1984.")
    check("content the deletion rules must keep",
          " | ".join(constrain(r, m) for r, m in [
              ("we should not scratch that or move it", "We should move it."),
              ("send to Alice then send to Bob", "Send to Bob."),
              ("if the path fails turn right", "If the path fails turn."),
              ("compare lakes like Erie and Ontario", "Compare lakes Erie and Ontario."),
              ("I don't want scratch that I want the red one", "I want the red one."),
          ]),
          lambda s: s == "We should not scratch that or move it. | Send to Alice then send to Bob."
                       " | If the path fails turn right. | Compare lakes like Erie and Ontario."
                       " | I want the red one.")
    check("grok review: contractions, items, like, tags, so, doubles",
          " | ".join(constrain(r, m) for r, m in [
              ("I'd like to ship it today", "I like to ship it today."),
              ("I'll check the logs now", "I check the logs now."),
              ("we'll ship it Friday", "Well ship it Friday."),
              ("we're late for the meeting", "were late for the meeting."),
              ("buy milk buy eggs", "Buy eggs."),
              ("we have we need to fix this", "We need to fix this."),
              ("works like magic", "Works magic."),
              ("that's okay", "That's."),
              ("so much better this way", "Much better this way."),
              ("apples and oranges", "Apples, oranges."),
              ("I had had enough of it", "I had enough of it."),
              ("no no no don't do that", "No, don't do that."),
              ("we need need to fix it", "We need to fix it."),
              ("all the disks in the fast fast fast fetch", "All the disks in the fastfetch."),
          ]),
          lambda s: s == "I'd like to ship it today. | I'll check the logs now. | We'll ship it Friday."
                       " | We're late for the meeting. | Buy milk buy eggs. | We need to fix this."
                       " | Works like magic. | That's okay. | So much better this way. | Apples and oranges."
                       " | I had had enough of it. | No no no, don't do that. | We need to fix it."
                       " | All the disks in the fastfetch.")
    check("trivial short takes",
          f"{is_trivial('Apple Music')},{is_trivial('New thread.')},{is_trivial('um okay')},"
          f"{is_trivial('the the')},{is_trivial('Go home', hints='x')},{is_trivial('erase a PR')}",
          lambda s: s == "True,True,False,False,False,False")
    check("numbers only detection",
          f"{is_numbers_only('192.168.1.1')},{is_numbers_only('three hundred')},{is_numbers_only('42 89')}",
          lambda s: s == "True,False,True")

    if fails:
        print("\n".join(fails))
        return 1
    print(f"gate selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
