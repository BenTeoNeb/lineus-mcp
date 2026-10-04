import math

from lineus_mcp import config, machine
from lineus_mcp.machine import (
    _blend,
    _radial,
    clearance_check,
    in_envelope,
    pen_down_z,
    simulate_strokes,
    to_machine,
)


def test_page_maps_onto_the_measured_envelope():
    assert to_machine(0, 0) == (700, -800)
    assert all(in_envelope(*to_machine(u, v)) for u, v in ((0, 0), (80, 0), (0, 45), (80, 45)))


def test_the_published_drawing_area_is_not_all_reachable():
    """The official 'app drawing area' corner (1775, 1000) is at radius 2037."""
    assert math.hypot(1775, 1000) > 1950 and not in_envelope(1775, 1000)


def test_simulation_pins_endpoints_and_rounds_corners():
    s = [(20, 20), (40, 20), (40, 30)]
    b = _blend(s)
    assert math.dist(b[0], s[0]) < 1e-9 and math.dist(b[-1], s[-1]) < 1e-9
    assert min(math.dist(p, (40, 20)) for p in b) > 0.2


def test_stroke_end_tick_is_radial_and_grows_with_reach():
    du, dv, r = _radial(40, 20)
    assert abs(du * du + dv * dv - 1) < 1e-6 and r > 0
    near = math.dist(simulate_strokes([[(40, 5), (45, 5)]])[0][0], (40, 5))
    far = math.dist(simulate_strokes([[(40, 40), (45, 40)]])[0][0], (40, 40))
    assert far > near
    assert simulate_strokes([s := [(1, 1), (9, 9)]]) == simulate_strokes([s])


def test_pen_depth_is_flat_by_default():
    assert machine.PEN_PLANE is None
    assert {pen_down_z(u, v) for u, v in ((0, 0), (80, 45))} == {config.PEN_DOWN_Z}
    assert clearance_check([[(0, 45)]]) == []


def test_measured_plane_clamps_where_contact_is_high(monkeypatch):
    monkeypatch.setattr(machine, "PEN_PLANE", (-4.330, 7.768, 575.9))
    assert pen_down_z(80, 0) < config.PEN_DOWN_MAX
    assert pen_down_z(0, 45) == config.PEN_DOWN_MAX
    assert clearance_check([[(0, 45)]]) and not clearance_check([[(80, 0)]])


def test_a_malformed_plane_is_ignored():
    assert config._parse_plane("nonsense") is None and config._parse_plane("") is None
