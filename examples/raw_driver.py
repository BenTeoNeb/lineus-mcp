#!/usr/bin/env python3
"""Minimal Line-us driver: TCP 1337, waits for ok/error after each command."""
import math
import socket
import sys

HOST, PORT = "line-us.local", 1337
# Conservative safe box inside the non-rectangular drawing area (units ~20/mm)
XMIN, XMAX, YMIN, YMAX = 800, 1600, -600, 600

class LineUs:
    def __init__(self, host=HOST):
        self.s = socket.create_connection((host, PORT), timeout=30)
        print(self._read())                      # hello banner
    def _read(self):
        buf = b""
        while not buf.endswith(b"\0"):
            c = self.s.recv(1)
            if not c: break
            buf += c
        return buf.decode(errors="replace").strip("\r\n\0")
    def cmd(self, line):
        self.s.sendall(line.encode() + b"\n")
        r = self._read()
        if not r.startswith("ok"): print(f"!! {line} -> {r}")
        return r
    def move(self, x, y, z):
        x = max(XMIN, min(XMAX, round(x))); y = max(YMIN, min(YMAX, round(y)))
        return self.cmd(f"G01 X{x} Y{y} Z{z}")
    def polyline(self, pts):
        self.move(*pts[0], 1000); self.move(*pts[0], 0)
        for p in pts[1:]: self.move(*p, 0)
        self.move(*pts[-1], 1000)
    def close(self):
        self.cmd("G28"); self.s.close()

def star(cx=1200, cy=0, r=350, n=5):
    pts = [(cx + r*math.cos(math.pi/2 + i*4*math.pi/n), cy + r*math.sin(math.pi/2 + i*4*math.pi/n)) for i in range(n+1)]
    return [pts]

def spiral(cx=1200, cy=0, turns=6, rmax=380, steps=360):
    return [[(cx + rmax*t/steps*math.cos(turns*2*math.pi*t/steps), cy + rmax*t/steps*math.sin(turns*2*math.pi*t/steps)) for t in range(steps+1)]]

DEMOS = {"star": star, "spiral": spiral}

if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "star"
    bot = LineUs(sys.argv[2] if len(sys.argv) > 2 else HOST)
    for pl in DEMOS[what](): bot.polyline(pl)
    bot.close(); print("done")
