"""The MCP server: tools, resources and instructions."""
from __future__ import annotations

import json
import math
import os
import time

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from .checks import lint_scene, small_feature_check
from .config import CANVAS_H, CANVAS_W, DATA_DIR, DEFAULT_SPEED, HOST, SMALL_FEATURE_MM, Stroke
from .doodles import doodle_sheet, qd_bundle, qd_categories, qd_list
from .expr import ExprError
from .geometry import fit, order_human, simplify, travel_mm
from .machine import clearance_check, envelope_bounds_mm, envelope_check
from .render import render_png, write_preview
from .robot import job_lock, jobs, robot, submit
from .scene import compile_cached_full, strokes_from_svg
from .text import STROKE_FONTS, stroke_font, strokes_from_stroke_font, strokes_from_text, text_block


def _preview_result(strokes: list[Stroke], simulate: bool, warnings: list[str] | None = None):
    note, png = write_preview(strokes, simulate)
    if warnings:
        note += "\nDRAWING CHECKS:\n- " + "\n- ".join(warnings)
    out = [Image(data=render_png(strokes), format="png")]
    if simulate:
        out.append(Image(data=png, format="png"))
    out.append(note)
    return out


mcp = FastMCP("lineus", instructions=(
    "Controls a Line-us pen-plotter robot arm. Coordinates are millimetres, "
    "origin top-left of the page, u right, v down. "
    f"The PAGE is {CANVAS_W:.0f} x {CANVAS_H:.0f} mm and is the default framing for "
    "draw_text/draw_svg, but it is NOT the limit: draw_paths may go outside it, "
    "anywhere the arm reaches (an annular sector, roughly 3x the page area). "
    "Negative u and v are legal there. preview_* draws the reachable boundary in "
    "green and the page in grey; anything unreachable is reported as a warning "
    "rather than silently bent. "
    "Always call preview_* first and look at the image before drawing. "
    "Drawing is asynchronous: draw_* returns a job_id; poll get_job. "
    "Only one drawing runs at a time. Use abort to stop and lift the pen. "
    f"Corners are rounded by ~1-2 mm whatever their size, so shapes under "
    f"{SMALL_FEATURE_MM:.0f} mm lose their corners; draw_* takes speed=1..30 "
    f"(default {DEFAULT_SPEED}, lower is slower and sharper) and order=fast|human|asis "
    "(human is the DEFAULT: reading order, looks like a person drawing; "
    "fast = nearest-neighbour, least travel but hops around; asis = as supplied).\n\n"
    "DRAWING WELL ON THIS MACHINE -- learned on paper, mostly by getting it wrong:\n"
    "- Prefer preview_scene/draw_scene over raw paths: curves are expressions, figures are "
    "smooth control points, and preview and draw are guaranteed to match.\n"
    "- Judge a drawing by the SIMULATED preview image, never the clean one. Every drawing "
    "that disappointed on paper looked fine as a clean render. Read the DRAWING CHECKS in "
    "the preview note: each names a location to fix.\n"
    "- Nothing under ~2 mm survives: the arm rounds every corner by 1-2 mm. Small closed "
    "shapes such as eyes need at least 2.5 mm in their narrowest dimension.\n"
    "- Lines closer than ~1.15 mm fuse into a blot. Avoid that everywhere except where you "
    "want solid black: there use fill with spacing ~0.45. Fill eyes and noses solid -- "
    "outlined eyes vanish among other lines.\n"
    "- Every pen lift leaves a small radial tick, so prefer long continuous strokes.\n"
    "- One-line art is ONE designed path through the figure (smooth control points, the "
    "line crossing itself to make the features). Do not draw separate pieces and glue "
    "them with join.one_line: the bridges read as glue.\n"
    "- Overlapping shapes: set \"occlude\": true on the scene and list shapes back to "
    "front, so nearer closed shapes hide what is behind them. Without it every outline "
    "shows through and the drawing reads as a wireframe.\n"
    "- To draw from a picture, trace it: {\"trace\": \"file.png\"} follows the centre "
    "of each line. Works on clean line art; simplify busy images first, and remember the "
    "page is small -- detail closer than ~1.15 mm at page size will blot.\n"
    "- Low-poly / geometric art is a mesh: use \"join\": {\"explode\": true, "
    "\"dedupe\": true}, or every shared edge is drawn twice and looks doubled.\n"
    "- Recognisability is silhouette and proportion, not detail. A fox is a hard taper "
    "from cheek ruffs to a long narrow snout; a cat reads from slanted almond eyes and a "
    "pear-shaped body with a short neck; tilting a head about the neck adds character. "
    "Draft, preview, critique against the reference, fix -- expect several rounds.\n"
    "- For doodles, use real ones: doodles() browses Google's Quick, Draw! drawings -- single "
    "strokes in a person's own order. Look at the contact sheet before choosing a pick.\n"
    "- Start from a worked example rather than from nothing: get_example() lists them, "
    "get_example('one_line_cat') returns a scene to modify.\n"
    "- Text: list_fonts(). Use the handwriting faces; pick by aperture, and keep lines "
    "short -- the page width, not the box height, sets the letter size."))


