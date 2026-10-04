"""Pure stroke geometry: simplification, ordering, fitting, splines, hatching."""
from __future__ import annotations

import math

from .config import MIN_SEG_MM, Stroke


def simplify(stroke: Stroke) -> Stroke:
    out = [stroke[0]]
    for p in stroke[1:]:
        if math.dist(p, out[-1]) >= MIN_SEG_MM:
            out.append(p)
    if len(out) == 1 and len(stroke) > 1:
        out.append(stroke[-1])
    return out


def order_strokes(strokes: list[Stroke]) -> list[Stroke]:
    """Greedy nearest-neighbour ordering with stroke reversal (less pen-up travel)."""
    left = [s for s in strokes if s]
    out, cur = [], (0.0, 0.0)
    while left:
        best_i, best_rev, best_d = 0, False, float("inf")
        for i, s in enumerate(left):
            d0, d1 = math.dist(cur, s[0]), math.dist(cur, s[-1])
            if d0 < best_d: best_i, best_rev, best_d = i, False, d0
            if d1 < best_d: best_i, best_rev, best_d = i, True, d1
        s = left.pop(best_i)
        s = s[::-1] if best_rev else s
        out.append(s); cur = s[-1]
    return out


def order_human(strokes: list[Stroke]) -> list[Stroke]:
    """Draw the way a person would: row by row, letter by letter, and within a
    letter in the order the letterform itself wants. Never reverses a stroke.

    Costs more pen-up travel than order_strokes(), but it is a QUALITY setting,
    not just a cosmetic one: drawing a glyph's strokes consecutively approaches
    every junction from nearby and from a consistent side, so servo backlash
    stays correlated and the joins actually meet. Scattering them (as the
    nearest-neighbour order does) decorrelates that error and the joins offset.

    Rows: merge strokes whose VERTICAL extents overlap -- not a distance
    threshold on centres, which merges adjacent text lines (the centres within
    one line spread further than the gap to the next).
    Glyphs: likewise merge by overlapping HORIZONTAL extents within a row. Joined
    scripts legitimately merge into one cluster, which is correct for them.
    Within a glyph the supplied order is kept untouched.
    """
    if not strokes:
        return []
    tagged = list(enumerate(strokes))          # (original index, stroke)
    def vmin(t): return min(p[1] for p in t[1])
    def vmax(t): return max(p[1] for p in t[1])
    def umin(t): return min(p[0] for p in t[1])
    def umax(t): return max(p[0] for p in t[1])

    def merge(items, lo, hi):
        """Group items whose [lo,hi] intervals overlap, in ascending lo order."""
        groups: list[list] = []
        edge = None
        for it in sorted(items, key=lo):
            if edge is None or lo(it) > edge:
                groups.append([it]); edge = hi(it)
            else:
                groups[-1].append(it); edge = max(edge, hi(it))
        return groups

    out: list[Stroke] = []
    for row in merge(tagged, vmin, vmax):             # rows, top to bottom
        for glyph in merge(row, umin, umax):          # glyphs, left to right
            out += [t[1] for t in sorted(glyph)]      # font's own order within the glyph
    return out


def travel_mm(strokes: list[Stroke]) -> float:
    """Total pen-up distance for a given stroke order."""
    d, cur = 0.0, (0.0, 0.0)
    for s in strokes:
        d += math.dist(cur, s[0]); cur = s[-1]
    return d


def fit_params(strokes: list[Stroke], x: float, y: float, w: float, h: float):
    """Uniform scale and offset that fits strokes into the box (x,y,w,h), centred."""
    pts = [p for s in strokes for p in s]
    if not pts:
        return None
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    sw, sh = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    k = min(w / sw, h / sh)
    return k, x + (w - sw * k) / 2 - minx * k, y + (h - sh * k) / 2 - miny * k


def apply_fit(strokes: list[Stroke], params) -> list[Stroke]:
    if params is None:
        return [list(s) for s in strokes]
    k, ox, oy = params
    return [[(px * k + ox, py * k + oy) for px, py in s] for s in strokes]


def fit(strokes: list[Stroke], x: float, y: float, w: float, h: float) -> list[Stroke]:
    """Scale strokes uniformly to fit the box (x,y,w,h) in canvas mm, centred."""
    return apply_fit(strokes, fit_params(strokes, x, y, w, h))


