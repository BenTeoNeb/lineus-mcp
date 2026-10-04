"""Ladybug as LINE art, filling the 80 x 45 mm page. Six legs, two antennae.

Continuous strokes instead of dots: no dot-smearing, and hatching gives genuine
solid blacks. Body is shortened vertically from the dot version to leave room
for the legs, which need the top and bottom margins.

Writes ladybug_lines.json.
"""
import json
import math

W, H = 80.0, 45.0
BCX, BCY, BA, BB = 46.0, 22.5, 27.0, 14.5      # body ellipse (legs need the margins)
HCX, HCY, HR = 15.0, 22.5, 7.0                 # head
HATCH = 1.15                                    # hatch line spacing (mm)

SPOTS = [(32, 14.0, 4.0), (47, 11.5, 3.4), (60, 15.0, 3.0),
         (32, 31.0, 4.0), (47, 33.5, 3.4), (60, 30.0, 3.0)]

strokes = []


def ellipse(cx, cy, a, b, step=1.1):
    pts, ang = [], 0.0
    while ang < 2 * math.pi:
        pts.append((cx + a * math.cos(ang), cy + b * math.sin(ang)))
        r = math.hypot(a * math.sin(ang), b * math.cos(ang))
        ang += step / max(r, 1e-6)
    pts.append(pts[0])
    return pts


def hatch_circle(cx, cy, r, spacing=HATCH, clip=None):
    """Horizontal hatch inside a circle; `clip` can exclude part of it."""
    out = []
    v = cy - r + spacing * 0.5
    while v < cy + r:
        dx = math.sqrt(max(r * r - (v - cy) ** 2, 0.0))
        u0, u1 = cx - dx, cx + dx
        if clip:
            u0, u1 = clip(u0, u1, v)
        if u1 - u0 > 0.6:
            out.append([(u0, v), (u1, v)])
        v += spacing
    return out


def body_edge_v(u, upper=True):
    """v of the body ellipse at a given u."""
    t = 1.0 - ((u - BCX) / BA) ** 2
    if t <= 0:
        return BCY
    d = BB * math.sqrt(t)
    return BCY - d if upper else BCY + d


# --- body, head, elytra split
strokes.append(ellipse(BCX, BCY, BA, BB))
strokes.append(ellipse(HCX, HCY, HR, HR))
strokes.append([(22.0, BCY), (BCX + BA - 1.0, BCY)])

# head hatched solid, but not where the body covers it
strokes += hatch_circle(HCX, HCY, HR,
                        clip=lambda u0, u1, v: (u0, min(u1, 19.5)))

# --- spots: outline + hatch
for sx, sy, sr in SPOTS:
    strokes.append(ellipse(sx, sy, sr, sr, step=0.9))
    strokes += hatch_circle(sx, sy, sr - 0.25)

# --- legs: 3 per side, attached at the body edge, splayed fore/side/aft
LEGS_U = [25.0, 36.0, 48.0]
# (knee du, knee OUTWARD dv, foot du, foot OUTWARD dv) -- dv positive = away from body
LEG_OUT = [(-7.0, 6.0, -6.0, 2.5),        # front: forward and out
           (-3.0, 5.5, -6.0, 1.5),        # middle: sideways
           (4.0, 5.0, 6.0, 1.2)]          # hind: backward and out
for u, (d1u, d1v, d2u, d2v) in zip(LEGS_U, LEG_OUT):
    for upper in (True, False):
        s = -1 if upper else 1
        v0 = body_edge_v(u, upper)
        knee = (u + d1u, v0 + s * d1v)
        foot = (knee[0] + d2u, knee[1] + s * d2v)
        strokes.append([(u, v0), knee, foot])

# --- antennae
strokes.append([(11.5, 18.0), (6.5, 12.5), (3.5, 10.0)])
strokes.append([(11.5, 27.0), (6.5, 32.5), (3.5, 35.0)])

pts = [p for s in strokes for p in s]
us = [p[0] for p in pts]; vs = [p[1] for p in pts]
print(f"strokes {len(strokes)}   points {len(pts)}")
print(f"extent u {min(us):.1f}..{max(us):.1f}   v {min(vs):.1f}..{max(vs):.1f}   (page {W:.0f} x {H:.0f})")
assert 0 <= min(us) and max(us) <= W and 0 <= min(vs) and max(vs) <= H, "off page!"

json.dump([[[round(u, 1), round(v, 1)] for u, v in s] for s in strokes],
          open("ladybug_lines.json", "w"))
print("wrote ladybug_lines.json")
