"""The scene language: producers, modifiers and the deterministic compiler."""
from __future__ import annotations

import hashlib
import io
import json
import math

from .config import CANVAS_H, CANVAS_W, MAX_POINTS, Stroke
from .doodles import doodle_strokes
from .expr import Expr, ExprError
from .geometry import apply_fit, catmull_rom, fit_params, hatch_polygon
from .occlude import Occluder, closed_polygons, rejoin_hatch
from .planner import plan_paths
from .text import text_block


def strokes_from_svg(svg: str) -> list[Stroke]:
    from svgelements import SVG, Close, Move, Path, Shape
    doc = SVG.parse(io.StringIO(svg), reify=True)
    strokes: list[Stroke] = []
    for el in doc.elements():
        if not isinstance(el, Shape):
            continue
        p = Path(el)
        cur: Stroke = []
        for seg in p.segments():
            if isinstance(seg, Move):
                if len(cur) > 1: strokes.append(cur)
                cur = [(seg.end.x, seg.end.y)]
                continue
            n = max(2, int(seg.length(error=1e-2) / 0.5) + 1) if hasattr(seg, "length") else 2
            for i in range(1, n + 1):
                q = seg.point(i / n)
                cur.append((q.x, q.y))
            if isinstance(seg, Close) and len(cur) > 1:
                strokes.append(cur); cur = [cur[-1]]
        if len(cur) > 1: strokes.append(cur)
    return strokes


SCENE_VARS = ("t", "i", "n")


def _num_or_expr(v, variables=SCENE_VARS):
    if isinstance(v, (int, float)):
        c = float(v)
        return lambda **_: c
    return Expr(str(v), variables)


def _transform(strokes: list[Stroke], tr: dict) -> list[Stroke]:
    about = tr.get("about") or [0.0, 0.0]
    ax, ay = float(about[0]), float(about[1])
    sc = tr.get("scale", 1.0)
    if isinstance(sc, (int, float)):
        sx = sy = float(sc)
    else:
        sx, sy = float(sc[0]), float(sc[1])
    rot = math.radians(float(tr.get("rotate", 0.0)))
    ca, sa = math.cos(rot), math.sin(rot)
    tx, ty = (tr.get("translate") or [0.0, 0.0])[:2]
    out = []
    for s in strokes:
        ns = []
        for x, y in s:
            x, y = (x - ax) * sx, (y - ay) * sy
            x, y = x * ca - y * sa, x * sa + y * ca
            ns.append((x + ax + float(tx), y + ay + float(ty)))
        out.append(ns)
    return out


