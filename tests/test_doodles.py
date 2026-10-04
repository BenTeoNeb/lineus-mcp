import json
import urllib.request

import pytest

from lineus_mcp import config, doodles
from lineus_mcp.doodles import doodle_strokes, qd_bundle, qd_categories, qd_list, qd_web
from lineus_mcp.expr import ExprError
from lineus_mcp.scene import compile_scene


def test_bundled_set():
    b = qd_bundle()
    assert len(b) == 31 and sum(len(v) for v in b.values()) == 184
    assert len(qd_categories()) == 345


def test_a_doodle_compiles_into_its_box_deterministically():
    sc = {"shapes": [{"doodle": "cat", "pick": 0, "box": [10, 5, 30, 30]}]}
    st = compile_scene(sc)
    assert all(9.99 <= x <= 40.01 and 4.99 <= y <= 35.01 for s in st for x, y in s)
    assert compile_scene(sc) == st


def test_no_box_means_the_page_not_millimetres():
    st = compile_scene({"shapes": [{"doodle": "cat", "pick": 0}]})
    assert max(x for s in st for x, _ in s) <= config.CANVAS_W + 0.01


def test_strokes_keep_the_authors_order_and_smoothing_only_adds_points():
    raw = qd_bundle()["cat"][0]
    plain = compile_scene({"shapes": [{"doodle": "cat", "pick": 0, "smooth": False}]})
    smooth = doodle_strokes("cat", 0)
    assert len(plain) == len(raw) == len(smooth)
    assert sum(len(s) for s in smooth) > sum(len(s[0]) for s in raw)


@pytest.mark.parametrize("call, message", [
    (lambda: compile_scene({"shapes": [{"doodle": "cat", "pick": 99}]}), "pick must be"),
    (lambda: doodle_strokes("unicornz"), "no Quick, Draw! category"),
    (lambda: qd_web("../../etc/passwd"), "no Quick, Draw! category"),
    (lambda: qd_list("camel", "bundled"), "not in the bundled set"),
    (lambda: qd_list("cat", "sideways"), "auto, bundled or web"),
])
def test_errors_are_clear(call, message):
    with pytest.raises(ExprError, match=message):
        call()


class _Resp:
    def __init__(self, data): self.data = data
    def read(self, n=-1): return self.data[:n] if n >= 0 else self.data
    def __enter__(self): return self
    def __exit__(self, *a): pass


@pytest.fixture
def fake_net(monkeypatch, tmp_path):
    """Serve bundled drawings as if they were the dataset, and cache into tmp."""
    lines = [json.dumps({"recognized": True, "drawing": d})
             for v in qd_bundle().values() for d in v]
    payload = ("\n".join(lines) + "\n{\"cut off by the byte ra").encode()
    seen = {}

    def urlopen(req, timeout=0):
        seen["url"], seen["range"] = req.full_url, req.headers.get("Range")
        return _Resp(payload)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(doodles, "CACHE_DIR", str(tmp_path))
    doodles._qd_web.clear()
    yield seen
    doodles._qd_web.clear()


def test_web_fetch_is_a_byte_range_ranked_and_cached(fake_net, tmp_path):
    items, src = qd_list("camel", "auto")
    assert src == "web" and 0 < len(items) <= doodles.QD_KEEP
    assert fake_net["range"] == f"bytes=0-{doodles.QD_FETCH_BYTES - 1}"
    qd_list("hot air balloon", "web")
    assert fake_net["url"].endswith("hot%20air%20balloon.ndjson")
    assert any(p.name.startswith("camel.") for p in (tmp_path / "quickdraw").iterdir())


def test_a_cached_category_works_offline_and_identically(fake_net, monkeypatch):
    items, _ = qd_list("camel", "auto")

    def down(*a, **k): raise OSError("network is down")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    doodles._qd_web.clear()
    assert qd_list("camel", "auto")[0] == items
    with pytest.raises(ExprError, match="could not fetch"):
        qd_list("castle", "web")
