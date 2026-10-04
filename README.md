# lineus-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent draw with a
[Line-us](https://www.line-us.com) robot drawing arm.

Line-us was a small Kickstarter pen plotter: a three-servo arm that speaks G-code over
TCP. The company is gone and the cloud service with it, but the hardware still works
perfectly on the local network. This server puts it behind MCP so Claude (or any MCP
client) can preview and draw.

```
you      ──▶  Claude  ──▶  lineus-mcp  ──▶  TCP 1337  ──▶  Line-us
                              │
                        preview PNG ◀── see it before it draws
```

## What it does

| Tool | Purpose |
|---|---|
| `get_status` | Firmware banner, diagnostics, page size, reachable envelope, last job |
| `preview_paths` / `preview_svg` / `preview_text` | Render a PNG of what *would* be drawn — no movement |
| `draw_paths` | Polylines in millimetres: `[[[u,v], [u,v], ...], ...]` |
| `draw_svg` | SVG line art fitted into a box (strokes only; fills are not hatched) |
| `draw_text` | Single-stroke [Hershey](https://en.wikipedia.org/wiki/Hershey_fonts) text |
| `get_job` / `abort` / `home` | Async job control |
| `draw_orientation_test` | Frame + an "F" to verify the canvas orientation |

Drawing is asynchronous: `draw_*` returns a `job_id`, and you poll `get_job`.
Only one drawing runs at a time.

## Coordinates

Millimetres, origin top-left of the page, `u` right, `v` down.

The **page** is 80 × 45 mm and is the default framing for text and SVG. It is *not* a
boundary — `draw_paths` may use negative coordinates and go anywhere the arm reaches.
The real limit is the measured **envelope**: a circular segment of outer radius
1850 units (~92 mm) cut off by a straight inner chord, roughly three times the page area.

Points outside the envelope are reported as warnings, never silently moved. This matters:
the firmware clamps out-of-reach targets *radially* toward the origin, which would distort
a drawing without any error.

## Install

`uv` must be installed, and the config needs its **absolute** path — a GUI client such as
Claude Desktop does not inherit your shell `PATH`, and a pyenv/asdf shim will not resolve.

```sh
command -v uv || echo "missing: brew install uv"
pyenv which uv 2>/dev/null   # if uv came from pyenv, use this path
```

**Claude Code**

```sh
claude mcp add lineus -e LINEUS_HOST=line-us.local -- \
  <abs-path-to-uv> run --script /path/to/lineus_mcp.py
```

**Claude Desktop** — edit `claude_desktop_config.json`, then restart:

```json
{
  "mcpServers": {
    "lineus": {
      "command": "<abs-path-to-uv>",
      "args": ["run", "--script", "/path/to/lineus_mcp.py"],
      "env": { "LINEUS_HOST": "line-us.local" }
    }
  }
}
```

Dependencies are declared inline ([PEP 723](https://peps.python.org/pep-0723/)), so
`uv run --script` fetches them on first run. No virtualenv to manage.

**Try it without a robot:** set `LINEUS_MOCK=1`. Everything works except the moving.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LINEUS_HOST` | `line-us.local` | Hostname or IP. Use the IP if mDNS is unreliable. |
| `LINEUS_MOCK` | unset | `1` to run without hardware |
| `LINEUS_SPEED` | `5` | Default `G94` speed, 2 (slow, sharp) … 30 (fast, rounded) |
| `LINEUS_MIN_SPEED` | `2` | Floor. **Speed 1 can wedge the firmware** — see below |
| `LINEUS_TIMEOUT` | `180` | Socket timeout in seconds |
| `LINEUS_SWAP` / `LINEUS_FLIP_U` / `LINEUS_FLIP_V` | `1`/`0`/`0` | Orientation fixes |
| `LINEUS_R_MAX` / `LINEUS_R_MIN` / `LINEUS_X_MIN` | `1850`/`650`/`650` | Envelope limits |

Run `draw_orientation_test` first. The "F" must read normally at the top-left; if it is
mirrored or rotated, adjust the `SWAP`/`FLIP` variables.

## Things we learned the hard way

All of this was measured on real hardware, and several of it corrects something we had
believed earlier. Where a conclusion was overturned, the overturned version is named too —
the wrong guesses are the useful part.

**Two separate faults, not one.** They look like a single "the arm does not settle"
problem, and we spent a long time treating them as one. They are not:

*Corner rounding is planner blending.* The firmware accepts the next move while still
executing the current one — exactly one command of lookahead — so it blends through
vertices instead of stopping. The radius is roughly **constant at 1–2 mm**, which makes it
invisible on a 20 mm shape and fatal on a 6 mm one.

*The pen-landing smear is geometric.* The lift axis is not vertical: the nib travels
**radially** with respect to the shoulder as it descends, laying ink from first contact
until Z bottoms out. So every stroke start and end gets a short radial drag, every one
pointing at the same place and growing with distance from it. Timing has nothing to do with
it — at constant radius, speed changes nothing, and the fix is a shallower pen-down `Z`
(the server uses 300, not 0), not a pause. Any amount of waiting is wasted effort here.

**Line art beats dot art, by a lot.** The same ladybug: 358 dots took 154 s and every mark
smeared. 17 polylines took 33 s and came out clean. For solid blacks, hatch at ~1.15 mm —
do not pack dots.

**Stroke order is a quality setting, not just cosmetics.** `order="human"` (the default)
draws row by row, letter by letter, in the letterform's own stroke order, never reversing a
stroke. It costs ~2.8× the pen-up travel but only ~4% wall-clock, because travel is not the
bottleneck — and the joins actually meet, because servo backlash stays correlated within a
glyph. `order="fast"` (nearest-neighbour) is available when throughput matters.

**Pen height is mechanical and fragile.** `Z0` (down) and `Z1000` (up) are hard limits with
no software headroom — a negative Z is rejected and snaps the pen fully *up*. Total lift
travel is only ~3 mm, so the paper surface has to sit inside that window. Re-check after
every pen change; `examples/penhold.py` parks the arm so you can set it against the real
contact point.

**Do not use speed 1.** A `G01` blocks until the move completes, and at `S1` a long travel
can exceed the socket timeout; the firmware's command parser then stops responding entirely
and only a power cycle recovers it. The server floors speed at 2 for this reason. Symptom:
`get_status` times out while `ping` and `nc -z line-us.local 1337` both succeed.

## Examples

Standalone generator scripts in [`examples/`](examples/) — each prints a stroke list you
can hand to `draw_paths`:

- `harmonograph.py` — damped Lissajous figure, one unbroken stroke. The machine's best case.
- `stipple.py` — tonal stippling by variable-radius Poisson sampling
- `ladybug_lines.py` — line art with hatched fills
- `envelope_art.py` — draws the robot's own reachable envelope, plus a spiral into its centre
- `penhold.py` — parks the arm for pen-height adjustment
- `corner_speed.py` — corner-sharpness test across the speed range

`lineus.py` is a dependency-free 45-line driver, useful for poking the robot directly:
`python3 lineus.py star`.

## Safety

- Always preview before drawing.
- Never expose port 1337 to the internet. There is no authentication.

## Licence

MIT — see [LICENSE](LICENSE).