def _produce_one(shape: dict, i: int, n: int) -> list[Stroke]:
    """The base geometry of one shape, before fill/transform/fit."""
    if "param" in shape:
        p = shape["param"]
        tr = p.get("t") or [0.0, 1.0, 100]
        if len(tr) != 3:
            raise ExprError('param "t" must be [start, stop, steps]')
        t0, t1, steps = float(tr[0]), float(tr[1]), int(tr[2])
        if not (2 <= steps <= MAX_POINTS):
            raise ExprError(f'param "t" steps must be 2..{MAX_POINTS}, got {steps}')
        fx, fy = _num_or_expr(p.get("x", "t")), _num_or_expr(p.get("y", "t"))
        pts = []
        for k in range(steps):
            t = t0 + (t1 - t0) * k / (steps - 1)
            pts.append((fx(t=t, i=i, n=n), fy(t=t, i=i, n=n)))
        if p.get("closed"):
            pts.append(pts[0])
        return [pts]
    if "path" in shape or "paths" in shape:
        raw = [shape["path"]] if "path" in shape else shape["paths"]
        sm = shape.get("smooth")
        n = 12 if sm is True else (int(sm) if sm else 0)
        out = []
        for pth in raw:
            if not pth:
                continue
            try:
                pts = [(float(a), float(b)) for a, b in pth]
            except (TypeError, ValueError):
                raise ExprError("path points must be [u, v] NUMBERS; for computed "
                                "coordinates use a \"param\" producer") from None
            if n:
                pts = catmull_rom(pts, n, bool(shape.get("closed")))
            elif shape.get("closed") and len(pts) > 2:
                pts = pts + [pts[0]]
            out.append(pts)
        return out
    if "doodle" in shape:
        sm = shape.get("smooth", 6)
        return doodle_strokes(str(shape["doodle"]), int(shape.get("pick", 0)),
                              shape.get("source", "auto"),
                              0 if sm is False else (6 if sm is True else int(sm or 0)))
    if "trace" in shape:
        from .trace import trace_strokes
        t = shape["trace"]
        t = {"image": t} if isinstance(t, str) else dict(t)
        if "image" not in t:
            raise ExprError('trace needs "image": a file path or a data: URL')
        allowed = {"image", "threshold", "invert", "resolution", "spur", "smooth", "despeckle"}
        bad = sorted(set(t) - allowed)
        if bad:
            raise ExprError(f"trace: unknown option(s) {bad}; allowed {sorted(allowed)}")
        img = t.pop("image")
        return trace_strokes(str(img), **t)
    if "text" in shape:
        return text_block(shape["text"], shape.get("font", "futural"),
                          shape.get("align", "left"), float(shape.get("leading", 1.4)))
    if "svg" in shape:
        return strokes_from_svg(shape["svg"])
    raise ExprError(f"shape has no producer; expected one of "
                    f"param/path/paths/text/svg/doodle/trace, got keys {sorted(shape)}")