@mcp.tool(annotations=ToolAnnotations(
    title='Robot status', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=True))
def get_status() -> dict:
    """Robot firmware banner, canvas size in mm, and current/last job."""
    try:
        banner = robot.hello()
        diag = robot.cmd("M122")
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "error": str(e), "host": HOST}
    last = max(jobs.values(), key=lambda j: j.started, default=None)
    umin, vmin, umax, vmax = envelope_bounds_mm()
    return {"connected": True, "banner": banner, "diagnostics": diag,
            "page_mm": {"width": CANVAS_W, "height": CANVAS_H},
            "envelope_mm": {"u_min": round(umin, 1), "v_min": round(vmin, 1),
                            "u_max": round(umax, 1), "v_max": round(vmax, 1),
                            "note": "annular sector, not a rectangle; corners of this "
                                    "bounding box are NOT reachable"},
            "speed_default": DEFAULT_SPEED,
            "last_job": get_job(last.id) if last else None}


def _paths_to_strokes(paths: list[list[list[float]]]) -> list[Stroke]:
    return [[(float(p[0]), float(p[1])) for p in path] for path in paths if path]


@mcp.tool(annotations=ToolAnnotations(
    title='Preview paths', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False))
def preview_paths(paths: list[list[list[float]]], simulate: bool = True) -> list:
    """Render polylines (canvas mm, [[ [u,v], ... ], ...]) without drawing.
    Returns the diagnostic view (pink = pen-up travel, grey box = page) and, with
    simulate=true (default), what the arm will actually put on paper."""
    s = order_human([simplify(x) for x in _paths_to_strokes(paths)])
    return _preview_result(s, simulate)


@mcp.tool(annotations=ToolAnnotations(
    title='Draw paths', readOnlyHint=False, destructiveHint=False, idempotentHint=False,
    openWorldHint=True))
def draw_paths(paths: list[list[list[float]]], speed: int | None = None,
               order: str = "human") -> dict:
    """Draw polylines given in canvas millimetres: [[ [u,v], [u,v], ... ], ...].
    Each inner list is one pen-down stroke. Returns a job_id.
    speed: G94 S, the max step size for PEN-DOWN moves (1 finest/slowest ..
           30 coarsest/fastest, default 5). Not a speed dial: it is interpolation
           granularity. Pen-UP travel is G94 P, set by LINEUS_TRAVEL_SPEED.
    order: how strokes are sequenced.
      "human" (default) top row first, left to right within a row, never
              reversed. Looks like a person drawing. Costs more pen-up travel
              (~2.8x on text) but this machine is slow enough that it rarely matters.
      "fast"  nearest-neighbour, may draw a stroke backwards. Least travel, but
              it hops between rows and looks chaotic to watch.
      "asis"  exactly the order supplied, untouched — you control the sequence."""
    return submit(_paths_to_strokes(paths), speed, order)


