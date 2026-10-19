import math
import time

from lineus_core.config import PEN_DOWN_MAX, PEN_MARGIN, TRAVEL_Z
from lineus_core.machine import pen_down_z, to_machine
from lineus_jog import app
from lineus_jog.app import HOME_U, HOME_V, Link, clamp_move, lead_ok, reachable, scene_of, stick


class FakeRobot:
    def __init__(self):
        self.sent = []

    def cmd(self, line):
        self.sent.append(line); return "ok"


def g01(u, v, z):
    x, y = to_machine(u, v)
    return f"G01 X{x} Y{y} Z{z}"


def test_home_is_reachable():
    assert reachable(HOME_U, HOME_V)


def test_moves_inside_the_envelope_are_untouched():
    assert clamp_move(40, 20, 41, 21) == (41, 21)


def test_a_move_into_the_wall_slides_along_it_and_never_leaves():
    # Find the far edge straight out from the page, then push diagonally into it.
    v = 20.0
    while reachable(40, v + 0.5):
        v += 0.5
    u1, v1 = clamp_move(40, v, 45, v + 5)
    assert reachable(u1, v1)
    assert (u1, v1) != (40, v), "pushing diagonally into a wall should still move"


def test_a_far_unreachable_target_stops_at_the_edge():
    u, v = clamp_move(40, 20, 40, 500)
    assert reachable(u, v) and v > 40 and not reachable(u, v + 1)


def test_stick_has_a_deadzone_and_full_push_is_full_speed():
    assert stick(0.1, 0.05) == (0.0, 0.0)
    x, y = stick(1.0, 0.0)
    assert math.isclose(x, 1.0) and y == 0.0
    small, _ = stick(0.4, 0.0)
    assert 0 < small < 0.4 / 2, "the response is quadratic: half a push is well under half speed"


def test_cursor_cannot_run_away_from_the_arm_but_can_come_back():
    arm = (40, 20)
    assert lead_ok((40, 20), (44, 20), arm)
    assert not lead_ok((45, 20), (47, 20), arm)
    assert lead_ok((47, 20), (46, 20), arm)


def test_link_travels_then_touches_down_then_draws_then_lifts():
    r = FakeRobot()
    link = Link(r)
    link.set(30, 20, True); link._step()
    link.set(32, 20, True); link._step()
    link.set(32, 20, False); link._step()
    assert r.sent == [g01(30, 20, TRAVEL_Z), g01(30, 20, pen_down_z(30, 20)),
                      g01(32, 20, pen_down_z(32, 20)), g01(32, 20, TRAVEL_Z)]
    assert link.strokes == [[(30, 20), (32, 20)]]
    assert link._settled()


def test_trimming_the_depth_with_the_pen_down_presses_at_once():
    r = FakeRobot()
    link = Link(r)
    link.set(30, 20, True); link._step()
    z0 = pen_down_z(30, 20)
    link.trim(-50)
    assert not link._settled(), "a new depth must be sent even if the pen has not moved"
    link._step()
    assert r.sent[-1] == g01(30, 20, z0 - 50)
    assert link._settled()
    link.set(32, 20, True); link._step()
    assert r.sent[-1] == g01(32, 20, z0 - 50), "the trim sticks for the rest of the stroke"


def test_trimming_with_the_pen_up_sends_nothing_and_never_reaches_pen_up_z():
    link = Link(FakeRobot())
    link.trim(-50)
    assert link._settled()
    link.trim(+5000)
    assert link.z(30, 20) == PEN_DOWN_MAX
    link.trim(-5000)
    assert link.z(30, 20) == 0


def test_the_hint_names_the_setting_that_keeps_the_depth():
    link = Link(FakeRobot())
    link.trim(-25)
    assert link.keep_hint() in (f"LINEUS_PEN_DOWN_Z={link.z(*link.target[:2])}",
                                f"LINEUS_PEN_MARGIN={PEN_MARGIN + 25}")


def test_link_ignores_moves_below_the_robot_resolution():
    r = FakeRobot()
    link = Link(r)
    link.set(HOME_U + 0.05, HOME_V, False)
    assert link._settled()


def test_a_hand_drawing_saves_as_a_scene_and_drops_lone_dots():
    scene = scene_of([[(10, 10), (20, 15), (30, 10)], [(5, 5)]])
    assert scene["shapes"] == [{"paths": [[[10, 10], [20, 15], [30, 10]]]}]


class SessionRobot(FakeRobot):
    """Also counts connections and releases, like the real Robot."""
    def __init__(self):
        super().__init__(); self.hellos = self.closes = 0

    def hello(self):
        self.hellos += 1; return "hello"

    def close(self):
        self.closes += 1


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_an_idle_jog_lets_go_of_the_arm_and_takes_it_back_on_the_next_move(monkeypatch):
    monkeypatch.setattr(app, "IDLE_RELEASE", 0.1)
    r = SessionRobot()
    link = Link(r)
    link.start()
    try:
        assert wait_for(lambda: link.status == "ready")
        link.set(30, 20, False)
        assert wait_for(lambda: not link.connected), "idle with the pen up: release the arm"
        assert r.closes >= 1 and r.hellos == 1
        link.set(31, 20, False)
        assert wait_for(lambda: r.sent[-1] == g01(31, 20, TRAVEL_Z))
        assert r.hellos == 2 and r.sent.count("G94 S5") == 2, "reconnect resends the step sizes"
    finally:
        link.stop()


def test_a_jog_never_lets_go_with_the_pen_down(monkeypatch):
    monkeypatch.setattr(app, "IDLE_RELEASE", 0.05)
    link = Link(SessionRobot())
    link.start()
    try:
        link.set(30, 20, True)
        assert wait_for(lambda: link.arm[2])
        time.sleep(0.3)
        assert link.connected
    finally:
        link.stop()
