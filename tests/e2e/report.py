#!/usr/bin/env python3
"""Turn results.jsonl (+ the run's reliability numbers) into the short report."""
import argparse, json, datetime

ap = argparse.ArgumentParser()
ap.add_argument("results")
ap.add_argument("--stats", default="")
ap.add_argument("--isolated", default="0")
ap.add_argument("--voxtype", default="")
ap.add_argument("--shuush", default="")
ap.add_argument("--out", default="")
ap.add_argument("--latest", default="")
a = ap.parse_args()
rows = [json.loads(l) for l in open(a.results) if l.strip()]
stats = {}
try:
    stats = json.load(open(a.stats)) if a.stats else {}
except (OSError, ValueError):
    pass
p = [r for r in rows if r["status"] == "pass"]
k = [r for r in rows if r["status"] == "known"]
f = [r for r in rows if r["status"] == "fail"]
s = [r for r in rows if r["status"] == "skip"]
L = []
L.append(f"# Shuush dictation e2e — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
L.append("")
L.append(f"shuush `{a.shuush}` · {a.voxtype or 'voxtype ?'} · network isolation: {'yes (user+net+pid namespaces)' if a.isolated == '1' else 'NO — not isolated'}")
L.append("")
rt = [r["scenario"] for r in rows if r.get("retried")]
L.append(f"**{len(p)} passed, {len(k)} with known bugs, {len(f)} failed, {len(s)} skipped** of {len(rows)} scenarios")
L.append("")
if rt:
    L.append(f"flaky this run (failed once, passed on the second run): {', '.join(rt)}")
    L.append("")
L.append("| scenario | result | checks | seconds |")
L.append("|---|---|---|---|")
for r in rows:
    st = {"pass": "pass", "known": "pass, known bug", "fail": "**FAIL**", "skip": "skip"}[r["status"]]
    if r.get("retried"):
        st += " (needed a second run)"
    L.append(f"| {r['scenario']} | {st} | {r.get('checks', '-')} | {r.get('seconds', '-')} |")
if f:
    L.append("")
    L.append("## Failures")
    for r in f:
        L.append(f"### {r['scenario']}")
        for x in r.get("failures", []):
            L.append(f"- {x['check']}\n  - want: `{x['want']}`\n  - got: `{x['got']}`")
bugs = [(r["scenario"], b) for r in rows for b in r.get("bugs", [])]
if bugs:
    L.append("")
    L.append("## Known bugs (found by this harness, not fixed)")
    for sc, b in bugs:
        if b.get("fixed"):
            L.append(f"- **{b['bug']}** — `{sc}`: this check now passes; drop the known-bug mark")
        else:
            L.append(f"- **{b['bug']}** — `{sc}`: {b['check']}\n  - want: `{b['want']}`\n  - got: `{b['got']}`")
if s:
    L.append("")
    L.append("## Skipped")
    for r in s:
        L.append(f"- {r['scenario']}: {r.get('reason', '')}")
t = stats.get("totals") if isinstance(stats, dict) else None
if t:
    L.append("")
    L.append("## Reliability of this run (`shuush stats reliability`)")
    L.append(f"- takes attempted: {t.get('attempted', 0)}, delivered {t.get('delivered', 0)} ({stats.get('delivered_rate_pct', '–')}%), "
             f"recovered {t.get('recovered', 0)}, clipboard only {t.get('clipboard_only', 0)}, failed {t.get('failed', 0)}, "
             f"private not inserted {t.get('private_not_inserted', 0)}, cancelled {t.get('cancelled', 0)}")
    for app, d in (stats.get("by_app") or {}).items():
        L.append(f"  - {app}: {d.get('delivered', 0)}/{d.get('attempted', 0)} delivered")
out = "\n".join(L) + "\n"
for path in (a.out, a.latest):
    if path:
        open(path, "w").write(out)
print(out if not a.out else "", end="")
