"""The KitsuneHud pill's styles: what it reads, and that the default is today's."""
import re
from pathlib import Path

HUD = Path(__file__).resolve().parents[1] / "osd/kitsune-hud/KitsuneHud.qml"
SRC = HUD.read_text()


def test_styles_and_their_default():
    assert 'readonly property var pillStyles: ["field", "words", "ticker", "status"]' in SRC
    assert re.search(r'envStyle: _style\(Quickshell\.env\("VOXTYPE_OSD_PILL"\)\) \|\| "field"', SRC)


def test_the_host_wishes_are_read_beside_its_pid_file():
    assert '"/kuwai/voxtype-pill.json"' in SRC
    assert "root.targetBox = t && t.w > 0 && t.h > 0 ? t : null" in SRC


def test_the_default_keeps_todays_meter_and_width_math():
    assert "readonly property bool calmMeter: pillStyle !== \"field\"" in SRC
    assert "root.calmMeter ? Math.max(0, spacingW) : spacingW" in SRC


def test_text_scale_is_clamped():
    assert "Math.max(1, Math.min(2, n))" in SRC


def test_words_style_never_waits_and_status_never_shows_words():
    gate = SRC[SRC.index("function _gateWords()"):]
    gate = gate[: gate.index("\n    }\n")]
    assert 'pillStyle === "status"' in gate and "wordsOk = false" in gate
    assert 'pillStyle === "words"' in gate and "wordsOk = true" in gate