def _svg_fit(svg: str, x: float | None, y: float | None, w: float | None, h: float | None):
    x = 0.0 if x is None else x; y = 0.0 if y is None else y
    w = CANVAS_W - x if w is None else w; h = CANVAS_H - y if h is None else h
    return fit(strokes_from_svg(svg), x, y, w, h)


@mcp.tool(annotations=ToolAnnotations(
    title='Preview SVG', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False))
def preview_svg(svg: str, x: float | None = None, y: float | None = None,
                w: float | None = None, h: float | None = None,
                simulate: bool = True) -> list:
    """Render an SVG (paths/shapes, strokes only — fills are ignored) fitted into
    the box (x,y,w,h) in canvas mm (default: whole canvas). Returns a PNG."""
    s = order_human(_svg_fit(svg, x, y, w, h))
    return _preview_result(s, simulate)


@mcp.tool(annotations=ToolAnnotations(
    title='Draw SVG', readOnlyHint=False, destructiveHint=False, idempotentHint=False,
    openWorldHint=True))
def draw_svg(svg: str, x: float | None = None, y: float | None = None,
             w: float | None = None, h: float | None = None,
             speed: int | None = None, order: str = "human") -> dict:
    """Draw an SVG fitted into box (x,y,w,h) in canvas mm, aspect ratio kept.
    Line art works best; fills are not hatched. Returns a job_id."""
    return submit(_svg_fit(svg, x, y, w, h), speed, order)


def _text_fit(text, x, y, w, h, font):
    x = 0.0 if x is None else x; y = 0.0 if y is None else y
    w = CANVAS_W - x if w is None else w; h = CANVAS_H - y if h is None else h
    src = (text_block(text, font) if stroke_font(font) is not None
           else strokes_from_text(text, font))
    return fit(src, x, y, w, h)


@mcp.tool(annotations=ToolAnnotations(
    title='Preview text', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False))
def preview_text(text: str, x: float | None = None, y: float | None = None,
                 w: float | None = None, h: float | None = None,
                 font: str = "futural", simulate: bool = True) -> list:
    """Render single-stroke (Hershey) text fitted into box (x,y,w,h) mm. Use \\n for
    new lines. Prefer the HANDWRITING faces -- print: neutral architect pancakes delight
    casual; cursive (welded, one stroke per word): italienne cursive2 brush allure.
    call list_fonts() for the measurements. Hershey names still work but most of them
    retrace every stem."""
    s = order_human(_text_fit(text, x, y, w, h, font))
    return _preview_result(s, simulate)


@mcp.tool(annotations=ToolAnnotations(
    title='Draw text', readOnlyHint=False, destructiveHint=False, idempotentHint=False,
    openWorldHint=True))
def draw_text(text: str, x: float | None = None, y: float | None = None,
              w: float | None = None, h: float | None = None,
              font: str = "futural", speed: int | None = None,
              order: str = "human") -> dict:
    """Write text with a single-stroke Hershey font fitted into box (x,y,w,h) mm.
    Prefer the handwriting faces: print `neutral`/`architect`, cursive `italienne`/
    `cursive2` (these weld into one stroke per word). See list_fonts(). For multi-line or
    MULTI-FONT text prefer a scene: draw_scene puts several fonts and alignment in one call.
    speed: G94 S, max step size for pen-down moves, 1 (finest) .. 30 (coarsest)."""
    return submit(_text_fit(text, x, y, w, h, font), speed, order)


@mcp.tool(annotations=ToolAnnotations(
    title='Preview a scene', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=True))
