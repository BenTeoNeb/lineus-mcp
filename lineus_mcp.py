# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "svgelements>=1.9", "Hershey-Fonts>=2.1", "pillow>=10"]
# ///
"""
Line-us MCP server — lets an AI agent draw with a Line-us robot.

Agent-facing coordinate system: a rectangular canvas in millimetres,
origin top-left, u to the right, v downward. The server maps it to
Line-us units (~20/mm), clamps to a safe box, orders strokes, and
streams G-code over TCP 1337 with ok-handshake in a background job.

Env vars: LINEUS_HOST (default line-us.local), LINEUS_MOCK=1 (no robot),
LINEUS_SWAP / LINEUS_FLIP_U / LINEUS_FLIP_V (orientation fixes, 0/1).
"""
from __future__ import annotations
import ast, hashlib, io, json, math, os, socket, threading, time, uuid
from dataclasses import dataclass, field
from mcp.server.fastmcp import FastMCP, Image

HOST = os.environ.get("LINEUS_HOST", "line-us.local")
PORT = 1337
MOCK = os.environ.get("LINEUS_MOCK") == "1"
UNITS_PER_MM = 20.0
# The PAGE: a rectangle used as the default framing for text/SVG and as the canvas
# origin. It is NOT a hard boundary -- draw_paths may go outside it, anywhere inside
# the measured envelope below.
XMIN, XMAX, YMIN, YMAX = 700, 1600, -800, 800
# The ENVELOPE: what the arm can actually reach, measured on hardware over 45 probes.
# An annular sector, NOT the rectangle the official docs give as the app drawing area --
# that rectangle's far corners sit at radius 2037, past the 1950 the arm can reach, and
# the firmware silently clamps them back along the ray AND lifts the pen.
# Outer radius measured at 1950 units, flat to +/-0.5 over -110..+110 deg; we keep a 5%
# margin. X_MIN keeps us in front of the shoulder (beyond that the pen is behind the
# base, where the robot body and the edge of the paper are).
ENV_R_MAX = float(os.environ.get("LINEUS_R_MAX", "1850"))
ENV_R_MIN = float(os.environ.get("LINEUS_R_MIN", "650"))
ENV_X_MIN = float(os.environ.get("LINEUS_X_MIN", "650"))
SWAP = os.environ.get("LINEUS_SWAP", "1") == "1"      # u -> Y axis by default
FLIP_U = os.environ.get("LINEUS_FLIP_U", "0") == "1"
FLIP_V = os.environ.get("LINEUS_FLIP_V", "0") == "1"
CANVAS_W = ((YMAX - YMIN) if SWAP else (XMAX - XMIN)) / UNITS_PER_MM
CANVAS_H = ((XMAX - XMIN) if SWAP else (YMAX - YMIN)) / UNITS_PER_MM
MAX_POINTS = 20000
MIN_SEG_MM = 0.2          # drop points closer than this (robot resolution)
# G94 S<n> is NOT a speed: per the official GCode spec it is the MAXIMUM STEP SIZE in
# drawing units for an interpolated move. Smaller = more steps = slower and finer. 1..30,
# firmware default 5. It only applies to PEN-DOWN moves, which the firmware defines as
# Z < 500. The arm also keeps one command of lookahead, so it blends consecutive moves
# rather than stopping at a vertex -- that is what rounds corners.
DEFAULT_SPEED = int(os.environ.get("LINEUS_SPEED", "5"))
# G94 P<n> is the same thing for PEN-UP moves. Firmware default 15; we default to 30.
# Travel speed was PROVEN not to affect mark quality (2026-10-04: four groups of dots at
# constant radius, P1/P5/P15/P30, identical tics), so this is a pure throughput knob.
# Measured on 12 pen-up hops: P5 8.37s, P15 2.87s, P30 1.55s -- 1.85x faster than default.
# Avoid P1: 29s for the same hops, and it dropped the connection.
TRAVEL_SPEED = max(2, min(30, int(os.environ.get("LINEUS_TRAVEL_SPEED", "30"))))
PEN_DOWN_MAX = 499      # firmware treats Z >= 500 as pen-up; a "down" Z must stay below it
# S1 subdivides every move into 1-unit steps. A long travel then outlasts the socket
# timeout, and recovering from that wedged the firmware completely: it kept answering
# TCP but never sent its banner again, and only a power cycle brought it back.
MIN_SPEED = int(os.environ.get("LINEUS_MIN_SPEED", "2"))
SOCKET_TIMEOUT = float(os.environ.get("LINEUS_TIMEOUT", "180"))
# Pen-down Z. NOT 0: the Z axis is coupled to radius, so the nib drags radially as it
# descends and every stroke start/end gets a hook. Less over-travel = less drag.
# Measured 2026-10-04 at r=1500: Z0 and Z150 solid but hooked, Z300 solid AND clean,
# Z450 breaks up (loses contact). Per-pen; recalibrate after a pen change.
PEN_DOWN_Z = max(0, min(PEN_DOWN_MAX,
                       int(os.environ.get("LINEUS_PEN_DOWN_Z", "300"))))
