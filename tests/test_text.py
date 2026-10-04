import math

import pytest

from lineus_mcp.scene import compile_scene
from lineus_mcp.server import list_fonts
from lineus_mcp.text import STROKE_FONTS, stroke_font, strokes_from_stroke_font

HUMAN = {"l": 1, "H": 3, "n": 1, "o": 1, "e": 1, "s": 1, "t": 2, "i": 2}


@pytest.mark.parametrize("name", list(STROKE_FONTS))
def test_handwriting_faces_draw_each_letter_once(name):
    drawn = sum(len(strokes_from_stroke_font(c, name)) for c in HUMAN)
    assert drawn / sum(HUMAN.values()) < 1.45, "Hershey timesr is 3.0x, rowmant 7.2x"


@pytest.mark.parametrize("name, kind", [(k, v[1]) for k, v in STROKE_FONTS.items()])
def test_cursive_welds_and_print_does_not(name, kind):
    n = len(strokes_from_stroke_font("minimum", name))
    assert (n < 7) if kind == "cursive" else (n >= 5)


@pytest.mark.parametrize("name", [k for k, v in STROKE_FONTS.items() if v[1] == "cursive"])
def test_welding_adds_no_hop_longer_than_its_threshold(name):
    glyphs, _kind, join = stroke_font(name)
    for word in ("minimum", "smooth", "handwriting", "jolly", "across"):
        own = {round(math.dist(a, b), 4) for ch in word if ch in glyphs
               for sp in glyphs[ch][0] for a, b in zip(sp, sp[1:])}
        for s in strokes_from_stroke_font(word, name):
            for a, b in zip(s, s[1:]):
                d = math.dist(a, b)
                assert round(d, 4) in own or d <= join + 1e-6


def test_text_falls_back_and_mixes():
    assert len(compile_scene({"shapes": [{"text": "Slow", "font": "casual",
                                          "box": [4, 4, 72, 10]}]})) < 20
    assert compile_scene({"shapes": [{"text": "Slow", "font": "futural", "box": [4, 4, 72, 10]}]})
    assert strokes_from_stroke_font("hi", "nope-not-a-font")
    mixed = {"shapes": [{"text": [{"s": "Print", "font": "casual"},
                                  {"s": "cursive", "font": "italienne"}], "box": [4, 4, 72, 22]}]}
    assert len(compile_scene(mixed)) > 4


def test_list_fonts_reports_the_measurements():
    r = list_fonts()
    by = {f["font"]: f for f in r["handwriting"]}
    assert len(by) == 9
    assert by["brush"]["aperture_mm_at_5mm_cap"] == 0.89
    assert by["allure"]["small_text"] == "closes up"
    assert by["architect"]["small_text"] == "safe"
    assert by["casual"]["small_text"] == "marginal", "it failed on paper at 1.78 mm"
    assert r["recommended"] == {"print": ["neutral", "architect"],
                                "cursive": ["italienne", "cursive2"]}
    assert "hershey" not in r


def test_hershey_is_opt_in_and_deduplicated():
    h = list_fonts(include_hershey=True)["hershey"]
    assert h["distinct_faces"] == 25 and h["advertised_names"] == 32
