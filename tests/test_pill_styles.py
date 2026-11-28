"""The KitsuneHud pill's styles: what it reads, and that the default is the ticker."""
import re
from pathlib import Path

HUD = Path(__file__).resolve().parents[1] / "osd/kitsune-hud/KitsuneHud.qml"
SRC = HUD.read_text()


def test_styles_and_their_default():
    assert 'readonly property var pillStyles: ["field", "words", "ticker", "status"]' in SRC
    assert re.search(r'envStyle: _style\(Quickshell\.env\("VOXTYPE_OSD_PILL"\)\) \|\| "ticker"', SRC)


def test_the_host_wishes_are_read_beside_its_pid_file():
    assert '"/kuwai/voxtype-pill.json"' in SRC
    assert "root.targetBox = t && t.w > 0 && t.h > 0 ? t : null" in SRC


def test_the_field_style_keeps_its_meter_and_width_math():
    assert "readonly property bool calmMeter: pillStyle !== \"field\"" in SRC
    assert "root.calmMeter ? Math.max(0, spacingW) : spacingW" in SRC


def test_text_scale_is_clamped():
    assert "Math.max(1, Math.min(2, n))" in SRC


def test_words_style_never_waits_and_status_never_shows_words():
    gate = SRC[SRC.index("function _gateWords()"):]
    gate = gate[: gate.index("\n    }\n")]
    assert 'pillStyle === "status"' in gate and "wordsOk = false" in gate
    assert 'pillStyle === "words"' in gate and "wordsOk = true" in gate


def test_the_ticker_is_one_line_at_every_text_size():
    assert 'root.pillStyle === "ticker" && !root.hasText ? tickH' in SRC
    assert "readonly property real tickH: Math.ceil(Math.max(tickLine.height, statusLine.height, 16 * root.ts)) + 2 * Math.round(6 * root.ts)" in SRC


def test_the_ticker_reaches_only_a_little_into_the_window():
    y = SRC[SRC.index("function _cardY(h)"):]
    y = y[: y.index("\n    }\n")]
    assert "Math.max(Number(t.seam) - h / 2, bottom - seatReach)" in y
    assert "readonly property int seatReach: 8" in SRC


def test_inside_a_maximised_window_it_sits_in_the_bottom_right_corner():
    assert 'property string insideSeat: "right"' in SRC
    x = SRC[SRC.index("function _cardX(w)"):]
    x = x[: x.index("\n    }\n")]
    assert 'insideSeat === "left"' in x and 'insideSeat === "right"' in x


def test_it_glides_on_the_hosts_spring_and_stops_for_reduced_motion():
    assert "const g = w.glide || {};" in SRC
    assert "root.hostStill = w.still === true;" in SRC
    assert '=== "1" || hostStill' in SRC
    assert "FrameAnimation { running: root.sliding; onTriggered: root._slide(frameTime) }" in SRC
    move = SRC[SRC.index("function _moveTo(apply)"):]
    move = move[: move.index("\n    }\n")]
    assert "reducedMotion" in move and "slideX = fromX - Math.round(_cardX(card.width));" in move
    assert "x: Math.round(root._cardX(width) + root.slideX)" in SRC


def test_the_ticker_draws_words_as_plain_text():
    for name in ("tickOld", "tickFresh"):
        block = SRC[SRC.index("id: " + name):]
        block = block[: block.index("}")]
        assert "textFormat: Text.PlainText" in block


def test_no_host_no_wait_for_one():
    assert 'if (pillStyle === "ticker" && hostSeen && !onTarget) entranceHold.restart();' in SRC


def test_the_pill_reads_the_target_window_again_when_a_take_starts():
    # voxtype-toggle.sh swaps target_window (a link moved into place), so a
    # watch on the old file goes stale and the pill named the previous app.
    assert 'onVstateChanged: if (vstate === "recording" || vstate === "streaming") targetWindowFile.reload()' in SRC
    assert "id: targetWindowFile" in SRC