# ...but contact height is NOT constant across the sheet. Measured 2026-10-04 on a 3x3
# grid (scratchpad/zprobe5.py: at each point a staircase of ticks each drawn at a fixed Z,
# so the readout is a COUNT of surviving marks): contact Z runs 375 at one corner to >=900
# at the far one. Two mechanical causes, both confirmed:
#   reach (v): the arm droops the further it extends -- the user felt this by hand first
#   swing (u): a left/right asymmetry that SURVIVES G40, so it is play, not the firmware
#              ZMap -- that hypothesis was tested with G40 and killed: the asymmetry
#              survives with the factory ZMap bypassed
# LINEUS_PEN_PLANE="a,b,c" gives contact_Z(u,v) = a*u + b*v + c in canvas mm, and the
# pen-down Z becomes contact - LINEUS_PEN_MARGIN. Unset = the flat PEN_DOWN_Z above.
#
# DEFAULT OFF, AND THE DEPTH CORRECTION IS NOT VALIDATED. A 9-position A/B on paper
# (scratchpad/validate_plane.py, 2026-10-04) drew each position twice, once with the plane
# and once with the flat Z300, and found NO difference: all 18 lines solid, mean ink
# 0.332-0.384 either way, no systematic difference at the ends. At one position the two
# lines were drawn at Z100 and Z300 -- 200 units apart -- and are indistinguishable.
# The prediction (faint at low over-press, hooked at high) was wrong on both counts.
# So: line quality is INSENSITIVE to over-press across at least 79..539 units, and the
# flat constant is good enough. Do not enable this expecting better strokes.
# What the plane IS good for is clearance_check() below, which predicts where pen-up
# travel will mark -- a real defect seen on three separate sheets.
# For this machine the surface measured "-4.330,7.768,575.9".
# How far PAST contact to press. This is the real quality knob -- re-reading the old
# pendepth2 arcs through the plane shows they varied Z and POSITION together, and what
# actually tracked quality was over-press, not the commanded Z:
#     -797 solid, big hook   -615 solid, small hook   -341 solid and CLEAN   -3 broken
# So: less over-press = less radial hook, until contact is lost. 250 sits in the clean
# band and stays clear of the plane's own +/-122 residual, which 60 would not have.
PEN_MARGIN = int(os.environ.get("LINEUS_PEN_MARGIN", "250"))


def _parse_plane(s: str | None):
    if not s:
        return None
    try:
        a, b, c = (float(x) for x in s.split(","))
    except ValueError:
        return None
    return (a, b, c)


PEN_PLANE = _parse_plane(os.environ.get("LINEUS_PEN_PLANE"))
TRAVEL_Z = 1000           # the pen-up height; a hard firmware limit, no headroom above it
MIN_CLEARANCE = 80        # warn below this much room between contact and TRAVEL_Z
SMALL_FEATURE_MM = 10.0   # warn when a stroke's bbox is smaller than this
# Where preview_* writes a PNG THE USER CAN ACTUALLY LOOK AT. MCP image content goes to
# the agent, not to the person at the terminal -- "where do i see it" was a real question
# and the honest answer was "you cannot". The filename is STABLE so an open viewer
# (macOS Preview, VS Code) reloads it in place as you iterate on a scene.
PREVIEW_PATH = os.environ.get(
    "LINEUS_PREVIEW", os.path.join(os.path.dirname(os.path.abspath(__file__)), "_preview.png"))
NIB_MM = float(os.environ.get("LINEUS_NIB_MM", "0.5"))


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

Point = tuple[float, float]
Stroke = list[Point]

# ---------------------------------------------------------------- robot link
class Robot:
    def __init__(self):
        self.sock: socket.socket | None = None
        self.banner = ""
        self.lock = threading.Lock()

    def _connect(self):
        if MOCK:
            self.banner = 'hello VERSION:"mock" NAME:line-us SERIAL:0'; return
        # A single G01 blocks until the move finishes, and at G94 S1 a long travel
        # can take minutes. A short timeout kills the job mid-move AND wedges the
        # robot's command parser (recovery needs a power cycle). Be generous.
        self.sock = socket.create_connection((HOST, PORT), timeout=SOCKET_TIMEOUT)
        self.banner = self._read()

    def _read(self) -> str:
        buf = b""
        while not buf.endswith(b"\0"):
            c = self.sock.recv(1)
            if not c:
                raise ConnectionError("Line-us closed the connection")
            buf += c
        return buf.decode(errors="replace").strip("\r\n\0")

    def cmd(self, line: str) -> str:
        with self.lock:
            if MOCK:
                time.sleep(0.002); return "ok"
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    self.sock.sendall(line.encode() + b"\n")
                    return self._read()
                except (OSError, ConnectionError):
                    self.close()
                    if attempt == 2:
                        raise
        return "error"

    def hello(self) -> str:
        with self.lock:
            if self.sock is None and not MOCK:
                self._connect()
            elif MOCK:
                self._connect()
        return self.banner

    def close(self):
        if self.sock:
            try: self.sock.close()
            except OSError: pass
        self.sock = None

robot = Robot()

# ---------------------------------------------------------------- geometry
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

