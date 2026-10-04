"""Stroke planning: weld, explode at junctions, dedupe, chain into trails, one line."""
from __future__ import annotations

import math

from .config import Stroke


class _Welder:
    """Snap points to canonical ids: points within `tol` share one.

    Rounding to a grid is NOT a tolerance -- two points 0.01 mm apart can straddle a cell
    boundary while two 0.29 mm apart share a cell -- so this searches the neighbouring
    cells and measures the real distance.
    """

    def __init__(self, tol: float):
        self.tol = max(tol, 1e-6)
        self.cells: dict = {}
        self.pts: list = []

    def id(self, p) -> int:
        cx, cy = math.floor(p[0] / self.tol), math.floor(p[1] / self.tol)
        best, bd = None, self.tol
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for k in self.cells.get((gx, gy), ()):
                    d = math.dist(p, self.pts[k])
                    if d <= bd:
                        best, bd = k, d
        if best is None:
            best = len(self.pts)
            self.pts.append(p)
            self.cells.setdefault((cx, cy), []).append(best)
        return best


def explode(strokes: list[Stroke], tol: float = 0.3) -> list[Stroke]:
    """Split polylines at their JUNCTIONS, so shared edges become visible to the planner.

    A junction is a vertex shared with another stroke, or revisited by the same one.
    Splitting at EVERY vertex instead -- the first version -- breaks on anything dense: a
    smooth curve is sampled every ~0.1 mm, finer than the weld tolerance, so neighbouring
    samples weld into one node and the graph scrambles. On the fox that made the planner
    invent 28 segments and drop 27, and walk one edge there and straight back.
    """
    w = _Welder(tol)
    ids = [[w.id(p) for p in s] for s in strokes]
    count: dict = {}
    for row in ids:
        prev = None
        for k in row:
            if k != prev:
                count[k] = count.get(k, 0) + 1
            prev = k
    out = []
    for s, row in zip(strokes, ids):
        if len(s) < 2:
            continue
        cur = [s[0]]
        for j in range(1, len(s)):
            cur.append(s[j])
            if j < len(s) - 1 and count.get(row[j], 0) > 1:
                out.append(cur)
                cur = [s[j]]
        if len(cur) >= 2:
            out.append(cur)
    return out


