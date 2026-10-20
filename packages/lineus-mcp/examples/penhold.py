"""Park the arm over the paper and hold a given Z so the pen can be adjusted
against the real contact point.

Usage:
  python3 penhold.py down   -> X1250 Y0, Z0    (pen-down height: set the pen so it JUST marks)
  python3 penhold.py up     -> X1250 Y0, Z1000 (pen-up height: check the nib CLEARS the paper)
  python3 penhold.py home   -> release, G28

Holds position and exits; the arm stays put because nothing else commands it.
"""
import socket
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "down"
Z = {"down": 0, "up": 1000, "home": None}[MODE]

s = socket.create_connection(("line-us.local", 1337), timeout=15)


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
    print(f"  {line:22s} -> {r}")
    return r


read()
if MODE == "home":
    cmd("G01 Z1000")
    cmd("G28")
    print("\nreleased, arm home.")
else:
    cmd(f"G01 X1250 Y0 Z{Z}")
    where = "PEN DOWN — slide the pen down until it just marks the paper, plus a hair" \
        if MODE == "down" else "PEN UP — the nib must visibly CLEAR the paper"
    print(f"\nholding at X1250 Y0 Z{Z}\n  {where}")
s.close()
