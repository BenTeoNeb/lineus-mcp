import json
import math
import os

import pytest

from lineus_mcp.config import DATA_DIR
from lineus_mcp.expr import ExprError
from lineus_mcp.scene import compile_scene, scene_digest

CIRCLE = {"shapes": [{"param": {"t": [0, 6.2832, 200], "x": "33+5.5*cos(t)",
                                "y": "30+5.5*sin(t)"}}]}


def test_a_circle_is_an_expression_not_a_primitive():
    pts = [p for s in compile_scene(CIRCLE) for p in s]
    r = [math.hypot(x - 33, y - 30) for x, y in pts]
    assert max(r) - min(r) < 0.01 and len(pts) == 200


def test_compilation_is_deterministic_and_content_hashed():
    assert compile_scene(CIRCLE) == compile_scene(CIRCLE)
    assert scene_digest(CIRCLE) == scene_digest(json.loads(json.dumps(CIRCLE)))
    other = {"shapes": [{"param": {"t": [0, 1, 2], "x": "t", "y": "t"}}]}
    assert scene_digest(CIRCLE) != scene_digest(other)


def test_repeat_exposes_the_index():
    sc = {"shapes": [{"repeat": 5, "param": {"t": [0, 6.2832, 50], "x": "20+i*8+3*cos(t)",
                                            "y": "20+3*sin(t)"}}]}
    st = compile_scene(sc)
    cx = [sum(p[0] for p in s) / len(s) for s in st]
    assert len(st) == 5 and all(abs(cx[k + 1] - cx[k] - 8) < 0.1 for k in range(4))


def test_fill_adds_hatching():
    sc = {"shapes": [{"param": {"t": [0, 6.2832, 120], "x": "10*cos(t)", "y": "10*sin(t)",
                                "closed": True}, "fill": {"hatch": 45, "spacing": 1.15}}]}
    assert len(compile_scene(sc)) > 1


def test_multi_font_text_block_lands_in_its_box():
    sc = {"shapes": [{"text": [{"s": "Hello", "font": "timesr"},
                               {"s": "World", "font": "scriptc"}], "box": [4, 4, 72, 20]}]}
    pts = [p for s in compile_scene(sc) for p in s]
    assert 3.9 <= min(p[0] for p in pts) and max(p[0] for p in pts) <= 76.1
    assert 3.9 <= min(p[1] for p in pts) and max(p[1] for p in pts) <= 24.1


@pytest.mark.parametrize("bad, message", [
    ({"shapes": [{"nope": 1}]}, "producer"),
    ({"shapes": [{"param": {"t": [0, 1], "x": "t", "y": "t"}}]}, "start, stop, steps"),
    ({"shapes": [{"param": {"t": [0, 1, 10], "x": "wobble", "y": "t"}}]}, "unknown name"),
    ({"nope": []}, "shapes"),
])
def test_errors_are_messages_not_tracebacks(bad, message):
    with pytest.raises(ExprError, match=message):
        compile_scene(bad)


def test_a_scene_is_far_smaller_than_its_coordinates():
    harm = {"shapes": [{"param": {"t": [0, 251.3, 2000],
                                  "x": "40+28*exp(-0.0085*t)*sin(2.01*t)",
                                  "y": "22+18*exp(-0.0085*t)*sin(3.03*t+1.2)"}}]}
    st = compile_scene(harm)
    raw = json.dumps([[[round(x, 2), round(y, 2)] for x, y in s] for s in st])
    assert sum(len(s) for s in st) == 2000
    assert len(raw) > 100 * len(json.dumps(harm))


def test_smooth_expands_control_points():
    sc = {"shapes": [{"path": [[0, 0], [20, 10], [40, 0], [60, 20]], "smooth": True}]}
    assert len(compile_scene(sc)[0]) > 30


@pytest.mark.parametrize("name", sorted(
    f[:-5] for f in os.listdir(os.path.join(DATA_DIR, "examples")) if f.endswith(".json")))
def test_every_bundled_example_compiles(name):
    with open(os.path.join(DATA_DIR, "examples", name + ".json"), encoding="utf-8") as fh:
        assert compile_scene(json.load(fh))