def hatch_polygon(poly: Stroke, angle_deg: float, spacing: float,
                  join: bool = True) -> list[Stroke]:
    """Fill a closed polygon with parallel lines at angle_deg, `spacing` mm apart.

    Scanline fill with the even-odd rule, so holes and concave shapes work. Spans are
    joined SERPENTINE where consecutive ends are close enough that the connector stays
    on the boundary: every pen lift costs a landing smear (item 7), so halving the lift
    count is a quality win as well as a speed one. The 1.6*spacing threshold is what
    keeps the connector from cutting across a concave waist.
    """
    if len(poly) < 3 or spacing <= 0:
        return []
    a = math.radians(angle_deg)
    cf, sf = math.cos(-a), math.sin(-a)
    rot = [(x * cf - y * sf, x * sf + y * cf) for x, y in poly]
    ys = [p[1] for p in rot]
    lo, hi = min(ys), max(ys)
    rows: list[tuple[float, float, float]] = []
    y = lo + spacing / 2.0
    n = len(rot)
    while y < hi:
        xs = []
        for k in range(n):
            (xa, ya), (xb, yb) = rot[k], rot[(k + 1) % n]
            if (ya <= y < yb) or (yb <= y < ya):
                xs.append(xa + (y - ya) * (xb - xa) / (yb - ya))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            if xs[k + 1] - xs[k] > 1e-9:
                rows.append((y, xs[k], xs[k + 1]))
        y += spacing
    strokes: list[Stroke] = []
    cur: Stroke | None = None
    flip = False
    for (yy, xa, xb) in rows:
        seg = [(xb, yy), (xa, yy)] if flip else [(xa, yy), (xb, yy)]
        flip = not flip
        if (join and cur is not None
                and math.hypot(seg[0][0] - cur[-1][0], seg[0][1] - cur[-1][1])
                <= spacing * 1.6):
            cur.extend(seg)
        else:
            if cur:
                strokes.append(cur)
            cur = list(seg)
    if cur:
        strokes.append(cur)
    cb, sb = math.cos(a), math.sin(a)
    return [[(x * cb - y * sb, x * sb + y * cb) for x, y in s] for s in strokes]


def catmull_rom(points: Stroke, samples: int = 12, closed: bool = False) -> Stroke:
    """Smooth curve through control points -- CENTRIPETAL Catmull-Rom (alpha=0.5).

    Centripetal, not uniform: the uniform parameterisation puts cusps and self-
    intersecting loops wherever control points bunch up, which is exactly where a
    hand-drawn figure has them. Centripetal is provably free of both.

    This is what makes hand-authored art affordable. A figure is ~30 control points
    instead of ~400 sampled ones, and the curve stays smooth under the firmware's
    corner blending rather than fighting it.
    """
    pts = [tuple(map(float, p)) for p in points]
    if len(pts) < 3:
        return pts
    if closed and math.dist(pts[0], pts[-1]) > 1e-9:
        pts.append(pts[0])
    if closed:
        ctrl = [pts[-2]] + pts + [pts[1]]
    else:
        ctrl = [pts[0]] + pts + [pts[-1]]
    out: Stroke = []
    for i in range(len(ctrl) - 3):
        p0, p1, p2, p3 = ctrl[i:i + 4]
        # knot spacing by sqrt of chord length = centripetal
        t0 = 0.0
        t1 = t0 + max(math.dist(p0, p1), 1e-9) ** 0.5
        t2 = t1 + max(math.dist(p1, p2), 1e-9) ** 0.5
        t3 = t2 + max(math.dist(p2, p3), 1e-9) ** 0.5
        for s in range(samples):
            t = t1 + (t2 - t1) * s / samples

            def lerp(a, b, ta, tb, tt):
                if tb - ta < 1e-12:
                    return a
                k = (tt - ta) / (tb - ta)
                return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)

            a1 = lerp(p0, p1, t0, t1, t)
            a2 = lerp(p1, p2, t1, t2, t)
            a3 = lerp(p2, p3, t2, t3, t)
            b1 = lerp(a1, a2, t0, t2, t)
            b2 = lerp(a2, a3, t1, t3, t)
            out.append(lerp(b1, b2, t1, t2, t))
    out.append(pts[-1])
    return out
