"""What the arm can reach, how the pen behaves, and a simulation of what it really draws."""
from __future__ import annotations

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
    MIN_CLEARANCE,
    PEN_DOWN_MAX,
    PEN_DOWN_Z,
    PEN_MARGIN,
    PEN_PLANE,
    SWAP,
    TRAVEL_Z,
    UNITS_PER_MM,
    XMIN,
    YMIN,
    Stroke,
)
from .geometry import order_human


def contact_z(u: float, v: float) -> float | None:
    """Commanded Z at which the nib first touches paper at (u, v), or None if uncalibrated."""
    if PEN_PLANE is None:
        return None
    a, b, c = PEN_PLANE
    return a * u + b * v + c


def pen_down_z(u: float, v: float) -> int:
    """Pen-down Z to use at (u, v): just past contact, never into the pen-up regime.

    Returns the flat PEN_DOWN_Z unless a plane is configured, which is the default --
    see the note above: the depth correction showed no measurable benefit on paper.

    Clamping at PEN_DOWN_MAX is not a formality. Contact varies by ~550 units across the
    page but the whole pen-down band is only 0..499, so where contact is high the clamp
    binds and the pen still over-presses. Software narrows that; it cannot remove it.
    Only taking the mechanical play out of the arm can.
    """
    cz = contact_z(u, v)
    if cz is None:
        return PEN_DOWN_Z
    return max(0, min(PEN_DOWN_MAX, round(cz - PEN_MARGIN)))


def to_machine(u: float, v: float) -> tuple[int, int]:
    """canvas mm -> Line-us units. No clamping: out-of-reach points are reported by
    envelope_check() instead of being silently bent (the firmware would clamp them
    radially, which distorts a drawing without telling anyone)."""
    a = (CANVAS_W - u) if FLIP_U else u
    b = (CANVAS_H - v) if FLIP_V else v
    if SWAP:   # u along Y, v along X (v down = towards the robot body)
        x = XMIN + b * UNITS_PER_MM
        y = YMIN + a * UNITS_PER_MM
    else:
        x = XMIN + a * UNITS_PER_MM
        y = YMIN + b * UNITS_PER_MM
    return (round(x), round(y))


def in_envelope(x: float, y: float) -> bool:
    """Is this machine point physically reachable (with our safety margin)?"""
    r = math.hypot(x, y)
    return x >= ENV_X_MIN and ENV_R_MIN <= r <= ENV_R_MAX


def envelope_bounds_mm() -> tuple[float, float, float, float]:
    """Bounding box of the envelope in canvas mm: (umin, vmin, umax, vmax)."""
    ymax = math.sqrt(max(ENV_R_MAX ** 2 - ENV_X_MIN ** 2, 0.0))
    us = [(y - YMIN) / UNITS_PER_MM for y in (-ymax, ymax)]
    vs = [(x - XMIN) / UNITS_PER_MM for x in (ENV_X_MIN, ENV_R_MAX)]
    if SWAP:
        return min(us), min(vs), max(us), max(vs)
    return min(vs), min(us), max(vs), max(us)


def clearance_check(strokes: list[Stroke]) -> list[str]:
    """Warn where the pen may not lift clear of the paper.

    Pen-up is Z1000 and that is a hard firmware limit -- there is no headroom above it.
    Where contact Z approaches 1000 the nib stays in contact during travel and draws
    spurious lines between strokes. Observed on paper 2026-10-04 at the far corner
    (low u, high v), where a Z1000 travel move left visible ink.
    """
    if PEN_PLANE is None:
        return []
    worst, at = None, None
    for st in strokes:
        for u, v in st:
            cz = contact_z(u, v)
            if worst is None or cz > worst:
                worst, at = cz, (u, v)
    clear = TRAVEL_Z - worst
    if clear >= MIN_CLEARANCE:
        return []
    return [f"pen-up clearance is only {clear:.0f} units at u={at[0]:.0f} v={at[1]:.0f} "
            f"(contact Z {worst:.0f}, travel Z {TRAVEL_Z}); the nib may drag during travel "
            f"and draw lines between strokes. Raise the pen in the clamp, or keep the "
            f"drawing away from that corner."]


