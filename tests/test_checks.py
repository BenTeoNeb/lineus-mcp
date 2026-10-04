import math

from conftest import example
from lineus_mcp.checks import _proximity, lint_scene, small_feature_check
from lineus_mcp.geometry import hatch_polygon
from lineus_mcp.scene import compile_scene, compile_scene_full


def warnings_for(scene):
    st, meta = compile_scene_full(scene)
    return lint_scene(st, meta)


def test_corner_warning_only_fires_on_real_corners():
    assert small_feature_check([[(0, 0), (6, 0), (6, 6), (0, 6), (0, 0)]], 5)
    circle = [[(3 * math.cos(a * math.pi / 30), 3 * math.sin(a * math.pi / 30))
               for a in range(61)]]
    assert not small_feature_check(circle, 5), "a circle has no corners to lose"
    blob = [(16 * math.cos(a * math.pi / 60), 16 * math.sin(a * math.pi / 60)) for a in range(121)]
    assert not small_feature_check(hatch_polygon(blob, 45, 1.15), 5)
    assert not small_feature_check([[(0, 0), (30, 0), (30, 30), (0, 30), (0, 0)]], 5)


def test_quiet_on_the_finished_drawings():
    assert not warnings_for(example("one_line_cat"))
    assert not warnings_for(example("geometric_fox"))


def test_mesh_without_join_is_drawn_twice():
    fox = {k: v for k, v in example("geometric_fox").items() if k != "join"}
    assert any("SAME line twice" in w and "(" in w for w in warnings_for(fox))


def test_an_eye_too_narrow_will_fill_in():
    narrow = {"shapes": [{"paths": [[[30, 20], [35, 19.7], [40, 20], [35, 20.3], [30, 20]]],
                          "smooth": 8}]}
    wide = {"shapes": [{"paths": [[[30, 20], [35, 18.6], [40, 20], [35, 21.4], [30, 20]]],
                        "smooth": 8}]}
    assert any("within" in w for w in warnings_for(narrow))
    assert not warnings_for(wide)


def test_parallels_merge_and_report_where():
    close = {"shapes": [{"paths": [[[10, 10], [40, 10]], [[10, 10.6], [40, 10.6]]]}]}
    assert any("within" in w and "(25,10)" in w for w in warnings_for(close))
    assert not warnings_for({"shapes": [{"paths": [[[10, 10], [40, 10]], [[10, 12], [40, 12]]]}]})


def test_a_crossing_is_not_a_merge():
    assert not warnings_for({"shapes": [{"paths": [[[10, 10], [40, 30]], [[10, 30], [40, 10]]]}]})


def test_the_blotting_harmonograph_is_caught():
    blot = {"shapes": [{"param": {"t": [0, 190, 1400], "x": "40+34*exp(-0.011*t)*sin(2.01*t)",
                                  "y": "31+11*exp(-0.011*t)*sin(3.02*t+1.2)"}}]}
    fixed = {"shapes": [{"param": {"t": [0, 47, 900], "x": "40+33*exp(-0.028*t)*sin(2.01*t)",
                                   "y": "30+10*exp(-0.028*t)*sin(3.02*t+1.2)"}}]}
    assert any("within" in w for w in warnings_for(blot))
    merges = lambda sc: len(_proximity(compile_scene(sc), [])[1])
    assert merges(fixed) < merges(blot)


def test_one_line_gluing_separate_pieces_is_flagged():
    glue = {"shapes": [{"paths": [[[10, 10], [30, 10], [30, 30], [10, 30], [10, 10]],
                                  [[15, 15], [17, 15]], [[23, 15], [25, 15]],
                                  [[18, 24], [22, 24]]]}], "join": {"one_line": True}}
    assert any("glued" in w for w in warnings_for(glue))
    assert not any("glued" in w for w in warnings_for(example("one_line_cat")))
