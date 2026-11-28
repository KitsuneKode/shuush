#!/usr/bin/env python3
"""Offline read-only replay check of learning rules against dictation history.

Never writes to history.jsonl, corrections.tsv, vocabulary.txt, or any user
state. Measures how many past mishearings the current rules would fix and how
many ordinary words they would wrongly change (false positives).
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voxtype_learn as learn
import voxtype_pp_gate as gate


def _load_corrections_file(path: Path) -> list[tuple[str, str, str, str]]:
    """Return [(heard, meant, source, scope), ...] from a corrections.tsv file."""
    rows: list[tuple[str, str, str, str]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 2 and f[0].strip() and f[1].strip():
            heard = learn.norm(f[0])
            meant = f[1].strip()
            src = (f[2].strip() if len(f) >= 3 and f[2].strip() else "user")
            scope = learn.normalize_scope(f[4]) if len(f) >= 5 else ""
            rows.append((heard, meant, src, scope))
    return rows


def _load_vocab_terms(vocab_path: Path | None, vocab_dir: Path | None, mode: str, cls: str, use_scopes: bool) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if vocab_path and vocab_path.is_file():
        for line in vocab_path.read_text(encoding="utf-8", errors="replace").splitlines():
            t = line.split("#", 1)[0].strip()
            if t and len(re.sub(r"[^0-9A-Za-z]", "", t)) >= 3 and t.lower() not in seen:
                out.append(t)
                seen.add(t.lower())
    if use_scopes and vocab_dir and vocab_dir.is_dir():
        for p in sorted(vocab_dir.glob("*.txt")):
            if learn.scope_matches(p.stem, mode=mode, cls=cls):
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    t = line.split("#", 1)[0].strip()
                    if t and len(re.sub(r"[^0-9A-Za-z]", "", t)) >= 3 and t.lower() not in seen:
                        out.append(t)
                        seen.add(t.lower())
    return out


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    parts = [re.escape(w) for w in phrase.split()]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])", re.I)


def replay(
    history_path: Path | None = None,
    corrections_path: Path | None = None,
    vocab_path: Path | None = None,
    vocab_dir: Path | None = None,
    use_scopes: bool = True,
) -> dict:
    """Replay history.jsonl read-only against current rules and vocabulary."""
    hp = history_path or learn.HISTORY
    cp = corrections_path or learn.TSV
    vp = vocab_path or learn.VOCAB_FILE
    vd = vocab_dir if vocab_dir is not None else learn.VOCAB_DIR

    all_rules = _load_corrections_file(cp)
    common = learn._common_words()
    patterns = {h: _phrase_pattern(h) for h, _m, _s, _sc in all_rules if h}
    meant_patterns = {m.lower(): _phrase_pattern(m) for _h, m, _s, _sc in all_rules if m}

    takes = 0
    total_words = 0
    confirmed_fixes = 0
    new_fixes = 0
    hints_helped = 0
    vocab_fixes = 0
    false_positives = 0
    fp_examples: list[dict] = []
    per_rule: dict[str, dict] = {}

    if hp.is_file():
        lines = hp.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = []

    for idx, line in enumerate(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("via") == "private":
            continue
        raw = str(row.get("raw") or "")
        clean = str(row.get("clean") or "")
        if not raw.strip():
            continue
        takes += 1
        total_words += len(raw.split())
        mode = str(row.get("mode") or "default")
        cls = str(row.get("app_class") or "")

        active_rewrite: list[tuple[str, str]] = []
        active_hints: list[tuple[str, str]] = []
        for h, m, src, scope in all_rules:
            if use_scopes and scope and not learn.scope_matches(scope, mode=mode, cls=cls):
                continue
            if src == "hint" or h in gate.AMBIGUOUS:
                active_hints.append((h, m))
            else:
                active_rewrite.append((h, m))

        terms = _load_vocab_terms(vp, vd, mode, cls, use_scopes)
        after_hard = gate.hard_apply(raw, pairs=active_rewrite, vocab=terms)

        # Evaluate deterministic hard_apply rewrites
        for h, m in active_rewrite:
            pat = patterns.get(h)
            if not pat:
                continue
            matches = list(pat.finditer(raw))
            if not matches:
                continue
            # Check that hard_apply actually rewrote at least one occurrence (not shielded)
            if after_hard == raw:
                continue
            mpat = meant_patterns.get(m.lower())
            in_clean = bool(mpat and mpat.search(clean)) or (m.lower() in clean.lower())
            still_heard_in_clean = bool(pat.search(clean))
            htoks = learn.words(h)
            is_ordinary_english = all(t in common for t in htoks) and len(htoks) == 1

            rstat = per_rule.setdefault(h, {
                "heard": h, "meant": m, "hits": 0,
                "confirmed_fixes": 0, "new_fixes": 0, "false_positives": 0,
            })
            rstat["hits"] += len(matches)
            if in_clean:
                confirmed_fixes += len(matches)
                rstat["confirmed_fixes"] += len(matches)
            elif is_ordinary_english and still_heard_in_clean:
                false_positives += len(matches)
                rstat["false_positives"] += len(matches)
                if len(fp_examples) < 20:
                    mo = matches[0]
                    fp_examples.append({
                        "take_index": idx,
                        "kind": "rewrite",
                        "heard": h,
                        "meant": m,
                        "snippet": raw[max(0, mo.start() - 30):min(len(raw), mo.end() + 30)],
                    })
            else:
                # Either non-English mishearing or multi-word ASR slip missed in historical clean
                new_fixes += len(matches)
                rstat["new_fixes"] += len(matches)

        # Evaluate context hints (never cause blind false positives)
        for h, m in active_hints:
            pat = patterns.get(h)
            if pat and pat.search(raw):
                mpat = meant_patterns.get(m.lower())
                if (mpat and mpat.search(clean)) or (m.lower() in clean.lower()):
                    hints_helped += 1

        # Evaluate vocabulary.txt finish() pass on raw by matching full vocab terms
        after_fin = gate.finish(after_hard, terms=terms)
        if after_fin != after_hard:
            for term in terms:
                letters = re.sub(r"[^0-9A-Za-z]", "", term)
                if not letters:
                    continue
                body = r"[\s-]?".join(re.escape(c) for c in letters)
                vpat = re.compile(r"(?<![0-9A-Za-z])" + body + r"(?![0-9A-Za-z])", re.I)
                for m_raw in vpat.finditer(after_hard):
                    got = m_raw.group(0)
                    if got == term:
                        continue
                    # Did finish() actually rewrite this span to `term`?
                    if term not in after_fin:
                        continue
                    is_single_common = (" " not in term and "-" not in term and got.lower() in common
                                        and got.replace(" ", "").replace("-", "") == got)
                    if is_single_common and got in clean and term not in clean:
                        false_positives += 1
                        if len(fp_examples) < 20:
                            fp_examples.append({
                                "take_index": idx,
                                "kind": "vocab",
                                "heard": got,
                                "meant": term,
                                "snippet": after_hard[max(0, m_raw.start() - 25):min(len(after_hard), m_raw.end() + 25)],
                            })
                    else:
                        vocab_fixes += 1

    rule_list = sorted(per_rule.values(), key=lambda r: (-r["hits"], r["heard"]))
    return {
        "history": str(hp),
        "corrections": str(cp),
        "takes": takes,
        "words": total_words,
        "rules_loaded": len(all_rules),
        "rules_fired": len(per_rule),
        "confirmed_fixes": confirmed_fixes,
        "new_fixes": new_fixes,
        "total_rule_fixes": confirmed_fixes + new_fixes,
        "vocab_fixes": vocab_fixes,
        "hints_helped": hints_helped,
        "false_positives": false_positives,
        "false_positive_rate_per_word": round(false_positives / max(1, total_words), 6),
        "false_positive_rate_per_take": round(false_positives / max(1, takes), 6),
        "per_rule": rule_list,
        "false_positive_examples": fp_examples,
    }


def format_replay(report: dict) -> str:
    lines = [
        f"Offline replay ({report['takes']} takes, {report['words']} words, read-only)",
        f"  Rules loaded:        {report['rules_loaded']} ({report['rules_fired']} fired)",
        f"  Mishearings fixed:   {report['total_rule_fixes']} ({report['confirmed_fixes']} confirmed in history, {report['new_fixes']} newly caught)",
        f"  Vocabulary fixes:    {report['vocab_fixes']}",
        f"  Context hints ready: {report['hints_helped']}",
        f"  False positives:     {report['false_positives']} ({report['false_positive_rate_per_word'] * 100:.4f}% of words, {report['false_positive_rate_per_take'] * 100:.2f}% of takes)",
    ]
    if report["per_rule"]:
        lines.append("  Top firing rules:")
        for r in report["per_rule"][:8]:
            fp_note = f", {r['false_positives']} FP" if r["false_positives"] else ""
            lines.append(f"    {r['heard']} -> {r['meant']}: {r['hits']}× ({r['confirmed_fixes']} confirmed, {r['new_fixes']} new{fp_note})")
    if report["false_positive_examples"]:
        lines.append("  False positive samples:")
        for ex in report["false_positive_examples"][:5]:
            lines.append(f"    [{ex['kind']}] {ex['heard']} -> {ex['meant']}: {ex['snippet']!r}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    hp = None
    cp = None
    vp = None
    for i, a in enumerate(argv):
        if a == "--history" and i + 1 < len(argv):
            hp = Path(argv[i + 1])
        elif a == "--corrections" and i + 1 < len(argv):
            cp = Path(argv[i + 1])
        elif a == "--vocab" and i + 1 < len(argv):
            vp = Path(argv[i + 1])
    rep = replay(history_path=hp, corrections_path=cp, vocab_path=vp)
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(format_replay(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