def envelope_check(strokes: list[Stroke]) -> list[str]:
    """Report points the arm cannot reach. These are NOT clamped by us; the firmware
    would clamp them radially, silently distorting the drawing, so we name them."""
    bad = []
    for st in strokes:
        for u, v in st:
            x, y = to_machine(u, v)
            if not in_envelope(x, y):
                bad.append((u, v, x, y))
    if not bad:
        return []
    u, v, x, y = bad[0]
    r = math.hypot(x, y)
    why = ("too close to the shoulder" if (x < ENV_X_MIN or r < ENV_R_MIN)
           else f"out of reach (radius {r:.0f} > {ENV_R_MAX:.0f})")
    return [f"{len(bad)} point(s) outside the reachable envelope, e.g. ({u:.1f},{v:.1f}) mm "
            f"-> machine ({x},{y}): {why}. Per the official spec the firmware moves to the "
            f"nearest reachable point AND LIFTS THE PEN — so this does not merely distort "
            f"the drawing, it breaks the stroke. Move or rescale it instead."]


# A clean render is a promise the pen does not keep. Every drawing that disappointed on
# paper looked fine on screen, so preview_* now shows what the arm will actually do, using
# the faults that were measured rather than guessed:
#   corner blending  the firmware keeps one command of lookahead and blends through
#                    vertices, rounding corners by a roughly CONSTANT 1-2 mm
#   radial tick      the lift axis is not vertical, so the nib travels along the line to
#                    the shoulder as it touches down and lifts off: a short tick on that
#                    radial line at every stroke end, longer the further the arm reaches
# The tick model is an approximation: its AXIS (radial) and its growth with reach were
# measured; its length is calibrated loosely and its sign along the axis was not pinned.
BLEND_MM = float(os.environ.get("LINEUS_BLEND_MM", "1.5"))


TICK_MM = float(os.environ.get("LINEUS_TICK_MM", "0.5"))     # at radius 1500 units


def machine_to_canvas(x: float, y: float) -> tuple[float, float]:
    """Inverse of to_machine, without rounding."""
    a = (y - YMIN) / UNITS_PER_MM if SWAP else (x - XMIN) / UNITS_PER_MM
    b = (x - XMIN) / UNITS_PER_MM if SWAP else (y - YMIN) / UNITS_PER_MM
    return ((CANVAS_W - a) if FLIP_U else a, (CANVAS_H - b) if FLIP_V else b)


def _radial(u: float, v: float) -> tuple[float, float, float]:
    """Unit canvas vector pointing AWAY from the shoulder at (u, v), and the radius."""
    a = (CANVAS_W - u) if FLIP_U else u
    b = (CANVAS_H - v) if FLIP_V else v
    x = XMIN + (b if SWAP else a) * UNITS_PER_MM
    y = YMIN + (a if SWAP else b) * UNITS_PER_MM
    r = math.hypot(x, y) or 1.0
    u1, v1 = machine_to_canvas(x, y)
    u2, v2 = machine_to_canvas(x + 20 * x / r, y + 20 * y / r)
    du, dv = u2 - u1, v2 - v1
    n = math.hypot(du, dv) or 1.0
    return du / n, dv / n, r


def _blend(s: Stroke, win: float = BLEND_MM, step: float = 0.15) -> Stroke:
    """Moving average along arc length, ENDPOINTS PINNED. A naive average shortens every
    stroke at both ends -- the machine does not: the pen reaches the commanded end before
    the lift. So the window shrinks symmetrically toward each end instead of truncating."""
    r = [s[0]]
    for a, b in zip(s, s[1:]):
        d = math.dist(a, b)
        if d < 1e-9:
            continue
        n = max(1, int(d / step))
        for k in range(1, n + 1):
            r.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
    if len(r) < 3:
        return r
    w = max(1, int(win / step) // 2)
    out = []
    for i in range(len(r)):
        k = min(w, i, len(r) - 1 - i)
        seg = r[i - k:i + k + 1]
        out.append((sum(p[0] for p in seg) / len(seg), sum(p[1] for p in seg) / len(seg)))
    return out


def simulate_strokes(strokes: list[Stroke]) -> list[Stroke]:
    """What the paper gets: blended corners plus a radial tick at both ends of each stroke."""
    out = []
    for s in order_human(strokes):
        if len(s) < 2:
            out.append(s)
            continue
        b = _blend(s)
        for end in (0, -1):
            du, dv, r = _radial(*b[end])
            t = TICK_MM * r / 1500.0
            tip = (b[end][0] + du * t, b[end][1] + dv * t)
            b = [tip] + b if end == 0 else b + [tip]
        out.append(b)
    return out