def preview_scene(scene: dict, simulate: bool = True) -> list:
    """Render a declarative SCENE as a PNG without drawing. Prefer this over
    preview_paths: a scene is ~50x smaller than the coordinates it compiles to, and
    draw_scene given the SAME scene provably draws what you previewed.

    scene = {"shapes": [ ...shape... ], "fit": [x,y,w,h] (optional, scales the lot),
             "occlude": true or {"rejoin": bool} (optional: hidden-line removal, below),
             "join": {...} (optional: explode/dedupe/chain/one_line stroke planning)}

    There is deliberately NO library of shapes -- a fixed catalogue would handle the
    dull cases and send everything interesting back to pasting raw points. Curves are
    EXPRESSIONS, so the vocabulary is open:

      {"param": {"t": [0, 6.2832, 200],
                 "x": "33+5.5*cos(t)", "y": "30+5.5*sin(t)", "closed": true}}

    is a circle; change the expressions and it is a spiral, rose, lissajous or
    harmonograph. Variables: t, plus i and n inside "repeat". Functions: sin cos tan
    asin acos atan atan2 sinh cosh tanh exp log sqrt hypot floor ceil fmod degrees
    radians abs min max round pow sign clamp lerp; constants pi, tau, e.

    Producers (exactly one per shape):
      "param"  {"t":[start,stop,steps], "x":expr, "y":expr, "closed":bool}
      "path"   [[u,v], ...]                      one stroke, the escape hatch
      "paths"  [[[u,v], ...], ...]               several strokes
      "text"   "a\nb", or [{"s":"a","font":"timesr"}, {"s":"b","font":"scriptc"}]
               with "font", "align" (left|center|right), "leading" (default 1.4)
      "svg"    "<svg>..."
      "doodle" "cat", with "pick" (index), "source" (auto|bundled|web) -- real drawings
               from Google's Quick, Draw!, single strokes in the order a person drew them.
               Call doodles() first to SEE the candidates and choose a pick.
      "trace"  "path/to/drawing.png" (or a data: URL), or {"image": ..., "threshold":
               0..255, "invert": bool, "resolution": px (600), "spur": 0.02, "smooth": 5}
               -- a line-art IMAGE traced along the CENTRE of each line, so every line
               becomes one stroke instead of an outline drawn twice. For dark lines on a
               light background; solid black areas thin to a skeleton, so avoid them.
               Fitted to the page unless given a box. Add "join": {"chain": true} to cut
               the pen lifts (typically by half or more).
    Modifiers (any producer):
      "fill"      {"hatch":deg, "spacing":mm (default 1.15), "cross":bool,
                   "outline":bool} -- hatching is how you get solid black here
      "transform" {"translate":[dx,dy], "rotate":deg, "scale":s|[sx,sy], "about":[x,y]}
      "box"/"fit" [x,y,w,h]  scale this shape into a box
      "repeat"    N, with i (0..N-1) and n available in the expressions
      "opaque"    false: with scene "occlude", this shape hides nothing (default true)

    "occlude": true is HIDDEN-LINE REMOVAL. List shapes BACK TO FRONT; every CLOSED
    stroke (or filled shape) hides whatever earlier shapes drew underneath it, hatching
    included. That is how one thing stands in front of another -- a paw over a body,
    hills over mountains -- instead of every outline showing through. Open strokes never
    hide anything. A ring drawn as two closed strokes keeps its hole see-through.
    "occlude": {"rejoin": true} also re-joins hatching that occlusion cut into separate
    spans (fewer pen lifts). Off by default: not yet compared on paper."""
    try:
        _, strokes, meta = compile_cached_full(scene)
    except ExprError as e:
        return [f"scene error: {e}"]
    return _preview_result(order_human(strokes), simulate, lint_scene(strokes, meta))


@mcp.tool(annotations=ToolAnnotations(
    title='Draw a scene', readOnlyHint=False, destructiveHint=False, idempotentHint=False,
    openWorldHint=True))
def draw_scene(scene: dict, speed: int | None = None, order: str = "human") -> dict:
    """Draw a declarative scene (same object as preview_scene -- see it for the shape
    language). Compilation is deterministic and content-hashed, so passing the scene you
    previewed draws exactly what you saw. Returns a job_id and the scene_id."""
    try:
        sid, strokes, meta = compile_cached_full(scene)
    except ExprError as e:
        return {"error": str(e)}
    r = submit(strokes, speed, order)
    r["scene_id"] = sid
    if "warnings" in r:
        r["warnings"] = r["warnings"] + lint_scene(strokes, meta)
    return r


