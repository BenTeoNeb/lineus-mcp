# lineus-core

The [Line-us](https://www.line-us.com) robot arm as measured on real hardware: what it
can reach, how the pen behaves, how canvas millimetres map onto its coordinates, and the
TCP link that drives it. No third-party dependencies.

It is shared by the two ways of driving the arm in this repository:
[lineus-mcp](../lineus-mcp), for an AI agent, and [lineus-jog](../lineus-jog), for a
person at a keyboard or gamepad. Calibration and envelope fixes land here once and both
pick them up.

```
config.py     every setting of the arm, read from LINEUS_* environment variables
machine.py    the envelope, the pen model, and a simulation of what the pen really draws
geometry.py   ordering, fitting, splines, hatching
checks.py     drawing checks that report where a drawing will fail
robot.py      the TCP link and background drawing jobs
```

The settings are documented with lineus-mcp, in its
[Configuration](../lineus-mcp/README.md#configuration) table. The arm's own ones (host,
speeds, pen depth, envelope, orientation) are read here, so they apply to lineus-jog too.
