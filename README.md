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
| `preview_scene` / `draw_scene` | **The main interface.** A declarative scene, compiled to strokes server-side |
| `plan_scene` | Stroke/point counts, bounding box, travel and warnings — without drawing or rendering |
| `list_fonts` | The handwriting faces, with the measurements that say which will survive |
| `get_example` | Finished drawings to start from instead of a blank page (also as `lineus://examples` resources) |
| `preview_paths` / `preview_svg` / `preview_text` | Render a PNG of what *would* be drawn — no movement |
| `draw_paths` | Polylines in millimetres: `[[[u,v], [u,v], ...], ...]` — the escape hatch |
| `draw_svg` | SVG line art fitted into a box (strokes only; fills are not hatched) |
| `draw_text` | A single line or block of single-stroke text |
| `get_status` | Firmware banner, diagnostics, page size, reachable envelope, last job |
| `get_job` / `abort` / `home` | Async job control |
| `draw_orientation_test` | Frame + an "F" to verify the canvas orientation |

Drawing is asynchronous: `draw_*` returns a `job_id`, and you poll `get_job`.
Only one drawing runs at a time.

Every `preview_*` returns two pictures: a **diagnostic** view (envelope, page, pen-up travel)
and a **simulated** one showing what the arm will actually put on paper. The simulation
applies the faults measured on this hardware: corners blended through by the firmware's
one-command lookahead, and a short tick at both ends of every stroke, lying along the line
to the shoulder because the pen-lift axis isn't vertical, and longer the further the arm
reaches. The simulated image is also written to **`_preview.png`** next to the server,
so you can watch iterations in any viewer that reloads on change. Pass `simulate=false`
for the clean ink render instead.

Judge drawings by the simulation, not the clean render. Every drawing in this project
that disappointed on paper had looked fine as a clean render.

### Drawing checks

`preview_scene`, `draw_scene` and `plan_scene` also run checks that report **where** to fix
something, in page millimetres. Each check exists because the mistake was actually made:

| check | catches |
|---|---|
| same line twice | a mesh drawn without `join`, so every shared edge doubles |
| lines merging | two lines running within 1.1 mm without meeting — parallels that blot, sliver triangles that fill in, an eye too narrow to stay open |
| glued one-line | `one_line` bridging several separate pieces, which reads as glue |

They're measured as *runs* along the line, so a crossing doesn't count. Two lines that cross
come together only briefly; that's the difference from either fault. Fills are exempt,
since blotting is the point there.

What no check can catch is bad drawing: proportion, silhouette, character. That knowledge
is in the server's instructions, which every agent reads on connecting, and in the examples.

## Scenes

Sending coordinates is expensive and fragile — a page of generated curves is tens of
thousands of characters, and preview and draw are two separate pastes that can silently
disagree. A **scene** is a description that the server compiles:

```json
{"shapes": [
  {"text": "Slow is smooth", "font": "neutral", "align": "center", "box": [4, 2, 72, 10]},
  {"param": {"t": [0, 47, 900],
             "x": "40+33*exp(-0.028*t)*sin(2.01*t)",
             "y": "30+10*exp(-0.028*t)*sin(3.02*t+1.2)"}},
  {"param": {"t": [0, 6.2832, 160], "x": "55+8*cos(t)", "y": "11+8*sin(t)", "closed": true},
   "fill": {"hatch": 45, "spacing": 1.15}}
]}
```

That harmonograph is 125 characters where its 2,000 points would be 27,542 — about 220×.

**There is deliberately no library of shapes.** A fixed vocabulary of circle/rect/arc would
handle the dull cases and send everything interesting straight back to pasting
coordinates, which is the problem scenes exist to solve. Curves are **expressions**, so a
circle, a spiral, a rose and a harmonograph are the same producer with different formulae.
Expressions are evaluated by a whitelisted AST walk — no imports, no attribute access, no
`exec` — so the open-endedness costs no safety.

| producer | |
|---|---|
| `param` | `{"t": [start, stop, steps], "x": expr, "y": expr, "closed": bool}` |
| `text` | a string, or a list of `{"s", "font"}` for multi-font blocks |
| `path` / `paths` | raw points, the escape hatch |
| `svg` | imported line art |

| modifier | |
|---|---|
| `smooth` | `true`, or samples per span — centripetal Catmull-Rom through the control points |
| `closed` | close the path before smoothing |
| `fill` | `{"hatch": deg, "spacing": mm, "cross": bool}` — how you get solid black here |
| `transform` | `translate`, `rotate`, `scale`, `about` |
| `box` / `fit` | scale this shape into `[x, y, w, h]` |
| `repeat` | N, exposing `i` and `n` to the expressions |

`smooth` is what makes hand-drawn figures affordable: a cat is ~30 control points instead
of ~400 sampled ones. It is *centripetal* Catmull-Rom specifically — the uniform
parameterisation puts cusps and little self-intersecting loops wherever control points
bunch up, which is exactly where a drawn figure has them.

### Planning the strokes

A scene-level `"join"` block rewrites the whole pile of strokes before drawing:

```json
{"join": {"explode": true, "dedupe": true, "chain": true, "one_line": false, "weld": 0.3}}
```

| | |
|---|---|
| `explode` | break polylines into segments first, so shared edges become visible |
| `dedupe` | drop segments already drawn — **on by default** |
| `chain` | join strokes that meet into the fewest continuous trails |
| `one_line` | bridge every trail into a single unbroken stroke |
| `weld` | how close two ends must be to count as touching, in mm |

This matters more than it sounds. A triangle mesh supplied as triangles **redraws every
interior edge twice**: on the bundled fox, 1,075 mm of ink against 609 mm planned — 43%
of the drawing — and the repeat lands slightly off the original so the edge reads as
doubled. Planned, it also drops from 48 strokes to 8 — and every pen lift costs a landing smear, so fewer lifts is
a quality setting here, not just a faster one.

