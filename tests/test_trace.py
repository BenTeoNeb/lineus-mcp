import base64
import io
import math

import pytest
from PIL import Image, ImageDraw

from lineus_mcp.doodles import qd_bundle
from lineus_mcp.expr import ExprError
from lineus_mcp.scene import compile_scene
from lineus_mcp.trace import trace_strokes


def data_url(img: Image.Image) -> str:
    buf = io.BytesIO(); img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def canvas(w=600, h=400):
    im = Image.new("L", (w, h), 255)
    return im, ImageDraw.Draw(im)


def length(s):
    return sum(math.dist(a, b) for a, b in zip(s, s[1:]))


def test_a_ring_traces_as_one_closed_stroke_on_its_centre_line():
    im, d = canvas()
    d.ellipse((200, 100, 400, 300), outline=0, width=10)        # centre line r ~ 95
    st = trace_strokes(data_url(im), resolution=600)
    assert len(st) == 1
    s = st[0]
    assert math.dist(s[0], s[-1]) < 1e-6
    r = [math.hypot(x - 300, y - 200) for x, y in s]
    assert abs(sum(r) / len(r) - 95) < 2 and max(r) - min(r) < 5


def test_a_cross_comes_out_as_lines_through_one_junction():
    im, d = canvas()
    d.line((100, 200, 500, 200), fill=0, width=9)
    d.line((300, 50, 300, 350), fill=0, width=9)
    st = trace_strokes(data_url(im), resolution=600)
    assert len(st) == 4
    ends = [p for s in st for p in (s[0], s[-1])]
    centre = [p for p in ends if math.dist(p, (300, 200)) < 4]
    assert len(centre) == 4 and len({(round(x, 6), round(y, 6)) for x, y in centre}) == 1
    for s in st:   # straight: every point near the axis line
        assert all(min(abs(y - 200), abs(x - 300)) < 3 for x, y in s)


def test_short_separate_strokes_are_kept_and_dust_is_not():
    im, d = canvas()
    d.line((100, 200, 500, 200), fill=0, width=8)
    d.line((290, 120, 310, 120), fill=0, width=6)           # an "eye": short, separate
    d.point([(50, 50), (51, 50), (50, 51)], fill=0)          # dust
    st = trace_strokes(data_url(im), resolution=600)
    assert len(st) == 2 and min(length(s) for s in st) > 10


def test_whiskers_on_a_line_are_pruned():
    im, d = canvas()
    d.line((100, 200, 500, 200), fill=0, width=8)
    d.line((300, 200, 300, 192), fill=0, width=8)            # a bump, not a branch
    assert len(trace_strokes(data_url(im), resolution=600)) == 1


def test_light_on_dark_needs_invert():
    im = Image.new("L", (300, 200), 0)
    ImageDraw.Draw(im).line((20, 100, 280, 100), fill=255, width=8)
    with pytest.raises(ExprError, match="invert"):
        trace_strokes(data_url(im))
    assert len(trace_strokes(data_url(im), invert=True)) == 1


def test_a_doodle_survives_a_render_and_trace_round_trip():
    strokes = [list(zip(xs, ys)) for xs, ys in qd_bundle()["cat"][0]]
    pts = [p for s in strokes for p in s]
    k = 520 / max(max(p[0] for p in pts), max(p[1] for p in pts))
    im, d = canvas(600, 600)
    src = [[(40 + x * k, 40 + y * k) for x, y in s] for s in strokes]
    for s in src:
        d.line(s, fill=0, width=7, joint="curve")
    st = trace_strokes(data_url(im), resolution=600)

    def near(p, lines):
        best = float("inf")
        for s in lines:
            for a, b in zip(s, s[1:]):
                dx, dy = b[0] - a[0], b[1] - a[1]
                L = dx * dx + dy * dy
                t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
                best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
        return best
    assert max(near(p, src) for s in st for p in s) < 4          # nothing invented
    total = sum(length(s) for s in src)
    assert sum(length(s) for s in st) > 0.85 * total              # nothing much lost


def test_trace_is_a_scene_producer_fitted_to_the_page_by_default(tmp_path):
    im, d = canvas()
    d.ellipse((200, 100, 400, 300), outline=0, width=10)
    path = tmp_path / "ring.png"; im.save(path)
    st = compile_scene({"shapes": [{"trace": str(path)}]})
    from lineus_mcp.config import CANVAS_H, CANVAS_W
    assert len(st) == 1
    assert max(y for s in st for _, y in s) == pytest.approx(CANVAS_H, abs=0.01)
    assert max(x for s in st for x, _ in s) <= CANVAS_W + 0.01
    assert compile_scene({"shapes": [{"trace": {"image": str(path)}}]}) == st


@pytest.mark.parametrize("trace,message", [
    ("/no/such/file.png", "no such image"),
    ({"image": "x.png", "colour": 1}, "unknown option"),
    ({"threshold": 100}, "needs \"image\""),
])
def test_trace_errors_are_clear(trace, message):
    with pytest.raises(ExprError, match=message):
        compile_scene({"shapes": [{"trace": trace}]})


def test_a_non_image_file_is_refused_without_echoing_it(tmp_path):
    p = tmp_path / "secret.txt"; p.write_text("hunter2 " * 50)
    with pytest.raises(ExprError) as e:
        compile_scene({"shapes": [{"trace": str(p)}]})
    assert "hunter2" not in str(e.value)
