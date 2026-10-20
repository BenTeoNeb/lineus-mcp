"""Does G94 speed (or repeating the corner point) sharpen corners?

Row A: five 120-unit squares, one per speed  S1 S5 S10 S20 S30
Row B: same five speeds, but each corner point sent TWICE (forces the planner
       to arrive at the vertex before leaving it, since G4 dwell is unsupported)

Machine coords. Pen down per square, up between. Hard caps X 600-2100, Y -1100..1100.
"""
import socket
import time

SPEEDS = [1, 5, 10, 20, 30]
SIDE = 120                      # 6 mm
ROW_A_X, ROW_B_X = 1000, 1250   # near edge of each square
Y0, DY = -600, 300

s = socket.create_connection(("line-us.local", 1337), timeout=30)


def read():
    b = b""
    while not b.endswith(b"\0"):
        c = s.recv(1)
        if not c:
            raise ConnectionError("closed")
        b += c
    return b.decode(errors="replace").strip("\r\n\0")


def cmd(line):
    s.sendall(line.encode() + b"\n")
    r = read()
    if r.startswith("error"):
        print(f"    !! {line} -> {r}")
    return r


def square(x0, ycen, speed, doubled):
    y0, y1 = ycen - SIDE // 2, ycen + SIDE // 2
    x1 = x0 + SIDE
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    if doubled:
        pts = [p for p in pts for _ in (0, 1)]
    for x, y in pts:
        assert 600 <= x <= 2100 and -1100 <= y <= 1100, f"refused {x},{y}"
    cmd(f"G94 S{speed}")
    cmd(f"G01 X{pts[0][0]} Y{pts[0][1]} Z1000")
    cmd(f"G01 X{pts[0][0]} Y{pts[0][1]} Z0")
    for x, y in pts:
        cmd(f"G01 X{x} Y{y} Z0")
    cmd("G01 Z1000")


read()
for label, rowx, doubled in (("A single-point corners", ROW_A_X, False),
                             ("B doubled corners", ROW_B_X, True)):
    print(f"\nrow {label}  (X{rowx}..{rowx+SIDE})")
    for i, sp in enumerate(SPEEDS):
        y = Y0 + i * DY
        t = time.time()
        square(rowx, y, sp, doubled)
        print(f"  S{sp:<3d} at Y{y:<6d} drew in {time.time()-t:5.2f}s")

cmd("G94 S10")
cmd("G28")
s.close()
print("\ndone — compare corner sharpness across each row, and row A vs row B.")
