"""Trace a line-art IMAGE into single strokes along the CENTRE of each line.

Ordinary tracing (potrace, Inkscape's default Trace Bitmap) follows the EDGES of the ink,
so every line of a drawing comes back as a thin closed outline and the pen draws it
twice. Here the ink is thinned to a one-pixel skeleton (Zhang-Suen), the skeleton is read
as a graph -- endpoints and junctions joined by runs of pixels -- and each run becomes one
stroke. Spurs (the short whiskers thinning grows at corners and blobs) are pruned and the
pixel staircase is smoothed away. That is what autotrace -centerline does, without the
external binary.

Works best on what it is for: dark lines on a light background, a few pixels thick.
A solid black area thins to its medial axis, which is rarely what you want -- hatch it
instead.
"""
from __future__ import annotations

import base64
import io
import math
import os

import numpy as np

from .config import Stroke
from .expr import ExprError

MAX_RESOLUTION = 1500


def load_gray(src: str, resolution: int) -> np.ndarray:
    """A file path or a data: URL / bare base64 PNG/JPEG, as a float gray array 0..255,
    with transparency composited onto white and the long side scaled to `resolution`."""
    from PIL import Image
    try:
        if src.startswith("data:"):
            data = base64.b64decode(src.split(",", 1)[1])
            img = Image.open(io.BytesIO(data))
        elif os.path.exists(os.path.expanduser(src)):
            img = Image.open(os.path.expanduser(src))
        elif len(src) > 200:
            img = Image.open(io.BytesIO(base64.b64decode(src)))
        else:
            raise ExprError(f"trace: no such image file: {src}")
        img.load()
    except ExprError:
        raise
    except Exception as e:
        raise ExprError(f"trace: could not read the image ({type(e).__name__})") from None
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")
    k = resolution / max(img.size)
    if abs(k - 1) > 1e-3:
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))),
                         Image.LANCZOS)
    return np.asarray(img, dtype=np.float64)


def otsu(gray: np.ndarray) -> float:
    hist = np.bincount(np.clip(gray, 0, 255).astype(np.int64).ravel(), minlength=256)
    total = hist.sum()
    w = np.cumsum(hist); mu = np.cumsum(hist * np.arange(256))
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * w / total - mu) ** 2 / (w * (total - w))
    between = np.nan_to_num(between, nan=-1.0)
    # A clean two-level image ties every threshold between its levels; the first index
    # of the plateau is the dark level itself, so take the plateau's middle.
    best = np.flatnonzero(between >= between.max() - 1e-9 * max(1.0, between.max()))
    return float((best[0] + best[-1]) / 2)


