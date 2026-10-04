"""Text: single-line handwriting faces (bundled, OFL) and Hershey fonts."""
from __future__ import annotations

import math
import os

from .config import DATA_DIR, Stroke


def strokes_from_text(text: str, font: str = "futural") -> list[Stroke]:
    from HersheyFonts import HersheyFonts
    f = HersheyFonts(); f.load_default_font(font); f.normalize_rendering(10)
    strokes: list[Stroke] = []
    line_h = 14.0
    for li, line in enumerate(text.split("\n")):
        for st in f.strokes_for_text(line):
            strokes.append([(x, -y + li * line_h) for x, y in st])   # Hershey y is up
    return strokes


# Hershey is an ENGRAVING font set, and its "bold"/"serif" faces fake weight by retracing
# every stem: `rowmant` draws a capital H with 27 strokes, `timesr` uses 3.0x the strokes a
# person would. On paper that reads as a sketchy scribble, not handwriting -- which is
# exactly what the 2026-10-04 test sheet showed on the timesr and futuram lines.
#
# These are real single-line HANDWRITING faces (fonts/, SIL OFL, see fonts/NOTICE.md),
# measured at 0.8-1.2x a human's stroke count. The cursive ones also have small gaps
# between one letter's exit and the next letter's entry, so they can be WELDED into one
# continuous stroke per word -- which is what makes cursive cursive.
FONT_DIR = os.path.join(DATA_DIR, "fonts")


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
