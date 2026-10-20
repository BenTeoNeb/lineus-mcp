"""Layered landscape: a worked example of hidden-line removal ("occlude": true).

Shapes are listed BACK TO FRONT and each closed one hides whatever is already on the page
underneath it: the sun sits behind the mountains, the mountains' hatching stops at the
hills, the trees stand on the middle hill and their feet vanish behind the front one.
Nothing here is clipped by hand -- delete "occlude" and every layer shows through.

The hills are sampled sine curves rather than smoothed paths: a smoothed CLOSED path
overshoots at its sharp bottom corners and bulges out of the frame.

Writes src/lineus_mcp/data/examples/layered_landscape.json.
"""
import json
import math
import os

L, R, T, B = 2.0, 78.0, 2.0, 43.0      # the frame


def hill(base, amp, waves, phase, step=1.5):
    top = []
    u = L
    while u < R:
        top.append([round(u, 2), round(base + amp * math.sin(waves * u / 76 * math.tau + phase), 2)])
        u += step
    top.append([R, round(base + amp * math.sin(waves * math.tau + phase), 2)])
    return top + [[R, B], [L, B], top[0]]


trees = []
for i in range(5):
    u0 = 16 + i * 9.5
    trees.append([[u0, 34.5 - 0.5 * i], [u0 + 3.2, 25.5 - 0.5 * i], [u0 + 6.4, 34.5 - 0.5 * i],
                  [u0, 34.5 - 0.5 * i]])

scene = {
    "_comment": ("Hidden-line removal: shapes are listed BACK TO FRONT with \"occlude\": true, "
                 "and each closed shape hides what is already under it -- the sun behind the "
                 "mountains, the hatching stopping at the hills, the trees in front of the "
                 "middle hill. The sun rings are opaque:false so they do not hide anything. "
                 "join dedupes the hill bases that coincide with the frame."),
    "occlude": True,
    "join": {"explode": True, "dedupe": True, "chain": True},
    "shapes": [
        {"param": {"t": [0, 6.2832, 160], "x": "57+6*cos(t)", "y": "15+6*sin(t)",
                   "closed": True}},
        {"repeat": 2, "opaque": False,
         "param": {"t": [0, 6.2832, 140], "x": "57+(8.4+2*i)*cos(t)",
                   "y": "15+(8.4+2*i)*sin(t)", "closed": True}},
        {"path": [[L, B], [L, 27], [11, 16], [17, 21], [27, 10], [37, 22], [45, 17],
                  [58, 26], [66, 19], [R, 29], [R, B], [L, B]],
         "fill": {"hatch": 60, "spacing": 1.6}},
        {"path": hill(30, 2.2, 1.5, 0.4)},
        {"paths": trees},
        {"path": hill(37.5, 1.6, 1.2, 2.2)},
        {"path": [[L, T], [R, T], [R, B], [L, B], [L, T]], "opaque": False},
    ],
}

out = os.path.join(os.path.dirname(__file__), "..", "src", "lineus_mcp", "data", "examples",
                   "layered_landscape.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(scene, fh, separators=(",", ":"))
print(f"wrote {os.path.normpath(out)}")
