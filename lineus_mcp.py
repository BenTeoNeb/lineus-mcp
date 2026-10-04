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

def fit(strokes: list[Stroke], x: float, y: float, w: float, h: float) -> list[Stroke]:
    """Scale strokes uniformly to fit the box (x,y,w,h) in canvas mm, centred."""
    pts = [p for s in strokes for p in s]
    if not pts:
        return []
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    sw, sh = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    k = min(w / sw, h / sh)
    ox = x + (w - sw * k) / 2 - minx * k
    oy = y + (h - sh * k) / 2 - miny * k
    return [[(px * k + ox, py * k + oy) for px, py in s] for s in strokes]

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


def write_preview(strokes: list[Stroke]) -> str:
    """Write the ink view where the user can open it. Returns a note for the tool result."""
    try:
        with open(PREVIEW_PATH, "wb") as fh:
            fh.write(render_ink_png(strokes))
        return (f"Ink preview (true {NIB_MM} mm nib, no travel lines) written to "
                f"{PREVIEW_PATH} — open it once and it reloads in place on every preview. "
                f"The image returned alongside is the diagnostic view: green envelope, "
                f"grey page, pink pen-up travel.")
    except OSError as e:
        return f"could not write {PREVIEW_PATH}: {e}"


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
    if "path" in shape:
        return [[(float(a), float(b)) for a, b in shape["path"]]]
    if "paths" in shape:
        return [[(float(a), float(b)) for a, b in pth] for pth in shape["paths"] if pth]
    if "text" in shape:
        return text_block(shape["text"], shape.get("font", "futural"),
                          shape.get("align", "left"), float(shape.get("leading", 1.4)))
    if "svg" in shape:
        return strokes_from_svg(shape["svg"])
    raise ExprError(f"shape has no producer; expected one of param/path/paths/text/svg, "
                    f"got keys {sorted(shape)}")


def compile_scene(scene: dict) -> list[Stroke]:
    """Compile a declarative scene to strokes. Deterministic: same scene, same strokes."""
    if not isinstance(scene, dict) or "shapes" not in scene:
        raise ExprError('scene must be {"shapes": [...]}')
    out: list[Stroke] = []
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
            fill = shape.get("fill")
            if fill:
                sp = float(fill.get("spacing", 1.15))
                ang = float(fill.get("hatch", 45.0))
                hatched: list[Stroke] = []
                for s in ss:
                    hatched += hatch_polygon(s, ang, sp)
                    if fill.get("cross"):
                        hatched += hatch_polygon(s, ang + 90.0, sp)
                ss = (ss if fill.get("outline", True) else []) + hatched
            if shape.get("transform"):
                ss = _transform(ss, shape["transform"])
            if shape.get("box") or shape.get("fit"):
                b = shape.get("box") or shape.get("fit")
                ss = fit(ss, float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            out += ss
    if scene.get("fit"):
        b = scene["fit"]
        out = fit(out, float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    return [s for s in out if len(s) >= 1]


def scene_digest(scene: dict) -> str:
    """Content hash of a scene. preview and draw derive the SAME id from the SAME input,
    so they cannot diverge -- and no id is ever issued, so none can go stale."""
    blob = json.dumps(scene, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


_scene_cache: dict[str, list[Stroke]] = {}


def compile_cached(scene: dict) -> tuple[str, list[Stroke]]:
    sid = scene_digest(scene)
    if sid not in _scene_cache:
        if len(_scene_cache) > 64:
            _scene_cache.clear()
        _scene_cache[sid] = compile_scene(scene)
    return sid, _scene_cache[sid]

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
    "fast = nearest-neighbour, least travel but hops around; asis = as supplied)."))

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
def preview_paths(paths: list[list[list[float]]]) -> list:
    """Render polylines (canvas mm, [[ [u,v], ... ], ...]) as a PNG without drawing.
    Black = pen down, pink = pen-up travel, grey box = canvas edge."""
    s = order_human([simplify(x) for x in _paths_to_strokes(paths)])
    return [Image(data=render_png(s), format="png"), write_preview(s)]

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
                w: float | None = None, h: float | None = None) -> Image:
    """Render an SVG (paths/shapes, strokes only — fills are ignored) fitted into
    the box (x,y,w,h) in canvas mm (default: whole canvas). Returns a PNG."""
    s = order_human(_svg_fit(svg, x, y, w, h))
    return [Image(data=render_png(s), format="png"), write_preview(s)]

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
                 font: str = "futural") -> list:
    """Render single-stroke (Hershey) text fitted into box (x,y,w,h) mm. Use \\n for
    new lines. Prefer the HANDWRITING faces -- print: neutral architect pancakes delight
    casual; cursive (welded, one stroke per word): italienne cursive2 brush allure.
    call list_fonts() for the measurements. Hershey names still work but most of them
    retrace every stem."""
    s = order_human(_text_fit(text, x, y, w, h, font))
    return [Image(data=render_png(s), format="png"), write_preview(s)]

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
def preview_scene(scene: dict) -> list:
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
    Modifiers (any producer):
      "fill"      {"hatch":deg, "spacing":mm (default 1.15), "cross":bool,
                   "outline":bool} -- hatching is how you get solid black here
      "transform" {"translate":[dx,dy], "rotate":deg, "scale":s|[sx,sy], "about":[x,y]}
      "box"/"fit" [x,y,w,h]  scale this shape into a box
      "repeat"    N, with i (0..N-1) and n available in the expressions"""
    try:
        _, strokes = compile_cached(scene)
    except ExprError as e:
        return [f"scene error: {e}"]
    s = order_human(strokes)
    return [Image(data=render_png(s), format="png"), write_preview(s)]

@mcp.tool()
def draw_scene(scene: dict, speed: int | None = None, order: str = "human") -> dict:
    """Draw a declarative scene (same object as preview_scene -- see it for the shape
    language). Compilation is deterministic and content-hashed, so passing the scene you
    previewed draws exactly what you saw. Returns a job_id and the scene_id."""
    try:
        sid, strokes = compile_cached(scene)
    except ExprError as e:
        return {"error": str(e)}
    r = submit(strokes, speed, order)
    r["scene_id"] = sid
    return r

@mcp.tool()
def plan_scene(scene: dict) -> dict:
    """Compile a scene and report what it would cost, WITHOUT drawing or rendering:
    stroke/point counts, bounding box, pen-up travel and any warnings."""
    try:
        sid, strokes = compile_cached(scene)
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
                         + clearance_check(strokes))}

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
