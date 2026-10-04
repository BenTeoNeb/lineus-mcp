"""Hidden-line removal: shapes drawn later hide what lies underneath them.

A pen cannot paint over a line, so "in front of" has to be done by NOT DRAWING the part
of an earlier stroke that a later closed shape covers. That is what turns a pile of
outlines into line art: a paw in front of a body, a head over a collar, stacked hills.
Without it every overlap reads as a see-through wireframe.

Same idea as vpype's occult plugin, done natively: occult needs vpype (Python >= 3.11)
and shapely, and the geometry here is small enough that a segment/edge clip with a grid
index is fast in pure Python.
"""
from __future__ import annotations

import math

from .config import Stroke

CLOSE_MM = 0.3      # a stroke whose ends are this close is a closed outline, so it occludes
EDGE_MM = 0.05      # a line lying ON an occluder's edge is kept: shared edges must survive
MIN_PIECE_MM = 0.2  # drop slivers of a clipped stroke shorter than the robot resolves
_CELL = 2.0         # edge-index cell, mm


def closed_polygons(strokes: list[Stroke]) -> list[Stroke]:
    """The strokes of a shape that enclose an area, as polygons (last point dropped)."""
    out = []
    for s in strokes:
        if len(s) >= 4 and math.dist(s[0], s[-1]) <= CLOSE_MM:
            out.append(list(s[:-1]))
        elif len(s) >= 3 and math.dist(s[0], s[-1]) <= CLOSE_MM:
            out.append(list(s))
    return [p for p in out if abs(_area(p)) > 1e-6]


def _area(poly: Stroke) -> float:
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]))


class Occluder:
    """A set of polygons treated as ONE opaque region under the even-odd rule, so a
    shape drawn as an outer ring plus an inner ring is a donut, not a disc."""

    def __init__(self, polys: list[Stroke]):
        self.polys = [p for p in polys if len(p) >= 3]
        self.edges: list[tuple[float, float, float, float]] = []
        for p in self.polys:
            for a, b in zip(p, p[1:] + p[:1]):
                if a != b:
                    self.edges.append((a[0], a[1], b[0], b[1]))
        self.grid: dict[tuple[int, int], list[int]] = {}
        for k, (x1, y1, x2, y2) in enumerate(self.edges):
            for cell in _cells(x1, y1, x2, y2):
                self.grid.setdefault(cell, []).append(k)
        xs = [q[0] for p in self.polys for q in p]
        ys = [q[1] for p in self.polys for q in p]
        self.box = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        self.pboxes = [(min(q[0] for q in p), min(q[1] for q in p),
                        max(q[0] for q in p), max(q[1] for q in p)) for p in self.polys]

    def covers(self, x: float, y: float, keep_edges: bool = True) -> bool:
        """Inside the region. With keep_edges, points within EDGE_MM of an edge count as
        outside, so a line shared with the occluder's own outline survives."""
        inside = False
        for (x0, y0, x1, y1), p in zip(self.pboxes, self.polys):
            if x0 <= x <= x1 and y0 <= y <= y1 and _pip(x, y, p):
                inside = not inside
        if not inside:
            return self._on_edge(x, y) if not keep_edges else False
        if not keep_edges:
            return True
        return not self._on_edge(x, y)

    def _on_edge(self, x: float, y: float) -> bool:
        cx, cy = int(math.floor(x / _CELL)), int(math.floor(y / _CELL))
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for k in self.grid.get((gx, gy), ()):
                    if _seg_dist(x, y, *self.edges[k]) < EDGE_MM:
                        return True
        return False

    def crossings(self, ax, ay, bx, by) -> list[float]:
        """Parameters t in (0,1) where segment a->b crosses an occluder edge."""
        ts = []
        seen = set()
        for cell in _cells(ax, ay, bx, by):
            for k in self.grid.get(cell, ()):
                if k in seen:
                    continue
                seen.add(k)
                t = _intersect(ax, ay, bx, by, *self.edges[k])
                if t is not None:
                    ts.append(t)
        return ts

    def clip(self, strokes: list[Stroke], keep_edges: bool = True) -> list[Stroke]:
        """Remove the parts of strokes this region covers."""
        if self.box is None:
            return strokes
        bx0, by0, bx1, by1 = self.box
        out: list[Stroke] = []
        for s in strokes:
            sx = [q[0] for q in s]; sy = [q[1] for q in s]
            if not s or max(sx) < bx0 or min(sx) > bx1 or max(sy) < by0 or min(sy) > by1:
                out.append(s)
                continue
            if len(s) == 1:
                if not self.covers(*s[0], keep_edges):
                    out.append(s)
                continue
            cur: Stroke = []
            for a, b in zip(s, s[1:]):
                ts = sorted({0.0, 1.0, *self.crossings(a[0], a[1], b[0], b[1])})
                for t0, t1 in zip(ts, ts[1:]):
                    if t1 - t0 < 1e-9:
                        continue
                    tm = (t0 + t1) / 2
                    hidden = self.covers(a[0] + (b[0] - a[0]) * tm,
                                         a[1] + (b[1] - a[1]) * tm, keep_edges)
                    p0 = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
                    p1 = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
                    if hidden:
                        if len(cur) > 1:
                            out.append(cur)
                        cur = []
                        continue
                    if not cur:
                        cur = [p0]
                    elif math.dist(cur[-1], p0) > 1e-9:
                        cur.append(p0)
                    cur.append(p1)
            if len(cur) > 1:
                out.append(cur)
        return [s for s in out if len(s) == 1 or _length(s) >= MIN_PIECE_MM]


