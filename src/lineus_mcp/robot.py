"""The TCP link to the arm and the background drawing jobs."""
from __future__ import annotations

import socket
import threading
import time
import uuid
from dataclasses import dataclass, field

from .checks import small_feature_check
from .config import (
    DEFAULT_SPEED,
    HOST,
    MAX_POINTS,
    MIN_SPEED,
    MOCK,
    PEN_DOWN_MAX,
    PEN_DOWN_Z,
    PEN_PLANE,
    PORT,
    SOCKET_TIMEOUT,
    TRAVEL_SPEED,
    TRAVEL_Z,
    Stroke,
)
from .geometry import order_human, order_strokes, simplify, travel_mm
from .machine import clearance_check, envelope_check, pen_down_z, to_machine


class Robot:
    def __init__(self):
        self.sock: socket.socket | None = None
        self.banner = ""
        self.lock = threading.Lock()

    def _connect(self):
        if MOCK:
            self.banner = 'hello VERSION:"mock" NAME:line-us SERIAL:0'; return
        # A single G01 blocks until the move finishes, and at G94 S1 a long travel
        # can take minutes. A short timeout kills the job mid-move AND wedges the
        # robot's command parser (recovery needs a power cycle). Be generous.
        self.sock = socket.create_connection((HOST, PORT), timeout=SOCKET_TIMEOUT)
        self.banner = self._read()

    def _read(self) -> str:
        buf = b""
        while not buf.endswith(b"\0"):
            c = self.sock.recv(1)
            if not c:
                raise ConnectionError("Line-us closed the connection")
            buf += c
        return buf.decode(errors="replace").strip("\r\n\0")

    def cmd(self, line: str) -> str:
        with self.lock:
            if MOCK:
                time.sleep(0.002); return "ok"
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    self.sock.sendall(line.encode() + b"\n")
                    return self._read()
                except (OSError, ConnectionError):
                    self.close()
                    if attempt == 2:
                        raise
        return "error"

    def hello(self) -> str:
        with self.lock:
            if self.sock is None and not MOCK:
                self._connect()
            elif MOCK:
                self._connect()
        return self.banner

    def close(self):
        if self.sock:
            try: self.sock.close()
            except OSError: pass
        self.sock = None


robot = Robot()


@dataclass
class Job:
    id: str
    strokes: list[Stroke]
    total: int
    speed: int = DEFAULT_SPEED
    pen_z: int = PEN_DOWN_Z
    travel: int = TRAVEL_SPEED
    done: int = 0
    state: str = "queued"          # queued|running|done|aborted|error
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    abort: threading.Event = field(default_factory=threading.Event)


jobs: dict[str, Job] = {}


job_lock = threading.Lock()   # one drawing at a time


def run_job(job: Job):
    with job_lock:
        job.state = "running"; job.started = time.time()
        try:
            robot.cmd(f"G94 S{job.speed}")        # pen-down step size
            robot.cmd(f"G94 P{job.travel}")       # pen-up step size
            for s in job.strokes:
                if job.abort.is_set(): break
                x, y = to_machine(*s[0]); robot.cmd(f"G01 X{x} Y{y} Z{TRAVEL_Z}")
                robot.cmd(f"G01 X{x} Y{y} Z{pen_down_z(*s[0])}")
                for p in s[1:]:
                    if job.abort.is_set(): break
                    x, y = to_machine(*p)
                    r = robot.cmd(f"G01 X{x} Y{y} Z{pen_down_z(*p)}")
                    if r.startswith("error"):
                        job.error = r
                    job.done += 1
                robot.cmd(f"G01 X{x} Y{y} Z{TRAVEL_Z}")
                job.done += 1
            robot.cmd("G28")
            job.state = "aborted" if job.abort.is_set() else "done"
        except Exception as e:  # noqa: BLE001
            job.state, job.error = "error", f"{type(e).__name__}: {e}"
            try: robot.cmd("G01 Z1000")
            except Exception: pass
        job.finished = time.time()


def _pen_z_report(strokes: list[Stroke]):
    """What pen-down Z this job will actually use: one number, or the range over the page."""
    if PEN_PLANE is None:
        return PEN_DOWN_Z
    zs = [pen_down_z(u, v) for st in strokes for u, v in st]
    lo, hi = min(zs), max(zs)
    return lo if lo == hi else {"min": lo, "max": hi,
                                "clamped_at_max": sum(z == PEN_DOWN_MAX for z in zs)}


def submit(strokes: list[Stroke], speed: int | None = None,
           order: str = "human") -> dict:
    speed = DEFAULT_SPEED if speed is None else max(MIN_SPEED, min(30, int(speed)))
    order = (order or "human").lower()
    if order not in ("fast", "human", "asis"):
        return {"error": f"order must be fast|human|asis, got {order!r}"}
    simple = [simplify(s) for s in strokes if len(s) >= 1]
    if order == "fast":
        strokes = order_strokes(simple)          # nearest-neighbour, may reverse strokes
    elif order == "human":
        strokes = order_human(simple)            # top row first, left to right, no reversal
    else:
        strokes = simple                         # exactly as supplied
    n = sum(len(s) for s in strokes)
    if n == 0:
        return {"error": "nothing to draw"}
    if n > MAX_POINTS:
        return {"error": f"{n} points exceeds limit {MAX_POINTS}; simplify the input"}
    job = Job(id=uuid.uuid4().hex[:8], strokes=strokes, total=n, speed=speed,
              pen_z=PEN_DOWN_Z, travel=TRAVEL_SPEED)
    jobs[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return {"job_id": job.id, "strokes": len(strokes), "points": n, "speed": speed,
            "order": order, "pen_up_travel_mm": round(travel_mm(strokes)),
            "pen_down_z": _pen_z_report(strokes), "travel_speed": TRAVEL_SPEED,
            "warnings": (envelope_check(strokes) + small_feature_check(strokes, speed)
                         + clearance_check(strokes)),
            "hint": "poll get_job(job_id) — drawing takes roughly 0.05–0.1 s per point"}
