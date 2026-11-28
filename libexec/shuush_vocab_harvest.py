#!/usr/bin/env python3
"""Harvest candidate vocabulary terms from the owner's workspace with consent.

Never writes to vocabulary.txt. Candidates are saved to
$XDG_STATE_HOME/voxtype/vocab-candidates.json and wait for explicit approval in
Studio's Learning Inbox or `shuush learn accept-vocab`.

Sources:
  - Git repository names and local branch names under ~/Projects
  - Kitty tab titles (`kitty @ ls` when remote control is available)
  - Window titles (Hyprland / Niri / Sway clients, skipping private windows)
  - Frequent capitalised / distinctive terms in cleaned dictation history
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voxtype_learn as learn
import voxtype_pp_gate as gate

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "target", "dist", "build", "out",
    ".venv", "venv", "__pycache__", ".worktrees", ".next", ".turbo", ".cache",
    "vendor", "Pods", "DerivedData",
})
GENERIC_TOKENS = frozenset({
    "main", "master", "dev", "develop", "development", "feat", "feature", "fix",
    "bugfix", "hotfix", "chore", "docs", "doc", "test", "tests", "spec", "wip",
    "release", "merge", "branch", "origin", "head", "pull", "push", "draft",
    "refactor", "perf", "style", "ci", "cd", "build", "deps", "bump", "update",
    "misc", "temp", "tmp", "demo", "sample", "example", "examples", "template",
    "templates", "starter", "sandbox", "playground", "notes", "archive", "backup",
    "old", "new", "final", "copy", "local", "remote", "server", "client", "app",
    "apps", "web", "site", "website", "api", "backend", "frontend", "mobile",
    "desktop", "cli", "tool", "tools", "utils", "common", "shared", "core",
    "lib", "libs", "pkg", "packages", "src", "source", "bin", "scripts", "config",
    "configs", "settings", "assets", "public", "static", "styles", "themes",
    "plugins", "extensions", "skills", "internals", "assignments", "portfolio",
    "projects", "home", "users", "downloads", "documents", "browser", "window",
    "untitled", "tab", "tabs", "zsh", "bash", "fish", "sh", "tmux", "nvim",
    "vim", "ssh", "sudo", "python", "python3", "node", "bun", "npm", "cargo",
    "git", "http", "https", "www", "com", "org", "net", "io", "dev", "app",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "today", "tomorrow",
    "yesterday", "also", "then", "when", "what", "where", "which", "while",
    "here", "there", "this", "that", "these", "those", "just", "only", "still",
    "even", "much", "more", "most", "some", "many", "very", "really", "okay",
    "alright", "yeah", "sure", "please", "thanks", "thank", "hello", "hey",
    "sorry", "actually", "basically", "probably", "maybe", "number", "point",
    "item", "bullet", "google", "github", "gmail", "youtube", "reddit", "twitter",
})
_PRIVATE_TITLE_SKIP = re.compile(
    r"(?i)\b(password|passphrase|sign[ -]?in|log[ -]?in|2fa|otp|verification code|secret|token|private)\b"
)
_HEX_RE = re.compile(r"^[0-9a-f]{6,}$", re.I)
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*$", re.I)
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_.+#-]{1,36}\b")
_MULTI_PROPER_RE = re.compile(r"\b([A-Z][A-Za-z0-9+#.-]{1,20}(?:\s+[A-Z][A-Za-z0-9+#.-]{1,20}){1,2})\b")


def _clean_term(raw: str) -> str:
    return raw.strip(".,;:!?\"'()[]{}<>|/\\~`")


def _is_good_candidate(term: str, common: set[str], allow_lowercase_ident: bool = False) -> bool:
    t = _clean_term(term)
    if not t or len(t) < 3 or len(t) > 36:
        return False
    low = t.lower()
    if low in GENERIC_TOKENS or low in learn.STOPWORDS:
        return False
    if _HEX_RE.match(t) or _VERSION_RE.match(t):
        return False
    if not any(c.isalpha() for c in t):
        return False
    if "://" in t or "/" in t or "\\" in t or "@" in t:
        return False
    if len(re.sub(r"[^A-Za-z0-9]", "", t)) < 3:
        return False
    # Reject tokens that look like random passwords or opaque hashes.
    if gate.looks_like_password(t):
        return False
    words = t.split()
    if len(words) > 3:
        return False
    if len(words) >= 2:
        # Multi-word proper name: at least one word must be non-generic and not all common stopwords.
        if any(w.lower() in learn.STOPWORDS for w in words):
            return False
        if all(w.lower() in GENERIC_TOKENS for w in words):
            return False
        if all(w.lower() in common for w in words):
            # Allow brand-like 2-word terms with distinctive digits/punctuation (e.g. "T3 Code")
            if not any(any(c.isdigit() or c in ".+#" for c in w) or (len(w) <= 3 and w.isupper()) for w in words):
                return False
        return True
    # Single-token candidate
    has_internal_cap = any(c.isupper() for c in t[1:]) and any(c.islower() for c in t)
    has_special = any(c in ".-_+#" for c in t[1:-1])
    has_digit = any(c.isdigit() for c in t)
    is_acronym = t.isupper() and 2 <= len(t) <= 6
    if has_internal_cap or has_special or has_digit or is_acronym:
        # Still skip hyphenated common English phrases like "non-profit" if all parts are common/generic.
        parts = [p for p in re.split(r"[-_.]+", low) if p]
        if len(parts) >= 2 and all(p in common or p in GENERIC_TOKENS for p in parts) and not has_internal_cap and not has_digit:
            return False
        return True
    if low in common:
        return False
    if t[0].isupper():
        return True
    return allow_lowercase_ident and len(t) >= 4


def infer_scope(term: str, sources: set[str]) -> str:
    """Suggest 'terminal' for code/CLI/repo identifiers where global use could over-match."""
    t = term.strip()
    if not t:
        return ""
    if "_" in t or (t.islower() and "-" in t) or (t.isupper() and len(t) <= 3):
        return "terminal"
    if t.islower() and sources <= {"project", "branch", "kitty"}:
        return "terminal"
    return ""


def _read_git_branches(git_path: Path) -> list[str]:
    """Read HEAD and local branch names directly without spawning git subprocesses."""
    git_dir = git_path
    if git_path.is_file():
        try:
            first = git_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if first.startswith("gitdir:"):
                raw = first.split(":", 1)[1].strip()
                git_dir = Path(raw) if os.path.isabs(raw) else (git_path.parent / raw).resolve()
        except (OSError, IndexError):
            return []
    branches: list[str] = []
    head = git_dir / "HEAD"
    try:
        line = head.read_text(encoding="utf-8", errors="replace").strip()
        if line.startswith("ref: refs/heads/"):
            branches.append(line.removeprefix("ref: refs/heads/"))
    except OSError:
        pass
    heads_dir = git_dir / "refs" / "heads"
    if heads_dir.is_dir():
        try:
            for root, _dirs, files in os.walk(heads_dir):
                for fn in files:
                    rel = (Path(root) / fn).relative_to(heads_dir).as_posix()
                    if rel not in branches:
                        branches.append(rel)
                    if len(branches) >= 12:
                        return branches
        except OSError:
            pass
    return branches


def _branch_terms(branch: str, common: set[str]) -> list[str]:
    out: list[str] = []
    for seg in re.split(r"[/]+", branch):
        seg = seg.strip()
        if not seg or seg.lower() in GENERIC_TOKENS:
            continue
        # Try whole hyphenated segment and individual non-generic sub-tokens
        if _is_good_candidate(seg, common, allow_lowercase_ident=True):
            out.append(seg)
        for tok in re.split(r"[-_]+", seg):
            if _is_good_candidate(tok, common, allow_lowercase_ident=True) and tok not in out:
                out.append(tok)
    return out


def harvest_projects(projects_dir: Path | None = None, max_depth: int = 3) -> list[dict]:
    """Find candidate terms from git repository names and branch names under ~/Projects."""
    base = projects_dir
    if base is None:
        env = os.environ.get("SHUUSH_PROJECTS_DIR", "").strip()
        base = Path(env) if env else (Path.home() / "Projects")
    if not base.is_dir():
        return []
    common = learn._common_words()
    found: list[dict] = []
    seen_repos: set[Path] = set()

    def walk(cur: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        git_marker = cur / ".git"
        if depth > 0 and git_marker.exists():
            if cur not in seen_repos:
                seen_repos.add(cur)
                repo = cur.name
                if _is_good_candidate(repo, common, allow_lowercase_ident=True):
                    found.append({"term": repo, "source": "project", "detail": f"Git repo {repo}"})
                for tok in re.split(r"[-_]+", repo):
                    if tok != repo and _is_good_candidate(tok, common, allow_lowercase_ident=True):
                        found.append({"term": tok, "source": "project", "detail": f"Git repo {repo}"})
                for br in _read_git_branches(git_marker):
                    for bt in _branch_terms(br, common):
                        found.append({"term": bt, "source": "branch", "detail": f"Branch {br} in {repo}"})
            return
        for child in entries:
            if child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            if child.is_dir() and not child.is_symlink():
                walk(child, depth + 1)

    walk(base, 0)
    return found


def _default_kitty_tabs() -> list[str]:
    titles: list[str] = []
    cmds: list[list[str]] = []
    listen = os.environ.get("KITTY_LISTEN_ON", "").strip()
    if listen:
        cmds.append(["kitty", "@", "--to", listen, "ls"])
    else:
        cmds.append(["kitty", "@", "ls"])
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=0.5, stdin=subprocess.DEVNULL)
            if res.returncode != 0 or not res.stdout.strip():
                continue
            data = json.loads(res.stdout)
            if isinstance(data, list):
                for os_win in data:
                    for tab in os_win.get("tabs", []) if isinstance(os_win, dict) else []:
                        t = str(tab.get("title") or "").strip()
                        if t:
                            titles.append(t)
                        for win in tab.get("windows", []) if isinstance(tab, dict) else []:
                            wt = str(win.get("title") or "").strip()
                            if wt and wt != t:
                                titles.append(wt)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return titles


def _extract_title_terms(title: str, common: set[str], allow_lower: bool = False) -> list[str]:
    if not title or _PRIVATE_TITLE_SKIP.search(title):
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Strip obvious path segments so directory clutter does not pollute suggestions.
    cleaned = re.sub(r"(?:~|\.{1,2})?/[\w./-]+", " ", title)
    for m in _MULTI_PROPER_RE.finditer(cleaned):
        cand = _clean_term(m.group(1))
        if _is_good_candidate(cand, common, allow_lowercase_ident=False) and cand.lower() not in seen:
            seen.add(cand.lower())
            out.append(cand)
    for m in _TOKEN_RE.finditer(cleaned):
        cand = _clean_term(m.group(0))
        if _is_good_candidate(cand, common, allow_lowercase_ident=allow_lower) and cand.lower() not in seen:
            seen.add(cand.lower())
            out.append(cand)
    return out


def harvest_kitty_tabs(kitty_runner=None) -> list[dict]:
    """Extract candidate terms from open kitty tab titles."""
    titles = kitty_runner() if kitty_runner is not None else _default_kitty_tabs()
    common = learn._common_words()
    found: list[dict] = []
    for title in titles or []:
        if not isinstance(title, str) or gate.private_reason("kitty", title):
            continue
        for term in _extract_title_terms(title, common, allow_lower=True):
            found.append({"term": term, "source": "kitty", "detail": f"Kitty tab: {title[:50]}"})
    return found


def _default_window_clients() -> list[dict]:
    for cmd, kind in (
        (["hyprctl", "clients", "-j"], "hypr"),
        (["niri", "msg", "-j", "windows"], "niri"),
        (["swaymsg", "-t", "get_tree"], "sway"),
    ):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=0.5, stdin=subprocess.DEVNULL)
            if res.returncode != 0 or not res.stdout.strip():
                continue
            data = json.loads(res.stdout)
            if kind == "hypr" and isinstance(data, list):
                return [{"class": str(w.get("class") or ""), "title": str(w.get("title") or "")} for w in data if isinstance(w, dict)]
            if kind == "niri" and isinstance(data, list):
                return [{"class": str(w.get("app_id") or ""), "title": str(w.get("title") or "")} for w in data if isinstance(w, dict)]
            if kind == "sway" and isinstance(data, dict):
                out: list[dict] = []
                stack = [data]
                while stack:
                    node = stack.pop()
                    if not isinstance(node, dict):
                        continue
                    if node.get("name") and (node.get("app_id") or node.get("window_properties")):
                        cls = node.get("app_id") or (node.get("window_properties") or {}).get("class") or ""
                        out.append({"class": str(cls), "title": str(node.get("name") or "")})
                    stack.extend(node.get("nodes") or [])
                    stack.extend(node.get("floating_nodes") or [])
                return out
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return []


def harvest_window_titles(clients_runner=None) -> list[dict]:
    """Extract candidate terms from open window titles, skipping private windows."""
    clients = clients_runner() if clients_runner is not None else _default_window_clients()
    common = learn._common_words()
    found: list[dict] = []
    for win in clients or []:
        if not isinstance(win, dict):
            continue
        cls = str(win.get("class") or "")
        title = str(win.get("title") or "")
        if not title or gate.private_reason(cls, title):
            continue
        for term in _extract_title_terms(title, common, allow_lower=False):
            found.append({"term": term, "source": "window", "detail": f"Window: {title[:50]}"})
    return found


def harvest_history_terms(history_path: Path | None = None, min_count: int = 2) -> list[dict]:
    """Find frequent capitalised / distinctive terms in cleaned dictation history (read-only)."""
    hp = history_path or learn.HISTORY
    if not hp.is_file():
        return []
    common = learn._common_words()
    counts: collections.Counter[str] = collections.Counter()
    forms: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    try:
        lines = hp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("via") == "private":
            continue
        clean = str(row.get("clean") or "")
        if not clean.strip():
            continue
        # Multi-word proper names
        for m in _MULTI_PROPER_RE.finditer(clean):
            cand = _clean_term(m.group(1))
            if _is_good_candidate(cand, common, allow_lowercase_ident=False):
                k = cand.lower()
                counts[k] += 1
                forms[k][cand] += 1
        # Single-word terms, respecting sentence starts
        after_end = True
        for raw_tok in clean.split():
            tok = _clean_term(raw_tok)
            ends = raw_tok.rstrip("\"')]}*_`")[-1:] in ".!?"
            if tok:
                has_inner = any(c.isupper() for c in tok[1:]) or any(c in ".-_+#" for c in tok[1:-1]) or any(c.isdigit() for c in tok)
                is_cap = tok[0].isupper() and (not after_end or tok.lower() not in common or has_inner)
                if (is_cap or has_inner) and _is_good_candidate(tok, common, allow_lowercase_ident=False):
                    k = tok.lower()
                    counts[k] += 1
                    forms[k][tok] += 1
            after_end = ends
    found: list[dict] = []
    for k, n in counts.most_common(60):
        if n < min_count:
            continue
        best = forms[k].most_common(1)[0][0]
        found.append({"term": best, "source": "history", "count": n, "detail": f"Used {n}× in your dictations"})
    return found


def _plain_source_summary(sources: set[str], count: int, details: list[str]) -> str:
    parts: list[str] = []
    if "history" in sources and count >= 2:
        parts.append(f"Used {count}× in your dictations")
    if "project" in sources:
        parts.append("From ~/Projects")
    if "branch" in sources and "project" not in sources:
        parts.append("From git branch")
    if "kitty" in sources:
        parts.append("From kitty tab")
    if "window" in sources:
        parts.append("From window title")
    return " · ".join(parts) if parts else (details[0] if details else "Suggested from your workspace")


def harvest_all(
    projects_dir: Path | None = None,
    history_path: Path | None = None,
    kitty_runner=None,
    clients_runner=None,
    save: bool = True,
    limit: int = 40,
) -> list[dict]:
    """Harvest vocabulary candidates across all sources. NEVER adds to vocabulary.txt."""
    existing_vocab = {re.sub(r"[^a-z0-9]", "", t.lower()) for t in learn._vocab_map(include_scoped=True).values()}
    existing_lower = {t.lower() for t in learn._vocab_map(include_scoped=True).values()}
    rejected = learn.load_vocab_rejected()

    raw_items: list[dict] = []
    raw_items.extend(harvest_projects(projects_dir=projects_dir))
    raw_items.extend(harvest_kitty_tabs(kitty_runner=kitty_runner))
    raw_items.extend(harvest_window_titles(clients_runner=clients_runner))
    raw_items.extend(harvest_history_terms(history_path=history_path))

    merged: dict[str, dict] = {}
    source_weight = {"history": 3.0, "project": 2.5, "kitty": 2.0, "window": 1.8, "branch": 1.2}
    for item in raw_items:
        term = _clean_term(str(item.get("term") or ""))
        if not term:
            continue
        low = term.lower()
        key_flat = re.sub(r"[^a-z0-9]", "", low)
        if low in existing_lower or key_flat in existing_vocab or low in rejected:
            continue
        rec = merged.get(low)
        if rec is None:
            rec = {
                "term": term,
                "sources": set(),
                "count": 0,
                "score": 0.0,
                "details": [],
            }
            merged[low] = rec
        # Prefer a capitalised/CamelCase form over all-lowercase when merging across sources.
        if any(c.isupper() for c in term) and not any(c.isupper() for c in rec["term"]):
            rec["term"] = term
        src = str(item.get("source") or "")
        if src:
            rec["sources"].add(src)
        inc = int(item.get("count") or 1)
        if src == "history":
            rec["count"] = max(rec["count"], inc)
            rec["score"] += source_weight.get(src, 1.0) + min(inc, 15) * 0.6
        else:
            rec["count"] += 1
            rec["score"] += source_weight.get(src, 1.0)
        det = str(item.get("detail") or "")
        if det and det not in rec["details"]:
            rec["details"].append(det)

    now = learn.now_iso()
    old_cands = learn.load_vocab_candidates() if save else {}
    ranked: list[dict] = []
    for low, rec in merged.items():
        sources = rec["sources"]
        # Cross-source bonus: appearing in multiple places (e.g. repo + history) is a strong signal.
        score = rec["score"] + max(0, len(sources) - 1) * 2.5
        prev = old_cands.get(low) if isinstance(old_cands.get(low), dict) else {}
        scope = prev.get("scope") if prev.get("scope") is not None else infer_scope(rec["term"], sources)
        out_item = {
            "term": rec["term"],
            "sources": sorted(sources),
            "count": rec["count"],
            "score": round(score, 2),
            "scope": scope,
            "summary": _plain_source_summary(sources, rec["count"], rec["details"]),
            "first_seen": prev.get("first_seen") or now,
            "last_seen": now,
        }
        ranked.append(out_item)

    ranked.sort(key=lambda r: (-r["score"], -r["count"], r["term"].lower()))
    ranked = ranked[:limit]

    if save:
        updated = dict(old_cands)
        # Drop any old candidates that are now accepted or rejected.
        for k in list(updated):
            if k in existing_lower or k in rejected or re.sub(r"[^a-z0-9]", "", k) in existing_vocab:
                updated.pop(k, None)
        for item in ranked:
            updated[item["term"].lower()] = item
        learn.save_vocab_candidates(updated)
    return ranked


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    items = harvest_all(save="--no-save" not in argv)
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print("No new vocabulary suggestions found.")
        return 0
    for it in items:
        sc = f" [{learn.scope_label(it['scope'])}]" if it.get("scope") else ""
        print(f"  {it['term']:<24}{sc}  ({it['summary']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
