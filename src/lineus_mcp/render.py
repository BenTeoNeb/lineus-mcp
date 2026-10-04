"""PNG renders: the diagnostic view and the ink view."""
from __future__ import annotations

import io
import math
import os

from .config import (
    CANVAS_H,
    CANVAS_W,
    ENV_R_MAX,
    ENV_R_MIN,
    ENV_X_MIN,
    FLIP_U,
    FLIP_V,
    NIB_MM,
    PREVIEW_PATH,
    SWAP,
    UNITS_PER_MM,
    XMIN,
    YMIN,
    Stroke,
)
from .machine import envelope_bounds_mm, simulate_strokes


def render_png(strokes: list[Stroke], scale: float = 5.0) -> bytes:
    from PIL import Image as PILImage
    from PIL import ImageDraw
    umin, vmin, umax, vmax = envelope_bounds_mm()
    pad = 10
    W = int((umax - umin) * scale) + 2 * pad
    H = int((vmax - vmin) * scale) + 2 * pad
    def px(u, v):
        return (pad + (u - umin) * scale, pad + (v - vmin) * scale)
    img = PILImage.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    # reachable envelope outline, sampled in machine space and mapped back
    def machine_to_canvas(x, y):
        a = (y - YMIN) / UNITS_PER_MM if SWAP else (x - XMIN) / UNITS_PER_MM
        b = (x - XMIN) / UNITS_PER_MM if SWAP else (y - YMIN) / UNITS_PER_MM
        u = (CANVAS_W - a) if FLIP_U else a
        v = (CANVAS_H - b) if FLIP_V else b
        return u, v
    amax = math.degrees(math.acos(min(1.0, ENV_X_MIN / ENV_R_MAX)))
    outer, inner = [], []
    k = -amax
    while k <= amax + 1e-9:
        rad = math.radians(k)
        outer.append(px(*machine_to_canvas(ENV_R_MAX * math.cos(rad), ENV_R_MAX * math.sin(rad))))
        k += 2.0
    amin = (math.degrees(math.acos(min(1.0, ENV_X_MIN / ENV_R_MIN)))
            if ENV_R_MIN > ENV_X_MIN else 0.0)
    k = -amin
    while k <= amin + 1e-9:
        rad = math.radians(k)
        inner.append(px(*machine_to_canvas(ENV_R_MIN * math.cos(rad), ENV_R_MIN * math.sin(rad))))
        k += 2.0
    if len(outer) > 1:
        d.line(outer, fill=(150, 200, 150), width=2)
    if len(inner) > 1:
        d.line(inner, fill=(150, 200, 150), width=2)
    if outer and inner:
        d.line([outer[0], inner[0]], fill=(150, 200, 150), width=2)
        d.line([outer[-1], inner[-1]], fill=(150, 200, 150), width=2)

    # the default page
    d.rectangle([px(0, 0), px(CANVAS_W, CANVAS_H)], outline=(200, 200, 200))
    prev = None
    for s in strokes:
        pts = [px(u, v) for u, v in s]
        if prev is not None:
            d.line([prev, pts[0]], fill=(255, 170, 170), width=1)   # pen-up travel
        if len(pts) > 1:
            d.line(pts, fill="black", width=2, joint="curve")
        else:
            d.point(pts, fill="black")
        prev = pts[-1]
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def render_ink_png(strokes: list[Stroke], nib_mm: float = NIB_MM,
                   scale: float = 12.0) -> bytes:
    """What the PAPER will look like: ink only, at true nib width.

    Deliberately NOT the same picture as render_png(). That one is a diagnostic -- it
    shows the envelope, the page and pen-up travel, which is what the agent needs to
    check reachability and ordering. This one shows what you get, and it is the view
    that catches a curve whose loops are closer together than the nib is wide. A
    harmonograph that looks like a lovely wire figure at 2 px per line comes out as a
    solid black lozenge at 0.5 mm, and only this render says so before the pen moves.
    """
    from PIL import Image as PILImage
    from PIL import ImageDraw
    pad = 8
    W = int(CANVAS_W * scale) + 2 * pad
    H = int(CANVAS_H * scale) + 2 * pad
    img = PILImage.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([pad, pad, W - pad, H - pad], outline=(225, 225, 225))
    w = max(1, round(nib_mm * scale))
    for s in strokes:
        pts = [(pad + u * scale, pad + v * scale) for u, v in s]
        if len(pts) > 1:
            d.line(pts, fill="black", width=w, joint="curve")
        else:
            d.ellipse([pts[0][0] - w / 2, pts[0][1] - w / 2,
                       pts[0][0] + w / 2, pts[0][1] + w / 2], fill="black")
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def write_preview(strokes: list[Stroke], simulate: bool = True) -> tuple[str, bytes]:
    """Write the preview the user can open, and return (note, png).

    Simulated by default: the clean render is what you meant, the simulated one is what
    the paper will get, and only the second has ever predicted a disappointment.
    """
    shown = simulate_strokes(strokes) if simulate else strokes
    png = render_ink_png(shown)
    what = ("SIMULATED -- corners blended by the firmware's ~1.5 mm lookahead and a radial "
            "tick at each stroke end, as measured on this arm" if simulate else
            f"clean ink at true {NIB_MM} mm nib, no travel lines")
    try:
        os.makedirs(os.path.dirname(PREVIEW_PATH) or ".", exist_ok=True)
        with open(PREVIEW_PATH, "wb") as fh:
            fh.write(png)
        return (f"Preview written to {PREVIEW_PATH} ({what}); it reloads in place on every "
                f"preview. Judge the drawing by the simulated image, not the diagnostic one "
                f"(green envelope, grey page, pink pen-up travel)."), png
    except OSError as e:
        return f"could not write {PREVIEW_PATH}: {e}", png
