import math

import pytest

from conftest import example, ink, segment_counts
from lineus_mcp.occlude import closed_polygons, occlude
from lineus_mcp.scene import compile_scene, compile_scene_full


def circle(cx, cy, r, steps=360, **extra):
    return {"param": {"t": [0, 6.283185307179586, steps], "x": f"{cx}+{r}*cos(t)",
                      "y": f"{cy}+{r}*sin(t)", "closed": True}, **extra}


def square(x0, y0, x1, y1, **extra):
    return {"path": [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], **extra}


def test_a_later_circle_hides_the_part_of_an_earlier_one_it_covers():
    # two r=10 circles 10 apart: each covers 120 degrees of the other
    st, meta = compile_scene_full({"occlude": True, "shapes": [circle(20, 20, 10),
                                                               circle(30, 20, 10)]})
    full = 2 * math.pi * 10
    assert ink(st) == pytest.approx(full + full * 2 / 3, rel=0.01)
    assert meta["hidden_mm"] == pytest.approx(full / 3, rel=0.02)
    assert all(math.hypot(x - 30, y - 20) >= 10 - 1e-3 for s in st[:-1] for x, y in s)


def test_occlusion_is_off_unless_asked():
    st = compile_scene({"shapes": [circle(20, 20, 10), circle(30, 20, 10)]})
    assert ink(st) == pytest.approx(2 * 2 * math.pi * 10, rel=0.01)


def test_a_line_under_a_square_is_cut_exactly_at_the_edges():
    st = compile_scene({"occlude": True, "shapes": [{"path": [[0, 10], [40, 10]]},
                                                     square(10, 0, 30, 20)]})
    pieces = sorted((s[0][0], s[-1][0]) for s in st if len(s) == 2)
    assert pieces == [pytest.approx((0, 10)), pytest.approx((30, 40))]


def test_open_strokes_do_not_occlude():
    st = compile_scene({"occlude": True, "shapes": [square(10, 0, 30, 20),
                                                     {"path": [[0, 10], [40, 10]]}]})
    assert ink(st) == pytest.approx(80 + 40)


def test_opaque_false_draws_through():
    sc = {"occlude": True, "shapes": [{"path": [[0, 10], [40, 10]]},
                                      square(10, 0, 30, 20, opaque=False)]}
    assert ink(compile_scene(sc)) == pytest.approx(40 + 80)


def test_a_shared_edge_survives():
    # adjacent squares: the line ON the occluder's boundary is not "under" it
    st = compile_scene({"occlude": True, "shapes": [square(0, 0, 10, 10),
                                                     square(10, 0, 20, 10)]})
    counts = segment_counts(st, q=0.01)
    on_edge = [k for k in counts
               if abs(k[0][0] - 1000) < 2 and abs(k[1][0] - 1000) < 2]
    assert on_edge and ink(st) == pytest.approx(80)


def test_even_odd_a_ring_shape_leaves_its_hole_open():
    ring = {"paths": [[[10, 10], [50, 10], [50, 30], [10, 30], [10, 10]],
                      [[20, 15], [40, 15], [40, 25], [20, 25], [20, 15]]]}
    st = compile_scene({"occlude": True, "shapes": [{"path": [[0, 20], [60, 20]]}, ring]})
    line = sorted((s[0][0], s[-1][0]) for s in st if len(s) == 2)
    assert line == [pytest.approx((0, 10)), pytest.approx((20, 40)), pytest.approx((50, 60))]


def test_a_fill_occludes_and_its_hatching_is_occluded_too():
    sc = {"occlude": True, "shapes": [
        square(0, 0, 20, 20, fill={"hatch": 0, "spacing": 2}),
        square(10, -5, 30, 25, fill={"hatch": 90, "spacing": 2, "outline": False})]}
    st = compile_scene(sc)
    # nothing from the first square survives strictly inside the second
    first = [s for s in st if all(abs(p[1] - s[0][1]) < 1e-9 for p in s)]   # 0-deg hatch
    assert first and all(x <= 10 + 1e-6 for s in first for x, _ in s)


def test_repeat_instances_stack_in_order():
    sc = {"occlude": True, "shapes": [dict(circle("10+i*8", 20, 6), repeat=4)]}
    st, meta = compile_scene_full(sc)
    assert meta["hidden_mm"] > 0
    assert compile_scene(sc) == st


def test_closed_polygons_needs_closure():
    assert closed_polygons([[(0, 0), (1, 0), (1, 1)]]) == []
    assert len(closed_polygons([[(0, 0), (1, 0), (1, 1), (0, 0)]])) == 1
    assert occlude([[(0, 0), (5, 0)]], []) == [[(0, 0), (5, 0)]]


def test_the_landscape_example_hides_what_is_behind():
    sc = example("layered_landscape")
    st, meta = compile_scene_full(sc)
    flat, _ = compile_scene_full({**sc, "occlude": False})
    assert meta["hidden_mm"] > 300 and ink(st) < ink(flat) - 300
    # the hatching never shows below the middle hill (v > 33 everywhere along it)
    hatch = [s for s in st if len(s) >= 2 and abs(s[0][0] - s[1][0]) > 0.3
             and abs((s[1][1] - s[0][1]) / (s[1][0] - s[0][0]) - math.tan(math.radians(60))) < 0.01]
    assert hatch and all(v < 33 for s in hatch for _, v in s)


def test_hatching_cut_by_occlusion_is_left_cut_unless_rejoin_is_asked():
    shapes = [square(0, 0, 20, 20, fill={"hatch": 0, "spacing": 2}),
              square(10, -5, 30, 25, fill={"hatch": 90, "spacing": 2, "outline": False})]
    assert len(compile_scene({"occlude": True, "shapes": shapes})) == 9
    assert len(compile_scene({"occlude": {"rejoin": False}, "shapes": shapes})) == 9


def test_hatching_cut_by_occlusion_is_rejoined_on_request():
    sc = {"occlude": {"rejoin": True}, "shapes": [
        square(0, 0, 20, 20, fill={"hatch": 0, "spacing": 2}),
        square(10, -5, 30, 25, fill={"hatch": 90, "spacing": 2, "outline": False})]}
    st = compile_scene(sc)
    flat_hatch = [s for s in st if len(s) > 2 and all(x <= 10 + 1e-6 for x, _ in s)]
    assert len(st) <= 4 and flat_hatch, "the cut serpentine is one pass again, not 10 lifts"


def test_a_rejoin_never_crosses_the_shape_in_front():
    # a circle's cut points are joined by chords that would run INSIDE the circle
    sc = {"occlude": {"rejoin": True},
          "shapes": [square(0, 0, 30, 20, fill={"hatch": 0, "spacing": 1.15}), circle(15, 10, 6)]}
    st = compile_scene(sc)
    for s in st[:-1]:
        for a, b in zip(s, s[1:]):
            for t in (0.25, 0.5, 0.75):
                x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                assert math.hypot(x - 15, y - 10) > 6 - 0.05
    assert len(st) < 14
