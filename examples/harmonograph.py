"""Harmonograph: one unbroken stroke, pure smooth curves — the ideal subject
for this machine (no corners to round, no pen lifts to smear).

Two slightly detuned pendulums per axis with exponential decay. The detuning is
what makes the figure drift and weave instead of closing on itself.

Auto-fits to the 80 x 45 page with a margin, so it can never run off.
Writes harmonograph.json: a single stroke.
"""
import json
import math
import sys

W, H, MARGIN = 80.0, 45.0, 2.5
STEP_MM = 1.4            # point spacing along the curve

# (amplitude, frequency, phase, decay) — two terms per axis
PX = [(1.00, 2.000, 0.00, 0.0085),
      (0.62, 2.996, 1.20, 0.0120)]
PY = [(1.00, 3.000, math.pi / 2, 0.0095),
      (0.52, 2.004, 2.10, 0.0105)]
T_END, T_STEP = 52.0, 0.014


def axis(terms, t):
    return sum(a * math.exp(-d * t) * math.sin(f * t + p) for a, f, p, d in terms)


raw = []
t = 0.0
while t < T_END:
    raw.append((axis(PX, t), axis(PY, t)))
    t += T_STEP

# fit to the page
xs = [p[0] for p in raw]; ys = [p[1] for p in raw]
sx = (W - 2 * MARGIN) / (max(xs) - min(xs))
sy = (H - 2 * MARGIN) / (max(ys) - min(ys))
cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
pts = [(W / 2 + (x - cx) * sx, H / 2 + (y - cy) * sy) for x, y in raw]

# thin to STEP_MM so we do not send thousands of sub-resolution points
out = [pts[0]]
for p in pts[1:]:
    if math.dist(p, out[-1]) >= STEP_MM:
        out.append(p)
if math.dist(out[-1], pts[-1]) > 0.3:
    out.append(pts[-1])

us = [p[0] for p in out]; vs = [p[1] for p in out]
print(f"raw {len(raw)} -> {len(out)} points, one stroke")
print(f"extent u {min(us):.1f}..{max(us):.1f}  v {min(vs):.1f}..{max(vs):.1f}  (page {W:.0f} x {H:.0f})")
assert 0 <= min(us) and max(us) <= W and 0 <= min(vs) and max(vs) <= H, "off page!"

json.dump([[[round(u, 1), round(v, 1)] for u, v in out]], open("harmonograph.json", "w"))
print("wrote harmonograph.json")
