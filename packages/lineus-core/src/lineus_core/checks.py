"""Drawing checks that report where a drawing will fail on this machine."""
from __future__ import annotations

import math

from .config import SMALL_FEATURE_MM, Stroke


def small_feature_check(strokes: list[Stroke], speed: int) -> list[str]:
    """Corners are rounded by a roughly constant ~1-2 mm radius, so small shapes
    lose their corners entirely. Warn about strokes tighter than SMALL_FEATURE_MM.

    Only strokes that actually HAVE a corner count. An earlier version warned about any
    dense polyline under the threshold, so once scenes could emit sampled curves it fired
    on every small circle (no corners at all) and on short hatch spans -- noise on exactly
    the drawings the check was meant to help.
    """
    worst = None
    for st in strokes:
        if len(st) < 3:
            continue                      # a line has no corners to lose
        us = [p[0] for p in st]; vs = [p[1] for p in st]
        size = max(max(us) - min(us), max(vs) - min(vs))
        if size >= SMALL_FEATURE_MM or size < 1.5:
            continue                      # big enough, or a speck the dot guidance covers
        sharp = False
        for a, b, c in zip(st, st[1:], st[2:]):
            v1 = (b[0] - a[0], b[1] - a[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            n1, n2 = math.hypot(*v1), math.hypot(*v2)
            if n1 < 1e-9 or n2 < 1e-9:
                continue
            cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
            if cosang < 0.77:             # turn sharper than ~40 degrees
                sharp = True
                break
        if sharp and (worst is None or size < worst):
            worst = size
    if worst is None:
        return []
    msg = (f"smallest cornered shape is {worst:.1f} mm; corners are rounded by ~1-2 mm "
           f"regardless of size, so shapes under {SMALL_FEATURE_MM:.0f} mm lose their corners")
    if speed > 5:
        msg += f" — speed S{speed} makes this worse, try speed=1..5"
    return [msg]


# Each of these is a mistake actually made while iterating drawings for this machine,
# turned into something that fires BEFORE the paper. They report WHERE, because every
# fix was made at a specific spot. What they cannot catch is bad drawing: proportion,
# silhouette, character. That knowledge lives in the server instructions instead.
MERGE_MM = 1.1      # measured: two passes closer than ~1.15 mm fuse into one blot


TOUCH_MM = 0.15     # closer than this the two passes are ON TOP of each other


MERGE_RUN_MM = 3.0  # length they must run close before it is a blot. 3 mm, not 2: two


                    # edges meeting at angle a stay within MERGE_MM for ~1/sin(a) mm, so
                    # 3 mm flags slivers under ~18 deg and spares the 20-30 deg corners
                    # that low-poly art is made of
DUP_RUN_MM = 2.0    # length two passes must OVERLAP to be a duplicate. Measured as a run,


                    # not per segment: a crossing puts tiny segments in the same cells too
SAME_ARC_MM = 3.0   # along one stroke, neighbours nearer than this in arc are just the line


def _point_in_poly(p, poly) -> bool:
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def _dist_to_poly(p, poly) -> float:
    best = float("inf")
    for a, b in zip(poly, poly[1:] + poly[:1]):
        ax, ay = a; bx, by = b
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L < 1e-12 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
        best = min(best, math.dist(p, (ax + t * dx, ay + t * dy)))
    return best


def _proximity(strokes: list[Stroke], fills: list[Stroke]) -> tuple[list, list]:
    """Where two passes of the pen run alongside each other.

    Returns (overlaps, merges), each a list of (u, v, closest_mm, run_mm), one per place:
      overlaps  passes ON TOP of each other for >= DUP_RUN_MM: something drawn twice
      merges    passes within MERGE_MM without touching for >= MERGE_RUN_MM: a blot
    Both are measured as RUNS along the line. A crossing brings two passes together too,
    but only briefly, which is exactly what separates it from either fault.
    """
    pts = []                                    # (u, v, stroke, arc)
    for sid, s in enumerate(strokes):
        arc = 0.0
        for a, b in zip(s, s[1:]):
            d = math.dist(a, b)
            if d < 1e-9:
                continue
            n = max(1, int(d / 0.25))
            for k in range(n):
                t = k / n
                pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, sid, arc + d * t))
            arc += d
        if len(s) >= 2:
            pts.append((s[-1][0], s[-1][1], sid, arc))
    if not pts:
        return [], []
    boxes = []
    for f in fills:
        xs = [q[0] for q in f]; ys = [q[1] for q in f]
        boxes.append((min(xs) - 0.6, min(ys) - 0.6, max(xs) + 0.6, max(ys) + 0.6, f))

    def in_fill(p):
        for x0, y0, x1, y1, f in boxes:
            if x0 <= p[0] <= x1 and y0 <= p[1] <= y1:
                if _point_in_poly(p, f) or _dist_to_poly(p, f) < 0.6:
                    return True
        return False

    cell = MERGE_MM
    grid: dict = {}
    for i, (u, v, _s, _a) in enumerate(pts):
        grid.setdefault((int(u // cell), int(v // cell)), []).append(i)
    near = [float("inf")] * len(pts)
    for i, (u, v, sid, arc) in enumerate(pts):
        cx, cy = int(u // cell), int(v // cell)
        best = float("inf")
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j in grid.get((gx, gy), ()):
                    uj, vj, sj, aj = pts[j]
                    if sj == sid and abs(aj - arc) <= SAME_ARC_MM:
                        continue
                    d = math.hypot(uj - u, vj - v)
                    if d < best:
                        best = d
        near[i] = best
    fill_mask = [in_fill((u, v)) for u, v, _s, _a in pts]

    def runs(pred, min_len):
        found, i = [], 0
        while i < len(pts):
            if not pred(i):
                i += 1
                continue
            j = i
            while j + 1 < len(pts) and pts[j + 1][2] == pts[i][2] and pred(j + 1):
                j += 1
            length = pts[j][3] - pts[i][3]
            if length >= min_len:
                m = (i + j) // 2
                found.append((pts[m][0], pts[m][1], min(near[i:j + 1]), length))
            i = j + 1
        clusters = []                           # one report per place, not per pass
        for f in sorted(found, key=lambda q: -q[3]):
            if all(math.dist(f[:2], c[:2]) > 3.0 for c in clusters):
                clusters.append(f)
        return clusters

    overlaps = runs(lambda i: near[i] <= TOUCH_MM and not fill_mask[i], DUP_RUN_MM)
    merges = runs(lambda i: TOUCH_MM < near[i] < MERGE_MM and not fill_mask[i], MERGE_RUN_MM)
    return overlaps, merges


def lint_scene(final: list[Stroke], meta: dict) -> list[str]:
    """Drawing-quality warnings, each with a location in page millimetres."""
    out = []
    overlaps, merges = _proximity(final, meta.get("fills", []))
    if overlaps:
        where = ", ".join(f"({u:.0f},{v:.0f})" for u, v, _d, _l in overlaps[:3])
        out.append(f"{len(overlaps)} place(s) where the pen goes over the SAME line twice "
                   f"(e.g. {where}). The second pass lands slightly off the first, so the "
                   f"edge reads as doubled. If this is a mesh, add \"join\": "
                   f"{{\"explode\": true, \"dedupe\": true}}.")
    for u, v, d, length in merges[:5]:
        out.append(f"two lines stay within {MERGE_MM} mm of each other for {length:.0f} mm "
                   f"at ({u:.0f},{v:.0f}), {d:.1f} mm at the closest -- closer than ~1.15 mm "
                   f"they fuse into a blot. Spread them or widen the angle where they meet; "
                   f"a small closed shape like an eye needs >= 2.5 mm across.")
    if len(merges) > 5:
        out.append(f"...and {len(merges) - 5} more places like that.")
    jb = meta.get("join_stats", {})
    if jb.get("bridged_pieces", 0) >= 3 or jb.get("longest_bridge_frac", 0) > 0.2:
        u, v = jb.get("longest_bridge_at", (0, 0))
        out.append(f"one_line glued {jb.get('bridged_pieces')} separate pieces together; the "
                   f"longest bridge (at ({u:.0f},{v:.0f})) spans "
                   f"{100 * jb.get('longest_bridge_frac', 0):.0f}% of the drawing. Bridges "
                   f"between unrelated pieces read as glue. For one-line art, design ONE "
                   f"path with smooth control points that travels through the figure.")
    return out
