import math

from conftest import ink
from lineus_mcp.geometry import catmull_rom, fit, hatch_polygon, order_human

SQUARE = [(0, 0), (20, 0), (20, 20), (0, 20)]


def rows(strokes):
    seg = [(a, b) for s in strokes for a, b in zip(s, s[1:])]
    return [p for p in seg if abs(p[0][1] - p[1][1]) < 1e-6 and abs(p[0][0] - p[1][0]) > 1e-6]


def test_hatch_rows_spacing_and_serpentine():
    h = hatch_polygon(SQUARE, 0, 2.0)
    r = rows(h)
    assert len(r) == 10
    assert all(abs(abs(a[0] - b[0]) - 20) < 1e-6 for a, b in r)
    assert abs(r[1][0][1] - r[0][0][1] - 2.0) < 1e-6
    assert len(h) == 1, "serpentine joins the spans into one stroke"


def test_hatch_is_rotation_consistent():
    assert ink(hatch_polygon(SQUARE, 0, 2.0)) == ink(hatch_polygon(SQUARE, 90, 2.0))
    assert 150 < sum(math.dist(a, b) for s in hatch_polygon(SQUARE, 45, 2.0)
                     for a, b in zip(s, s[1:]) if math.dist(a, b) < 30) < 400


def test_catmull_rom_closed_and_interpolating():
    c = catmull_rom(SQUARE, 12, closed=True)
    assert math.dist(c[0], c[-1]) < 1e-6
    assert all(min(math.dist(p, q) for q in c) < 1e-6 for p in SQUARE)


def test_catmull_rom_eight_points_make_a_circle():
    cp = [(20 + 10 * math.cos(a * math.pi / 4), 20 + 10 * math.sin(a * math.pi / 4))
          for a in range(8)]
    r = [math.hypot(x - 20, y - 20) for x, y in catmull_rom(cp, 16, closed=True)]
    assert max(r) - min(r) < 0.25


def test_centripetal_spline_has_no_cusp_where_points_bunch():
    c = catmull_rom([(0, 0), (5, 0), (5.2, 0), (5.4, 0), (10, 0), (10, 8)], 12)
    reversals = sum(1 for a, b, d in zip(c, c[1:], c[2:])
                    if (b[0] - a[0]) * (d[0] - b[0]) + (b[1] - a[1]) * (d[1] - b[1]) < -1e-9)
    assert reversals == 0


def test_fit_is_uniform_and_centred():
    out = fit([[(0, 0), (10, 5)]], 0, 0, 40, 40)
    (x0, y0), (x1, y1) = out[0]
    assert (x1 - x0) == 40 and (y1 - y0) == 20 and y0 == 10


def test_order_human_reads_top_row_first():
    top, bottom = [(0, 0), (5, 0)], [(0, 20), (5, 20)]
    assert order_human([bottom, top])[0] == top
