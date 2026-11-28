"""shuush stats reliability: the counts, the ledger and the doctor line."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "libexec"))
import shuush_reliability as rel  # noqa: E402

NOW = 1_800_000_000.0


def iso(t):
    import datetime
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def hist(take, outcome, age_h, cls="kitty"):
    return {"ts": iso(NOW - age_h * 3600), "take_id": take, "outcome": outcome, "app_class": cls, "raw": "secret words", "clean": "secret words"}


def test_counts_rates_and_apps(tmp_path):
    h, o = tmp_path / "h.jsonl", tmp_path / "o.jsonl"
    write(h, [hist("1", "delivered", 1), hist("2", "delivered", 2), hist("3", "recovered", 3, "chromium"),
              hist("4", "clipboard_only", 4), {"ts": iso(NOW - 3600), "raw": "old row without outcome"}])
    write(o, [{"ts": iso(NOW - 5 * 3600), "take_id": "5", "outcome": "failed", "source": "outcome", "app_class": "kitty"},
              {"ts": iso(NOW - 6 * 3600), "take_id": "6", "outcome": "cancelled", "source": "outcome"},
              {"ts": iso(NOW - 8 * 86400), "take_id": "7", "outcome": "failed", "source": "outcome"}])
    r = rel.report(h, o, days=7, now=NOW)
    assert r["totals"]["delivered"] == 2 and r["totals"]["recovered"] == 1 and r["totals"]["failed"] == 1
    assert r["totals"]["cancelled"] == 1
    assert r["attempted"] == 5
    assert r["delivered_rate_pct"] == 40.0 and r["failure_rate_pct"] == 20.0
    assert r["by_app"]["kitty"]["attempted"] == 4 and r["by_app"]["chromium"]["recovered"] == 1


def test_history_wins_over_a_transient_ledger_row_and_nothing_is_double_counted(tmp_path):
    h, o = tmp_path / "h.jsonl", tmp_path / "o.jsonl"
    write(h, [hist("1", "delivered", 1)])
    write(o, [{"ts": iso(NOW - 3600), "take_id": "1", "outcome": "cancelled", "source": "outcome"}])
    r = rel.report(h, o, days=1, now=NOW)
    assert r["totals"]["delivered"] == 1 and r["totals"]["cancelled"] == 0


def test_record_snapshots_outcome_and_flags_takes_without_a_receipt(tmp_path):
    rt = tmp_path / "voxtype"
    (rt / "takes" / "111").mkdir(parents=True)
    (rt / "takes" / "111" / "target_window").write_text('{"class": "kitty"}')
    (rt / "takes" / "222").mkdir()          # never got an outcome: the daemon died or the mic never opened
    (rt / "takes" / "333").mkdir()          # still running: too young to judge
    (rt / "outcome.json").write_text(json.dumps({"take_id": "111", "outcome": "delivered", "at_ms": int(NOW * 1000)}))
    now = time.time()
    for d in ("111", "222"):
        os.utime(rt / "takes" / d, (now - 600, now - 600))
    o, h = tmp_path / "o.jsonl", tmp_path / "h.jsonl"
    assert rel.record(rt, o, h, now=now) == 2
    assert rel.record(rt, o, h, now=now) == 0          # idempotent
    rows = {r["take_id"]: r for r in map(json.loads, o.read_text().splitlines())}
    assert rows["111"]["outcome"] == "delivered" and rows["111"]["app_class"] == "kitty"
    assert rows["222"]["outcome"] == "no_receipt" and "333" not in rows
    assert "words" not in o.read_text()


def test_the_daemons_transient_cancel_in_shuush_delivery_is_not_a_cancelled_take(tmp_path):
    rt = tmp_path / "voxtype"
    (rt / "takes" / "9").mkdir(parents=True)
    (rt / "takes" / "9" / "shuush_delivery").write_text("")
    (rt / "outcome.json").write_text(json.dumps({"take_id": "9", "outcome": "cancelled", "at_ms": 1}))
    o = tmp_path / "o.jsonl"
    assert rel.record(rt, o, tmp_path / "h.jsonl", now=time.time()) == 0


def test_doctor_warns_only_above_the_threshold_with_enough_takes(tmp_path, monkeypatch):
    h, o = tmp_path / "h.jsonl", tmp_path / "o.jsonl"
    rt = tmp_path / "rt"
    rt.mkdir()
    write(h, [hist(str(i), "delivered", 1) for i in range(8)] + [hist("f1", "failed", 1), hist("f2", "failed", 2)])
    d = rel.doctor(h, o, rt, now=NOW)
    assert d["status"] == "warn" and "20%" in d["label"] and "8/10 delivered" in d["label"]
    monkeypatch.setenv("SHUUSH_RELIABILITY_WARN_PCT", "25")
    assert rel.doctor(h, o, rt, now=NOW)["status"] == "ok"
    monkeypatch.delenv("SHUUSH_RELIABILITY_WARN_PCT")
    write(h, [hist("a", "failed", 1), hist("b", "failed", 1)])   # too few takes to call it a pattern
    assert rel.doctor(h, o, rt, now=NOW)["status"] == "ok"
    write(h, [hist("old", "failed", 40)])                         # outside the last day
    assert rel.doctor(h, o, rt, now=NOW)["status"] == "ok"


def test_cli_prints_a_report_and_json(tmp_path):
    h, o = tmp_path / "h.jsonl", tmp_path / "o.jsonl"
    now = time.time()
    write(h, [{"ts": iso(now - 60), "take_id": "1", "outcome": "delivered", "app_class": "kitty"}])
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path), "XDG_STATE_HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, "-I", str(ROOT / "libexec/shuush_stats.py"), "reliability", "--history", str(h), "--outcomes", str(o)],
                         capture_output=True, text=True, env=env, check=True).stdout
    assert "delivered" in out and "kitty" in out
    js = json.loads(subprocess.run([sys.executable, "-I", str(ROOT / "libexec/shuush_stats.py"), "reliability", "--json", "--history", str(h), "--outcomes", str(o)],
                                   capture_output=True, text=True, env=env, check=True).stdout)
    assert js["totals"]["delivered"] == 1
