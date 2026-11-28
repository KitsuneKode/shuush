#!/usr/bin/env python3
"""shuush stats reliability — how often a take ends where it was meant to.

Every take ends one way: delivered into the field, left on the clipboard,
recovered (kept for `shuush recover` because its window changed), failed,
private-and-not-inserted, cancelled, or it ended with no receipt at all (the
daemon died, the microphone never opened). This module counts them from what
Shuush already keeps, and never reads or stores dictated text:

  history.jsonl      rows written after delivery carry `outcome`, `take_id`,
                     `app_class`, `app_family` (voxtype-review.py)
  outcomes.jsonl     `record` appends the daemon's outcome.json (one slot, rewritten
                     by every take) and any take directory that never got a receipt;
                     private takes and takes that fail before cleanup are only here
                     ($XDG_STATE_HOME/shuush/outcomes.jsonl)

    shuush stats reliability [--days 7] [--json] [--history PATH] [--outcomes PATH]
    shuush_reliability.py record        called in the background by the toggle
    shuush_reliability.py doctor        what `shuush doctor` prints (JSON)

Rates. `attempted` is every take that reached delivery or should have:
delivered, clipboard_only, recovered, failed, private_not_inserted and no_receipt.
`delivered_rate` is delivered / attempted. `failure_rate` counts failed and
no_receipt takes: the ones where the words were at risk of being lost, which is
what `shuush doctor` warns about (SHUUSH_RELIABILITY_WARN_PCT, default 10, over the
last day, with at least SHUUSH_RELIABILITY_MIN takes, default 5). Cancelled takes are
the user's choice and count in neither.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ATTEMPTED = ("delivered", "clipboard_only", "recovered", "failed", "private_not_inserted", "no_receipt")
BUCKETS = ATTEMPTED + ("cancelled",)
# What the daemon, the shuush worker and older daemons call a take's end -> one of BUCKETS.
CANON = {
    "delivered": "delivered", "ok": "delivered",
    "clipboard_only": "clipboard_only",
    "recovered": "recovered",
    "failed": "failed", "error": "failed", "output_failed": "failed", "no_input": "failed",
    "private_not_inserted": "private_not_inserted",
    "cancelled": "cancelled", "empty": "cancelled", "no_speech": "cancelled", "too_short": "cancelled",
    "no_receipt": "no_receipt", "unknown": "no_receipt",
}
GRACE_S = 150          # a take directory younger than this may still be running
FAMILY_FALLBACK = "unknown"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "voxtype"


def default_history() -> Path:
    return state_dir() / "voxtype" / "history.jsonl"


def default_outcomes() -> Path:
    return state_dir() / "shuush" / "outcomes.jsonl"


def _rows(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError:
        pass
    return out


def _epoch(ts) -> float | None:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return None
    return None


def _family(app_class: str, mode: str = "") -> str:
    try:
        import shuush_timing
        return shuush_timing.classify_app_family(app_class, mode)
    except Exception:
        return FAMILY_FALLBACK


# ── record: the ledger ───────────────────────────────────────────────────────────


def _take_class(take_dir: Path) -> str:
    try:
        return str(json.loads((take_dir / "target_window").read_text(encoding="utf-8")).get("class") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def record(runtime: Path | None = None, outcomes: Path | None = None, history: Path | None = None,
           now: float | None = None) -> int:
    """Append what is new: outcome.json if it names a take not yet recorded, and every
    take directory older than GRACE_S that has no receipt anywhere. Idempotent."""
    runtime = runtime or runtime_dir()
    outcomes = outcomes or default_outcomes()
    history = history or default_history()
    now = time.time() if now is None else now
    known = {(r.get("take_id"), r.get("outcome")) for r in _rows(outcomes) if r.get("take_id")}
    have_receipt = {r.get("take_id") for r in _rows(outcomes) if r.get("take_id") and r.get("source") == "outcome"}
    have_receipt |= {r.get("take_id") for r in _rows(history) if r.get("take_id") and r.get("outcome")}
    new: list[dict] = []

    try:
        doc = json.loads((runtime / "outcome.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if isinstance(doc, dict) and doc.get("take_id") and doc.get("outcome"):
        tid = str(doc["take_id"])
        kind = CANON.get(str(doc["outcome"]), "")
        # In shuush delivery the daemon first writes "cancelled" (it handed over an empty string)
        # and the worker's own receipt follows: only the worker's counts as the take's end.
        transient = kind == "cancelled" and doc.get("delivery_mode") is None and (runtime / "takes" / tid / "shuush_delivery").exists()
        if kind and not transient and (tid, kind) not in known:
            new.append({
                "ts": datetime.datetime.fromtimestamp((doc.get("at_ms") or now * 1000) / 1000).astimezone().isoformat(timespec="seconds"),
                "take_id": tid, "outcome": kind, "source": "outcome", "at_ms": doc.get("at_ms"),
                "app_class": _take_class(runtime / "takes" / tid),
                "private": bool(doc.get("private")) or (runtime / "takes" / tid / "pp_private").exists(),
            })
            have_receipt.add(tid)

    takes = runtime / "takes"
    if takes.is_dir():
        for d in sorted(takes.iterdir()):
            if not d.is_dir() or not d.name.isdigit() or d.name in have_receipt:
                continue
            # a take whose start never reached the daemon leaves a directory too: no_receipt is the point
            try:
                age = now - d.stat().st_mtime
            except OSError:
                continue
            if age < GRACE_S:
                continue
            disp = ""
            try:
                disp = (d / "disposition").read_text().strip()
            except OSError:
                pass
            kind = CANON.get(disp, "") if disp else "no_receipt"
            if (d.name, kind) in known:
                continue
            new.append({
                "ts": datetime.datetime.fromtimestamp(d.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                "take_id": d.name, "outcome": kind, "source": "scan",
                "app_class": _take_class(d), "private": (d / "pp_private").exists(),
            })
    if new:
        outcomes.parent.mkdir(parents=True, exist_ok=True)
        with outcomes.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        try:
            os.chmod(outcomes, 0o600)
        except OSError:
            pass
    return len(new)


# ── report ────────────────────────────────────────────────────────────────────────


def collect(history: Path, outcomes: Path, since: float) -> list[dict]:
    """One row per take: {take_id, outcome (bucket), ts, app_class, app_family}. The history row
    wins (it is written after delivery), then the newest receipt in the ledger, then a scan."""
    by_take: dict[str, dict] = {}
    anon: list[dict] = []

    def put(key, row, rank):
        row["_rank"] = rank
        if not key:
            anon.append(row)
            return
        cur = by_take.get(key)
        if cur is None or (rank, row.get("_at") or 0) >= (cur["_rank"], cur.get("_at") or 0):
            by_take[key] = row

    for r in _rows(history):
        kind = CANON.get(str(r.get("outcome") or ""), "")
        t = _epoch(r.get("ts"))
        if not kind or t is None:
            continue   # older rows carry no outcome: not counted, not guessed
        cls = str(r.get("app_class") or "")
        put(str(r.get("take_id") or ""), {
            "outcome": kind, "t": t, "app_class": cls,
            "app_family": str(r.get("app_family") or "") or (_family(cls, str(r.get("mode") or "")) if cls else FAMILY_FALLBACK),
            "_at": t * 1000,
        }, 3)
    for r in _rows(outcomes):
        kind = CANON.get(str(r.get("outcome") or ""), "")
        t = _epoch(r.get("ts"))
        if not kind or t is None:
            continue
        cls = str(r.get("app_class") or "")
        put(str(r.get("take_id") or ""), {
            "outcome": kind, "t": t, "app_class": cls,
            "app_family": _family(cls) if cls else FAMILY_FALLBACK,
            "_at": float(r.get("at_ms") or t * 1000),
        }, 2 if r.get("source") == "outcome" else 1)
    rows = list(by_take.values()) + anon
    return [r for r in rows if r["t"] >= since]


def summarise(rows: list[dict], days: float, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    totals = {b: 0 for b in BUCKETS}
    by_app: dict[str, dict] = defaultdict(lambda: {b: 0 for b in BUCKETS})
    by_family: dict[str, dict] = defaultdict(lambda: {b: 0 for b in BUCKETS})
    by_day: dict[str, dict] = defaultdict(lambda: {b: 0 for b in BUCKETS})
    for r in rows:
        k = r["outcome"]
        totals[k] += 1
        by_app[r["app_class"] or "(unknown app)"][k] += 1
        by_family[r["app_family"] or FAMILY_FALLBACK][k] += 1
        by_day[datetime.datetime.fromtimestamp(r["t"]).strftime("%Y-%m-%d")][k] += 1

    def fin(d: dict) -> dict:
        d = dict(d)
        att = sum(d[b] for b in ATTEMPTED)
        d["attempted"] = att
        d["delivered_rate_pct"] = round(100.0 * d["delivered"] / att, 1) if att else None
        d["failure_rate_pct"] = round(100.0 * (d["failed"] + d["no_receipt"]) / att, 1) if att else None
        return d

    t = fin(totals)
    return {
        "days": days,
        "since": datetime.datetime.fromtimestamp(now - days * 86400).isoformat(timespec="seconds"),
        "totals": {k: v for k, v in t.items() if k in BUCKETS or k == "attempted"},
        "attempted": t["attempted"],
        "delivered_rate_pct": t["delivered_rate_pct"],
        "failure_rate_pct": t["failure_rate_pct"],
        "by_app": {k: fin(v) for k, v in sorted(by_app.items(), key=lambda kv: -sum(kv[1].values()))},
        "by_family": {k: fin(v) for k, v in sorted(by_family.items(), key=lambda kv: -sum(kv[1].values()))},
        "by_day": {k: fin(v) for k, v in sorted(by_day.items())},
    }


def report(history: Path, outcomes: Path, days: float = 7.0, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return summarise(collect(history, outcomes, now - days * 86400), days, now)


def _pct(v) -> str:
    return "–" if v is None else f"{v:g}%"


def format_report(rep: dict, history: Path, outcomes: Path) -> str:
    t = rep["totals"]
    L = [f"Shuush dictation reliability, last {rep['days']:g} day(s)  ({history}, {outcomes})", ""]
    if not rep["attempted"] and not t["cancelled"]:
        L.append("  No takes with a recorded outcome yet.")
        L.append("  (older history rows carry no outcome; `shuush toggle` starts recording them from now on)")
        return "\n".join(L)
    L.append(f"  takes that should have landed: {rep['attempted']}")
    L.append(f"    delivered            {t['delivered']:>5}   {_pct(rep['delivered_rate_pct'])}")
    L.append(f"    clipboard only       {t['clipboard_only']:>5}   (typing failed; the words are on the clipboard)")
    L.append(f"    recovered            {t['recovered']:>5}   (window changed; `shuush recover` brings them back)")
    L.append(f"    private, not typed   {t['private_not_inserted']:>5}   (window changed; nothing kept, by design)")
    L.append(f"    failed               {t['failed']:>5}")
    L.append(f"    no receipt           {t['no_receipt']:>5}   (daemon died, microphone never opened)")
    L.append(f"  cancelled / empty      {t['cancelled']:>5}   (your choice; not counted)")
    L.append(f"  failure rate (failed + no receipt): {_pct(rep['failure_rate_pct'])}")

    def table(title: str, groups: dict) -> None:
        if not groups:
            return
        L.append("")
        L.append(f"  {title}")
        L.append(f"    {'':<22s} {'takes':>5s} {'delivered':>10s} {'recovered':>10s} {'failed':>7s} {'no rcpt':>8s}")
        for k, d in groups.items():
            L.append(f"    {k:<22.22s} {d['attempted']:>5d} {_pct(d['delivered_rate_pct']):>10s} {d['recovered']:>10d} {d['failed']:>7d} {d['no_receipt']:>8d}")

    table("by app", rep["by_app"])
    table("by app family", rep["by_family"])
    table("by day", rep["by_day"])
    return "\n".join(L)


def doctor(history: Path | None = None, outcomes: Path | None = None, runtime: Path | None = None, now: float | None = None) -> dict:
    """The line for `shuush doctor`: ok, or warn when the last day's failure rate is above the threshold."""
    history = history or default_history()
    outcomes = outcomes or default_outcomes()
    try:
        record(runtime=runtime, outcomes=outcomes, history=history, now=now)
    except OSError:
        pass
    warn_pct = float(os.environ.get("SHUUSH_RELIABILITY_WARN_PCT", "10") or 10)
    min_n = int(os.environ.get("SHUUSH_RELIABILITY_MIN", "5") or 5)
    rep = report(history, outcomes, days=1.0, now=now)
    t = rep["totals"]
    n = rep["attempted"]
    if n == 0:
        return {"status": "ok", "label": "dictation reliability: no takes with a recorded outcome in the last 24 h", "hint": "", "report": rep}
    bad = t["failed"] + t["no_receipt"]
    rate = rep["failure_rate_pct"]
    bits = [f"{t['delivered']}/{n} delivered"]
    for key, name in (("recovered", "recovered"), ("clipboard_only", "clipboard only"), ("failed", "failed"), ("no_receipt", "no receipt")):
        if t[key]:
            bits.append(f"{t[key]} {name}")
    label = f"dictation reliability (24 h): {', '.join(bits)}"
    if n >= min_n and rate is not None and rate > warn_pct:
        return {"status": "warn", "label": f"{label}; {rate:g}% lost or unaccounted for (limit {warn_pct:g}%)",
                "hint": "shuush stats reliability --days 1 (what failed and where); shuush recover", "report": rep}
    return {"status": "ok", "label": label, "hint": "", "report": rep}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="shuush-reliability")
    ap.add_argument("action", nargs="?", default="report", choices=("report", "record", "doctor"))
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--history", default="")
    ap.add_argument("--outcomes", default="")
    ap.add_argument("--runtime", default="")
    a = ap.parse_args(argv)
    history = Path(a.history) if a.history else default_history()
    outcomes = Path(a.outcomes) if a.outcomes else default_outcomes()
    runtime = Path(a.runtime) if a.runtime else None
    if a.action == "record":
        record(runtime=runtime, outcomes=outcomes, history=history)
        return 0
    if a.action == "doctor":
        print(json.dumps(doctor(history, outcomes, runtime)))
        return 0
    try:
        record(runtime=runtime, outcomes=outcomes, history=history)   # the last take is not in the ledger yet
    except OSError:
        pass
    rep = report(history, outcomes, a.days)
    if a.json:
        print(json.dumps({"history": str(history), "outcomes": str(outcomes), **rep}, indent=2))
    else:
        print(format_report(rep, history, outcomes))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