def render_png(strokes: list[Stroke], scale: float = 5.0) -> bytes:
    from PIL import Image as PILImage, ImageDraw
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
    amin = math.degrees(math.acos(min(1.0, ENV_X_MIN / ENV_R_MIN))) if ENV_R_MIN > ENV_X_MIN else 0.0
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
    from PIL import Image as PILImage, ImageDraw
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
        with open(PREVIEW_PATH, "wb") as fh:
            fh.write(png)
        return (f"Preview written to {PREVIEW_PATH} ({what}); it reloads in place on every "
                f"preview. Judge the drawing by the simulated image, not the diagnostic one "
                f"(green envelope, grey page, pink pen-up travel)."), png
    except OSError as e:
        return f"could not write {PREVIEW_PATH}: {e}", png


def _preview_result(strokes: list[Stroke], simulate: bool, warnings: list[str] | None = None):
    note, png = write_preview(strokes, simulate)
    if warnings:
        note += "\nDRAWING CHECKS:\n- " + "\n- ".join(warnings)
    out = [Image(data=render_png(strokes), format="png")]
    if simulate:
        out.append(Image(data=png, format="png"))
    out.append(note)
    return out


# ---------------------------------------------------------------- sources
def strokes_from_svg(svg: str) -> list[Stroke]:
    from svgelements import SVG, Shape, Path, Move, Close
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

def strokes_from_text(text: str, font: str = "futural") -> list[Stroke]:
    from HersheyFonts import HersheyFonts
    f = HersheyFonts(); f.load_default_font(font); f.normalize_rendering(10)
    strokes: list[Stroke] = []
    line_h = 14.0
    for li, line in enumerate(text.split("\n")):
        for st in f.strokes_for_text(line):
            strokes.append([(x, -y + li * line_h) for x, y in st])   # Hershey y is up
    return strokes


# ------------------------------------------------- single-line (stroke) fonts
# Hershey is an ENGRAVING font set, and its "bold"/"serif" faces fake weight by retracing
# every stem: `rowmant` draws a capital H with 27 strokes, `timesr` uses 3.0x the strokes a
# person would. On paper that reads as a sketchy scribble, not handwriting -- which is
# exactly what the 2026-10-04 test sheet showed on the timesr and futuram lines.
#
# These are real single-line HANDWRITING faces (fonts/, SIL OFL, see fonts/NOTICE.md),
# measured at 0.8-1.2x a human's stroke count. The cursive ones also have small gaps
# between one letter's exit and the next letter's entry, so they can be WELDED into one
# continuous stroke per word -- which is what makes cursive cursive.
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
# join threshold in em; a cursive face welds, a print face never does
CURSIVE_JOIN_EM = float(os.environ.get("LINEUS_JOIN_EM", "0.13"))
STROKE_FONTS = {
    "casual":     ("EMSCasualHand",  "print",   0.0),
    "architect":  ("EMSTech",        "print",   0.0),
    "pancakes":   ("EMSPancakes",    "print",   0.0),
    "delight":    ("EMSDelight",     "print",   0.0),
    "neutral":    ("EMSReadability", "print",   0.0),
    "cursive2":   ("EMSCapitol",     "cursive", CURSIVE_JOIN_EM),
    "brush":      ("EMSBrush",       "cursive", CURSIVE_JOIN_EM),
    "allure":     ("EMSAllure",      "cursive", CURSIVE_JOIN_EM),
    "italienne":  ("EMSSwiss",       "cursive", CURSIVE_JOIN_EM),
}
_sf_cache: dict = {}


def _load_stroke_font(file_stem: str):
    """Parse an SVG 1.1 stroke font into {char: (subpaths, advance)}, cap height = 10.

    Scaled so cap height is 10 units, the same convention HersheyFonts.normalize_rendering(10)
    gives us, so the two font systems are interchangeable downstream.
    """
    import re
    from svgelements import Path
    path = os.path.join(FONT_DIR, file_stem + ".svg")
    src = open(path, encoding="utf-8", errors="replace").read()

    def attr(name, default):
        m = re.search(rf'{name}="(-?[\d.]+)"', src)
        return float(m.group(1)) if m else default

    upem = attr("units-per-em", 1000.0)
    cap = attr("cap-height", 500.0) or 500.0
    k = 10.0 / (cap / upem)                      # -> cap height 10
    default_adv = attr("horiz-adv-x", 0.5 * upem)
    glyphs: dict[str, tuple[list[Stroke], float]] = {}
    for m in re.finditer(r"<glyph([^>]*?)/>", src, re.S):
        a = dict(re.findall(r'(\S+)="([^"]*)"', m.group(1)))
        ch = a.get("unicode")
        if ch is None or len(ch) != 1:
            continue
        adv = float(a.get("horiz-adv-x", default_adv) or default_adv) / upem * k
        subs: list[Stroke] = []
        cur: Stroke = []
        for seg in Path(a.get("d", "") or "M 0 0").segments():
            t = type(seg).__name__
            if t == "Move":
                if len(cur) > 1:
                    subs.append(cur)
                cur = [(seg.end.x / upem * k, -seg.end.y / upem * k)]   # font y up, canvas v down
            elif t == "Close":
                continue
            else:
                n = 1 if t == "Line" else 14
                for i in range(1, n + 1):
                    q = seg.point(i / n)
                    cur.append((q.x / upem * k, -q.y / upem * k))
        if len(cur) > 1:
            subs.append(cur)
        glyphs[ch] = (subs, adv)
    if " " not in glyphs:
        glyphs[" "] = ([], 0.25 * k)
    return glyphs, k      # k is units per em: coordinates are scaled to cap height 10,
                          # so one em is NOT 10 units and a join threshold given in em
                          # has to be multiplied by it. Getting this wrong made the
                          # threshold ~200x too small and nothing welded.