`chain` is Hierholzer with odd-vertex pairing, not greedy extension. Greedy looks fine and
is not: it strands edges and leaves extra trails. A component with *k* odd-degree vertices
needs exactly `max(1, k/2)` trails, so the odd vertices are paired with dummy edges, the
Eulerian circuit is found, and the circuit is cut back open at the dummies. Pairing nearest
first also minimises the travel between the trails that result.

`one_line` bridges with **tangent-continuous** cubic Hermite hops — the line leaves and
rejoins along the direction it was already travelling, because a straight connector reads
as a mistake. The firmware's 1–2 mm corner blending then smooths any residual kink, which
is the one place that fault helps us.

Variables are `t`, plus `i` and `n` inside a `repeat`. Functions: `sin cos tan asin acos
atan atan2 sinh cosh tanh exp log sqrt hypot floor ceil fmod degrees radians abs min max
round pow sign clamp lerp`, and `pi tau e`.

**No scene id is ever issued.** Pass the same scene to `draw_scene` that you passed to
`preview_scene`: compilation is deterministic and content-hashed, so the two agree by
construction, and there is no id to go stale.

## Handwriting

Bundled in [`fonts/`](fonts/) are nine **single-line** faces under the SIL Open Font
License — real handwriting typefaces, not engraving fonts — with attribution in
[`fonts/NOTICE.md`](fonts/NOTICE.md).

| | faces |
|---|---|
| print | `neutral` `architect` `pancakes` `delight` `casual` |
| cursive | `italienne` `cursive2` `brush` `allure` |

The cursive faces **weld**: a letter's exit sits close enough to the next letter's entry
that they join into one continuous stroke per word. "minimum" is 4 strokes, not 7 lifts.

The classic [Hershey](https://en.wikipedia.org/wiki/Hershey_fonts) names still work, but
most of them are a bad idea on a plotter — see below.

## Coordinates

Millimetres, origin top-left of the page, `u` right, `v` down.

The **page** is 80 × 45 mm and is the default framing for text and SVG. It is *not* a
boundary — `draw_paths` may use negative coordinates and go anywhere the arm reaches.
The real limit is the measured **envelope**: a circular segment of outer radius
1850 units (~92 mm) cut off by a straight inner chord, roughly three times the page area.

Points outside the envelope are reported as warnings, never silently moved. This matters:
the firmware clamps out-of-reach targets *radially* toward the origin **and lifts the pen**,
so an out-of-range point does not merely distort a stroke, it breaks it.

Note that the "app drawing area" the official documentation gives — x 650…1775,
y −1000…1000 — is **not** all reachable. Its far corners sit at radius 2037 against the
1950 this arm actually manages, so a drawing that fills that rectangle quietly loses its
corners. Other projects hardcode it. The envelope here was measured over 45 probes instead.

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
| `LINEUS_TRAVEL_SPEED` | `30` | `G94 P`, pen-*up* step size. Proven not to affect mark quality, so it is pure throughput |
| `LINEUS_PEN_DOWN_Z` | `300` | How far down the pen goes. Not 0 — see the landing smear below |
| `LINEUS_PREVIEW` | `./_preview.png` | Where the ink preview is written |
| `LINEUS_NIB_MM` | `0.5` | Nib width for that preview. Set it when you change pens |
| `LINEUS_JOIN_EM` | `0.13` | How close a cursive letter's exit must be to the next letter's entry to weld |
| `LINEUS_BLEND_MM` | `1.5` | Corner-blending window used by the simulated preview |
| `LINEUS_TICK_MM` | `0.5` | Simulated stroke-end tick at radius 1500 units; scales with reach |
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

**Most engraving fonts retrace every stem, and it shows.** Hershey's bold and serif faces
fake weight by drawing each stem two or three times side by side — a capital `H` in
`rowmant` is **27 strokes**, against the 3 a person uses. On a plotter that reads as a
sketchy scribble rather than writing. `futural` is the only Hershey face that does not do
it. The bundled single-line faces all draw a letter about once.

**Pick a face by its aperture, not by how it looks on screen.** The measurement that
predicts legibility here is the width of the opening in a letter — the gap in `s`, `a`,
`e`, `o` — against the 1–2 mm that corner blending eats. `brush` has a 0.89 mm aperture
at a 5 mm cap height, and turns "oo" into something like "rr" on paper. `architect` has
2.74 mm and stays crisp. `list_fonts` reports this per face. Two faces chosen from clean
renders both lost to the ones aperture favoured, so trust the number over the preview.

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

Scenes in [`examples/`](examples/), ready to pass straight to `preview_scene` — or
fetched by the agent with `get_example`, and best used as starting points ("the cat, lying
down"). Each carries a `_comment` explaining what made it work:

- `scene_demo.json` — text, a hatched blob, a harmonograph and a repeat family.
  473 characters of geometry compiling to 1,836 points (the file is longer; it is
  commented)
- `one_line_cat.json` — a sitting cat in a single continuous line. One *designed* path,
  not an outline with details bridged on: bridging separate pieces reads as glue
- `geometric_fox.json` — a low-poly fox head, 48 triangles with solid-filled eyes and nose,
  no angle under 19°. Its mesh is 48 strokes and 1,075 mm of ink naively, 8 strokes and
  609 mm planned — exactly its unique edge length, so nothing is drawn twice
- `text_oneliners.json` — one-liners, with a note on why the page width sets your cap
  height rather than the box you ask for

Standalone generator scripts, each printing a stroke list you can hand to `draw_paths`:

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
