"""Draw the robot's own reachable envelope, plus a spiral converging on its centre.

The envelope is NOT an annulus in practice: the inner limit is a straight chord
(machine x >= 650), not a circle, so the region is a circular segment. The sector
half-angle shrinks with radius -- acos(650/r) -- reaching zero at r = 650. That
single point, machine (650, 0), is as close to the shoulder as the arm can get,
so it is the natural target for the spiral.

The spiral oscillates within the allowed angle while its radius decreases, so it
funnels into that tip.

Writes envelope_art.json. Prints the extent: this goes well outside the page.
"""
import json
import math

UPM, PX0, PY0 = 20.0, 700.0, -800.0
R_OUT, X_MIN = 1800.0, 660.0       # margin under the measured 1950 / 650
SWEEPS = 4


def to_canvas(x, y):
    return (y - PY0) / UPM, (x - PX0) / UPM


def polar(r, deg):
    a = math.radians(deg)
    return r * math.cos(a), r * math.sin(a)


def amax(r):
    return math.degrees(math.acos(min(1.0, X_MIN / r)))


strokes = []

# --- envelope outline: arc at R_OUT, closed by the straight inner chord
arc = []
a = -amax(R_OUT)
while a <= amax(R_OUT) + 1e-9:
    arc.append(to_canvas(*polar(R_OUT, a)))
    a += 0.7
# the chord x = X_MIN, from one arc end to the other
y_end = R_OUT * math.sin(math.radians(amax(R_OUT)))
chord = [to_canvas(X_MIN, y_end), to_canvas(X_MIN, -y_end)]
strokes.append(arc + chord + [arc[0]])

# --- spiral: radius falls, angle oscillates inside the shrinking sector
spiral = []
N = 2600
for i in range(N + 1):
    t = i / N
    r = R_OUT - (R_OUT - X_MIN - 2.0) * t
    span = amax(r)
    deg = span * math.sin(2 * math.pi * SWEEPS * t)
    spiral.append(to_canvas(*polar(r, deg)))
# thin to ~1.3 mm spacing
out = [spiral[0]]
for p in spiral[1:]:
    if math.dist(p, out[-1]) >= 1.3:
        out.append(p)
out.append(spiral[-1])
strokes.append(out)

pts = [p for s in strokes for p in s]
us = [p[0] for p in pts]; vs = [p[1] for p in pts]
print(f"strokes {len(strokes)}  points {len(pts)}  (outline {len(strokes[0])}, spiral {len(strokes[1])})")
print(f"extent u {min(us):.1f}..{max(us):.1f}   v {min(vs):.1f}..{max(vs):.1f}")
print(f"SIZE {max(us)-min(us):.0f} x {max(vs)-min(vs):.0f} mm   (the 80x45 page sits at u 0..80, v 0..45)")
print(f"overhang: {-min(us):.0f} mm left, {max(us)-80:.0f} mm right, "
      f"{-min(vs):.0f} mm above, {max(vs)-45:.0f} mm below the page")

# reachability against the real envelope
bad = 0
for u, v in pts:
    x, y = PX0 + v * UPM, PY0 + u * UPM
    if not (x >= 650 and 650 <= math.hypot(x, y) <= 1850):
        bad += 1
print(f"unreachable points: {bad}")

json.dump([[[round(u, 1), round(v, 1)] for u, v in s] for s in strokes],
          open("envelope_art.json", "w"))
print("wrote envelope_art.json")