def stroke_font(name: str):
    key = (name or "").lower()
    if key not in STROKE_FONTS:
        return None
    if key not in _sf_cache:
        stem, kind, join_em = STROKE_FONTS[key]
        glyphs, per_em = _load_stroke_font(stem)
        _sf_cache[key] = (glyphs, kind, join_em * per_em)
    return _sf_cache[key]


def strokes_from_stroke_font(text: str, name: str) -> list[Stroke]:
    """Lay out one line, welding letters together where a cursive face allows it.

    The weld is the whole point for cursive. A glyph's EXIT is the end of the subpath
    reaching furthest right and its ENTRY the start of the one furthest left -- not simply
    the last and first subpaths, because 'i' and 'j' finish with the dot, which sits high
    and to the left. Getting that wrong draws a line through the word, which is exactly
    what a first attempt did.
    """
    got = stroke_font(name)
    if got is None:
        try:
            return strokes_from_text(text, name)
        except ValueError:                     # not a Hershey name either
            return strokes_from_text(text, "futural")
    glyphs, _kind, join = got
    out: list[Stroke] = []
    x = 0.0
    exit_idx: int | None = None          # index in `out` of the stroke carrying the exit
    for ch in text:
        g = glyphs.get(ch) or glyphs.get(" ")
        if g is None:
            continue
        subs, adv = g
        placed = [[(px + x, py) for px, py in s] for s in subs]
        if placed:
            entry = min(placed, key=lambda s: s[0][0])
            welded = False
            if join and exit_idx is not None:
                if math.dist(out[exit_idx][-1], entry[0]) <= join:
                    out[exit_idx].extend(entry)      # continue the pen, no lift
                    welded = True
            rest = [s for s in placed if not (welded and s is entry)]
            base = len(out)
            out.extend(rest)
            ex = max(placed, key=lambda s: s[-1][0])
            if welded and ex is entry:
                pass                                  # exit still lives on out[exit_idx]
            else:
                exit_idx = base + rest.index(ex)
        x += adv
    return [s for s in out if len(s) > 1]


def font_kind(name: str) -> str:
    got = stroke_font(name)
    return got[1] if got else "hershey"


# ---------------------------------------------------------------- expressions
# The scene language is built on an expression evaluator, NOT a catalogue of shapes.
# A fixed vocabulary of circle/ellipse/arc would handle the dull cases and send every
# interesting drawing straight back to pasting raw coordinates, which is the problem
# this is meant to solve. So there is no circle primitive: a circle is
#     {"param": {"t": [0, 6.2832, 200], "x": "33+5.5*cos(t)", "y": "30+5.5*sin(t)"}}
# and so are the spiral, the rose, the lissajous and the harmonograph that actually
# ate the tokens. The vocabulary is open because it is a function evaluator.
#
# Safety: a whitelisted AST walk, not eval() on arbitrary input. No imports, no
# attribute access, no indexing, no comprehensions, no calls except the names below.
# That keeps the public server free of an exec() interface while leaving the
# expressiveness where it matters.
EXPR_FUNCS = {
    "abs": abs, "min": min, "max": max, "round": round, "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "exp": math.exp, "log": math.log, "sqrt": math.sqrt, "hypot": math.hypot,
    "floor": math.floor, "ceil": math.ceil, "fmod": math.fmod,
    "degrees": math.degrees, "radians": math.radians,
    "sign": lambda x: (x > 0) - (x < 0),
    "clamp": lambda x, lo, hi: lo if x < lo else (hi if x > hi else x),
    "lerp": lambda a, b, s: a + (b - a) * s,
}
EXPR_CONSTS = {"pi": math.pi, "tau": math.tau, "e": math.e}

_EXPR_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.IfExp,
    ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.And, ast.Or,
)


class ExprError(ValueError):
    pass


class Expr:
    """A compiled, sandboxed scalar expression over named variables."""

    __slots__ = ("src", "_code", "_names")

    def __init__(self, src: str, variables: tuple[str, ...]):
        self.src = src
        try:
            tree = ast.parse(src, mode="eval")
        except SyntaxError as e:
            raise ExprError(f"{src!r}: {e.msg}") from None
        allowed = set(variables) | set(EXPR_FUNCS) | set(EXPR_CONSTS)
        for node in ast.walk(tree):
            if not isinstance(node, _EXPR_NODES):
                raise ExprError(
                    f"{src!r}: {type(node).__name__} is not allowed in an expression")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in EXPR_FUNCS:
                    raise ExprError(f"{src!r}: only these calls are allowed: "
                                    f"{', '.join(sorted(EXPR_FUNCS))}")
                if node.keywords:
                    raise ExprError(f"{src!r}: keyword arguments are not allowed")
            if isinstance(node, ast.Name) and node.id not in allowed:
                raise ExprError(f"{src!r}: unknown name {node.id!r}. Available here: "
                                f"{', '.join(sorted(allowed))}")
        self._names = tuple(variables)
        self._code = compile(tree, "<expr>", "eval")

    def __call__(self, **vals) -> float:
        env = dict(EXPR_CONSTS)
        env.update(EXPR_FUNCS)
        env.update(vals)
        try:
            return float(eval(self._code, {"__builtins__": {}}, env))  # noqa: S307
        except ExprError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ExprError(f"{self.src!r} failed at {vals}: {type(e).__name__}: {e}") from None

