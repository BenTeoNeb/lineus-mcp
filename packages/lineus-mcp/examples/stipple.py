"""Stippled dot art for the Line-us: a lit sphere plus a star field.

Tone comes from SPACING, not from acceptance probability. A fixed minimum spacing
caps density everywhere and flattens the gradient; here the Poisson radius varies
with shading, so dark areas pack tightly and highlights stay open. That is what
gives a real tonal range.

Page is 80 x 45 mm. Writes stipple.json: single-point paths [[[u,v]], ...].
"""
import json
import math
import random

random.seed(11)

W, H = 80.0, 45.0
CX, CY, R = 29.0, 22.5, 17.5
R_DARK, R_LIGHT = 0.95, 3.4      # dot spacing (mm) in shadow vs highlight
LIMB_STEP = 1.5                  # silhouette dot spacing

LX, LY, LZ = -0.55, -0.60, 0.58
_n = math.sqrt(LX*LX + LY*LY + LZ*LZ)
LX, LY, LZ = LX/_n, LY/_n, LZ/_n


def darkness(u, v):
    """1 = deep shadow, 0 = full highlight. None outside the disc."""
    nx, ny = (u - CX) / R, (v - CY) / R
    s = nx*nx + ny*ny
    if s > 1.0:
        return None
    nz = math.sqrt(1.0 - s)
    diff = max(0.0, nx*LX + ny*LY + nz*LZ)
    val = 0.06 + 0.94 * (diff ** 0.85) + 0.20 * (s ** 4)   # + rim light at the limb
    return max(0.0, min(1.0, 1.0 - val))


def radius_at(u, v):
    d = darkness(u, v)
    if d is None:
        return None
    return R_LIGHT + (R_DARK - R_LIGHT) * (d ** 0.8)


CELL = R_LIGHT
grid = {}


def too_close(u, v, rad):
    gx, gy = int(u / CELL), int(v / CELL)
    span = int(rad / CELL) + 1
    for dx in range(-span, span + 1):
        for dy in range(-span, span + 1):
            for (pu, pv, pr) in grid.get((gx + dx, gy + dy), ()):
                lim = min(rad, pr)          # min: let a dense region abut a sparse one
                if (pu - u) ** 2 + (pv - v) ** 2 < lim * lim:
                    return True
    return False


def place(u, v, rad):
    grid.setdefault((int(u / CELL), int(v / CELL)), []).append((u, v, rad))


dots = []

# 1. silhouette first, so the limb is never crowded out by interior fill
a, limb = 0.0, 0
step = LIMB_STEP / R
while a < 2 * math.pi - 1e-9:
    u, v = CX + R * math.cos(a), CY + R * math.sin(a)
    a += step
    if not too_close(u, v, LIMB_STEP * 0.9):
        place(u, v, LIMB_STEP * 0.9); dots.append((u, v)); limb += 1

# 2. interior, variable-radius dart throwing
interior, tries = 0, 0
while tries < 400000:
    tries += 1
    u = random.uniform(CX - R, CX + R)
    v = random.uniform(CY - R, CY + R)
    rad = radius_at(u, v)
    if rad is None:
        continue
    if too_close(u, v, rad):
        continue
    place(u, v, rad); dots.append((u, v)); interior += 1

# 3. sparse stars in the open right-hand area
stars, tries = 0, 0
while stars < 34 and tries < 60000:
    tries += 1
    u = random.uniform(2.5, W - 2.5)
    v = random.uniform(2.5, H - 2.5)
    if (u - CX) ** 2 + (v - CY) ** 2 < (R + 4.0) ** 2:
        continue
    if random.random() > 0.2 + 0.8 * (u / W):
        continue
    if too_close(u, v, 4.5):
        continue
    place(u, v, 4.5); dots.append((u, v)); stars += 1

print(f"limb {limb}  interior {interior}  stars {stars}  TOTAL {len(dots)}")
us = [d[0] for d in dots]; vs = [d[1] for d in dots]
print(f"extent u {min(us):.1f}..{max(us):.1f}  v {min(vs):.1f}..{max(vs):.1f}   (page {W:.0f} x {H:.0f})")
assert min(us) >= 0 and max(us) <= W and min(vs) >= 0 and max(vs) <= H, "off page!"

json.dump([[[round(u, 1), round(v, 1)]] for u, v in dots], open("stipple.json", "w"))
print("wrote stipple.json")