def dedupe_strokes(strokes: list[Stroke], tol: float) -> tuple[list[Stroke], int]:
    """Drop strokes that duplicate one already kept, in either direction.

    A triangle mesh supplied as triangles draws every interior edge TWICE. On this
    machine that is not merely wasted time: the second pass lands slightly off the first
    and the edge reads as doubled -- the same fault that made Hershey's retracing faces
    look scribbled. Two pieces are the same if they share both ends AND their midpoint,
    so two different curves between the same junctions are not mistaken for one.
    """
    w = _Welder(tol)
    seen: set = set()
    out = []
    for s in strokes:
        if len(s) < 2:
            continue
        # the midpoint must not depend on direction: for a two-point segment s[len//2] is
        # the SECOND point, so A->B and B->A got different keys and nothing deduped
        n = len(s)
        m0, m1 = s[(n - 1) // 2], s[n // 2]
        mid = w.id(((m0[0] + m1[0]) / 2, (m0[1] + m1[1]) / 2))
        a, b = w.id(s[0]), w.id(s[-1])
        key = (min(a, b), max(a, b), mid)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out, len(strokes) - len(out)


def chain_strokes(strokes: list[Stroke], weld: float) -> list[Stroke]:
    """Join strokes whose ends meet into the FEWEST continuous trails.

    This is Hierholzer with odd-vertex pairing, not greedy extension. Greedy looks
    adequate and is not: it strands edges and leaves extra trails behind. Two triangles
    sharing an edge have exactly two odd-degree vertices, so one Eulerian trail covers
    them -- greedy returned two.

    The theory gives the exact answer. A connected component with k odd-degree vertices
    needs max(1, k/2) trails and no fewer, because every trail consumes two odd ends. So:
    pair the odd vertices up with dummy edges, which makes the component Eulerian, find
    the circuit, then cut it back open at the dummies. Pairing NEAREST-first is not
    arbitrary either -- each dummy becomes a pen-up lift, so nearest pairing also
    minimises the travel between the trails it creates.
    """
    welder = _Welder(weld)
    live = [list(s) for s in strokes if len(s) >= 2]
    if not live:
        return []
    canon = welder.pts

    def node(p):
        return welder.id(p)

    adj: dict = {}
    for i, s in enumerate(live):
        a, b = node(s[0]), node(s[-1])
        adj.setdefault(a, []).append((i, b))
        adj.setdefault(b, []).append((i, a))

    # connected components over the endpoint graph
    seen: set = set()
    comps = []
    for n0 in adj:
        if n0 in seen:
            continue
        stack, comp = [n0], []
        seen.add(n0)
        while stack:
            v = stack.pop()
            comp.append(v)
            for _e, w in adj[v]:
                if w not in seen:
                    seen.add(w)
                    stack.append(w)
        comps.append(comp)

    dummy_of: dict = {}
    next_edge = len(live)
    for comp in comps:
        odd = [n for n in comp if len(adj[n]) % 2 == 1]
        while len(odd) >= 2:                      # pair nearest first
            a = odd.pop(0)
            j = min(range(len(odd)), key=lambda k: math.dist(canon[a], canon[odd[k]]))
            b = odd.pop(j)
            adj[a].append((next_edge, b))
            adj[b].append((next_edge, a))
            dummy_of[next_edge] = True
            next_edge += 1

    used = [False] * next_edge
    ptr = {n: 0 for n in adj}
    out: list[Stroke] = []

    def walk(start_node):
        """Hierholzer: the Eulerian circuit from start, as (edge, from_node, to_node).

        The direction matters. An earlier version returned bare edge ids and the caller
        oriented each stroke by "whichever end is nearer the trail so far" -- which leaves
        the FIRST edge of every trail in its stored direction. Stored backwards, the trail
        starts from the wrong end, the next edge attaches to the nearer wrong point, and
        the pen jumps and retraces. On the fox: 3 edges, 25 mm drawn twice.
        """
        stack = [(start_node, None)]
        order = []
        while stack:
            v, _e = stack[-1]
            lst = adj[v]
            while ptr[v] < len(lst) and used[lst[ptr[v]][0]]:
                ptr[v] += 1
            if ptr[v] == len(lst):
                order.append(stack.pop())
            else:
                ei, w = lst[ptr[v]]
                used[ei] = True
                stack.append((w, ei))
        order.reverse()
        return [(order[k][1], order[k - 1][0], order[k][0]) for k in range(1, len(order))]

    for comp in comps:
        starts = [n for n in comp if len(adj[n]) % 2 == 1] or comp
        for s0 in starts:
            if all(used[e] for e, _w in adj[s0]):
                continue
            edges = walk(s0)
            # Cut the circuit back open at the dummies -- but ROTATE first so the
            # sequence begins just after one. The walk is a CIRCUIT, so a dummy sitting
            # mid-sequence would split a single trail into two halves that actually join
            # end to end around the wrap. Rotating makes one dummy produce one cut.
            cut = next((i for i, (e, _a, _b) in enumerate(edges) if e in dummy_of), None)
            if cut is not None:
                edges = edges[cut + 1:] + edges[:cut + 1]
            run: Stroke = []
            for e, frm, _to in edges:
                if e in dummy_of:
                    if len(run) >= 2:
                        out.append(run)
                    run = []
                    continue
                seg = list(live[e])
                if node(seg[0]) != frm:            # orient by the walk, never by distance
                    seg.reverse()
                run = seg if not run else run + seg[1:]
            if len(run) >= 2:
                out.append(run)
    return out


def bridge(a_end: Stroke, b_start: Stroke, samples: int = 10) -> Stroke:
    """A tangent-continuous hop from the end of one trail to the start of the next.

    A straight connector reads as a mistake; the line has to LEAVE and REJOIN along the
    direction it was already travelling, which is what makes a one-line drawing look
    deliberate. Cubic Hermite with tangents taken from the adjoining segments.
    The firmware's 1-2 mm corner blending then smooths any residual kink for us -- the
    one place where that fault helps.
    """
    p0, p1 = a_end[-1], b_start[0]
    d = math.dist(p0, p1)
    if d < 1e-9:
        return []
    t0 = a_end[-1][0] - a_end[-2][0], a_end[-1][1] - a_end[-2][1]
    t1 = b_start[1][0] - b_start[0][0], b_start[1][1] - b_start[0][1]
    n0 = math.hypot(*t0) or 1.0
    n1 = math.hypot(*t1) or 1.0
    k = d * 0.6
    m0 = (t0[0] / n0 * k, t0[1] / n0 * k)
    m1 = (t1[0] / n1 * k, t1[1] / n1 * k)
    out = []
    for i in range(1, samples):
        t = i / samples
        h00 = 2 * t ** 3 - 3 * t ** 2 + 1
        h10 = t ** 3 - 2 * t ** 2 + t
        h01 = -2 * t ** 3 + 3 * t ** 2
        h11 = t ** 3 - t ** 2
        out.append((h00 * p0[0] + h10 * m0[0] + h01 * p1[0] + h11 * m1[0],
                    h00 * p0[1] + h10 * m0[1] + h01 * p1[1] + h11 * m1[1]))
    return out


def to_one_line(strokes: list[Stroke], stats: dict | None = None) -> list[Stroke]:
    """Bridge every trail into a SINGLE continuous stroke, nearest end first."""
    live = [list(s) for s in strokes if len(s) >= 2]
    if stats is not None:
        stats["bridged_pieces"] = len(live)
    if len(live) <= 1:
        return live
    allp = [p for s in live for p in s]
    diag = math.dist((min(p[0] for p in allp), min(p[1] for p in allp)),
                     (max(p[0] for p in allp), max(p[1] for p in allp))) or 1.0
    longest = (0.0, (0.0, 0.0))
    cur = live.pop(0)
    while live:
        best, rev, bd = 0, False, float("inf")
        for i, s in enumerate(live):
            for r in (False, True):
                d = math.dist(cur[-1], s[-1] if r else s[0])
                if d < bd:
                    best, rev, bd = i, r, d
        nxt = live.pop(best)
        if rev:
            nxt.reverse()
        if bd > longest[0]:
            longest = (bd, ((cur[-1][0] + nxt[0][0]) / 2, (cur[-1][1] + nxt[0][1]) / 2))
        cur.extend(bridge(cur, nxt))
        cur.extend(nxt)
    if stats is not None:
        stats["longest_bridge_frac"] = longest[0] / diag
        stats["longest_bridge_at"] = longest[1]
    return [cur]


def plan_paths(strokes: list[Stroke], opts: dict) -> tuple[list[Stroke], dict]:
    """Dedupe, weld and chain a pile of strokes; optionally reduce it to one line.

    Fewer pen lifts is a QUALITY setting on this machine, not just a speed one: the nib
    travels radially as it descends, so every lift costs a short drag at both ends.
    """
    before = len(strokes)
    stats = {"strokes_in": before}
    weld = float(opts.get("weld", 0.3))
    if opts.get("explode"):
        strokes = explode(strokes, weld)
    if opts.get("dedupe", True):
        strokes, dropped = dedupe_strokes(strokes, weld)
        stats["duplicates_dropped"] = dropped
    if opts.get("chain", True):
        strokes = chain_strokes(strokes, weld)
    if opts.get("one_line"):
        strokes = to_one_line(strokes, stats)
    stats["strokes_out"] = len(strokes)
    stats["lifts_saved"] = max(0, before - len(strokes))
    return strokes, stats
