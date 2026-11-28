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

Usage: voxtype-learn [list | stats [--json] | pending | harvest [--json] | replay [--history PATH] [--json] | add HEARD MEANT [--scope SCOPE] | accept HEARD | reject HEARD | accept-vocab TERM [--scope SCOPE] | reject-vocab TERM | forget HEARD | fix | sync-toml | sync-parakeet]
"""
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voxtype_storage import atomic_write, locked

CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxtype"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "voxtype"
TSV = CONF / "corrections.tsv"
CANDIDATES = STATE / "learn-candidates.json"
REJECTED = STATE / "learn-rejected.json"
HEALTH = STATE / "learn-health.json"
VOCAB_CANDIDATES = STATE / "vocab-candidates.json"
VOCAB_REJECTED = STATE / "vocab-rejected.json"
HISTORY = STATE / "history.jsonl"
MAX_SPAN = 4            # words per side; longer swaps are rewrites, not mishearings
DEMOTE_UNDOS = 2        # undos before a blind rewrite is demoted to a context hint
REMOVE_UNDOS = 3        # undos before a demoted/hint rule is removed automatically
PARAKEET_MAX_TERMS = 128
# A heard phrase made only of these is a function word, not a mishearing.
# "a" -> "pkill dunst" matched every dictation that contained the article.
STOPWORDS = frozenset(
    "a an the in on to is it of and or for as at be by do if we he she me my "
    "so no not but from with that this was are were you your its i am be been "
    "being have has had does did can will just like than then them they their "
    "there here what when where who how into over also very really".split()
)
HEADER = ("# Learned mishearings: heard<TAB>meant<TAB>source<TAB>count[<TAB>scope].\n"
          "# Managed by voxtype_learn.py; hand edits are kept.\n")
VOCAB_FILE = CONF / "vocabulary.txt"
VOCAB_DIR = CONF / "vocabulary.d"
PARAKEET_VOCAB = CONF / "parakeet-vocabulary.txt"
CONFIG_TOML = CONF / "config.toml"
PP_ENV = CONF / "postprocess.env"
AMBIGUOUS = frozenset({"swimming", "piers", "codecs", "clawed", "next year's"})
SOURCE_LABELS = {
    "seed": "seed",
    "user": "user fix",
    "edit-watch": "edit-watch",
    "hint": "accepted hint",
    "auto": "auto",
}
PLAIN_SOURCE_LABELS = {
    "seed": "Built-in starter",
    "user": "Your fix",
    "edit-watch": "Learned from your edit",
    "hint": "Accepted hint",
    "auto": "Auto-learned",
}
SCOPE_LABELS = {
    "": "All apps",
    "global": "All apps",
    "terminal": "Code & terminal",
    "agent": "Code & terminal",
    "chat": "Chat",
    "browser": "Browser",
}
_TERMINAL_CLASS_RE = re.compile(
    r"^(?:ghostty|kitty|alacritty|foot|wezterm|konsole|xterm|.*terminal.*|neovide|cursor"
    r"|code|code-oss|zed|dev\.zed.*|com\.t3tools\.t3code|devin-desktop.*|jetbrains-.*|obsidian)$",
    re.I,
)
_CHAT_CLASS_RE = re.compile(
    r"^(?:zapzap|slack|discord|vesktop|webcord|telegram.*|whatsapp.*|signal.*|beeper.*|ferdium.*|franz.*)$",
    re.I,
)
_BROWSER_CLASS_RE = re.compile(
    r"^(?:zen|zen-alpha|app\.zen_browser\.zen|firefox.*|chromium.*|google-chrome.*|brave.*|vivaldi.*)$",
    re.I,
)
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


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_scope(scope: str | None) -> str:
    s = (scope or "").strip().lower()
    if s in ("", "global", "all"):
        return ""
    if s in ("terminal", "code", "agent"):
        return "terminal"
    if s in ("chat", "browser"):
        return s
    cleaned = re.sub(r"[^a-z0-9._:-]+", "", s)
    return cleaned


def scope_label(scope: str | None) -> str:
    sc = normalize_scope(scope)
    if sc in SCOPE_LABELS:
        return SCOPE_LABELS[sc]
    return sc.removeprefix("app:") or "All apps"


def source_label(source: str, demoted: bool = False) -> str:
    if demoted:
        return "demoted hint"
    return SOURCE_LABELS.get(source, source or "user fix")


def plain_source_label(source: str, demoted: bool = False) -> str:
    if demoted:
        return "Demoted to hint"
    return PLAIN_SOURCE_LABELS.get(source, source or "Your fix")


def scope_matches(scope: str | None, mode: str = "", cls: str = "") -> bool:
    sc = normalize_scope(scope)
    if not sc:
        return True
    m = (mode or "").strip().lower()
    c = (cls or "").strip()
    if sc == "terminal":
        return m in ("terminal", "agent") or bool(_TERMINAL_CLASS_RE.match(c))
    if sc == "chat":
        return m == "chat" or bool(_CHAT_CLASS_RE.match(c))
    if sc == "browser":
        return bool(_BROWSER_CLASS_RE.match(c))
    target = sc.removeprefix("app:")
    return bool(target and target in c.lower())


def _vocab_path_for(scope: str = "") -> Path:
    sc = normalize_scope(scope)
    if not sc:
        return VOCAB_FILE
    safe = re.sub(r"[^a-z0-9._-]+", "-", sc)
    return VOCAB_DIR / f"{safe}.txt"


def read_vocab(scope: str = "") -> list[str]:
    p = _vocab_path_for(scope)
    if not p.exists():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        t = line.split("#", 1)[0].strip()
        if t:
            out.append(t)
    return out


def list_vocab_scopes() -> list[str]:
    if not VOCAB_DIR.is_dir():
        return []
    return sorted(p.stem for p in VOCAB_DIR.glob("*.txt") if p.is_file())


def vocab_terms_for(mode: str = "", cls: str = "") -> list[str]:
    out = list(read_vocab(""))
    seen = {t.lower() for t in out}
    for sc in list_vocab_scopes():
        if scope_matches(sc, mode=mode, cls=cls):
            for t in read_vocab(sc):
                if t.lower() not in seen:
                    out.append(t)
                    seen.add(t.lower())
    return out


def _vocab_map(include_scoped: bool = True) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in read_vocab(""):
        key = re.sub(r"[^a-z0-9]", "", t.lower())
        if key:
            out[key] = t
    if include_scoped:
        for sc in list_vocab_scopes():
            for t in read_vocab(sc):
                key = re.sub(r"[^a-z0-9]", "", t.lower())
                if key and key not in out:
                    out[key] = t
    return out


def add_vocab_term(term: str, scope: str = "", sync_parakeet: bool = True) -> bool:
    t = term.strip()
    if not t:
        return False
    p = _vocab_path_for(scope)
    existing = read_vocab(scope)
    if t.lower() in {x.lower() for x in existing}:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Vocabulary terms ({scope_label(scope)})\n"
    text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else header
    if text and not text.endswith("\n"):
        text += "\n"
    atomic_write(p, text + t + "\n")
    if sync_parakeet and not normalize_scope(scope):
        try:
            sync_parakeet_vocab()
        except OSError:
            pass
    return True


def remove_vocab_term(term: str, scope: str = "", sync_parakeet: bool = True) -> bool:
    p = _vocab_path_for(scope)
    if not p.exists():
        return False
    want = term.strip().lower()
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    kept, removed = [], False
    for line in lines:
        if line.split("#", 1)[0].strip().lower() == want and not removed:
            removed = True
            continue
        kept.append(line)
    if removed:
        atomic_write(p, "".join(kept))
        if sync_parakeet and not normalize_scope(scope):
            try:
                sync_parakeet_vocab()
            except OSError:
                pass
    return removed


def load_vocab_candidates() -> dict[str, dict]:
    if not VOCAB_CANDIDATES.exists():
        return {}
    try:
        data = json.loads(VOCAB_CANDIDATES.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_vocab_candidates(cands: dict[str, dict]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    atomic_write(VOCAB_CANDIDATES, json.dumps(cands, ensure_ascii=False, indent=2) + "\n")


def load_vocab_rejected() -> set[str]:
    if not VOCAB_REJECTED.exists():
        return set()
    try:
        data = json.loads(VOCAB_REJECTED.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_vocab_rejected(keys: set[str]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    atomic_write(VOCAB_REJECTED, json.dumps(sorted(keys), ensure_ascii=False, indent=2) + "\n")


def accept_vocab_candidate(term: str, scope: str = "") -> bool:
    t = term.strip()
    if not t:
        return False
    cands = load_vocab_candidates()
    key = t.lower()
    entry = cands.get(key)
    chosen_scope = scope if scope is not None else (entry.get("scope", "") if isinstance(entry, dict) else "")
    added = add_vocab_term(t, scope=chosen_scope, sync_parakeet=True)
    removed_cand = False
    for k in [k for k, v in cands.items() if k == key or (isinstance(v, dict) and str(v.get("term", "")).lower() == key)]:
        cands.pop(k, None)
        removed_cand = True
    if removed_cand:
        save_vocab_candidates(cands)
    rej = load_vocab_rejected()
    if key in rej:
        rej.discard(key)
        save_vocab_rejected(rej)
    if normalize_scope(chosen_scope):
        try:
            sync_parakeet_vocab()
        except OSError:
            pass
    return added or removed_cand


def reject_vocab_candidate(term: str) -> str:
    key = term.strip().lower()
    rej = load_vocab_rejected()
    rej.add(key)
    save_vocab_rejected(rej)
    cands = load_vocab_candidates()
    removed = False
    for k in [k for k, v in cands.items() if k == key or (isinstance(v, dict) and str(v.get("term", "")).lower() == key)]:
        cands.pop(k, None)
        removed = True
    if removed:
        save_vocab_candidates(cands)
    return key


def unreject_vocab_candidate(key: str) -> None:
    rej = load_vocab_rejected()
    if key in rej:
        rej.discard(key)
        save_vocab_rejected(rej)


def env_flag(name: str, default: bool = True) -> bool:
    """A 0/1 switch from postprocess.env (last assignment wins)."""
    val = "1" if default else "0"
    try:
        text = PP_ENV.read_text(errors="replace")
    except OSError:
        return default
    for line in text.splitlines():
        m = re.match(rf"^{name}=([^\s#]+)", line.strip())
        if m:
            val = m.group(1).strip("\"'")
    return val == "1"


def _auto_learn_enabled() -> bool:
    return env_flag("PP_AUTO_LEARN")


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


def _add_vocab_if_distinctive(term: str, scope: str = "") -> bool:
    t = term.strip()
    if not t or len(t) < 2:
        return False
    distinctive = any(c.isupper() for c in t[1:]) or "." in t or "-" in t or "+" in t or t.isupper()
    # A capitalised name that is not an English word (Kaligo, Brionne, Shuush).
    name = t[:1].isupper() and " " not in t and t.lower() not in _common_words()
    if not (distinctive or name):
        return False
    existing = _vocab_map(include_scoped=True)
    key = re.sub(r"[^a-z0-9]", "", t.lower())
    if not key or key in existing:
        return False
    return add_vocab_term(t, scope=scope, sync_parakeet=True)


def hint_candidate(heard: str, meant: str) -> str | None:
    """A hand fix from an English word to a name (grog -> Groq, get hub ->
    GitHub) that is_safe_auto_candidate refuses because the heard words are
    English. Worth asking about, and once accepted it is a hint for the
    cleanup model only (source "hint"): "grog" is never rewritten blindly."""
    h, m = norm(heard), meant.strip()
    if not h or not m or h in AMBIGUOUS or is_rejected(h, m) or not is_mishearing(h, m):
        return None
    toks = words(h)
    if len(toks) > 3 or toks[0] in STOPWORDS or toks[-1] in STOPWORDS:
        return None
    if not any(c.isupper() for c in m) or m.lower() in _common_words():
        return None
    return m if sounds_alike(h, m) else None


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


def _load_rows() -> dict[str, list]:
    """Internal row loader: {heard: [meant, source, count, scope]}."""
    pairs: dict[str, list] = {}
    if TSV.exists():
        for line in TSV.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) >= 2 and f[0].strip():
                meant = f[1].strip()
                src = (f[2:3] or ["user"])[0].strip() or "user"
                try:
                    n = int((f[3:4] or ["1"])[0] or 1)
                except ValueError:
                    n = 1
                scope = normalize_scope(f[4]) if len(f) >= 5 else ""
                pairs[norm(f[0])] = [meant, src, n, scope]
    return pairs


def _save_rows(pairs: dict[str, list]) -> None:
    TSV.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for h, row in sorted(pairs.items()):
        m, src, n = row[0], row[1], row[2]
        scope = normalize_scope(row[3]) if len(row) >= 4 else ""
        if scope:
            lines.append(f"{h}\t{m}\t{src}\t{n}\t{scope}\n")
        else:
            lines.append(f"{h}\t{m}\t{src}\t{n}\n")
    atomic_write(TSV, HEADER + "".join(lines))


def load() -> dict[str, list]:
    return {h: [r[0], r[1], r[2]] for h, r in _load_rows().items()}


def _save(pairs: dict[str, list]) -> None:
    _save_rows(pairs)


def load_health() -> dict[str, dict]:
    if not HEALTH.exists():
        return {}
    try:
        data = json.loads(HEALTH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_health(health: dict[str, dict]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    atomic_write(HEALTH, json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _default_health_record(meant: str, source: str, scope: str = "", now: str = "") -> dict:
    return {
        "meant": meant,
        "source": source,
        "origin_source": source,
        "scope": normalize_scope(scope),
        "first_seen": now,
        "last_seen": now,
        "applied": 0,
        "last_applied": "",
        "undone": 0,
        "consecutive_undone": 0,
        "last_undone": "",
        "status": "hint" if source == "hint" else "active",
    }


def load_entries(include_removed: bool = False) -> list[dict]:
    """Return enriched provenance & health dicts for every rule."""
    rows = _load_rows()
    health = load_health()
    out: list[dict] = []
    for h, (meant, src, n, scope) in sorted(rows.items()):
        rec = health.get(h) if isinstance(health.get(h), dict) else {}
        demoted = rec.get("status") == "demoted"
        origin = str(rec.get("origin_source") or rec.get("source") or src)
        status = "demoted" if demoted else ("hint" if src == "hint" or h in AMBIGUOUS else "active")
        out.append({
            "heard": h,
            "meant": meant,
            "source": src,
            "origin_source": origin,
            "source_label": source_label(origin, demoted=demoted),
            "plain_source": plain_source_label(origin, demoted=demoted),
            "count": n,
            "scope": scope or normalize_scope(rec.get("scope", "")),
            "scope_label": scope_label(scope or rec.get("scope", "")),
            "first_seen": str(rec.get("first_seen") or ""),
            "last_seen": str(rec.get("last_seen") or ""),
            "applied": int(rec.get("applied") or 0),
            "last_applied": str(rec.get("last_applied") or ""),
            "undone": int(rec.get("undone") or 0),
            "consecutive_undone": int(rec.get("consecutive_undone") or 0),
            "last_undone": str(rec.get("last_undone") or ""),
            "status": status,
            "demoted": demoted,
        })
    if include_removed:
        for h, rec in sorted(health.items()):
            if h in rows or not isinstance(rec, dict) or rec.get("status") != "removed":
                continue
            origin = str(rec.get("origin_source") or rec.get("source") or "user")
            out.append({
                "heard": h,
                "meant": str(rec.get("meant") or ""),
                "source": str(rec.get("source") or origin),
                "origin_source": origin,
                "source_label": source_label(origin),
                "plain_source": plain_source_label(origin),
                "count": 0,
                "scope": normalize_scope(rec.get("scope", "")),
                "scope_label": scope_label(rec.get("scope", "")),
                "first_seen": str(rec.get("first_seen") or ""),
                "last_seen": str(rec.get("last_seen") or ""),
                "applied": int(rec.get("applied") or 0),
                "last_applied": str(rec.get("last_applied") or ""),
                "undone": int(rec.get("undone") or 0),
                "consecutive_undone": int(rec.get("consecutive_undone") or 0),
                "last_undone": str(rec.get("last_undone") or ""),
                "status": "removed",
                "demoted": False,
                "removed_at": str(rec.get("removed_at") or ""),
            })
    return out


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


def remember(heard: str, meant: str, source: str, undo=None, scope: str | None = None) -> str | None:
    """Store one pair with provenance & health metadata.
    An optional undo log records only the rows we changed."""
    if (why := unsafe_heard(heard)):
        return why
    key = norm(heard)
    now = now_iso()
    with locked(TSV):
        rows = _load_rows()
        old = rows.get(key)
        n = old[2] + 1 if old and old[0].lower() == meant.lower() else 1
        if old and old[1] == "user" and source not in ("user", "hint") and old[0].lower() != meant.lower():
            return "a hand-added pair already has this"
        sc = normalize_scope(scope) if scope is not None else (old[3] if old else "")
        rows[key] = [meant, source, n, sc]
        _save_rows(rows)
        if undo is not None:
            undo[key] = (undo.get(key, (old,))[0], list(rows[key]))
        with locked(HEALTH):
            health = load_health()
            prev = health.get(key) if isinstance(health.get(key), dict) else {}
            same_target = bool(prev and str(prev.get("meant", "")).lower() == meant.lower())
            rec = dict(prev) if same_target else _default_health_record(meant, source, sc, now)
            rec["meant"] = meant
            rec["source"] = source
            if not rec.get("origin_source") or source in ("user", "edit-watch") or not same_target:
                rec["origin_source"] = source
            rec["scope"] = sc
            if not rec.get("first_seen"):
                rec["first_seen"] = now
            rec["last_seen"] = now
            rec["status"] = "hint" if source == "hint" else "active"
            if source in ("user", "edit-watch"):
                rec["consecutive_undone"] = 0
                rec.pop("removed_at", None)
                rec.pop("demoted_at", None)
            health[key] = rec
            save_health(health)
    if sc:
        remove_replacement(key)
    try:
        sync_parakeet_vocab()
    except OSError:
        pass
    return None


def forget(heard, undo=None):
    key = norm(heard)
    with locked(TSV):
        rows = _load_rows()
        old = rows.pop(key, None)
        if old is None:
            return False
        _save_rows(rows)
        if undo is not None:
            undo[key] = (old, None)
        with locked(HEALTH):
            health = load_health()
            if key in health:
                health.pop(key, None)
                save_health(health)
    try:
        sync_parakeet_vocab()
    except OSError:
        pass
    return True


def undo_corrections(changes):
    with locked(TSV):
        rows = _load_rows()
        for key, (before, after) in changes.items():
            cur = rows.get(key)
            # Match either 3-col legacy or 4-col internal undo entries
            if cur == after or (cur and after and cur[:3] == after[:3]):
                if before is None:
                    rows.pop(key, None)
                else:
                    rows[key] = list(before) + [""] if len(before) == 3 else list(before)
        _save_rows(rows)
    try:
        sync_parakeet_vocab()
    except OSError:
        pass


def record_applied(heard: str, meant: str = "") -> None:
    """Record that a learned rule fired and survived cleanup on a dictation."""
    key = norm(heard)
    if not key:
        return
    now = now_iso()
    rows = _load_rows()
    row = rows.get(key)
    with locked(HEALTH):
        health = load_health()
        rec = health.get(key) if isinstance(health.get(key), dict) else None
        if rec is None:
            src = row[1] if row else "user"
            sc = row[3] if row else ""
            rec = _default_health_record(meant or (row[0] if row else ""), src, sc, now)
        elif meant and not rec.get("meant"):
            rec["meant"] = meant
        if not rec.get("first_seen"):
            rec["first_seen"] = now
        rec["applied"] = int(rec.get("applied") or 0) + 1
        rec["consecutive_undone"] = 0
        rec["last_applied"] = now
        rec["last_seen"] = now
        health[key] = rec
        save_health(health)


def record_undo(heard: str, meant: str = "") -> str:
    """Record that the user changed a rule's rewrite back.

    A rule that keeps getting undone is demoted to a context hint first, then
    removed automatically if it keeps getting undone.
    Returns one of: 'recorded', 'demoted', 'removed', 'missing'.
    """
    key = norm(heard)
    if not key:
        return "missing"
    now = now_iso()
    action = "recorded"
    removed_meant = ""
    with locked(TSV):
        rows = _load_rows()
        row = rows.get(key)
        with locked(HEALTH):
            health = load_health()
            rec = health.get(key) if isinstance(health.get(key), dict) else None
            if row is None and rec is None:
                return "missing"
            if rec is None:
                rec = _default_health_record(meant or row[0], row[1], row[3], now)
            if not rec.get("first_seen"):
                rec["first_seen"] = now
            rec["undone"] = int(rec.get("undone") or 0) + 1
            rec["consecutive_undone"] = int(rec.get("consecutive_undone") or 0) + 1
            rec["last_undone"] = now
            rec["last_seen"] = now
            applied = int(rec.get("applied") or 0)
            undone = rec["undone"]
            consec = rec["consecutive_undone"]
            cur_src = row[1] if row else str(rec.get("source") or "user")
            cur_meant = row[0] if row else str(rec.get("meant") or meant)
            already_demoted = rec.get("status") == "demoted" or cur_src == "hint"

            if row is not None and already_demoted and (
                (undone >= REMOVE_UNDOS and undone / max(1, applied) >= 0.30) or consec >= REMOVE_UNDOS
            ):
                rows.pop(key, None)
                _save_rows(rows)
                rec["status"] = "removed"
                rec["removed_at"] = now
                action = "removed"
                removed_meant = cur_meant
            elif row is not None and not already_demoted and (
                (undone >= DEMOTE_UNDOS and undone / max(1, applied) >= 0.20) or consec >= DEMOTE_UNDOS
            ):
                rec["origin_source"] = rec.get("origin_source") or cur_src
                rec["source"] = "hint"
                rec["status"] = "demoted"
                rec["demoted_at"] = now
                rows[key] = [row[0], "hint", row[2], row[3]]
                _save_rows(rows)
                action = "demoted"
            health[key] = rec
            save_health(health)

    if action in ("demoted", "removed"):
        remove_replacement(key)
        if action == "removed" and removed_meant:
            reject_pair(key, removed_meant)
        try:
            sync_parakeet_vocab()
        except OSError:
            pass
    return action


def demote_rule(heard: str) -> bool:
    """Manually demote an active instant-rewrite rule to a context-only hint."""
    key = norm(heard)
    now = now_iso()
    with locked(TSV):
        rows = _load_rows()
        row = rows.get(key)
        if row is None or row[1] == "hint":
            return False
        old_src = row[1]
        rows[key] = [row[0], "hint", row[2], row[3]]
        _save_rows(rows)
        with locked(HEALTH):
            health = load_health()
            rec = health.get(key) if isinstance(health.get(key), dict) else _default_health_record(row[0], old_src, row[3], now)
            rec["origin_source"] = rec.get("origin_source") or old_src
            rec["source"] = "hint"
            rec["status"] = "demoted"
            rec["demoted_at"] = now
            rec["last_seen"] = now
            health[key] = rec
            save_health(health)
    remove_replacement(key)
    try:
        sync_parakeet_vocab()
    except OSError:
        pass
    return True


def promote_rule(heard: str) -> bool:
    """Restore a demoted hint back to its original active rewrite source."""
    key = norm(heard)
    now = now_iso()
    with locked(TSV):
        rows = _load_rows()
        row = rows.get(key)
        if row is None:
            return False
        with locked(HEALTH):
            health = load_health()
            rec = health.get(key) if isinstance(health.get(key), dict) else None
            origin = (rec.get("origin_source") if rec else None) or "user"
            if origin == "hint":
                origin = "user"
            rows[key] = [row[0], origin, row[2], row[3]]
            _save_rows(rows)
            if rec is None:
                rec = _default_health_record(row[0], origin, row[3], now)
            rec["source"] = origin
            rec["status"] = "active"
            rec["consecutive_undone"] = 0
            rec["last_seen"] = now
            rec.pop("demoted_at", None)
            health[key] = rec
            save_health(health)
    sync_replacements()
    try:
        sync_parakeet_vocab()
    except OSError:
        pass
    return True


def _history_term_counts(history_path: Path | None = None) -> dict[str, int]:
    hp = history_path or HISTORY
    counts: dict[str, int] = {}
    if not hp.is_file():
        return counts
    try:
        text = hp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return counts
    for line in text.splitlines()[-500:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("via") == "private":
            continue
        clean = str(row.get("clean") or "")
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+#-]*", clean):
            k = re.sub(r"[^a-z0-9]", "", tok.lower())
            if len(k) >= 2:
                counts[k] = counts.get(k, 0) + 1
    return counts


def _parakeet_configured_file() -> str:
    if not CONFIG_TOML.is_file():
        return ""
    try:
        doc = tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8", errors="replace"))
        val = (doc.get("parakeet") or {}).get("vocabulary_file")
        return str(val) if val else ""
    except (OSError, ValueError):
        return ""


def sync_parakeet_vocab(history_path: Path | None = None, max_terms: int = PARAKEET_MAX_TERMS) -> dict:
    """Write parakeet-vocabulary.txt ordered most-useful-first within Parakeet's
    128-term rescoring limit (accounting for unique targets in corrections.tsv).
    """
    global_vocab = read_vocab("")
    entries = load_entries(include_removed=False)
    hist_counts = _history_term_counts(history_path)

    # Map canonical key -> (display term, priority score)
    scored: dict[str, tuple[str, float, int]] = {}
    vocab_keys: set[str] = set()
    for idx, term in enumerate(global_vocab):
        k = re.sub(r"[^a-z0-9]", "", term.lower())
        if not k or len(k) < 2 or term.lower() in STOPWORDS:
            continue
        vocab_keys.add(k)
        # Base score: frequency in history + slight preference for earlier file order
        h_freq = hist_counts.get(k, 0)
        score = 10.0 + min(h_freq, 30) * 1.5 - idx * 0.01
        if k not in scored or score > scored[k][1]:
            scored[k] = (term, score, idx)

    # Boost terms backed by active correction rules (the speaker actually had ASR miss them!)
    corr_only_targets: set[str] = set()
    source_bonus = {"edit-watch": 28.0, "user": 26.0, "auto": 16.0, "seed": 12.0, "hint": 8.0}
    for e in entries:
        if e.get("scope"):
            continue  # scoped rules should not bloat global Parakeet rescoring
        meant = e["meant"]
        k = re.sub(r"[^a-z0-9]", "", meant.lower())
        if not k or len(k) < 2 or meant.lower() in STOPWORDS:
            continue
        bonus = source_bonus.get(e["origin_source"], 10.0) + min(e["applied"], 20) * 2.0
        if e["demoted"]:
            bonus *= 0.3
        if k in scored:
            disp, prev_score, idx = scored[k]
            scored[k] = (disp, prev_score + bonus, idx)
        else:
            corr_only_targets.add(k)
            if not e["demoted"] and e["source"] != "hint":
                scored[k] = (meant, bonus, 10000)

    # vocab_rescore.rs reads vocabulary_file first, then corrections_file, truncating at MAX_TERMS (128).
    # Keep total unique terms <= max_terms.
    ranked = sorted(scored.values(), key=lambda item: (-item[1], item[2], item[0].lower()))
    chosen = [term for term, _sc, _idx in ranked[:max_terms]]
    trimmed = max(0, len(ranked) - len(chosen))

    PARAKEET_VOCAB.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Parakeet acoustic rescoring vocabulary ({len(chosen)}/{max_terms} terms, most useful first)\n"
        "# Managed by voxtype_learn.py; synced from vocabulary.txt and corrections.tsv.\n"
    )
    body = "".join(f"{t}\n" for t in chosen)
    atomic_write(PARAKEET_VOCAB, header + body)
    return {
        "file": str(PARAKEET_VOCAB),
        "count": len(chosen),
        "limit": max_terms,
        "total_candidates": len(ranked),
        "trimmed": trimmed,
        "configured_file": _parakeet_configured_file(),
    }


def parakeet_vocab_status(max_terms: int = PARAKEET_MAX_TERMS) -> dict:
    configured = _parakeet_configured_file()
    global_count = len(read_vocab(""))
    if PARAKEET_VOCAB.is_file():
        pk_count = sum(1 for l in PARAKEET_VOCAB.read_text(encoding="utf-8", errors="replace").splitlines()
                       if l.strip() and not l.lstrip().startswith("#"))
    else:
        pk_count = min(global_count, max_terms)
    return {
        "file": str(PARAKEET_VOCAB),
        "count": pk_count,
        "limit": max_terms,
        "total_vocab": global_count,
        "trimmed": max(0, global_count - max_terms),
        "configured_file": configured,
    }


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
    # Atomic: Studio watches this file and must never read half of it.
    tmp = CANDIDATES.with_suffix(".tmp")
    tmp.write_text(json.dumps(cands, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(CANDIDATES)


def is_hint_only(heard: str, meant: str) -> bool:
    v = load_candidates().get(f"{norm(heard)}\t{meant}")
    return isinstance(v, dict) and bool(v.get("hint"))


def note_hints(swaps: list[str], screen_terms: str = "", via: str = "") -> list[tuple[str, str]]:
    """Track hand fixes from English words to names (hint_candidate) as
    candidates flagged hint. Never learned without the user saying so."""
    existing = load()
    cands = load_candidates()
    noted = []
    for swap in swaps:
        raw_h, _, raw_m = swap.partition(" -> ")
        h = norm(raw_h)
        if not raw_m or h in existing or is_safe_auto_candidate(raw_h, raw_m, screen_terms):
            continue
        m = hint_candidate(raw_h, raw_m)
        if not m:
            continue
        key = f"{h}\t{m}"
        prev = cands.get(key)
        if isinstance(prev, dict) and not prev.get("hint"):
            continue
        n, asked = _cand_view(prev)
        rec = {"n": n + 1, "asked": asked, "hint": True}
        if via:
            rec["via"] = via
        cands[key] = rec
        noted.append((h, m))
    if noted:
        save_candidates(cands)
    return noted


def remove_replacement(heard: str) -> bool:
    """Take `heard` out of config.toml's [text] replacements (an undo of
    sync_replacements, which only ever adds)."""
    if not CONFIG_TOML.exists():
        return False
    text = CONFIG_TOML.read_text(encoding="utf-8", errors="replace")
    key = re.escape(norm(heard).replace("\\", "\\\\").replace('"', '\\"'))
    entry = r'"' + key + r'"\s*=\s*"(?:[^"\\]|\\.)*"'
    # Inline table: the entry and one comma next to it.
    updated = re.sub(r",[ \t]*" + entry + r"|" + entry + r"[ \t]*,?[ \t]*\n?", "", text, count=1, flags=re.I)
    if updated == text:
        return False
    tmp = CONFIG_TOML.with_suffix(".tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(CONFIG_TOML)
    return True


def accept_pair(heard: str, meant: str, undo=None, source: str = "user", scope: str = "") -> str | None:
    """Learn (heard -> meant) as the user's own fix; why not, if refused.
    A fix from an English word (is_hint_only) is kept as a hint only."""
    hint = is_hint_only(heard, meant)
    why = remember(heard, meant, "hint" if hint else source, undo, scope=scope or None)
    if why:
        return why
    _add_vocab_if_distinctive(meant, scope=scope)
    sync_replacements()
    cands = load_candidates()
    for k in [k for k in cands if norm(k.split("\t", 1)[0]) == norm(heard)]:
        cands.pop(k, None)
    save_candidates(cands)
    return None


def unasked(swaps: list[str], screen_terms: str = "") -> list[tuple[str, str]]:
    """Pending candidates among `swaps` the user has not been asked about;
    marks them asked, so each is offered once."""
    cands = load_candidates()
    fresh = []
    for swap in swaps:
        raw_h, _, raw_m = swap.partition(" -> ")
        canonical = raw_m and (is_safe_auto_candidate(raw_h, raw_m, screen_terms)
                               or hint_candidate(raw_h, raw_m))
        key = f"{norm(raw_h)}\t{canonical}"
        if canonical and key in cands:
            n, asked = _cand_view(cands[key])
            if not asked:
                cands[key] = {**cands[key], "n": n, "asked": True} if isinstance(cands[key], dict) else {"n": n, "asked": True}
                fresh.append((norm(raw_h), canonical))
    if fresh:
        save_candidates(cands)
    return fresh


def tally(swaps: list[str], screen_terms: str = "", source: str = "auto", scope: str = "", via: str = "") -> list[tuple[str, str]]:
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
            if not remember(h, canonical, source, scope=scope or None):
                cands.pop(key, None)
                promoted.append((h, canonical))
        else:
            rec = {"n": n, "asked": asked}
            if via:
                rec["via"] = via
            if scope:
                rec["scope"] = normalize_scope(scope)
            cands[key] = rec
    if changed:
        save_candidates(cands)
    if promoted:
        for _h, m in promoted:
            _add_vocab_if_distinctive(m, scope=scope)
        sync_replacements()
        notify("Learned" if len(promoted) == 1 else f"Learned {len(promoted)} fixes",
               "\n".join(f"{h} → {m}" for h, m in promoted),
               hints={"x-kuwai-calm": "1", "x-kuwai-group": "shuush-learn",
                      "x-kuwai-status": "Seen twice", "x-kuwai-tone": "positive"})
    return promoted


def sync_replacements() -> int:
    """Sync non-ambiguous global sure-name corrections into config.toml [text] replacements.

    Skips hint-only, demoted, and per-app-scoped rules (global voxtype replacements
    apply in every app regardless of profile).
    """
    if not CONFIG_TOML.exists():
        return 0
    text = CONFIG_TOML.read_text(encoding="utf-8", errors="replace")
    common = _common_words()
    learned: dict[str, str] = {}
    for h, (meant, src, _n, scope) in sorted(_load_rows().items()):
        if h in AMBIGUOUS or not h or not meant or src == "hint" or scope:
            continue
        toks = words(h)
        if len(toks) == 1 and toks[0] in common:
            continue
        learned[h] = meant
    if not learned:
        return 0

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    header_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
    m_inline = re.search(r"^(replacements\s*=\s*\{)([^}]*)(\})", text, re.M)
    m_sub = re.search(r"^\s*\[text\.replacements\]\s*(?:#.*)?$", text, re.M)

    added = 0
    if m_sub:
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        start = next(i for i, l in enumerate(lines) if header_re.match(l) and header_re.match(l).group(1).strip() == "text.replacements")
        end = next((i for i in range(start + 1, len(lines)) if header_re.match(lines[i])), len(lines))
        body = lines[start + 1 : end]
        existing: dict[str, int] = {}
        for idx, l in enumerate(body):
            km = re.match(r'^\s*"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"', l)
            if km:
                existing[km.group(1).lower()] = idx
        insert_at = len(body)
        while insert_at > 0 and not body[insert_at - 1].strip():
            insert_at -= 1
        extra: list[str] = []
        for h, meant in learned.items():
            entry = f'"{esc(h)}" = "{esc(meant)}"\n'
            if h in existing:
                body[existing[h]] = entry
            else:
                added += 1
                extra.append(entry)
        body[insert_at:insert_at] = extra
        updated = "".join(lines[: start + 1] + body + lines[end:])
    elif m_inline:
        current: dict[str, str] = {}
        for km in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"', m_inline.group(2)):
            current[km.group(1).lower()] = km.group(2)
        for h, meant in learned.items():
            if h not in current:
                added += 1
            current[h] = meant
        inner = ", ".join(f'"{esc(k)}" = "{esc(v)}"' for k, v in sorted(current.items()))
        new_line = f"{m_inline.group(1)} {inner} {m_inline.group(3)}"
        updated = text[:m_inline.start()] + new_line + text[m_inline.end():]
    else:
        added = len(learned)
        inner = ", ".join(f'"{esc(k)}" = "{esc(v)}"' for k, v in sorted(learned.items()))
        repl_line = f"replacements = {{ {inner} }}\n"
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        text_hdr = next((i for i, l in enumerate(lines) if header_re.match(l) and header_re.match(l).group(1).strip() == "text"), None)
        if text_hdr is not None:
            end = next((i for i in range(text_hdr + 1, len(lines)) if header_re.match(lines[i])), len(lines))
            ins = end
            while ins > text_hdr + 1 and not lines[ins - 1].strip():
                ins -= 1
            lines.insert(ins, repl_line)
            updated = "".join(lines)
        else:
            sep = "\n" if lines and lines[-1].strip() else ""
            updated = "".join(lines) + f"{sep}[text]\n{repl_line}"

    if updated != text:
        try:
            tomllib.loads(updated)
        except tomllib.TOMLDecodeError:
            return 0
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
    # Per clean word, the raw span behind it: a replace maps every word it
    # wrote to all the raw words it replaced ("hyper land" -> "hyperlink").
    span: list[tuple[int, int] | None] = [None] * len(clean)
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(a=raw, b=clean, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(b2 - b1):
                span[b1 + k] = (a1 + k, a1 + k + 1)
        elif op == "replace":
            for k in range(b1, b2):
                span[k] = (a1, a2)
    hits = [x for x in span[j1:j2] if x is not None]
    return raw[min(x[0] for x in hits):max(x[1] for x in hits)] if hits else clean[j1:j2]


def fix() -> list[tuple[str, str]]:
    last = last_dictation()
    fixed = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True).stdout
    if not last or not fixed.strip():
        return []
    raw, clean, new = (words(norm(s)) for s in (last["raw"], last["clean"], fixed))
    # Words are compared lower-cased, but MEANT keeps the clipboard's
    # spelling ("Next.js", "Groq") by reading it from the corrected text.
    cased = fixed.split()
    existing = _load_rows()
    learned = []
    for op, j1, j2, k1, k2 in difflib.SequenceMatcher(a=clean, b=new, autojunk=False).get_opcodes():
        if op != "replace" or j2 - j1 > MAX_SPAN or k2 - k1 > MAX_SPAN:
            continue
        heard = " ".join(heard_for(raw, clean, j1, j2))
        clean_span = " ".join(clean[j1:j2])
        meant = " ".join(cased[k1:k2]) if len(cased) == len(new) else " ".join(new[k1:k2])
        meant = meant.strip(".,;:!?")
        h_key = norm(heard)
        # If an existing rule turned heard -> clean_span and the user changed it back, record an undo!
        rule = existing.get(h_key)
        if rule and norm(rule[0]) == norm(clean_span) and norm(meant) != norm(clean_span):
            record_undo(h_key, rule[0])
            if norm(meant) == h_key:
                continue
        if h_key != norm(meant):
            why = remember(heard, meant, "user")
            if why:
                learned.append((heard, f"(skipped, {why})"))
            else:
                _add_vocab_if_distinctive(meant)
                learned.append((heard, meant))
    if any(not m.startswith("(skipped") for _, m in learned):
        sync_replacements()
    return learned


def stats() -> dict:
    """Summarize provenance, health, pending inbox, and vocabulary budgets."""
    entries = load_entries(include_removed=True)
    active_entries = [e for e in entries if e["status"] != "removed"]
    removed_entries = [e for e in entries if e["status"] == "removed"]

    by_source: dict[str, int] = {"seed": 0, "user fix": 0, "edit-watch": 0, "accepted hint": 0, "auto": 0}
    by_status: dict[str, int] = {"active": 0, "hint": 0, "demoted": 0, "removed": len(removed_entries)}
    by_scope: dict[str, int] = {}
    for e in active_entries:
        lbl = SOURCE_LABELS.get(e["origin_source"], e["origin_source"])
        by_source[lbl] = by_source.get(lbl, 0) + 1
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        sc = e["scope_label"]
        by_scope[sc] = by_scope.get(sc, 0) + 1

    total_applied = sum(e["applied"] for e in entries)
    total_undone = sum(e["undone"] for e in entries)
    top_applied = sorted((e for e in active_entries if e["applied"] > 0), key=lambda e: (-e["applied"], e["heard"]))[:10]
    demoted = [e for e in active_entries if e["demoted"]]
    undone_rules = sorted((e for e in active_entries if e["undone"] > 0), key=lambda e: (-e["undone"], e["heard"]))

    fix_cands = [k for k in load_candidates() if "\t" in k and k.split("\t", 1)[0].strip()]
    vocab_cands = load_vocab_candidates()
    scoped_vocab = {sc: len(read_vocab(sc)) for sc in list_vocab_scopes()}

    return {
        "total_rules": len(active_entries),
        "by_source": by_source,
        "by_status": by_status,
        "by_scope": by_scope,
        "total_applied": total_applied,
        "total_undone": total_undone,
        "top_applied": [{"heard": e["heard"], "meant": e["meant"], "source": e["source_label"],
                         "applied": e["applied"], "undone": e["undone"]} for e in top_applied],
        "demoted_rules": [{"heard": e["heard"], "meant": e["meant"], "source": e["source_label"],
                           "applied": e["applied"], "undone": e["undone"]} for e in demoted],
        "undone_rules": [{"heard": e["heard"], "meant": e["meant"], "status": e["status"],
                          "applied": e["applied"], "undone": e["undone"]} for e in undone_rules],
        "removed_rules": [{"heard": e["heard"], "meant": e["meant"], "source": e["source_label"],
                           "applied": e["applied"], "undone": e["undone"], "removed_at": e.get("removed_at", "")}
                          for e in removed_entries],
        "pending_fixes": len(fix_cands),
        "pending_vocab": len(vocab_cands),
        "vocab": {
            "global": len(read_vocab("")),
            "scoped": scoped_vocab,
            "parakeet": parakeet_vocab_status(),
        },
        "rules": entries,
    }


def format_stats(data: dict) -> str:
    src = data["by_source"]
    st = data["by_status"]
    pk = data["vocab"]["parakeet"]
    lines = [
        f"Shuush learning stats ({data['total_rules']} rules)",
        f"  By source:    seed={src.get('seed', 0)}  user fix={src.get('user fix', 0)}  "
        f"edit-watch={src.get('edit-watch', 0)}  accepted hint={src.get('accepted hint', 0)}  auto={src.get('auto', 0)}",
        f"  By health:    active={st.get('active', 0)}  hint={st.get('hint', 0)}  "
        f"demoted={st.get('demoted', 0)}  removed={st.get('removed', 0)}",
        f"  Activity:     applied {data['total_applied']}×  undone {data['total_undone']}×",
        f"  Inbox:        {data['pending_fixes']} pending fix(es), {data['pending_vocab']} vocabulary suggestion(s)",
        f"  Vocabulary:   {data['vocab']['global']} global"
        + ("".join(f", {n} {sc}" for sc, n in sorted(data["vocab"]["scoped"].items())) if data["vocab"]["scoped"] else "")
        + f"  (Parakeet rescoring: {pk['count']}/{pk['limit']})",
    ]
    if data["top_applied"]:
        lines.append("  Most helpful rules:")
        for r in data["top_applied"][:5]:
            und = f", undone {r['undone']}×" if r["undone"] else ""
            lines.append(f"    {r['heard']} -> {r['meant']}  ({r['source']}, applied {r['applied']}×{und})")
    if data["demoted_rules"]:
        lines.append("  Demoted to context hint:")
        for r in data["demoted_rules"]:
            lines.append(f"    {r['heard']} -> {r['meant']}  (applied {r['applied']}×, undone {r['undone']}×)")
    if data["removed_rules"]:
        lines.append("  Removed after repeated undos:")
        for r in data["removed_rules"][:5]:
            lines.append(f"    {r['heard']} -> {r['meant']}  (undone {r['undone']}×)")
    return "\n".join(lines)


def notify(title: str, body: str, actions: list[tuple[str, str]] | None = None,
           wait_s: float = 0, hints: dict[str, str] | None = None, transient: bool = False) -> str:
    """Show a notification. With `actions` (key, label) and `wait_s`, wait
    that long for a click and return its key ("" when closed or timed out).
    `hints` are Kuwai's x-kuwai-* (notch, group, status line); `transient`
    keeps it out of the notification history."""
    kuwai = next(
        (str(p) for p in (
            Path(os.environ.get("KUWAI_ROOT", "")) / "bin/kuwai-notify",
            Path.home() / ".local/share/kuwai/bin/kuwai-notify",
            Path.home() / "Projects/rice/bin/kuwai-notify",
        ) if p.is_file() and os.access(p, os.X_OK)),
        None,
    )
    if kuwai:
        cmd = [kuwai, "-a", "Shuush", "-i", "audio-input-microphone"]
        for key, label in actions or []:
            cmd += ["-A", f"{key}={label}"]
        for key, value in (hints or {}).items():
            cmd += ["-H", f"{key}={value}"]
        if transient:
            cmd.append("-t")
        waiting = bool(actions and wait_s)
        if waiting:
            cmd.append("-w")
        cmd += [title, body]
        try:
            # Its own session: a wait that times out takes its D-Bus monitor
            # with it instead of leaving it running.
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        except OSError:
            return ""
        if not waiting:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return ""
        # The click's key is printed as soon as it happens; take it then,
        # not when the notifier exits.
        import select
        choice = ""
        ready, _, _ = select.select([proc.stdout], [], [], wait_s)
        if ready:
            choice = proc.stdout.readline().strip()
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            pass
        proc.wait()
        return choice
    # Without Kuwai: straight over D-Bus (notify-send can break on a libnotify
    # mismatch), without buttons.
    try:
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                        "--object-path", "/org/freedesktop/Notifications",
                        "--method", "org.freedesktop.Notifications.Notify",
                        "Shuush", "0", "audio-input-microphone", title, body, "[]", "{}", "5000"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _parse_flag(args: list[str], flag: str) -> tuple[str | None, list[str]]:
    val = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == flag and i + 1 < len(args):
            val = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    return val, rest


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        for e in sorted(load_entries(), key=lambda x: (x["origin_source"], x["heard"])):
            sc = f" [{e['scope_label']}]" if e["scope"] else ""
            hlth = f" applied={e['applied']} undone={e['undone']}" if (e["applied"] or e["undone"]) else ""
            print(f"{e['source_label']:13} {e['count']:3}  {e['heard']} -> {e['meant']}{sc}{hlth}")
    elif cmd == "stats":
        data = stats()
        if "--json" in argv[2:]:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(format_stats(data))
    elif cmd == "harvest":
        import shuush_vocab_harvest as vh
        return vh.main(argv[2:])
    elif cmd == "replay":
        import shuush_learn_replay as lr
        return lr.main(argv[2:])
    elif cmd == "pending":
        rows = []
        for k, v in load_candidates().items():
            heard, _, meant = k.partition("\t")
            if not heard.strip():
                continue
            n, _asked = _cand_view(v)
            rows.append((n, heard, meant))
        vcands = load_vocab_candidates()
        if not rows and not vcands:
            print("nothing waiting")
        for n, heard, meant in sorted(rows, key=lambda r: (-r[0], r[1])):
            print(f"{n:3}  {heard} -> {meant}")
        if vcands:
            print("vocabulary suggestions:")
            for v in sorted(vcands.values(), key=lambda x: (-float(x.get("score", 0)), str(x.get("term", "")))):
                sc = f" [{scope_label(v.get('scope', ''))}]" if v.get("scope") else ""
                print(f"  + {v.get('term', '')}{sc}  ({v.get('summary', '')})")
    elif cmd == "add":
        scope, rest = _parse_flag(argv[2:], "--scope")
        if len(rest) != 2:
            print("usage: voxtype-learn add HEARD MEANT [--scope SCOPE]", file=sys.stderr)
            return 2
        why = remember(rest[0], rest[1], "user", scope=scope or "")
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return 1
        sc = f" [{scope_label(scope)}]" if scope else ""
        print(f"learned: {norm(rest[0])} -> {rest[1]}{sc}")
    elif cmd == "accept" and len(argv) == 3:
        want = norm(argv[2])
        cands = load_candidates()
        hit = [k for k in cands if k.split("\t", 1)[0] == want]
        if not hit:
            print("not found", file=sys.stderr)
            return 1
        heard, _, meant = hit[0].partition("\t")
        hint = is_hint_only(heard, meant)
        why = accept_pair(heard, meant)
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return 1
        print(f"learned{' as a hint' if hint else ''}: {heard} -> {meant}")
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
    elif cmd == "accept-vocab":
        scope, rest = _parse_flag(argv[2:], "--scope")
        if len(rest) != 1:
            print("usage: voxtype-learn accept-vocab TERM [--scope SCOPE]", file=sys.stderr)
            return 2
        if not accept_vocab_candidate(rest[0], scope=scope or ""):
            print("not added (already present or empty)", file=sys.stderr)
            return 1
        sc = f" [{scope_label(scope)}]" if scope else ""
        print(f"accepted vocabulary: {rest[0]}{sc}")
    elif cmd == "reject-vocab" and len(argv) == 3:
        reject_vocab_candidate(argv[2])
        print(f"rejected vocabulary: {argv[2]}")
    elif cmd == "forget" and len(argv) == 3:
        if not forget(argv[2]):
            print("not found", file=sys.stderr)
            return 1
        remove_replacement(argv[2])
    elif cmd == "sync-toml":
        n = sync_replacements()
        print(f"synced {n} new sure-name replacement(s) to {CONFIG_TOML}")
    elif cmd == "sync-parakeet":
        res = sync_parakeet_vocab()
        print(f"synced {res['count']}/{res['limit']} terms to {res['file']} (trimmed {res['trimmed']})")
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