@mcp.tool(annotations=ToolAnnotations(
    title='Plan a scene', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=True))
def plan_scene(scene: dict) -> dict:
    """Compile a scene and report what it would cost, WITHOUT drawing or rendering:
    stroke/point counts, bounding box, pen-up travel and any warnings."""
    try:
        sid, strokes, meta = compile_cached_full(scene)
    except ExprError as e:
        return {"error": str(e)}
    pts = [p for s in strokes for p in s]
    if not pts:
        return {"error": "scene compiled to nothing"}
    speed = DEFAULT_SPEED
    extra = {"hidden_mm": meta["hidden_mm"]} if "hidden_mm" in meta else {}
    return {**extra, "scene_id": sid, "strokes": len(strokes), "points": len(pts),
            "bbox_mm": [round(min(p[0] for p in pts), 2), round(min(p[1] for p in pts), 2),
                        round(max(p[0] for p in pts), 2), round(max(p[1] for p in pts), 2)],
            "pen_up_travel_mm": round(travel_mm(order_human(strokes))),
            "warnings": (envelope_check(strokes) + small_feature_check(strokes, speed)
                         + clearance_check(strokes) + lint_scene(strokes, meta))}


@mcp.tool(annotations=ToolAnnotations(
    title='List fonts', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False))
def list_fonts(include_hershey: bool = False) -> dict:
    """Every typeface available to text, draw_text and a scene's "text" producer.

    Two families, and the difference matters on paper:

    HANDWRITING (fonts/, SIL OFL) -- real single-line handwriting faces, print and
    cursive. Each letter is drawn ONCE. Prefer these.
    HERSHEY -- the classic 1967 engraving set, kept for compatibility. Its bold and serif
    faces fake weight by RETRACING every stem (a capital H in `rowmant` is 27 strokes),
    which on a pen plotter reads as a scribble. Pass include_hershey=true to list them.

    Three measurements decide whether a face works here, and they catch different
    failures -- all three were learned the hard way on paper:
      passes   strokes per letter vs a human's. Catches retracing.
      aperture the width of a letter's opening (the gap in s/a/e/o/g) against the 1-2 mm
               the firmware's corner blending eats. Catches letters that close into blobs:
               `brush` at 0.89 mm turned "oo" into "rr" on the test sheet.
      joins    for cursive, whether letters weld into one continuous stroke per word.
    """
    out: dict = {}
    rows = []
    for key, (stem, kind, _join) in STROKE_FONTS.items():
        glyphs = stroke_font(key)[0]
        human = {"l": 1, "H": 3, "n": 1, "o": 1, "e": 1, "s": 1, "t": 2, "i": 2}
        drawn = sum(len(glyphs[c][0]) for c in human if c in glyphs)
        want = sum(v for c, v in human.items() if c in glyphs)
        ap = 1e9
        for c in "saeog":
            for sp in glyphs.get(c, ([], 0))[0]:
                d = math.dist(sp[0], sp[-1]) * 0.5     # mm at a 5 mm cap height
                if 0.025 < d < ap:
                    ap = d
        joined = len(strokes_from_stroke_font("minimum", key)) if kind == "cursive" else None
        rows.append({
            "font": key, "kind": kind, "file": stem,
            "passes_vs_human": round(drawn / want, 2) if want else None,
            "aperture_mm_at_5mm_cap": round(ap, 2) if ap < 1e9 else None,
            "strokes_for_minimum": joined,
            # Three levels, not a boolean: aperture is a PROXY and the band is calibrated
            # from two paper sheets, not from theory. `casual` sits at 1.78 and still
            # failed -- its 's' flattens into a 'c', which is the spine collapsing rather
            # than the opening closing. So "marginal" means exactly that: test it.
            "small_text": ("safe" if ap >= 2.2 else
                           "marginal" if ap >= 1.6 else "closes up"),
        })
    rows.sort(key=lambda r: (r["kind"], -(r["aperture_mm_at_5mm_cap"] or 0)))
    out["handwriting"] = rows
    out["recommended"] = {"print": ["neutral", "architect"],
                          "cursive": ["italienne", "cursive2"]}
    out["notes"] = [
        "Chosen on PAPER, not from renders: two test sheets settled it after both of the "
        "faces picked from simulation lost. `casual` drew its 's' as a 'c'; `brush` mushed "
        "'oo'. Trust the aperture column over a screen preview.",
        "Avoid brush and allure below ~8 mm cap: apertures of 0.89 and 0.59 mm against "
        "1-2 mm of corner blending.",
        "small_text is a proxy calibrated from two sheets, not a guarantee. `casual` rates "
        "marginal at 1.78 mm and still failed on paper, because its 's' loses its spine "
        "rather than its opening. Draw a line before trusting a marginal face.",
        "Cursive faces WELD: 'minimum' is 4 strokes rather than 7 lifts. Fewer lifts also "
        "means fewer landing smears: the nib travels radially as it descends, so every "
        "stroke start carries a short drag.",
        "The page width sets the cap height, not the box height you ask for: fit() keeps "
        "aspect, so cap_mm ~= 72 / (chars * 0.66) on a full-width line.",
        "Attribution and licences: fonts/NOTICE.md. All SIL Open Font License.",
    ]
    if include_hershey:
        import hashlib

        from HersheyFonts import HersheyFonts
        names = sorted(HersheyFonts().default_font_names)
        sig: dict[str, list[str]] = {}
        cost: dict[str, int] = {}
        for n in names:
            g = HersheyFonts(); g.load_default_font(n); g.normalize_rendering(10)
            ss = [list(s) for s in g.strokes_for_text("Hamburgefons 123")]
            cost[n] = len(ss)
            sig.setdefault(hashlib.sha1(repr([[(round(x, 3), round(y, 3)) for x, y in s]
                                              for s in ss]).encode()).hexdigest(),
                           []).append(n)
        out["hershey"] = {
            "distinct_faces": len(sig), "advertised_names": len(names),
            "fonts": [{"font": v[0], "aliases": v[1:], "strokes_for_sample": cost[v[0]]}
                      for v in sig.values()],
            "note": "`meteorology` is NOT a symbol font, it is plain futural; `timesg` is "
                    "a GREEK face despite the name. futural is the only one that does not "
                    "retrace.",
        }
    return out


