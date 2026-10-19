"""Drive the arm by hand: keyboard, mouse or gamepad, live, in a window.

    lineus-jog                 # pip install lineus-jog, or uv sync in this repo
    lineus-jog --mock          # no robot, just the window

Each G01 blocks until the arm has finished the move, so the arm cannot keep up with a
60 Hz input loop. A background thread sends only the NEWEST target whenever the arm is
free; inputs that arrive while it is busy are dropped, not queued. That keeps the lag to
one move instead of letting it grow, and a curve becomes short chords between samples.

The arm takes one connection at a time, so after a few idle seconds with the pen up this
lets go of it, and lineus-mcp can draw while the window stays open.

Positions are canvas mm, like everything else here. Moves that leave the measured
envelope slide along its edge instead of being sent: the firmware would clamp them AND
lift the pen.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading
import time

from lineus_core.config import (
    DEFAULT_SPEED,
    HOST,
    MIN_SEG_MM,
    MIN_SPEED,
    PEN_DOWN_MAX,
    PEN_MARGIN,
    PEN_PLANE,
    PORT,
    TRAVEL_SPEED,
    TRAVEL_Z,
)
from lineus_core.machine import (
    envelope_bounds_mm,
    in_envelope,
    machine_to_canvas,
    pen_down_z,
    to_machine,
)
from lineus_core.robot import Robot

HOME_U, HOME_V = machine_to_canvas(1000, 1000)    # where G28 parks the arm
SPEEDS = (5.0, 10.0, 20.0, 40.0)                  # stick / key speed levels, mm/s
FINE = 0.2                                        # right stick and Shift: fraction of speed
LEAD_MM = 6.0                                     # how far the cursor may run ahead of the arm
IDLE_RELEASE = 5.0                                # s idle, pen up, before letting go of the arm
DEADZONE = 0.15
Z_STEP = 25                                       # pen-depth trim per press, machine Z units


def reachable(u: float, v: float) -> bool:
    return in_envelope(*to_machine(u, v))


def clamp_move(u0: float, v0: float, u1: float, v1: float) -> tuple[float, float]:
    """Where a move from (u0,v0) towards (u1,v1) may actually go.

    Into a wall, slide along it (the axis that still fits), so a stick pushed diagonally
    into the edge keeps moving. Failing that, stop at the last reachable point on the way.
    """
    if reachable(u1, v1):
        return u1, v1
    for u, v in ((u1, v0), (u0, v1)):
        if (u, v) != (u0, v0) and reachable(u, v):
            return u, v
    if not reachable(u0, v0):
        return u0, v0
    lo, hi = 0.0, 1.0
    for _ in range(20):
        mid = (lo + hi) / 2
        if reachable(u0 + (u1 - u0) * mid, v0 + (v1 - v0) * mid):
            lo = mid
        else:
            hi = mid
    return u0 + (u1 - u0) * lo, v0 + (v1 - v0) * lo


def stick(x: float, y: float, dead: float = DEADZONE) -> tuple[float, float]:
    """Stick deflection -> velocity fraction: a radial deadzone, then a quadratic response
    so small deflections are fine and a full push is fast."""
    m = math.hypot(x, y)
    if m < dead:
        return 0.0, 0.0
    k = ((min(m, 1.0) - dead) / (1.0 - dead)) ** 2 / m
    return x * k, y * k


def lead_ok(cur: tuple[float, float], new: tuple[float, float],
            arm: tuple[float, float], lead: float = LEAD_MM) -> bool:
    """May the cursor move to `new`? Never further than `lead` ahead of the arm, but a move
    back towards the arm is always allowed."""
    d = math.dist(new, arm)
    return d <= lead or d <= math.dist(cur, arm)


def scene_of(strokes: list[list[tuple[float, float]]]) -> dict:
    """A drawing made by hand, as a scene that preview_scene / draw_scene accept."""
    paths = [[[round(u, 2), round(v, 2)] for u, v in s] for s in strokes if len(s) >= 2]
    return {"_comment": "drawn by hand with lineus-jog", "shapes": [{"paths": paths}]}


class JogRobot(Robot):
    """Robot with its own host and a SHORT timeout: jog moves are a few mm, and a client
    that hangs for minutes because another one holds the arm is no use at a keyboard."""

    def __init__(self, host: str = HOST, timeout: float = 10.0, mock: bool = False):
        super().__init__()
        self.host, self.timeout, self.mock = host, timeout, mock

    def _connect(self):
        if self.mock:
            self.banner = 'hello VERSION:"mock" NAME:line-us SERIAL:0'; return
        self.sock = socket.create_connection((self.host, PORT), timeout=self.timeout)
        self.banner = self._read()

    def cmd(self, line: str) -> str:
        if self.mock:
            time.sleep(0.03)            # about one short move, so the lag is visible
            return "ok"
        return super().cmd(line)


class Link:
    """The background thread that owns the arm. The UI calls set(); this sends the latest
    target when the arm is free and records every pen-down move as the drawing."""

    def __init__(self, robot: Robot, speed: int = DEFAULT_SPEED, travel: int = TRAVEL_SPEED):
        self.robot, self.speed, self.travel = robot, speed, travel
        self.cv = threading.Condition()
        self.target = (HOME_U, HOME_V, False)
        self.arm = (HOME_U, HOME_V, False)        # last position the arm acknowledged
        self.arm_z = TRAVEL_Z
        self.dz = 0                               # live pen-depth trim, machine Z units
        self.strokes: list[list[tuple[float, float]]] = []
        self.status = "connecting"
        self.connected = False
        self.last_active = time.time()
        self.want_home = False
        self.stopping = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def set(self, u: float, v: float, down: bool):
        with self.cv:
            if (u, v, down) != self.target:
                self.target = (u, v, down)
                self.cv.notify()

    def home(self):
        with self.cv:
            self.want_home = True
            self.target = (HOME_U, HOME_V, False)
            self.cv.notify()

    def clear(self):
        with self.cv:
            self.strokes = [self.strokes[-1][-1:]] if self.arm[2] and self.strokes else []

    def z(self, u: float, v: float) -> int:
        """Pen-down Z here, with the live trim. Lower presses harder; 500 and up is pen-up."""
        return max(0, min(PEN_DOWN_MAX, pen_down_z(u, v) + self.dz))

    def trim(self, d: int):
        """Press harder (d < 0) or lighter (d > 0). Applied at once if the pen is down."""
        with self.cv:
            self.dz = max(-PEN_DOWN_MAX, min(PEN_DOWN_MAX, self.dz + d))
            self.cv.notify()

    def keep_hint(self) -> str:
        """The setting that makes the current depth permanent."""
        if PEN_PLANE is None:
            return f"LINEUS_PEN_DOWN_Z={self.z(*self.target[:2])}"
        return f"LINEUS_PEN_MARGIN={PEN_MARGIN - self.dz}"

    def stop(self):
        with self.cv:
            self.stopping = True
            self.cv.notify()
        self.thread.join(timeout=15)

    def _send(self, u: float, v: float, z: int) -> str:
        x, y = to_machine(u, v)
        r = self.robot.cmd(f"G01 X{x} Y{y} Z{z}")
        self.arm_z = z
        if r.startswith("error"):
            self.status = r
        return r

    def _step(self):
        """Bring the arm one command closer to the target."""
        u, v, down = self.target
        au, av, adown = self.arm
        moved = math.dist((u, v), (au, av)) >= MIN_SEG_MM
        if adown and not down:                       # lift where we are
            self._send(au, av, TRAVEL_Z)
            self.arm = (au, av, False)
        elif down and not adown:                     # travel there, then touch down
            if moved:
                self._send(u, v, TRAVEL_Z)
            self._send(u, v, self.z(u, v))
            self.arm = (u, v, True)
            self.strokes.append([(u, v)])
        elif moved:
            self._send(u, v, self.z(u, v) if down else TRAVEL_Z)
            self.arm = (u, v, down)
            if down:
                self.strokes[-1].append((u, v))
        elif down and self.arm_z != self.z(au, av):  # depth trimmed with the pen down
            self._send(au, av, self.z(au, av))

    def _settled(self) -> bool:
        u, v, down = self.target
        au, av, adown = self.arm
        return (down == adown and math.dist((u, v), (au, av)) < MIN_SEG_MM
                and not (down and self.arm_z != self.z(au, av)))

    def _ready(self) -> bool:
        return self.stopping or self.want_home or not self._settled()

    def _connect(self):
        self.status = "connecting"
        self.robot.hello()
        self.robot.cmd(f"G94 S{self.speed}")
        self.robot.cmd(f"G94 P{self.travel}")
        self.connected, self.status = True, "ready"

    def _release(self):
        """Let go of the arm so lineus-mcp (or anything else) can draw; cmd() reconnects."""
        self.robot.close()
        self.connected, self.status = False, "idle: arm free for other clients"

    def _run(self):
        try:
            self._connect()                          # say early whether the arm is there
        except Exception as e:  # noqa: BLE001
            self._release()
            self.status = f"no link: {type(e).__name__}: {e}"
        while True:
            with self.cv:
                while not self._ready():
                    if (self.connected and not self.arm[2]
                            and time.time() - self.last_active >= IDLE_RELEASE):
                        self._release()
                    self.cv.wait(timeout=min(1.0, IDLE_RELEASE))
                if self.stopping:
                    break
                home, self.want_home = self.want_home, False
            try:
                if not self.connected:
                    self._connect()
                if home:
                    self.robot.cmd("G28")
                    self.arm = (HOME_U, HOME_V, False)
                else:
                    self._step()
                self.last_active = time.time()
            except Exception as e:  # noqa: BLE001  -- show it, then retry on the next input
                self._release()
                self.status = f"no link: {type(e).__name__}: {e}"
                with self.cv:
                    self.cv.wait(timeout=2.0)
        if self.arm[2]:                              # never leave the pen on the paper
            try:
                au, av, _ = self.arm
                self._send(au, av, TRAVEL_Z)
            except Exception:  # noqa: BLE001
                pass
        self.robot.close()


HUD = 84                                          # status bar height, px
HELP = ("stick/WASD/arrows move  ·  right stick/Shift fine  ·  RT hold, A or Space: pen  ·  "
        "B lift  ·  LB/RB or [ ]: speed",
        "D-pad down/up or - =: press harder/lighter  ·  Start/H home  ·  Back/C clear  ·  "
        "Y/Enter save  ·  mouse: drag draws, right-click travels  ·  Esc quit")


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="lineus-jog", description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=HOST, help="Line-us hostname or IP")
    ap.add_argument("--mock", action="store_true", help="no robot: just the window")
    ap.add_argument("--speed", type=int, default=DEFAULT_SPEED,
                    help="G94 S, pen-down step size (2 slow and sharp .. 30 fast)")
    ap.add_argument("--debug-input", action="store_true", help="print every input event")
    args = ap.parse_args(argv)

    try:
        import pygame
        from pygame._sdl2 import controller
    except ImportError:
        sys.exit("lineus-jog needs pygame-ce: pip install pygame-ce")

    pygame.init()
    controller.init()
    umin, vmin, umax, vmax = envelope_bounds_mm()
    umin, vmin, umax, vmax = umin - 3, vmin - 3, umax + 3, vmax + 3
    scale = min(1200 / (umax - umin), 680 / (vmax - vmin))
    W, H = int((umax - umin) * scale), int((vmax - vmin) * scale) + HUD
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("lineus-jog")
    font = pygame.font.SysFont(None, 20)
    to_px = lambda u, v: (int((u - umin) * scale), int((v - vmin) * scale))
    to_mm = lambda x, y: (x / scale + umin, y / scale + vmin)

    # The envelope, painted once at 1 mm resolution.
    bg = pygame.Surface((W, H))
    bg.fill((246, 246, 243))
    for i in range(int(umin), int(umax) + 1):
        for j in range(int(vmin), int(vmax) + 1):
            if reachable(i + 0.5, j + 0.5):
                x, y = to_px(i, j)
                bg.fill((226, 238, 226), (x, y, math.ceil(scale), math.ceil(scale)))
    from lineus_core.config import CANVAS_H, CANVAS_W
    x0, y0 = to_px(0, 0); x1, y1 = to_px(CANVAS_W, CANVAS_H)
    pygame.draw.rect(bg, (170, 170, 170), (x0, y0, x1 - x0, y1 - y0), 1)

    link = Link(JogRobot(args.host, mock=args.mock), speed=max(MIN_SPEED, min(30, args.speed)))
    link.start()

    pads: dict[int, object] = {}
    def add_pads():
        for i in range(controller.get_count()):
            if controller.is_controller(i) and i not in pads:
                pads[i] = controller.Controller(i)
    add_pads()

    cu, cv = HOME_U, HOME_V
    pen_toggle = False
    level = 2
    note, note_until = "", 0.0
    mouse_down = False
    clock = pygame.time.Clock()

    def say(msg):
        nonlocal note, note_until
        note, note_until = msg, time.time() + 3
        print(msg)

    def save():
        name = time.strftime("jog-%Y%m%d-%H%M%S.json")
        with open(name, "w") as f:
            json.dump(scene_of(link.strokes), f, indent=1)
        say(f"saved {name}  (preview_scene / draw_scene can replay it)")

    def home():
        nonlocal cu, cv, pen_toggle
        cu, cv, pen_toggle = HOME_U, HOME_V, False
        link.home()

    def speed(d):
        nonlocal level
        level = max(0, min(len(SPEEDS) - 1, level + d))
        say(f"speed {SPEEDS[level]:g} mm/s")

    def trim(d):
        link.trim(d)
        say(f"pen Z {link.z(cu, cv)} ({'harder' if d < 0 else 'lighter'})  ·  "
            f"to keep it: {link.keep_hint()}")

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        for e in pygame.event.get():
            if args.debug_input and e.type not in (pygame.MOUSEMOTION, pygame.ACTIVEEVENT):
                print(pygame.event.event_name(e.type), e.dict)
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.CONTROLLERDEVICEADDED:
                add_pads()
                say("gamepad connected")
            elif e.type == pygame.CONTROLLERDEVICEREMOVED:
                pads.clear(); add_pads()
                say("gamepad disconnected")
            elif e.type == pygame.KEYDOWN:
                k = e.key
                if k in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif k == pygame.K_SPACE:
                    pen_toggle = not pen_toggle
                elif k == pygame.K_h:
                    home()
                elif k == pygame.K_c:
                    link.clear()
                elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    save()
                elif k == pygame.K_LEFTBRACKET:
                    speed(-1)
                elif k == pygame.K_RIGHTBRACKET:
                    speed(+1)
                elif k in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    trim(-Z_STEP)
                elif k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                    trim(+Z_STEP)
            elif e.type == pygame.CONTROLLERBUTTONDOWN:
                b = e.button
                if b == pygame.CONTROLLER_BUTTON_A:
                    pen_toggle = not pen_toggle
                elif b == pygame.CONTROLLER_BUTTON_B:
                    pen_toggle = False
                elif b == pygame.CONTROLLER_BUTTON_LEFTSHOULDER:
                    speed(-1)
                elif b == pygame.CONTROLLER_BUTTON_RIGHTSHOULDER:
                    speed(+1)
                elif b == pygame.CONTROLLER_BUTTON_DPAD_DOWN:
                    trim(-Z_STEP)
                elif b == pygame.CONTROLLER_BUTTON_DPAD_UP:
                    trim(+Z_STEP)
                elif b == pygame.CONTROLLER_BUTTON_START:
                    home()
                elif b == pygame.CONTROLLER_BUTTON_BACK:
                    link.clear()
                elif b == pygame.CONTROLLER_BUTTON_Y:
                    save()
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3) and e.pos[1] < H - HUD:
                cu, cv = clamp_move(cu, cv, *to_mm(*e.pos))
                mouse_down = e.button == 1
            elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                mouse_down = False
            elif e.type == pygame.MOUSEMOTION and mouse_down:
                cu, cv = clamp_move(cu, cv, *to_mm(*e.pos))

        # Continuous inputs: velocity from sticks and held keys.
        vel_u = vel_v = 0.0
        trigger = False
        for p in pads.values():
            lx, ly = stick(p.get_axis(pygame.CONTROLLER_AXIS_LEFTX) / 32767,
                           p.get_axis(pygame.CONTROLLER_AXIS_LEFTY) / 32767)
            rx, ry = stick(p.get_axis(pygame.CONTROLLER_AXIS_RIGHTX) / 32767,
                           p.get_axis(pygame.CONTROLLER_AXIS_RIGHTY) / 32767)
            vel_u += lx + rx * FINE
            vel_v += ly + ry * FINE
            trigger |= p.get_axis(pygame.CONTROLLER_AXIS_TRIGGERRIGHT) > 16000
        keys = pygame.key.get_pressed()
        ku = (keys[pygame.K_RIGHT] or keys[pygame.K_d]) - (keys[pygame.K_LEFT] or keys[pygame.K_a])
        kv = (keys[pygame.K_DOWN] or keys[pygame.K_s]) - (keys[pygame.K_UP] or keys[pygame.K_w])
        if ku or kv:
            f = FINE if (keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]) else 1.0
            n = math.hypot(ku, kv)
            vel_u += ku / n * f
            vel_v += kv / n * f
        if vel_u or vel_v:
            step = SPEEDS[level] * dt
            new = clamp_move(cu, cv, cu + vel_u * step, cv + vel_v * step)
            if lead_ok((cu, cv), new, link.arm[:2]):
                cu, cv = new

        down = pen_toggle or trigger or mouse_down
        link.set(cu, cv, down)

        # Draw.
        screen.blit(bg, (0, 0))
        for s in [list(s) for s in link.strokes]:
            if len(s) >= 2:
                pygame.draw.lines(screen, (20, 20, 20), False, [to_px(*p) for p in s], 2)
        au, av, adown = link.arm
        pygame.draw.line(screen, (200, 200, 200), to_px(au, av), to_px(cu, cv), 1)
        pygame.draw.circle(screen, (20, 20, 20), to_px(au, av), 3)
        pygame.draw.circle(screen, (210, 40, 40) if down else (40, 90, 210), to_px(cu, cv),
                           6, 0 if down else 2)
        pygame.draw.rect(screen, (255, 255, 255), (0, H - HUD, W, HUD))
        line1 = (f"{link.status}  ·  pen {'DOWN' if adown else 'up'} Z{link.z(cu, cv)}  ·  "
                 f"u {au:5.1f} v {av:5.1f} mm  ·  speed {SPEEDS[level]:g} mm/s  ·  "
                 f"gamepad: {', '.join(p.name for p in pads.values()) or 'none'}")
        if note and time.time() < note_until:
            line1 = note
        screen.blit(font.render(line1, True, (30, 30, 30)), (10, H - HUD + 8))
        for i, h in enumerate(HELP):
            screen.blit(font.render(h, True, (110, 110, 110)), (10, H - HUD + 34 + 22 * i))
        pygame.display.flip()

    link.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