# ---------------------------------------------------------------- hatching
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


# ---------------------------------------------------------------- text blocks
def text_block(lines, font: str = "futural", align: str = "left",
               leading: float = 1.4) -> list[Stroke]:
    """Lay out multi-line, optionally multi-FONT text.

    `lines` is a string (split on \\n) or a list of either strings or
    {"s": ..., "font": ...}. draw_text could only ever do one string in one font, so
    the 4-line multi-font job became 930 hand-merged points through draw_paths.

    Laid out in font units (cap height 10, line pitch 10*leading) and left for the
    caller to `fit` into a box, so scale is one concern rather than two.
    """
    from HersheyFonts import HersheyFonts
    if isinstance(lines, str):
        lines = lines.split("\n")
    items = [{"s": ln} if isinstance(ln, str) else dict(ln) for ln in lines]
    cache: dict[str, object] = {}

    def renderer(name):
        if name not in cache:
            f = HersheyFonts()
            f.load_default_font(name)
            f.normalize_rendering(10)
            cache[name] = f
        return cache[name]

    laid: list[tuple[list[Stroke], float]] = []
    for it in items:
        name = it.get("font") or font
        if stroke_font(name) is not None:          # a real single-line handwriting face
            ss = strokes_from_stroke_font(it["s"], name)
        else:                                       # Hershey, kept for compatibility
            f = renderer(name)
            ss = [[(x, -y) for x, y in st] for st in f.strokes_for_text(it["s"])]
        w = max((p[0] for s in ss for p in s), default=0.0)
        laid.append((ss, w))
    width = max((w for _, w in laid), default=0.0)
    out: list[Stroke] = []
    for li, (ss, w) in enumerate(laid):
        dx = 0.0 if align == "left" else ((width - w) if align == "right"
                                          else (width - w) / 2.0)
        dy = li * 10.0 * leading
        out.extend([[(x + dx, y + dy) for x, y in s] for s in ss])
    return out



# ---------------------------------------------------------------- curves
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


# ---------------------------------------------------------------- stroke planning
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



# ---------------------------------------------------------------- the machine, simulated
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


# ---------------------------------------------------------------- drawing checks
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



# ---------------------------------------------------------------- doodles (Quick, Draw!)
# Real drawings by real people, from Google's Quick, Draw! dataset (CC BY 4.0, see
# doodles/NOTICE.md). They are stored as RECORDED PEN STROKES in the order the person drew
# them -- not outlines, not fills -- which is exactly what a pen plotter wants: every line is
# one stroke and its width comes from the pen alone.
#
# Two sources. A curated set ships in doodles/quickdraw.json: 184 drawings in 31
# categories, every one chosen by eye. Anything else is fetched on demand from the dataset's
# public bucket, ranked automatically, and cached -- the cache is what keeps preview and draw
# identical. Web doodles get no human review, so use the doodles() tool to LOOK at the
# candidates before picking: many are scribbles, some have the word written in them.
DOODLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doodles")
QD_BUCKET = "https://storage.googleapis.com/quickdraw_dataset/full/simplified/"
QD_FETCH_BYTES = int(os.environ.get("LINEUS_QD_BYTES", "600000"))   # ~500 candidates
QD_KEEP = 48                         # ranked candidates kept per category in the cache
QD_RANK_VERSION = "v2"
CACHE_DIR = os.environ.get("LINEUS_CACHE",
                           os.path.join(os.path.expanduser("~"), ".cache", "lineus-mcp"))
_qd_bundle = None
_qd_categories = None
_qd_web: dict = {}


def qd_categories() -> list[str]:
    """The dataset's 345 category names. Fetching is only ever done for one of these, so
    the server cannot be pointed at an arbitrary URL."""
    global _qd_categories
    if _qd_categories is None:
        try:
            with open(os.path.join(DOODLE_DIR, "categories.txt"), encoding="utf-8") as fh:
                _qd_categories = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            _qd_categories = []
    return _qd_categories


def qd_bundle() -> dict:
    global _qd_bundle
    if _qd_bundle is None:
        try:
            with open(os.path.join(DOODLE_DIR, "quickdraw.json"), encoding="utf-8") as fh:
                _qd_bundle = json.load(fh).get("categories", {})
        except (OSError, ValueError):
            _qd_bundle = {}
    return _qd_bundle