def occlude(below: list[Stroke], polys: list[Stroke], keep_edges: bool = True) -> list[Stroke]:
    """Hide the parts of `below` that the closed polygons `polys` cover.

    keep_edges=True keeps a line lying exactly ON the boundary: two shapes sharing an
    edge both still have it. Pass False for HATCHING under a shape that draws its own
    outline -- a hatch connector running along that outline is a second pass over the
    same line, broken into hatch-spacing pieces that each land with a smear."""
    if not polys or not below:
        return below
    return Occluder(polys).clip(below, keep_edges)


def _length(s: Stroke) -> float:
    return sum(math.dist(a, b) for a, b in zip(s, s[1:]))


def _cells(x1, y1, x2, y2):
    gx0, gx1 = sorted((int(math.floor(x1 / _CELL)), int(math.floor(x2 / _CELL))))
    gy0, gy1 = sorted((int(math.floor(y1 / _CELL)), int(math.floor(y2 / _CELL))))
    if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > 4096:     # a huge segment: bbox cells only
        return [(gx, gy) for gx in (gx0, gx1) for gy in (gy0, gy1)]
    return [(gx, gy) for gx in range(gx0, gx1 + 1) for gy in range(gy0, gy1 + 1)]


def _pip(x, y, poly) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L < 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _intersect(ax, ay, bx, by, cx, cy, dx, dy):
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None                                    # parallel or collinear: no split
    qx, qy = cx - ax, cy - ay
    t = (qx * sy - qy * sx) / den
    u = (qx * ry - qy * rx) / den
    if 1e-9 < t < 1 - 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        return t
    return None


def rejoin_hatch(strokes: list[Stroke], spacing: float, occluders: list[Occluder],
                 reach: float = 1.6) -> list[Stroke]:
    """Re-join hatching that occlusion cut into pieces.

    The hatcher draws its fill as a serpentine: one pass, turning at the boundary. Hiding
    part of it cuts the turns away and leaves every span a separate stroke -- and every
    separate stroke is a pen lift with its landing tick. This walks the pieces again,
    connecting an end to the nearest piece end within `reach` spacings, the same rule the
    hatcher uses. A connector is only allowed where NO occluder covers it: the cut ends lie
    on a front shape's edge, and a chord between them can run inside that shape (any
    curved one), which would draw a line across something that is meant to be in front.
    """
    left = [list(s) for s in strokes if len(s) >= 2]
    if len(left) < 2 or spacing <= 0:
        return left
    lim = reach * spacing

    def clear(a, b) -> bool:
        for k in range(1, 8):
            t = k / 8
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            if any(o.covers(x, y) for o in occluders):
                return False
        return True

    out: list[Stroke] = []
    cur = left.pop(0)
    while left:
        end = cur[-1]
        cands = sorted((math.dist(end, p), k, rev) for k, s in enumerate(left)
                       for rev, p in ((False, s[0]), (True, s[-1])))
        hit = next(((k, rev) for d, k, rev in cands
                    if d <= lim and clear(end, left[k][-1 if rev else 0])), None)
        if hit is not None:
            s = left.pop(hit[0])
            cur.extend(s[::-1] if hit[1] else s)
            continue
        out.append(cur)
        cur = left.pop(0)
    out.append(cur)
    return out
