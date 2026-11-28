"""Sandboxed tests for learning provenance/health, edit-watch in Electron apps,
vocabulary harvesting with consent, Parakeet 128-term rescoring sync,
per-app vocabulary/rules, Studio Learned view helpers, and offline history replay.

Never touches ~/.config/voxtype, ~/.local/state/voxtype, ~/.local/share/shuush,
or the live voxtype daemon.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = ROOT / "libexec"
SHUUSH = ROOT / "bin" / "shuush"


def _load_sandbox_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conf = tmp_path / "config"
    state = tmp_path / "state"
    rt = tmp_path / "rt"
    (conf / "voxtype").mkdir(parents=True, exist_ok=True)
    (state / "voxtype").mkdir(parents=True, exist_ok=True)
    (rt / "voxtype").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))

    if str(LIBEXEC) not in sys.path:
        sys.path.insert(0, str(LIBEXEC))

    import voxtype_learn as learn
    import voxtype_pp_gate as gate
    import voxtype_edit_watch as ew
    import shuush_vocab_harvest as vh
    import shuush_learn_replay as lr

    learn = importlib.reload(learn)
    gate = importlib.reload(gate)
    ew = importlib.reload(ew)
    vh = importlib.reload(vh)
    lr = importlib.reload(lr)
    return conf / "voxtype", state / "voxtype", rt / "voxtype", learn, gate, ew, vh, lr


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["XDG_RUNTIME_DIR"] = str(tmp_path / "rt")
    return env


# ── 1. Provenance, health, demote -> remove lifecycle, and `shuush learn stats` ──


def test_provenance_health_and_demote_remove_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf, _state, _rt, learn, gate, _ew, _vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)
    (conf / "config.toml").write_text('[text]\nreplacements = {}\n', encoding="utf-8")

    # Seed rules across all provenance sources
    assert learn.remember("box type", "voxtype", "seed") is None
    assert learn.remember("webar", "Waybar", "user") is None
    assert learn.remember("hyper land", "Hyprland", "edit-watch") is None
    assert learn.remember("grog", "Groq", "hint") is None
    learn.sync_replacements()

    entries = {e["heard"]: e for e in learn.load_entries()}
    assert entries["box type"]["source_label"] == "seed"
    assert entries["webar"]["source_label"] == "user fix"
    assert entries["hyper land"]["source_label"] == "edit-watch"
    assert entries["grog"]["source_label"] == "accepted hint"
    assert entries["hyper land"]["first_seen"] and entries["hyper land"]["last_seen"]

    # Record applications
    learn.record_applied("hyper land", "Hyprland")
    learn.record_applied("hyper land", "Hyprland")
    e = {x["heard"]: x for x in learn.load_entries()}["hyper land"]
    assert e["applied"] == 2 and e["undone"] == 0 and e["last_applied"]

    # Active instant rewrite applies in hard_apply and is synced in config.toml
    assert "Hyprland" in gate.hard_apply("open hyper land now")
    assert '"hyper land" = "Hyprland"' in (conf / "config.toml").read_text(encoding="utf-8")

    # Undo #1: recorded, still active
    assert learn.record_undo("hyper land", "Hyprland") == "recorded"
    e1 = {x["heard"]: x for x in learn.load_entries()}["hyper land"]
    assert e1["undone"] == 1 and not e1["demoted"]

    # Undo #2 (consecutive / high undo rate): demoted to context hint!
    assert learn.record_undo("hyper land", "Hyprland") == "demoted"
    e2 = {x["heard"]: x for x in learn.load_entries()}["hyper land"]
    assert e2["demoted"] is True and e2["source"] == "hint" and e2["origin_source"] == "edit-watch"
    # Demoted rule no longer blindly rewrites in hard_apply or config.toml, but remains a context hint
    assert "hyper land" in gate.hard_apply("open hyper land now")
    assert '"hyper land"' not in (conf / "config.toml").read_text(encoding="utf-8")
    assert '"hyper land" -> Hyprland' in gate.hint_line("open hyper land now")

    # Undo #3: removed automatically and added to rejected list
    assert learn.record_undo("hyper land", "Hyprland") == "removed"
    assert "hyper land" not in learn.load()
    assert learn.is_rejected("hyper land", "Hyprland")
    removed = [r for r in learn.load_entries(include_removed=True) if r["status"] == "removed"]
    assert len(removed) == 1 and removed[0]["heard"] == "hyper land"

    # CLI `shuush learn stats --json` and plain text output
    res_json = subprocess.run([str(SHUUSH), "learn", "stats", "--json"], env=_env(tmp_path), capture_output=True, text=True, check=True)
    data = json.loads(res_json.stdout)
    assert data["total_rules"] == 3
    assert data["by_source"]["seed"] == 1
    assert data["by_source"]["user fix"] == 1
    assert data["by_source"]["accepted hint"] == 1
    assert data["by_status"]["removed"] == 1
    assert data["total_applied"] == 2 and data["total_undone"] == 3

    res_txt = subprocess.run([str(SHUUSH), "learn", "stats"], env=_env(tmp_path), capture_output=True, text=True, check=True)
    assert "Shuush learning stats" in res_txt.stdout
    assert "Removed after repeated undos:" in res_txt.stdout


def test_manual_demote_and_promote_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf, _state, _rt, learn, gate, _ew, _vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)
    (conf / "config.toml").write_text('[text]\nreplacements = {}\n', encoding="utf-8")
    learn.remember("huai", "kuwai", "edit-watch")
    learn.sync_replacements()
    assert '"huai" = "kuwai"' in (conf / "config.toml").read_text(encoding="utf-8")

    assert learn.demote_rule("huai") is True
    assert "huai" in gate.hard_apply("check huai config")
    assert '"huai"' not in (conf / "config.toml").read_text(encoding="utf-8")

    assert learn.promote_rule("huai") is True
    e = {x["heard"]: x for x in learn.load_entries()}["huai"]
    assert e["source"] == "edit-watch" and not e["demoted"]
    assert "kuwai" in gate.hard_apply("check huai config")


# ── 2. Edit learning with fake field_text.json stream in Electron apps (T3 Code, Zen) ──


def test_edit_watch_t3code_field_text_stream_captures_names_vocab_and_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf, _state, rt, learn, _gate, ew, _vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)
    learn.add_vocab_term("voxtype")
    field_file = rt / "field_text.json"
    target = {"pid": 1234, "class": "com.t3tools.T3Code", "title": "shuush — T3 Code", "address": "0xabc"}

    # Fake field-text stream: tick 0 unmodified -> tick 1 user fixes sentence-start name & lowercase vocab -> tick 2 stable
    stream = [
        {"before": "Hyper land works great with box type on Wayland right now.", "after": ""},
        {"before": "Hyprland works great with voxtype on Wayland right now.", "after": ""},
        {"before": "Hyprland works great with voxtype on Wayland right now.", "after": ""},
    ]
    step = {"i": 0}

    def fake_reader():
        idx = min(step["i"], len(stream) - 1)
        step["i"] += 1
        field_file.write_text(json.dumps(stream[idx]), encoding="utf-8")
        return ew.im_field(field_path=field_file, rt=rt)

    prompts: list[tuple[str, str]] = []

    def fake_notify(title, body, actions=None, **_kw):
        prompts.append((title, body))
        return "learn" if actions else ""

    res = ew.watch_field(
        "hyper land works great with box type on wayland right now",
        "Hyper land works great with box type on Wayland right now.",
        target,
        rt=rt,
        field_reader=fake_reader,
        active_addr_fn=lambda: "0xabc",
        ticks=(0, 0, 0),
        sleep_fn=lambda _s: None,
        notify_fn=fake_notify,
    )
    assert ("hyper land", "Hyprland") in res["accepted_prompt"]
    assert ("box type", "voxtype") in res["accepted_prompt"]
    entries = {e["heard"]: e for e in learn.load_entries()}
    assert entries["hyper land"]["source"] == "edit-watch"
    assert entries["box type"]["source"] == "edit-watch"
    assert any("T3 Code" in str(p) or "Hyprland" in p[1] for p in prompts)


def test_edit_watch_zen_spellings_auto_promote_and_reverts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conf, _state, rt, learn, _gate, ew, _vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)
    field_file = rt / "field_text.json"
    target = {"pid": 5678, "class": "zen", "title": "Docs — Zen Browser", "address": "0xdef"}

    # Sighting 1 in Zen: ignored prompt -> stays in Learning Inbox; spelling fixed too
    stream1 = [
        {"before": "Check the omarchi and kwarzle docs.", "after": ""},
        {"before": "Check the Omarchy and Kwarzle docs.", "after": ""},
        {"before": "", "after": ""},  # submitted/cleared -> previous edit is final
    ]
    idx = {"i": 0}

    def reader_for(frames):
        idx["i"] = 0

        def _read():
            pos = min(idx["i"], len(frames) - 1)
            idx["i"] += 1
            field_file.write_text(json.dumps(frames[pos]), encoding="utf-8")
            return ew.im_field(field_path=field_file, rt=rt)

        return _read

    res1 = ew.watch_field(
        "check the omarchi and kwarzle docs",
        "Check the omarchi and kwarzle docs.",
        target,
        rt=rt,
        field_reader=reader_for(stream1),
        active_addr_fn=lambda: "0xdef",
        ticks=(0, 0, 0),
        sleep_fn=lambda _s: None,
        notify_fn=lambda *_a, **_kw: "",  # ignored prompt
    )
    assert res1["ask"] == [("omarchi", "Omarchy")]
    assert res1["spellings"] == ["Kwarzle"]
    assert "omarchi" not in learn.load()

    # Sighting 2 in Zen: auto-promotes with source="edit-watch"!
    stream2 = [
        {"before": "Install omarchi on the laptop.", "after": ""},
        {"before": "Install Omarchy on the laptop.", "after": ""},
        {"before": "Install Omarchy on the laptop.", "after": ""},
    ]
    res2 = ew.watch_field(
        "install omarchi on the laptop",
        "Install omarchi on the laptop.",
        target,
        rt=rt,
        field_reader=reader_for(stream2),
        active_addr_fn=lambda: "0xdef",
        ticks=(0, 0, 0),
        sleep_fn=lambda _s: None,
        notify_fn=lambda *_a, **_kw: "",
    )
    assert ("omarchi", "Omarchy") in res2["promoted"]
    entries = {e["heard"]: e for e in learn.load_entries()}
    assert entries["omarchi"]["source"] == "edit-watch"

    # Revert test: an applied rule ("versal" -> "Vercel") gets changed back twice -> demoted!
    learn.remember("versal", "Vercel", "seed")
    for expected_action in ("recorded", "demoted"):
        rev_stream = [
            {"before": "Check the Vercel config today.", "after": ""},
            {"before": "Check the versal config today.", "after": ""},
            {"before": "Check the versal config today.", "after": ""},
        ]
        res_rev = ew.watch_field(
            "check the versal config today",
            "Check the Vercel config today.",
            target,
            applied_rules=[("versal", "Vercel")],
            rt=rt,
            field_reader=reader_for(rev_stream),
            active_addr_fn=lambda: "0xdef",
            ticks=(0, 0, 0),
            sleep_fn=lambda _s: None,
            notify_fn=lambda *_a, **_kw: "",
        )
        assert res_rev["reverts"] == [("versal", "Vercel", expected_action)]
    assert {e["heard"]: e for e in learn.load_entries()}["versal"]["demoted"] is True


def test_edit_watch_never_learns_from_private_takes_or_sensitive_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _conf, _state, rt, learn, _gate, ew, _vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)
    field_file = rt / "field_text.json"

    def fixed_reader(payload: dict):
        def _read():
            field_file.write_text(json.dumps(payload), encoding="utf-8")
            return ew.im_field(field_path=field_file, rt=rt)
        return _read

    base_target = {"pid": 123, "class": "com.t3tools.T3Code", "title": "shuush", "address": "0x1"}

    # 1. Private take
    r1 = ew.watch_field(
        "open hyper land config", "Open hyper land config.", base_target,
        via="private", rt=rt, field_reader=fixed_reader({"before": "Open Hyprland config.", "after": ""}),
        active_addr_fn=lambda: "0x1", ticks=(0, 0), sleep_fn=lambda _s: None, notify_fn=lambda *_a, **_kw: "learn",
    )
    assert r1 == {"skipped": "private"}

    # 2. sensitive_field marker
    (rt / "sensitive_field").write_text("1", encoding="utf-8")
    r2 = ew.watch_field(
        "open hyper land config", "Open hyper land config.", base_target,
        rt=rt, field_reader=fixed_reader({"before": "Open Hyprland config.", "after": ""}),
        active_addr_fn=lambda: "0x1", ticks=(0, 0), sleep_fn=lambda _s: None, notify_fn=lambda *_a, **_kw: "learn",
    )
    assert r2 == {"skipped": "sensitive_field"}
    (rt / "sensitive_field").unlink()

    # 3. field_text.off pause marker
    (rt / "field_text.off").write_text("1", encoding="utf-8")
    r3 = ew.watch_field(
        "open hyper land config", "Open hyper land config.", base_target,
        rt=rt, field_reader=fixed_reader({"before": "Open Hyprland config.", "after": ""}),
        active_addr_fn=lambda: "0x1", ticks=(0, 0), sleep_fn=lambda _s: None, notify_fn=lambda *_a, **_kw: "learn",
    )
    assert r3 == {"skipped": "sensitive_field"}
    (rt / "field_text.off").unlink()

    # 4. field_text.json marks password / sensitive purpose
    r4 = ew.watch_field(
        "open hyper land config", "Open hyper land config.", base_target,
        rt=rt, field_reader=fixed_reader({"before": "Open Hyprland config.", "after": "", "purpose": "password"}),
        active_addr_fn=lambda: "0x1", ticks=(0, 0), sleep_fn=lambda _s: None, notify_fn=lambda *_a, **_kw: "learn",
    )
    assert r4 == {"skipped": "password_field"}

    # 5. Zen sign-in page title
    zen_login = {"pid": 124, "class": "zen", "title": "Sign in - Google Accounts — Zen", "address": "0x2"}
    r5 = ew.watch_field(
        "open hyper land config", "Open hyper land config.", zen_login,
        rt=rt, field_reader=fixed_reader({"before": "Open Hyprland config.", "after": ""}),
        active_addr_fn=lambda: "0x2", ticks=(0, 0), sleep_fn=lambda _s: None, notify_fn=lambda *_a, **_kw: "learn",
    )
    assert r5 == {"skipped": "private_window"}

    # 6. Password manager class
    kp = {"pid": 125, "class": "org.keepassxc.KeePassXC", "title": "Vault.kdbx", "address": "0x3"}
    r6 = ew.watch_field(
        "open hyper land config", "Open hyper land config.", kp,
        rt=rt, field_reader=fixed_reader({"before": "Open Hyprland config.", "after": ""}),
        active_addr_fn=lambda: "0x3", ticks=(0, 0), sleep_fn=lambda _s: None, notify_fn=lambda *_a, **_kw: "learn",
    )
    assert r6 == {"skipped": "private_window"}

    assert learn.load() == {} and learn.load_candidates() == {}


# ── 3. Vocabulary harvesting with consent + Parakeet 128-term rescoring sync ──


def test_vocab_harvest_consent_and_parakeet_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf, state, _rt, learn, _gate, _ew, vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)

    # Fake ~/Projects with git repos & branches
    proj = tmp_path / "Projects"
    for repo, branch in [("kittymux", "feat/quickshell-bar"), ("ototabi", "kitsune/live")]:
        g = proj / repo / ".git"
        (g / "refs" / "heads" / Path(branch).parent).mkdir(parents=True, exist_ok=True)
        (g / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
        (g / "refs" / "heads" / branch).write_text("0" * 40 + "\n", encoding="utf-8")

    # Fake history with frequent capitalised terms + private take (must be ignored)
    hist = state / "history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"via": "groq", "raw": "check barionene", "clean": "Please check Barionene and Kaligo today."}),
        json.dumps({"via": "groq", "raw": "ship barionene", "clean": "Ship Barionene with Kaligo soon."}),
        json.dumps({"via": "private", "raw": "secret", "clean": "SecretVaultName should never harvest."}),
    ]) + "\n", encoding="utf-8")

    vocab_file = conf / "vocabulary.txt"
    vocab_file.write_text("Hyprland\n", encoding="utf-8")

    found = vh.harvest_all(
        projects_dir=proj,
        history_path=hist,
        kitty_runner=lambda: ["nvim kuwai-shell", "hyprctl monitor"],
        clients_runner=lambda: [
            {"class": "com.t3tools.T3Code", "title": "T3 Code — Quickshell"},
            {"class": "zen", "title": "Sign in — SuperSecretBank"},
        ],
        save=True,
    )
    terms = {item["term"].lower(): item for item in found}
    assert "kittymux" in terms
    assert "ototabi" in terms
    assert "barionene" in terms and "kaligo" in terms
    assert "t3 code" in terms and "quickshell" in terms
    assert "secretvaultname" not in terms
    assert "supersecretbank" not in terms

    # Consent invariant: vocabulary.txt MUST NOT be silently modified!
    assert learn.read_vocab("") == ["Hyprland"]
    assert learn.load_vocab_candidates()

    # Accept candidate -> flows into vocabulary.txt AND parakeet-vocabulary.txt
    assert learn.accept_vocab_candidate("Barionene", scope="") is True
    assert "Barionene" in learn.read_vocab("")
    assert "Barionene" in (conf / "parakeet-vocabulary.txt").read_text(encoding="utf-8")

    # Reject candidate -> stays excluded on re-harvest
    learn.reject_vocab_candidate("ototabi")
    found2 = vh.harvest_all(projects_dir=proj, history_path=hist, kitty_runner=lambda: [], clients_runner=lambda: [], save=True)
    assert all(x["term"].lower() != "ototabi" for x in found2)

    # Parakeet 128-term size limit & usefulness ordering
    learn.remember("barionet", "Barionene", "edit-watch")
    learn.record_applied("barionet", "Barionene")
    learn.record_applied("barionet", "Barionene")
    extra_vocab = ["Hyprland", "Barionene"] + [f"TermItem{i:03d}" for i in range(160)]
    vocab_file.write_text("\n".join(extra_vocab) + "\n", encoding="utf-8")
    res = learn.sync_parakeet_vocab(history_path=hist)
    assert res["count"] == learn.PARAKEET_MAX_TERMS == 128
    assert res["trimmed"] > 0
    pk_lines = [l.strip() for l in (conf / "parakeet-vocabulary.txt").read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")]
    assert len(pk_lines) == 128
    # Most useful term (active edit-watch correction + frequent in history) must be near the very top!
    assert pk_lines.index("Barionene") < 5


# ── 4. Per-app vocabulary & scoped corrections ──


def test_per_app_vocabulary_and_scoped_corrections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conf, _state, _rt, learn, gate, _ew, _vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)
    (conf / "config.toml").write_text('[text]\nreplacements = {}\n', encoding="utf-8")

    learn.add_vocab_term("Hyprland", scope="")
    learn.add_vocab_term("kittymux", scope="terminal")
    learn.remember("workeries", "worktrees", "user", scope="terminal")
    learn.remember("hyperlane", "Hyprland", "user", scope="")
    learn.sync_replacements()

    # Global config.toml replacements MUST include global rule but NOT terminal-only rule
    toml_text = (conf / "config.toml").read_text(encoding="utf-8")
    assert '"hyperlane" = "Hyprland"' in toml_text
    assert '"workeries"' not in toml_text

    # In terminal / T3 Code: scoped vocab and scoped rule apply!
    assert "kittymux" in gate.vocab_terms(mode="terminal")
    assert "kittymux" in gate.vocab_terms(mode="default", cls="com.t3tools.T3Code")
    assert gate.hard_apply("check workeries in hyperlane", mode="terminal") == "check worktrees in Hyprland"
    assert gate.finish("open kitty mux", mode="terminal") == "open kittymux"

    # In chat / Zen browser: terminal-only vocab & rule do NOT fire!
    assert "kittymux" not in gate.vocab_terms(mode="chat", cls="Slack")
    assert gate.hard_apply("check workeries in hyperlane", mode="chat", cls="Slack") == "check workeries in Hyprland"
    assert gate.finish("open kitty mux", mode="chat", cls="Slack") == "open kitty mux"

    # Gate CLI `vocab` subcommand respects mode & class
    out_term = subprocess.run(
        [sys.executable, "-I", str(LIBEXEC / "voxtype_pp_gate.py"), "vocab", "terminal", "kitty"],
        env=_env(tmp_path), capture_output=True, text=True, check=True,
    ).stdout
    out_chat = subprocess.run(
        [sys.executable, "-I", str(LIBEXEC / "voxtype_pp_gate.py"), "vocab", "chat", "Slack"],
        env=_env(tmp_path), capture_output=True, text=True, check=True,
    ).stdout
    assert "kittymux" in out_term and "Hyprland" in out_term
    assert "kittymux" not in out_chat and "Hyprland" in out_chat


# ── 5. Studio Learned view & inbox helpers ──


def test_studio_learned_view_helpers_and_css(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _conf, _state, _rt, learn, _gate, _ew, _vh, _lr = _load_sandbox_modules(tmp_path, monkeypatch)
    spec = importlib.util.spec_from_file_location("voxtype_studio_test", str(LIBEXEC / "voxtype-studio.py"))
    vs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vs)

    # Validate GTK4 CSS compiles cleanly
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    errs: list[str] = []
    prov = Gtk.CssProvider()
    prov.connect("parsing-error", lambda _p, _s, e: errs.append(str(e)))
    prov.load_from_data(vs.CSS)
    assert not errs, f"Studio CSS parse errors: {errs}"

    # Format learned row in plain language
    learn.remember("huai", "kuwai", "edit-watch", scope="terminal")
    learn.record_applied("huai", "kuwai")
    entry = learn.load_entries()[0]
    row = vs.format_learned_row(entry)
    assert row["origin"] == "Learned from your edit"
    assert "Helped 1×" in row["meta"]
    assert row["scope"] == "Code & terminal only"
    assert row["mode_badge"] == "Auto-fix"

    # Unified inbox includes both pending fixes and harvested vocab candidates
    learn.save_candidates({"omarchi\tOmarchy": {"n": 1, "asked": False, "via": "edit-watch (T3 Code)"}})
    learn.save_vocab_candidates({"kittymux": {"term": "kittymux", "scope": "terminal", "score": 5.0, "summary": "From ~/Projects"}})
    inbox = vs.build_inbox_items([], learn.load())
    kinds = {it["kind"] for it in inbox}
    assert kinds == {"fix", "vocab"}

    # Reverting an applied rule in Studio's inspector records an undo
    learn.remember("versal", "Vercel", "seed")
    vs.learn_from_pair("check versal today", "Check Vercel today.", "Check versal today.")
    e_v = {x["heard"]: x for x in learn.load_entries()}["versal"]
    assert e_v["undone"] == 1


# ── 6. Offline read-only history replay check ──


def test_offline_replay_readonly_and_scoping_reduces_false_positives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf, state, _rt, learn, _gate, _ew, _vh, lr = _load_sandbox_modules(tmp_path, monkeypatch)
    hist = state / "history.jsonl"
    hist.write_text("\n".join([
        json.dumps({"mode": "terminal", "via": "groq", "raw": "open hyperlane config", "clean": "Open Hyprland config."}),
        json.dumps({"mode": "terminal", "via": "groq", "raw": "fix box type bug", "clean": "Fix box type bug."}),
        json.dumps({"mode": "chat", "via": "groq", "raw": "the bench is in the park", "clean": "The bench is in the park."}),
        json.dumps({"mode": "terminal", "via": "groq", "raw": "run the bench suite", "clean": "Run the Benchmark suite."}),
    ]) + "\n", encoding="utf-8")
    tsv = conf / "corrections.tsv"
    tsv.write_text(
        "hyperlane\tHyprland\tseed\t1\n"
        "box type\tvoxtype\tseed\t1\n"
        "bench\tBenchmark\tuser\t1\tterminal\n",
        encoding="utf-8",
    )
    before_mtime = hist.stat().st_mtime_ns

    unscoped = lr.replay(history_path=hist, corrections_path=tsv, use_scopes=False)
    scoped = lr.replay(history_path=hist, corrections_path=tsv, use_scopes=True)

    # Strictly read-only
    assert hist.stat().st_mtime_ns == before_mtime
    assert unscoped["false_positives"] == 1
    assert scoped["false_positives"] == 0
    assert scoped["false_positive_rate_per_word"] <= unscoped["false_positive_rate_per_word"]
    assert scoped["confirmed_fixes"] >= 2
    assert scoped["new_fixes"] >= 1

    # CLI `shuush learn replay --json`
    out = subprocess.run(
        [str(SHUUSH), "learn", "replay", "--history", str(hist), "--json"],
        env=_env(tmp_path), capture_output=True, text=True, check=True,
    ).stdout
    parsed = json.loads(out)
    assert parsed["takes"] == 4 and parsed["false_positives"] == 0