EXAMPLES_DIR = os.path.join(DATA_DIR, "examples")


def _example_names() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(EXAMPLES_DIR) if f.endswith(".json"))
    except OSError:
        return []


def _example_index() -> list[dict]:
    rows = []
    for n in _example_names():
        try:
            sc = json.load(open(os.path.join(EXAMPLES_DIR, n + ".json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({"name": n, "about": sc.get("_comment", "")[:300]})
    return rows


@mcp.resource("lineus://examples")
def examples_resource() -> str:
    """Index of worked example scenes -- finished drawings to start from."""
    return json.dumps(_example_index(), indent=1)


@mcp.resource("lineus://examples/{name}")
def example_resource(name: str) -> str:
    """One worked example scene, ready for preview_scene / draw_scene."""
    if name not in _example_names():
        raise ValueError(f"no example {name!r}; have {_example_names()}")
    return open(os.path.join(EXAMPLES_DIR, name + ".json"), encoding="utf-8").read()


@mcp.tool(annotations=ToolAnnotations(
    title='Get an example scene', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False))
def get_example(name: str = "") -> dict:
    """Worked example scenes: finished drawings that went through several rounds of
    critique on this machine. With no name, lists them. With a name, returns the scene --
    pass it to preview_scene as-is, or modify it ("the cat, lying down") rather than
    starting from nothing. Each carries a _comment explaining what made it work."""
    if not name:
        return {"examples": _example_index()}
    if name not in _example_names():
        return {"error": f"no example {name!r}", "examples": _example_names()}
    return json.load(open(os.path.join(EXAMPLES_DIR, name + ".json"), encoding="utf-8"))


@mcp.tool(annotations=ToolAnnotations(
    title='Browse doodles', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=True))
def doodles(category: str = "", source: str = "auto", start: int = 0, count: int = 12):
    """Real doodles from Google's Quick, Draw! dataset (CC BY 4.0): single strokes, in the
    order a person drew them, so line width comes from the pen alone.

    With no category: the curated categories bundled with the server, and every category
    that can be fetched from the web (345 in all).
    With a category: a NUMBERED CONTACT SHEET of candidates. Look at it and choose a pick,
    then use {"doodle": category, "pick": N, "box": [x,y,w,h]} in a scene.

    source: "auto" (default) uses the curated set if the category has one, else fetches
    from the web; "web" always fetches -- more variety, NO human review; "bundled" never
    goes online. Fetched categories are cached on disk, which keeps preview and draw
    identical. Web candidates are ranked automatically but many are still scribbles or
    have the word written in them: that is what the contact sheet is for.
    Draw with order="asis" to replay a doodle exactly as its author drew it."""
    if not category:
        b = qd_bundle()
        return {"bundled": {c: len(v) for c, v in sorted(b.items())},
                "web_categories": qd_categories(),
                "note": f"{len(b)} curated categories ship with the server; any of the "
                        f"{len(qd_categories())} can be fetched from the web."}
    try:
        items, src = qd_list(category, source)
    except ExprError as e:
        return {"error": str(e)}
    start = max(0, int(start))
    count = max(1, min(36, int(count)))
    label = (f"{category} -- {src}, {len(items)} candidates, showing {start}.."
             f"{min(start + count, len(items)) - 1}. Pick by the red number.")
    note = (f"{len(items)} {src} candidates for {category!r}. Use "
            f'{{"doodle": "{category}", "pick": N, "box": [x, y, w, h]}}'
            + (' with "source": "web".' if src == "web" and category in qd_bundle() else ".")
            + (" These are UNREVIEWED: reject scribbles and drawings with words in them."
               if src == "web" else " These were each chosen by eye."))
    return [Image(data=doodle_sheet(items, start, count, label), format="png"), note]


@mcp.tool(annotations=ToolAnnotations(
    title='Drawing progress', readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    openWorldHint=False))
def get_job(job_id: str) -> dict:
    """Progress of a drawing job."""
    j = jobs.get(job_id)
    if not j:
        return {"error": "unknown job"}
    el = (j.finished or time.time()) - j.started
    eta = (el / j.done * (j.total - j.done)) if j.done and j.state == "running" else None
    return {"job_id": j.id, "state": j.state, "progress": f"{j.done}/{j.total}",
            "elapsed_s": round(el, 1), "eta_s": round(eta, 1) if eta else None,
            "error": j.error or None}


@mcp.tool(annotations=ToolAnnotations(
    title='Abort the drawing', readOnlyHint=False, destructiveHint=True, idempotentHint=True,
    openWorldHint=True))
def abort() -> dict:
    """Stop the current drawing, lift the pen and go home."""
    n = 0
    for j in jobs.values():
        if j.state in ("queued", "running"):
            j.abort.set(); n += 1
    return {"aborted_jobs": n}


@mcp.tool(annotations=ToolAnnotations(
    title='Home the arm', readOnlyHint=False, destructiveHint=False, idempotentHint=True,
    openWorldHint=True))
def home() -> dict:
    """Lift the pen and move the arm to its home position."""
    with job_lock:
        return {"result": robot.cmd("G28")}


@mcp.tool(annotations=ToolAnnotations(
    title='Draw the orientation test', readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=True))
def draw_orientation_test() -> dict:
    """Draws a frame around the canvas plus an 'F' in the top-left corner so a human
    can confirm the canvas orientation (F must read normally, top-left)."""
    W, H = CANVAS_W, CANVAS_H
    frame = [[(0, 0), (W, 0), (W, H), (0, H), (0, 0)]]
    f = [[(4, 16), (4, 4), (12, 4)], [(4, 10), (10, 10)]]
    return submit(frame + f)


def main() -> None:
    mcp.run()
