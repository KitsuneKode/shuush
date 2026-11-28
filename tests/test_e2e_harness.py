"""The e2e harness itself, without starting a compositor: syntax, the fake servers' rules, the scenario list."""
import subprocess
import sys
from pathlib import Path

E2E = Path(__file__).resolve().parent / "e2e"


def test_shell_files_parse():
    files = [str(p) for p in list(E2E.glob("*.sh")) + list((E2E / "lib").glob("*.sh")) + list((E2E / "scenarios").glob("*.sh"))]
    subprocess.run(["bash", "-n", *files], check=True)


def test_fake_services_selftest_and_cleanup_rule():
    subprocess.run([sys.executable, "-I", str(E2E / "fake_services.py"), "selftest"], check=True)
    out = subprocess.run([sys.executable, "-I", str(E2E / "fake_services.py"), "clean", "um first line\nuh second line"],
                         capture_output=True, text=True, check=True).stdout
    assert out == "First line.\nSecond line."


def test_every_scenario_has_a_description_and_a_run_function():
    names = []
    for f in sorted((E2E / "scenarios").glob("[0-9]*.sh")):
        text = f.read_text()
        assert "# desc: " in text and "scn_run()" in text, f.name
        names.append(f.stem)
    assert len(names) == len(set(n.split("_", 1)[1] for n in names))


def test_list_runs_without_a_world():
    out = subprocess.run(["bash", str(E2E / "run.sh"), "--list"], capture_output=True, text=True, check=True).stdout
    assert "short_take" in out and "private_take" in out


def test_report_renders(tmp_path):
    res = tmp_path / "r.jsonl"
    res.write_text('{"scenario":"a","status":"pass","checks":2,"seconds":1}\n'
                   '{"scenario":"b","status":"known","checks":1,"seconds":1,"bugs":[{"bug":"x","check":"c","want":"1","got":"2","fixed":false}]}\n'
                   '{"scenario":"c","status":"fail","checks":1,"seconds":1,"failures":[{"check":"d","want":"1","got":"2"}]}\n')
    out = subprocess.run([sys.executable, "-I", str(E2E / "report.py"), str(res)], capture_output=True, text=True, check=True).stdout
    assert "1 passed, 1 with known bugs, 1 failed" in out and "Known bugs" in out and "### c" in out