def _despeckle(ink: np.ndarray, min_px: int) -> np.ndarray:
    """Drop connected ink blobs smaller than min_px pixels (dust, JPEG noise)."""
    if min_px <= 1:
        return ink
    h, w = ink.shape
    seen = np.zeros_like(ink, dtype=bool)
    out = ink.copy()
    ys, xs = np.nonzero(ink)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        comp = [(y0, x0)]; seen[y0, x0] = True; k = 0
        while k < len(comp):
            y, x = comp[k]; k += 1
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < h and 0 <= xx < w and ink[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True; comp.append((yy, xx))
        if len(comp) < min_px:
            for y, x in comp:
                out[y, x] = False
    return out


def zhang_suen(ink: np.ndarray) -> np.ndarray:
    """Thin a boolean image to a one-pixel-wide skeleton (Zhang & Suen 1984), vectorised."""
    img = np.pad(ink.astype(np.uint8), 1)
    while True:
        changed = False
        for step in (0, 1):
            P = img
            p2, p3, p4 = P[:-2, 1:-1], P[:-2, 2:], P[1:-1, 2:]
            p5, p6, p7 = P[2:, 2:], P[2:, 1:-1], P[2:, :-2]
            p8, p9 = P[1:-1, :-2], P[:-2, :-2]
            ring = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            B = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            A = sum(((ring[k] == 0) & (ring[k + 1] == 1)).astype(np.uint8) for k in range(8))
            if step == 0:
                c = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                c = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            rm = (P[1:-1, 1:-1] == 1) & (B >= 2) & (B <= 6) & (A == 1) & c
            if rm.any():
                img[1:-1, 1:-1][rm] = 0
                changed = True
        if not changed:
            return img[1:-1, 1:-1].astype(bool)


_RING = [(0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1)]  # E NE N ..


def _thin_corners(sk: set) -> None:
    """Remove SIMPLE pixels (Yokoi 8-connectivity number 1) that are not line ends.

    Zhang-Suen leaves staircase corners where a pixel touches both its orthogonal and
    diagonal neighbours; read as a graph, each one is a fake three-way junction that
    splits a smooth line into stubs. Removing a simple pixel never changes the topology.
    """
    for p in sorted(sk):
        if p not in sk:
            continue
        x = [1 if (p[0] + dy, p[1] + dx) in sk else 0 for dy, dx in _RING]
        if sum(x) < 2:
            continue
        xb = [1 - v for v in x] + [1 - x[0], 1 - x[1]]
        n8 = sum(xb[k] - xb[k] * xb[k + 1] * xb[k + 2] for k in (0, 2, 4, 6))
        if n8 == 1:
            sk.discard(p)


def _neigh(p, sk):
    return [(p[0] + dy, p[1] + dx) for dy, dx in _RING if (p[0] + dy, p[1] + dx) in sk]


def skeleton_graph(sk: set):
    """Nodes (junction clusters and endpoints, as centroids) and the pixel runs between."""
    deg = {p: len(_neigh(p, sk)) for p in sk}
    nodepx = {p for p, d in deg.items() if d != 2}
    cluster: dict = {}
    centres: list[tuple[float, float]] = []
    for p in sorted(nodepx):
        if p in cluster:
            continue
        cid = len(centres); stack = [p]; cluster[p] = cid; members = []
        while stack:
            q = stack.pop(); members.append(q)
            for r in _neigh(q, sk):
                if r in nodepx and r not in cluster:
                    cluster[r] = cid; stack.append(r)
        centres.append((sum(m[0] for m in members) / len(members),
                        sum(m[1] for m in members) / len(members)))
    edges: list[tuple[int, int, list]] = []
    used: set = set()
    for p in sorted(nodepx):
        for q in _neigh(p, sk):
            if q in nodepx or q in used:
                continue
            path = [centres[cluster[p]], q]; prev, cur = p, q; used.add(q)
            end = None
            while True:
                nxt = [r for r in _neigh(cur, sk) if r != prev and r not in used]
                hit = [r for r in _neigh(cur, sk) if r in nodepx and r != prev]
                if hit and (not nxt or all(r in nodepx for r in nxt)):
                    end = cluster[hit[0]]; break
                nxt = [r for r in nxt if r not in nodepx]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]; used.add(cur); path.append(cur)
            if end is None:
                continue
            path.append(centres[end])
            edges.append((cluster[p], end, path))
    for p in sorted(sk):                                # closed loops: no node at all
        if p in nodepx or p in used:
            continue
        path = [p]; used.add(p); prev, cur = None, p
        while True:
            nxt = [r for r in _neigh(cur, sk) if r != prev and r not in used]
            if not nxt:
                break
            prev, cur = cur, nxt[0]; used.add(cur); path.append(cur)
        if len(path) > 2:
            path.append(path[0])
            cid = len(centres); centres.append(p)
            edges.append((cid, cid, path))
    return centres, edges


def _plen(path) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def prune(edges, spur_px: float, speck_px: float, rounds: int = 3):
    """Drop whiskers -- runs from an endpoint to a junction shorter than spur_px -- then
    re-join runs that now meet at a node of degree two, so a pruned corner becomes one
    smooth line again.

    A SEPARATE short stroke is not a whisker and gets a much smaller threshold: an eye,
    an eyebrow, a strand of hair are exactly that, and an early version that treated
    them like spurs traced a face as a bare profile."""
    edges = [list(e) for e in edges]
    for _ in range(rounds):
        deg: dict = {}
        for a, b, _p in edges:
            deg[a] = deg.get(a, 0) + 1; deg[b] = deg.get(b, 0) + 1
        keep = []
        for a, b, p in edges:
            short = _plen(p) < spur_px
            if short and a != b and (deg[a] == 1) != (deg[b] == 1):
                continue                                 # a whisker
            if a == b and short:
                continue                                 # a knot at a junction
            if deg[a] == 1 and deg[b] == 1 and _plen(p) < speck_px:
                continue                                 # a speck, not a short stroke
            keep.append([a, b, p])
        if len(keep) == len(edges):
            break
        edges = _merge_deg2(keep)
    return _merge_deg2(edges)


def contract_knots(edges, centres, knot_px: float):
    """Thinning splits an X crossing into two T junctions joined by a stub a few pixels
    long. Collapse every junction-to-junction run shorter than knot_px into one node, so
    the crossing lines come out straight through it instead of kinked at both ends."""
    centres = list(centres)
    parent = list(range(len(centres)))

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]; n = parent[n]
        return n
    deg: dict = {}
    for a, b, _p in edges:
        deg[a] = deg.get(a, 0) + 1; deg[b] = deg.get(b, 0) + 1
    keep = []
    for a, b, p in edges:
        if a != b and deg[a] >= 3 and deg[b] >= 3 and _plen(p) < knot_px:
            parent[find(b)] = find(a)
        else:
            keep.append((a, b, p))
    groups: dict = {}
    for n in range(len(centres)):
        groups.setdefault(find(n), []).append(n)
    for members in groups.values():
        if len(members) > 1:
            cy = sum(centres[m][0] for m in members) / len(members)
            cx = sum(centres[m][1] for m in members) / len(members)
            for m in members:
                centres[m] = (cy, cx)
    out = []
    for a, b, p in keep:
        p = [centres[a]] + list(p[1:-1]) + [centres[b]]
        out.append([find(a), find(b), p])
    return out


