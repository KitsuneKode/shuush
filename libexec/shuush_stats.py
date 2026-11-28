#!/usr/bin/env python3
"""shuush stats latency — p50/p90/p95 latency breakdowns from history.jsonl.

Prints latency percentiles overall and grouped by:
  - delivery method (type, paste, stream_type, ime, daemon, recovered, legacy)
  - text length band (short <80c, medium 80-299c, long >=300c)
  - app family (terminal, editor, browser, chat, other, unknown)

Handles both new rows (with per-stage monotonic `timing`, `delivery_method`,
`app_family`) and legacy rows (which only recorded `pp_ms`, `llm_ms`, `mode`,
`via`, and `clean`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shuush_timing  # noqa: E402

STAGE_METRICS = (
    ("release_to_done_ms", "release->done"),
    ("stop_to_done_ms", "stop->done"),
    ("release_tail_ms", "release tail"),
    ("asr_ms", "ASR (stop->tx)"),
    ("cleanup_ttfb_ms", "cleanup TTFB"),
    ("cleanup_ms", "cleanup total"),
    ("llm_ms", "LLM call"),
    ("delivery_ms", "delivery"),
)


def default_history_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "voxtype" / "history.jsonl"


def percentile(values: list[int | float], p: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return int(round(s[0]))
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return int(round(s[lo] + (s[hi] - s[lo]) * (k - lo)))


def summarize_values(values: list[int | float]) -> dict | None:
    if not values:
        return None
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
    }


def _row_metric(row: dict, key: str) -> int | None:
    timing = row.get("timing")
    if isinstance(timing, dict) and isinstance(timing.get(key), (int, float)):
        v = int(timing[key])
        if v >= 0:
            return v
    if key == "cleanup_ms" and isinstance(row.get("pp_ms"), (int, float)):
        return max(0, int(row["pp_ms"]))
    if key == "llm_ms" and isinstance(row.get("llm_ms"), (int, float)):
        return max(0, int(row["llm_ms"]))
    return None


def _row_delivery_method(row: dict) -> str:
    dm = row.get("delivery_method")
    if isinstance(dm, str) and dm.strip():
        return dm.strip()
    mode = row.get("delivery_mode")
    if isinstance(mode, str) and mode.strip():
        return mode.strip()
    return "legacy (unrecorded)"


def _row_app_family(row: dict) -> str:
    fam = row.get("app_family")
    if isinstance(fam, str) and fam.strip():
        return fam.strip()
    cls = row.get("app_class")
    if isinstance(cls, str) and cls.strip():
        return shuush_timing.classify_app_family(cls, str(row.get("mode") or ""))
    return "unknown"


def _row_length_band(row: dict) -> str:
    chars = row.get("chars")
    if not isinstance(chars, int):
        clean = row.get("clean")
        chars = len(clean) if isinstance(clean, str) else len(str(row.get("raw") or ""))
    return shuush_timing.length_band(chars)


def load_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if isinstance(d, dict):
                rows.append(d)
        except ValueError:
            continue
    return rows


def compute_group(rows: list[dict]) -> dict:
    metrics: dict[str, dict | None] = {}
    for key, _label in STAGE_METRICS:
        vals = [v for r in rows if (v := _row_metric(r, key)) is not None]
        metrics[key] = summarize_values(vals)
    return {"count": len(rows), "metrics": metrics}


def analyze(rows: list[dict]) -> dict:
    with_timing = sum(1 for r in rows if isinstance(r.get("timing"), dict) and r["timing"])
    by_method: dict[str, list[dict]] = defaultdict(list)
    by_band: dict[str, list[dict]] = defaultdict(list)
    by_family: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_method[_row_delivery_method(r)].append(r)
        by_band[_row_length_band(r)].append(r)
        by_family[_row_app_family(r)].append(r)

    band_order = [label for label, _, _ in shuush_timing.LENGTH_BANDS]
    family_order = ["terminal", "editor", "browser", "chat", "other", "unknown"]
    method_order = sorted(by_method.keys(), key=lambda m: (m.startswith("legacy"), m))

    return {
        "total": len(rows),
        "with_stage_timing": with_timing,
        "legacy_rows": len(rows) - with_timing,
        "overall": compute_group(rows),
        "by_delivery_method": {k: compute_group(by_method[k]) for k in method_order},
        "by_length_band": {k: compute_group(by_band[k]) for k in band_order if k in by_band},
        "by_app_family": {k: compute_group(by_family[k]) for k in family_order if k in by_family},
    }


def _fmt_triplet(stat: dict | None) -> str:
    if not stat:
        return "—"
    return f"{stat['p50']}/{stat['p90']}/{stat['p95']}ms (n={stat['n']})"


def _print_group_table(title: str, groups: dict[str, dict]) -> None:
    print(f"\n{title} (p50/p90/p95):")
    if not groups:
        print("  (no data)")
        return
    cols = (
        ("stop_to_done_ms", "stop->done"),
        ("cleanup_ms", "cleanup"),
        ("llm_ms", "llm"),
        ("delivery_ms", "delivery"),
    )
    header = f"  {'Group':<22s} {'N':>5s}  " + "  ".join(f"{lbl:<23s}" for _, lbl in cols)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name, grp in groups.items():
        cells = "  ".join(f"{_fmt_triplet(grp['metrics'].get(k)):<23s}" for k, _ in cols)
        print(f"  {name:<22s} {grp['count']:>5d}  {cells}")


def format_report(report: dict, path: Path) -> str:
    lines: list[str] = []
    lines.append(
        f"Shuush latency stats ({path}): {report['total']} take(s) "
        f"({report['with_stage_timing']} with per-stage timing, {report['legacy_rows']} legacy)"
    )
    if report["total"] == 0:
        lines.append("  No takes recorded yet.")
        return "\n".join(lines)
    lines.append("\nOverall stages (p50 / p90 / p95):")
    overall_m = report["overall"]["metrics"]
    for key, label in STAGE_METRICS:
        st = overall_m.get(key)
        if st:
            lines.append(
                f"  {label:<18s}  p50={st['p50']:>5d}ms  p90={st['p90']:>5d}ms  "
                f"p95={st['p95']:>5d}ms  (n={st['n']})"
            )
        else:
            lines.append(f"  {label:<18s}  — (not yet recorded)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="shuush-stats")
    ap.add_argument("topic", nargs="?", default="latency", choices=("latency", "reliability"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--history", default="", help="path to history.jsonl")
    ap.add_argument("--days", type=float, default=7.0, help="reliability: how many days back")
    ap.add_argument("--outcomes", default="", help="reliability: path to outcomes.jsonl")
    a = ap.parse_args(argv)
    if a.topic == "reliability":
        import shuush_reliability
        args = ["report", "--days", str(a.days)]
        if a.json:
            args.append("--json")
        if a.history:
            args += ["--history", a.history]
        if a.outcomes:
            args += ["--outcomes", a.outcomes]
        return shuush_reliability.main(args)

    path = Path(a.history) if a.history else default_history_path()
    rows = load_rows(path)
    report = analyze(rows)
    if a.json:
        print(json.dumps({"history": str(path), **report}, indent=2))
        return 0
    print(format_report(report, path))
    if report["total"] > 0:
        _print_group_table("By delivery method", report["by_delivery_method"])
        _print_group_table("By text length band", report["by_length_band"])
        _print_group_table("By app family", report["by_app_family"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
