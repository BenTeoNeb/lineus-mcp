import math

from conftest import example, ink, segment_counts
from lineus_mcp.planner import (
    _Welder,
    chain_strokes,
    dedupe_strokes,
    explode,
    plan_paths,
    to_one_line,
)
from lineus_mcp.scene import compile_scene

TRIS = [[(0, 0), (10, 0), (5, 8), (0, 0)], [(10, 0), (15, 8), (5, 8), (10, 0)]]


def test_explode_splits_only_at_junctions():
    # (15,8) belongs to one triangle, so it stays inside its piece
    assert len(explode(TRIS)) == 5
    curve = [[(x / 10, math.sin(x / 10)) for x in range(200)]]
    assert len(explode(curve, 0.3)) == 1, "a dense curve must survive whole"


def test_dedupe_drops_the_shared_edge_in_either_direction():
    pieces, dropped = dedupe_strokes(explode(TRIS), 0.3)
    assert dropped == 1 and len(pieces) == 4


def test_chain_four_edges_into_one_closed_trail():
    edges = [[(0, 0), (10, 0)], [(10, 0), (10, 10)], [(10, 10), (0, 10)], [(0, 10), (0, 0)]]
    trails = chain_strokes(edges, 0.3)
    assert len(trails) == 1 and len(trails[0]) == 5


def test_two_triangles_make_one_eulerian_trail():
    out, stats = plan_paths(TRIS, {"explode": True, "dedupe": True, "chain": True})
    assert len(out) == 1 and stats["duplicates_dropped"] == 1


def test_first_edge_stored_backwards_does_not_jump():
    """Regression: chain once oriented edges by distance, leaving the first edge of each
    trail in its stored direction -- stored backwards, the pen jumped and retraced."""
    rev = [[(10, 0), (0, 0)], [(10, 0), (10, 10)], [(0, 10), (10, 10)], [(0, 0), (0, 10)],
           [(0, 0), (10, 10)]]
    c = chain_strokes(rev, 0.3)
    assert abs(ink(c) - (40 + math.sqrt(200))) < 1e-6
    assert max(segment_counts(c).values()) == 1


def test_weld_is_a_tolerance_not_grid_rounding():
    w = _Welder(0.3)
    assert w.id((0.2999, 0)) == w.id((0.3001, 0)), "straddles a cell boundary, 0.0002 apart"


def test_fox_mesh_plans_to_exactly_its_unique_edge_length():
    """Regression: the planner once invented and dropped segments; the planned fox drew
    31 mm twice. Planned ink must equal the true unique edge length."""
    fox = example("geometric_fox")
    mesh = {"shapes": [fox["shapes"][0]], "fit": fox["fit"]}
    raw = compile_scene(mesh)
    planned = compile_scene(dict(mesh, join=fox["join"]))
    unique = {}
    for s in raw:
        for a, b in zip(s, s[1:]):
            ka, kb = (round(a[0] / .05), round(a[1] / .05)), (round(b[0] / .05), round(b[1] / .05))
            unique[(ka, kb) if ka <= kb else (kb, ka)] = math.dist(a, b)
    assert abs(ink(planned) - sum(unique.values())) < 0.5
    assert max(segment_counts(planned).values()) == 1


def test_one_line_bridges_tangentially():
    one = to_one_line([[(0, 0), (10, 0)], [(20, 5), (30, 5)], [(40, 0), (50, 0)]])
    assert len(one) == 1
    p = one[0]
    worst = min(((b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]))
                / ((math.hypot(b[0] - a[0], b[1] - a[1]) or 1)
                   * (math.hypot(c[0] - b[0], c[1] - b[1]) or 1))
                for a, b, c in zip(p, p[1:], p[2:]))
    assert worst > -0.3, "no hard reversal where a bridge joins"


def test_scene_fit_runs_before_join_so_the_weld_is_in_millimetres():
    grid = {"shapes": [{"paths": [[[0, 0], [100, 0], [50, 80], [0, 0]],
                                  [[100, 0], [150, 80], [50, 80], [100, 0]]]}],
            "fit": [10, 5, 60, 35], "join": {"explode": True, "dedupe": True}}
    assert len(compile_scene(grid)) == 1
