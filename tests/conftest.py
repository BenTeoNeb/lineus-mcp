"""Every test runs without hardware and without touching the user's cache or preview."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="lineus-test-")
os.environ["LINEUS_MOCK"] = "1"
os.environ["LINEUS_CACHE"] = _TMP
os.environ["LINEUS_PREVIEW"] = os.path.join(_TMP, "preview.png")
os.environ.pop("LINEUS_PEN_PLANE", None)        # the default build is the flat pen depth

import json  # noqa: E402

import pytest  # noqa: E402


def example(name: str) -> dict:
    from lineus_mcp.config import DATA_DIR
    with open(os.path.join(DATA_DIR, "examples", name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def ink(strokes) -> float:
    import math
    return sum(math.dist(a, b) for s in strokes for a, b in zip(s, s[1:]))


def segment_counts(strokes, q: float = 0.05) -> dict:
    """How many times each segment is drawn, keyed independently of direction."""
    out: dict = {}
    for s in strokes:
        for a, b in zip(s, s[1:]):
            ka = (round(a[0] / q), round(a[1] / q))
            kb = (round(b[0] / q), round(b[1] / q))
            if ka == kb:
                continue
            k = (ka, kb) if ka <= kb else (kb, ka)
            out[k] = out.get(k, 0) + 1
    return out


@pytest.fixture
def run():
    """Run a coroutine (the MCP API is async)."""
    import asyncio
    return asyncio.run
