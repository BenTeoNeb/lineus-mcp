"""Flowing walker: a figure drawn with CURVES, not assembled from shapes.

No reference image and no stock shapes. Every line is a smooth path through a few points,
placed from the figure-drawing canon: about 6.3 heads tall (one head = 130 units here),
three-quarter view, walking right, the near arm swinging back and the far arm forward.

What makes it read as a drawing rather than a diagram:
  - long contours: one line down the whole back, one down the front
  - the nose is a bump in the profile line, not a shape stuck on the face
  - detail is open strokes: the elbow and knee are single creases, the cuffs one line each
  - lines stop where something passes in front: the back line breaks behind the near arm
  - drawn SIDEWAYS along the page's 80 mm side, so the figure is 78 mm tall, not 43

Writes src/lineus_mcp/data/examples/flowing_walker.json.
"""
import json
import os

L = {
 # head: profile carries the nose; hair is a mass of tufts, not an outline
 "profile":   [(352,128),(366,150),(368,174),(380,194),(370,203),(372,213),(366,228),(356,236)],
 "jaw":       [(356,236),(338,240),(318,232),(306,216)],
 "ear":       [(304,186),(296,190),(296,203),(304,210)],
 "eye":       [(349,178),(349,186)],
 "mouth":     [(353,215),(365,214)],
 "hair":      [(310,178),(290,176),(262,190),(246,170),(252,138),(276,112),(310,100),(342,106),(362,124),(368,146)],
 "fringe":    [(368,146),(350,142),(334,150),(318,160),(312,176)],
 "nape":      [(252,192),(258,214),(276,228),(290,232)],
 # neck and collar
 "neck_f":    [(334,240),(336,256),(340,270)],
 "neck_b":    [(288,232),(286,250),(284,264)],
 "collar":    [(284,264),(304,276),(322,282),(340,272)],
 "lapel":     [(340,272),(352,292),(344,306)],
 # jacket body: one line down the back, one down the front
 "back":      [(284,264),(256,280),(240,310),(234,360),(233,410)],
 "back_low":  [(240,486),(246,520)],
 "front":     [(340,272),(366,292),(378,334),(380,390),(376,450),(370,520)],
 "opening":   [(344,306),(348,380),(350,450),(352,522)],
 "hem":       [(246,520),(300,529),(352,526),(370,520)],
 "pocket":    [(298,452),(318,450),(334,453)],
 # near arm, swinging back, in front of the body
 "arm_back":  [(300,292),(278,330),(262,392),(244,444),(224,488)],
 "arm_front": [(334,318),(316,384),(294,436),(266,482),(248,500)],
 "elbow":     [(282,414),(290,424)],
 "cuff":      [(222,486),(246,502)],
 "hand_n":    [(222,490),(208,512),(212,532),(228,532),(244,506)],
 # far arm, swinging forward, appearing past the chest
 "far_arm_f": [(380,366),(398,414),(416,456),(430,488)],
 "far_arm_b": [(376,436),(390,466),(406,496)],
 "far_cuff":  [(406,494),(432,486)],
 "hand_f":    [(408,500),(418,522),(436,530),(446,512),(434,492)],
 # trousers: near leg stepping forward, far leg pushing off behind
 "near_f":    [(366,524),(384,598),(394,642),(404,718),(414,790)],
 "near_b":    [(318,530),(336,600),(352,650),(368,720),(384,792)],
 "knee":      [(374,640),(384,652)],
 "near_hem":  [(384,792),(414,790)],
 "far_f":     [(316,582),(302,640),(286,704),(268,770)],
 "far_b":     [(262,526),(254,600),(240,668),(222,738),(210,772)],
 "far_hem":   [(210,772),(240,782),(268,772)],
 # shoes: front heel striking, back heel lifted
 "shoe_n":    [(386,794),(376,814),(400,826),(450,823),(462,810),(442,798),(416,792)],
 "shoe_f":    [(212,776),(200,790),(222,814),(262,826),(276,818),(254,800),(266,778)],
}

COMMENT = ("A figure drawn with curves, not shapes: every stroke is a smooth path through a few "
           "points. Long contours run through several parts (the whole back, the front), "
           "detail is open strokes (elbow, knee, cuffs), the nose is part of the profile, and "
           "the back line stops behind the near arm. Drawn sideways along the 80 mm side so "
           "it is 78 mm tall: turn the paper to view it. To change the pose, move the points; "
           "keep lines long and open.")
paths = [[[y, -x] for x, y in pts] for pts in L.values()]       # rotated: head toward u = 0
scene = {"_comment": COMMENT, "fit": [1, 1, 78, 43], "shapes": [{"paths": paths, "smooth": 10}]}
out = os.path.join(os.path.dirname(__file__), "..", "src", "lineus_mcp", "data", "examples",
                   "flowing_walker.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(scene, fh, separators=(",", ":"))
print(f"wrote {os.path.normpath(out)}")
