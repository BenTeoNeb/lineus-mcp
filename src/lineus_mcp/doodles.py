"""Doodles from Google's Quick, Draw! dataset: bundled, or fetched and cached."""
from __future__ import annotations

import io
import json
import math
import os

from .config import CACHE_DIR, DATA_DIR, Stroke
from .expr import ExprError
from .geometry import catmull_rom

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
DOODLE_DIR = os.path.join(DATA_DIR, "doodles")


QD_BUCKET = "https://storage.googleapis.com/quickdraw_dataset/full/simplified/"


QD_FETCH_BYTES = int(os.environ.get("LINEUS_QD_BYTES", "600000"))   # ~500 candidates


QD_KEEP = 48                         # ranked candidates kept per category in the cache


QD_RANK_VERSION = "v2"


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
    from PIL import Image as PILImage
    from PIL import ImageDraw
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