def compile_scene_full(scene: dict) -> tuple[list[Stroke], dict]:
    """Compile a scene to strokes, plus what the drawing checks need to know.

    Deterministic: same scene, same strokes. Fill outlines are carried through the same
    transforms as the drawing so the checks can tell an intended solid fill from an
    accidental blot.

    The scene-level fit runs BEFORE join, not after. join's weld tolerance is in
    millimetres, so it has to see millimetre coordinates; a scene authored on a 0..100
    grid and fitted afterwards was welding in grid units.
    """
    if not isinstance(scene, dict) or "shapes" not in scene:
        raise ExprError('scene must be {"shapes": [...]}')
    out: list[Stroke] = []
    # fill lines, one group per filled shape: already optimally joined, never re-planned.
    # Each group remembers its spacing and what hid parts of it, so that after occlusion
    # the cut-up serpentine can be re-joined without a connector crossing a front shape.
    groups: list[dict] = []
    fills: list[Stroke] = []
    occl = bool(scene.get("occlude"))
    # Re-joining cut hatching is OPT-IN. It saves pen lifts, but the goal is the drawing,
    # not throughput, and whether the joined serpentine looks better on paper than the
    # separate spans has not been tested on the robot.
    rejoin = isinstance(scene.get("occlude"), dict) and bool(scene["occlude"].get("rejoin"))
    hidden_mm = 0.0
    for idx, shape in enumerate(scene["shapes"]):
        if not isinstance(shape, dict):
            raise ExprError(f"shape {idx} is not an object")
        reps = int(shape.get("repeat", 1))
        if not (1 <= reps <= 1000):
            raise ExprError(f"shape {idx}: repeat must be 1..1000")
        for i in range(reps):
            try:
                ss = _produce_one(shape, i, reps)
            except ExprError as e:
                raise ExprError(f"shape {idx}: {e}") from None
            fpolys: list[Stroke] = []
            hh: list[Stroke] = []
            sp_eff = 0.0
            fill = shape.get("fill")
            if fill:
                sp = float(fill.get("spacing", 1.15))
                ang = float(fill.get("hatch", 45.0))
                hatched: list[Stroke] = []
                for s in ss:
                    hatched += hatch_polygon(s, ang, sp)
                    if fill.get("cross"):
                        hatched += hatch_polygon(s, ang + 90.0, sp)
                fpolys = [list(s) for s in ss if len(s) >= 3]
                hh = hatched
                sp_eff = sp
                ss = ss if fill.get("outline", True) else []
            if shape.get("transform"):
                tr = shape["transform"]
                ss, hh, fpolys = _transform(ss, tr), _transform(hh, tr), _transform(fpolys, tr)
                sc = tr.get("scale", 1.0)
                sp_eff *= (math.sqrt(abs(float(sc[0]) * float(sc[1])))
                           if isinstance(sc, (list, tuple)) else abs(float(sc)))
            b = shape.get("box") or shape.get("fit") or (
                [0, 0, CANVAS_W, CANVAS_H] if ("doodle" in shape or "trace" in shape) else None)
            if b:
                prm = fit_params(ss + hh, float(b[0]), float(b[1]), float(b[2]), float(b[3]))
                ss, hh, fpolys = apply_fit(ss, prm), apply_fit(hh, prm), apply_fit(fpolys, prm)
                if prm:
                    sp_eff *= prm[0]
            if occl and shape.get("opaque", True):
                # painter's order: this instance hides whatever is already on the page.
                # A filled shape is opaque over its fill polygon even if left open;
                # otherwise only its CLOSED strokes enclose anything.
                polys = [list(p) for p in fpolys] if fill else closed_polygons(ss)
                if polys:
                    occ = Occluder(polys)
                    before = _ink(out)
                    out = occ.clip(out)
                    hidden_mm += before - _ink(out)
                    for g in groups:
                        before = _ink(g["s"])
                        g["s"] = occ.clip(g["s"], keep_edges=not ss)
                        lost = before - _ink(g["s"])
                        if lost > 1e-9:
                            hidden_mm += lost
                            g["occ"].append(occ)
            out += ss
            if hh:
                groups.append({"s": hh, "sp": sp_eff, "occ": []})
            fills += fpolys
    hatch: list[Stroke] = []
    for g in groups:
        hatch += rejoin_hatch(g["s"], g["sp"], g["occ"]) if (rejoin and g["occ"]) else g["s"]
    if scene.get("fit"):
        b = scene["fit"]
        prm = fit_params(out + hatch, float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        out, hatch, fills = apply_fit(out, prm), apply_fit(hatch, prm), apply_fit(fills, prm)
        if prm:
            hidden_mm *= prm[0]
    meta: dict = {"fills": fills}
    if occl:
        meta["hidden_mm"] = round(hidden_mm, 1)
    join = scene.get("join")
    if join:
        opts = join if isinstance(join, dict) else {}
        out, st = plan_paths(out, opts)
        meta["join_stats"] = st
    return [s for s in out + hatch if len(s) >= 1], meta


def _ink(strokes: list[Stroke]) -> float:
    return sum(math.dist(a, b) for s in strokes for a, b in zip(s, s[1:]))


def compile_scene(scene: dict) -> list[Stroke]:
    """Compile a declarative scene to strokes. Deterministic: same scene, same strokes."""
    return compile_scene_full(scene)[0]


def scene_digest(scene: dict) -> str:
    """Content hash of a scene. preview and draw derive the SAME id from the SAME input,
    so they cannot diverge -- and no id is ever issued, so none can go stale."""
    blob = json.dumps(scene, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


_scene_cache: dict[str, tuple[list[Stroke], dict]] = {}


def compile_cached_full(scene: dict) -> tuple[str, list[Stroke], dict]:
    sid = scene_digest(scene)
    if sid not in _scene_cache:
        if len(_scene_cache) > 64:
            _scene_cache.clear()
        _scene_cache[sid] = compile_scene_full(scene)
    strokes, meta = _scene_cache[sid]
    return sid, strokes, meta


def compile_cached(scene: dict) -> tuple[str, list[Stroke]]:
    sid, strokes, _meta = compile_cached_full(scene)
    return sid, strokes