def qd_score(d: dict) -> float | None:
    """Rank a raw Quick, Draw! record for this plotter; None rejects it.

    Rewards CLEAN, moderate ink rather than detail. The first version rewarded point count,
    which suits animals and fails simple shapes: a clean five-point star has ~10 points, so
    it was rejected while scribbled stars ranked first. Ink per size ('density') is what
    separates a drawing from a scribble -- except for stars, whose outline is long by nature;
    no score substitutes for looking, which is why doodles() returns a contact sheet.
    """
    if not d.get("recognized"):
        return None
    st = d.get("drawing") or []
    n = len(st)
    pts = sum(len(s[0]) for s in st)
    if not (1 <= n <= 14) or pts < 8:
        return None
    xs = [x for s in st for x in s[0]]
    ys = [y for s in st for y in s[1]]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if min(w, h) < 50:
        return None
    ink = sum(math.dist((s[0][i], s[1][i]), (s[0][i + 1], s[1][i + 1]))
              for s in st for i in range(len(s[0]) - 1))
    dens = ink / math.hypot(w, h)
    if dens > 11:
        return None
    tiny = sum(1 for s in st if len(s[0]) <= 2)
    return (1.0 + (0.4 if 2 <= n <= 8 else 0.0) - 0.15 * max(0.0, dens - 6.5)
            - 0.08 * tiny + 0.25 * min(pts, 70) / 70)


def _qd_cache_path(category: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in category)
    return os.path.join(CACHE_DIR, "quickdraw",
                        f"{safe}.{QD_RANK_VERSION}.{QD_FETCH_BYTES}.json")


def qd_web(category: str) -> list:
    """Ranked candidates for a category from the dataset, fetched once and cached on disk.

    Only the first QD_FETCH_BYTES of the category file are read (a byte-range request):
    that is several hundred drawings, plenty to rank from, without pulling a 100 MB file.
    """
    if category not in qd_categories():
        raise ExprError(f"no Quick, Draw! category {category!r}. Call doodles() for the list.")
    if category in _qd_web:
        return _qd_web[category]
    path = _qd_cache_path(category)
    try:
        with open(path, encoding="utf-8") as fh:
            _qd_web[category] = json.load(fh)
            return _qd_web[category]
    except (OSError, ValueError):
        pass
    import urllib.parse
    import urllib.request
    url = QD_BUCKET + urllib.parse.quote(category) + ".ndjson"
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{QD_FETCH_BYTES - 1}",
                                               "User-Agent": "lineus-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read(QD_FETCH_BYTES + 1)
    except Exception as e:  # noqa: BLE001
        raise ExprError(f"could not fetch Quick, Draw! {category!r} ({type(e).__name__}: "
                        f"{e}). Offline? The bundled categories still work.") from None
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue                      # the last line is cut short by the byte range
        s = qd_score(d)
        if s is not None:
            rows.append((s, d["drawing"]))
    rows.sort(key=lambda r: -r[0])
    ranked = [r[1] for r in rows[:QD_KEEP]]
    if not ranked:
        raise ExprError(f"no usable doodles in the sample for {category!r}")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ranked, fh, separators=(",", ":"))
    except OSError:
        pass                              # still usable this session, just not cached
    _qd_web[category] = ranked
    return ranked


def qd_list(category: str, source: str = "auto") -> tuple[list, str]:
    """The candidate list for a category and which source it came from."""
    source = (source or "auto").lower()
    if source not in ("auto", "bundled", "web"):
        raise ExprError('doodle "source" must be auto, bundled or web')
    b = qd_bundle()
    if source in ("auto", "bundled") and category in b:
        return b[category], "bundled"
    if source == "bundled":
        raise ExprError(f"{category!r} is not in the bundled set. Bundled: "
                        f"{', '.join(sorted(b))}. Use source 'web' or 'auto' for the rest.")
    return qd_web(category), "web"


def doodle_strokes(category: str, pick: int = 0, source: str = "auto",
                   smooth: int = 6) -> list[Stroke]:
    """One doodle as strokes on its 0..255 grid, in the order its author drew them."""
    items, _src = qd_list(category, source)
    if not (0 <= pick < len(items)):
        raise ExprError(f"doodle {category!r} pick must be 0..{len(items) - 1}, got {pick}")
    out = []
    for xs, ys in (s[:2] for s in items[pick]):
        pts = [(float(x), float(y)) for x, y in zip(xs, ys)]
        if smooth and len(pts) >= 3:
            pts = catmull_rom(pts, int(smooth))
        if len(pts) >= 2:
            out.append(pts)
    return out


