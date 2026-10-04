"""Every setting: environment variables, the page, the envelope, pen and speed limits."""
from __future__ import annotations

import os

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


# Bundled data (fonts, doodles, example scenes) ships inside the package.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# Fetched doodles and the preview image live outside the package: an installed package
# directory is not ours to write to.
CACHE_DIR = os.environ.get("LINEUS_CACHE",
                           os.path.join(os.path.expanduser("~"), ".cache", "lineus-mcp"))
# Where preview_* writes a PNG THE USER CAN ACTUALLY LOOK AT. MCP image content goes to
# the agent, not to the person at the terminal -- "where do i see it" was a real question
# and the honest answer was "you cannot". The filename is STABLE so an open viewer
# (macOS Preview, VS Code) reloads it in place as you iterate on a scene; every preview
# note prints the path.
PREVIEW_PATH = os.environ.get("LINEUS_PREVIEW", os.path.join(CACHE_DIR, "preview.png"))


NIB_MM = float(os.environ.get("LINEUS_NIB_MM", "0.5"))


Point = tuple[float, float]
Stroke = list[Point]