def _merge_deg2(edges):
    while True:
        deg: dict = {}
        for a, b, _p in edges:
            if a == b:
                continue
            deg[a] = deg.get(a, 0) + 1; deg[b] = deg.get(b, 0) + 1
        node = next((n for n, d in deg.items() if d == 2), None)
        if node is None:
            return edges
        two = [k for k, e in enumerate(edges) if e[0] != e[1] and node in (e[0], e[1])]
        if len(two) != 2:
            return edges
        (a1, b1, p1), (a2, b2, p2) = edges[two[0]], edges[two[1]]
        if b1 != node:
            a1, b1, p1 = b1, a1, p1[::-1]
        if a2 != node:
            a2, b2, p2 = b2, a2, p2[::-1]
        merged = [a1, b2, p1 + p2[1:]]
        edges = [e for k, e in enumerate(edges) if k not in two] + [merged]


def straighten_junctions(edges, radius: float):
    """Thinning bends the skeleton as it nears a junction -- the medial axis of a Y is
    curved over about one line-width -- so traced polygons come out with bowed edges.
    Cut each run back by `radius` from any junction end and reconnect it straight to
    the junction centre."""
    deg: dict = {}
    for a, b, _p in edges:
        deg[a] = deg.get(a, 0) + 1; deg[b] = deg.get(b, 0) + 1
    out = []
    for a, b, p in edges:
        p = list(p)
        if a != b and len(p) > 4:
            if deg[a] >= 3:
                k = 1
                while k < len(p) - 2 and math.dist(p[k], p[0]) < radius:
                    k += 1
                p = [p[0]] + p[k:]
            if deg[b] >= 3:
                k = len(p) - 2
                while k > 1 and math.dist(p[k], p[-1]) < radius:
                    k -= 1
                p = p[:k + 1] + [p[-1]]
        out.append([a, b, p])
    return out


def _smooth(path, win: int):
    if win <= 1 or len(path) < 5:
        return list(path)
    h = win // 2
    closed = math.dist(path[0], path[-1]) < 1e-9
    n = len(path)
    out = []
    for k in range(n):
        if not closed and (k == 0 or k == n - 1):
            out.append(path[k]); continue
        acc_y = acc_x = 0.0; cnt = 0
        for j in range(k - h, k + h + 1):
            if closed:
                q = path[j % (n - 1)]
            elif 0 <= j < n:
                q = path[j]
            else:
                continue
            acc_y += q[0]; acc_x += q[1]; cnt += 1
        out.append((acc_y / cnt, acc_x / cnt))
    if closed:
        out[-1] = out[0]
    return out


def _rdp(pts, eps):
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (ay, ax), (by, bx) = pts[i], pts[j]
        dy, dx = by - ay, bx - ax
        L = math.hypot(dy, dx)
        best, bk = -1.0, -1
        for k in range(i + 1, j):
            py, px = pts[k]
            if L > 1e-12:
                d = abs(dx * (ay - py) - dy * (ax - px)) / L
            else:
                d = math.hypot(py - ay, px - ax)
            if d > best:
                best, bk = d, k
        if best > eps:
            keep[bk] = True
            stack += [(i, bk), (bk, j)]
    return [p for p, k in zip(pts, keep) if k]


def trace_strokes(src: str, threshold: float | None = None, invert: bool = False,
                  resolution: int = 600, spur: float = 0.02, smooth: int = 5,
                  despeckle: float = 0.00004) -> list[Stroke]:
    """Centerline strokes of a line-art image, in PIXEL units (x right, y down).

    resolution  long side the image is scaled to before thinning (lines should end up
                ~2-10 px thick; raise it for fine detail, lower it to ignore texture)
    spur        whiskers shorter than this FRACTION of the long side are pruned
    smooth      moving-average window along each line, in pixels (1 = off)
    despeckle   ink blobs smaller than this fraction of the image area are dropped
    """
    resolution = max(50, min(MAX_RESOLUTION, int(resolution)))
    gray = load_gray(src, resolution)
    thr = otsu(gray) if threshold is None else float(threshold)
    ink = gray > thr if invert else gray <= thr
    if ink.mean() > 0.6:
        raise ExprError("trace: over 60% of the image is ink -- it looks like light lines on "
                        "a dark background. Pass \"invert\": true, or a \"threshold\".")
    ink = _despeckle(ink, int(despeckle * ink.size))
    sk = {(int(y), int(x)) for y, x in zip(*np.nonzero(zhang_suen(ink)))}
    _thin_corners(sk)
    centres, edges = skeleton_graph(sk)
    edges = contract_knots(edges, centres, max(3.0, 0.012 * resolution))
    edges = prune(edges, spur * resolution, max(2.0, 0.006 * resolution))
    width = float(ink.sum()) / max(1, len(sk))          # mean line width, px
    edges = straighten_junctions(edges, 1.2 * width)
    out: list[Stroke] = []
    for _a, _b, path in edges:
        pts = _rdp(_smooth(path, int(smooth)), 0.5)
        if len(pts) >= 2:
            out.append([(float(x), float(y)) for y, x in pts])
    return out