def doodle_sheet(items: list, start: int, count: int, label: str) -> bytes:
    """A numbered contact sheet, so a doodle can be chosen by LOOKING -- the step no
    ranking replaces."""
    from PIL import Image as PILImage, ImageDraw
    T, PAD, COLS = 150, 8, 6
    sel = items[start:start + count]
    rows = max(1, (len(sel) + COLS - 1) // COLS)
    img = PILImage.new("RGB", (COLS * (T + PAD) + PAD, rows * (T + PAD) + PAD + 22), "white")
    d = ImageDraw.Draw(img)
    d.text((PAD, 4), label, fill=(60, 60, 140))
    for k, dr in enumerate(sel):
        x0 = PAD + (k % COLS) * (T + PAD)
        y0 = 22 + PAD + (k // COLS) * (T + PAD)
        xs = [x for s in dr for x in s[0]]
        ys = [y for s in dr for y in s[1]]
        sc = (T - 18) / max(max(xs) - min(xs), max(ys) - min(ys), 1)
        for s in dr:
            pts = [(x0 + 9 + (x - min(xs)) * sc, y0 + 9 + (y - min(ys)) * sc)
                   for x, y in zip(s[0], s[1])]
            if len(pts) > 1:
                d.line(pts, fill="black", width=2, joint="curve")
        d.rectangle([x0, y0, x0 + T, y0 + T], outline=(225, 225, 225))
        d.text((x0 + 3, y0 + 2), str(start + k), fill=(200, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- scene compiler
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
    sx, sy = (float(sc), float(sc)) if isinstance(sc, (int, float)) else (float(sc[0]), float(sc[1]))
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
            pts = [(float(a), float(b)) for a, b in pth]
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
    if "text" in shape:
        return text_block(shape["text"], shape.get("font", "futural"),
                          shape.get("align", "left"), float(shape.get("leading", 1.4)))
    if "svg" in shape:
        return strokes_from_svg(shape["svg"])
    raise ExprError(f"shape has no producer; expected one of param/path/paths/text/svg/doodle, "
                    f"got keys {sorted(shape)}")


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
    hatch: list[Stroke] = []       # fill lines: already optimally joined, never re-planned
    fills: list[Stroke] = []
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
                ss = ss if fill.get("outline", True) else []
            if shape.get("transform"):
                tr = shape["transform"]
                ss, hh, fpolys = _transform(ss, tr), _transform(hh, tr), _transform(fpolys, tr)
            b = shape.get("box") or shape.get("fit") or (
                [0, 0, CANVAS_W, CANVAS_H] if "doodle" in shape else None)
            if b:
                prm = fit_params(ss + hh, float(b[0]), float(b[1]), float(b[2]), float(b[3]))
                ss, hh, fpolys = apply_fit(ss, prm), apply_fit(hh, prm), apply_fit(fpolys, prm)
            out += ss
            hatch += hh
            fills += fpolys
    if scene.get("fit"):
        b = scene["fit"]
        prm = fit_params(out + hatch, float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        out, hatch, fills = apply_fit(out, prm), apply_fit(hatch, prm), apply_fit(fills, prm)
    meta: dict = {"fills": fills}
    join = scene.get("join")
    if join:
        opts = join if isinstance(join, dict) else {}
        out, st = plan_paths(out, opts)
        meta["join_stats"] = st
    return [s for s in out + hatch if len(s) >= 1], meta


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

# ---------------------------------------------------------------- jobs
@dataclass
class Job:
    id: str
    strokes: list[Stroke]
    total: int
    speed: int = DEFAULT_SPEED
    pen_z: int = PEN_DOWN_Z
    travel: int = TRAVEL_SPEED
    done: int = 0
    state: str = "queued"          # queued|running|done|aborted|error
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    abort: threading.Event = field(default_factory=threading.Event)

jobs: dict[str, Job] = {}
job_lock = threading.Lock()   # one drawing at a time

def run_job(job: Job):
    with job_lock:
        job.state = "running"; job.started = time.time()
        try:
            robot.cmd(f"G94 S{job.speed}")        # pen-down step size
            robot.cmd(f"G94 P{job.travel}")       # pen-up step size
            for s in job.strokes:
                if job.abort.is_set(): break
                x, y = to_machine(*s[0]); robot.cmd(f"G01 X{x} Y{y} Z{TRAVEL_Z}")
                robot.cmd(f"G01 X{x} Y{y} Z{pen_down_z(*s[0])}")
                for p in s[1:]:
                    if job.abort.is_set(): break
                    x, y = to_machine(*p)
                    r = robot.cmd(f"G01 X{x} Y{y} Z{pen_down_z(*p)}")
                    if r.startswith("error"):
                        job.error = r
                    job.done += 1
                robot.cmd(f"G01 X{x} Y{y} Z{TRAVEL_Z}")
                job.done += 1
            robot.cmd("G28")
            job.state = "aborted" if job.abort.is_set() else "done"
        except Exception as e:  # noqa: BLE001
            job.state, job.error = "error", f"{type(e).__name__}: {e}"
            try: robot.cmd("G01 Z1000")
            except Exception: pass
        job.finished = time.time()

def _pen_z_report(strokes: list[Stroke]):
    """What pen-down Z this job will actually use: one number, or the range over the page."""
    if PEN_PLANE is None:
        return PEN_DOWN_Z
    zs = [pen_down_z(u, v) for st in strokes for u, v in st]
    lo, hi = min(zs), max(zs)
    return lo if lo == hi else {"min": lo, "max": hi,
                                "clamped_at_max": sum(z == PEN_DOWN_MAX for z in zs)}


def submit(strokes: list[Stroke], speed: int | None = None,
           order: str = "human") -> dict:
    speed = DEFAULT_SPEED if speed is None else max(MIN_SPEED, min(30, int(speed)))
    order = (order or "human").lower()
    if order not in ("fast", "human", "asis"):
        return {"error": f"order must be fast|human|asis, got {order!r}"}
    simple = [simplify(s) for s in strokes if len(s) >= 1]
    if order == "fast":
        strokes = order_strokes(simple)          # nearest-neighbour, may reverse strokes
    elif order == "human":
        strokes = order_human(simple)            # top row first, left to right, no reversal
    else:
        strokes = simple                         # exactly as supplied
    n = sum(len(s) for s in strokes)
    if n == 0:
        return {"error": "nothing to draw"}
    if n > MAX_POINTS:
        return {"error": f"{n} points exceeds limit {MAX_POINTS}; simplify the input"}
    job = Job(id=uuid.uuid4().hex[:8], strokes=strokes, total=n, speed=speed,
              pen_z=PEN_DOWN_Z, travel=TRAVEL_SPEED)
    jobs[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return {"job_id": job.id, "strokes": len(strokes), "points": n, "speed": speed,
            "order": order, "pen_up_travel_mm": round(travel_mm(strokes)),
            "pen_down_z": _pen_z_report(strokes), "travel_speed": TRAVEL_SPEED,
            "warnings": (envelope_check(strokes) + small_feature_check(strokes, speed)
                         + clearance_check(strokes)),
            "hint": "poll get_job(job_id) — drawing takes roughly 0.05–0.1 s per point"}

# ---------------------------------------------------------------- MCP tools
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

@mcp.tool()
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

@mcp.tool()
def preview_paths(paths: list[list[list[float]]], simulate: bool = True) -> list:
    """Render polylines (canvas mm, [[ [u,v], ... ], ...]) without drawing.
    Returns the diagnostic view (pink = pen-up travel, grey box = page) and, with
    simulate=true (default), what the arm will actually put on paper."""
    s = order_human([simplify(x) for x in _paths_to_strokes(paths)])
    return _preview_result(s, simulate)

@mcp.tool()
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

@mcp.tool()
def preview_svg(svg: str, x: float | None = None, y: float | None = None,
                w: float | None = None, h: float | None = None,
                simulate: bool = True) -> list:
    """Render an SVG (paths/shapes, strokes only — fills are ignored) fitted into
    the box (x,y,w,h) in canvas mm (default: whole canvas). Returns a PNG."""
    s = order_human(_svg_fit(svg, x, y, w, h))
    return _preview_result(s, simulate)

@mcp.tool()
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

@mcp.tool()
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

@mcp.tool()
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

@mcp.tool()
def preview_scene(scene: dict, simulate: bool = True) -> list:
    """Render a declarative SCENE as a PNG without drawing. Prefer this over
    preview_paths: a scene is ~50x smaller than the coordinates it compiles to, and
    draw_scene given the SAME scene provably draws what you previewed.

    scene = {"shapes": [ ...shape... ], "fit": [x,y,w,h] (optional, scales the lot)}

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
    Modifiers (any producer):
      "fill"      {"hatch":deg, "spacing":mm (default 1.15), "cross":bool,
                   "outline":bool} -- hatching is how you get solid black here
      "transform" {"translate":[dx,dy], "rotate":deg, "scale":s|[sx,sy], "about":[x,y]}
      "box"/"fit" [x,y,w,h]  scale this shape into a box
      "repeat"    N, with i (0..N-1) and n available in the expressions"""
    try:
        _, strokes, meta = compile_cached_full(scene)
    except ExprError as e:
        return [f"scene error: {e}"]
    return _preview_result(order_human(strokes), simulate, lint_scene(strokes, meta))

@mcp.tool()
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

@mcp.tool()
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
    return {"scene_id": sid, "strokes": len(strokes), "points": len(pts),
            "bbox_mm": [round(min(p[0] for p in pts), 2), round(min(p[1] for p in pts), 2),
                        round(max(p[0] for p in pts), 2), round(max(p[1] for p in pts), 2)],
            "pen_up_travel_mm": round(travel_mm(order_human(strokes))),
            "warnings": (envelope_check(strokes) + small_feature_check(strokes, speed)
                         + clearance_check(strokes) + lint_scene(strokes, meta))}

@mcp.tool()
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
        from HersheyFonts import HersheyFonts
        import hashlib
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


EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")


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


@mcp.tool()
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


@mcp.tool()
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
            + (f' with "source": "web".' if src == "web" and category in qd_bundle() else ".")
            + (" These are UNREVIEWED: reject scribbles and drawings with words in them."
               if src == "web" else " These were each chosen by eye."))
    return [Image(data=doodle_sheet(items, start, count, label), format="png"), note]


@mcp.tool()
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

@mcp.tool()
def abort() -> dict:
    """Stop the current drawing, lift the pen and go home."""
    n = 0
    for j in jobs.values():
        if j.state in ("queued", "running"):
            j.abort.set(); n += 1
    return {"aborted_jobs": n}

@mcp.tool()
def home() -> dict:
    """Lift the pen and move the arm to its home position."""
    with job_lock:
        return {"result": robot.cmd("G28")}

@mcp.tool()
def draw_orientation_test() -> dict:
    """Draws a frame around the canvas plus an 'F' in the top-left corner so a human
    can confirm the canvas orientation (F must read normally, top-left)."""
    W, H = CANVAS_W, CANVAS_H
    frame = [[(0, 0), (W, 0), (W, H), (0, H), (0, 0)]]
    f = [[(4, 16), (4, 4), (12, 4)], [(4, 10), (10, 10)]]
    return submit(frame + f)

if __name__ == "__main__":
    mcp.run()
