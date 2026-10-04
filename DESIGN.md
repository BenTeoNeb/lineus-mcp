# Design notes

Status: proposal. Nothing here is built yet. Written after a long session of driving the
robot through the current tools, so the motivation is empirical rather than speculative.

## The problem

The server is a **transport for geometry when it should be a renderer of intent.**

Every drawing in that session followed the same shape: write a throwaway Python script,
run it, get a JSON point list, paste the list into `draw_paths`. The server contributed
stroke ordering, an envelope check, and G-code streaming. Everything that made a drawing
*a drawing* — ellipses, hatching, text layout, curve sampling — happened outside it, in
scripts that were never part of the project.

Measured cost of one session:

| drawing | strokes | points | JSON chars | ~tokens |
|---|---:|---:|---:|---:|
| 4 lines of Hershey text | 176 | 930 | 11,326 | 3,150 |
| harmonograph | 1 | 991 | 11,755 | 3,265 |
| envelope + spiral | 2 | 829 | 9,934 | 2,760 |
| ladybug line overlay | 17 | 468 | 5,534 | 1,540 |
| ladybug stipple | 358 | 358 | 4,929 | 1,370 |
| shaded sphere stipple | 311 | 311 | 4,317 | 1,200 |
| **total** | | | **47,795** | **~13,300** |

Several were sent twice (once to preview, once to draw) or re-sent after a failure, so the
real figure is over 20,000 tokens of coordinates.

Three consequences, in order of severity:

1. **Expensive and slow.** 930 points for four lines of text. The same thing expressed as
   intent is about 40 tokens.
2. **Preview and draw can silently disagree.** They are separate calls, each taking its own
   pasted array. Nothing guarantees the previewed geometry is the drawn geometry.
3. **Iteration is brutal.** Every tweak — stipple density, harmonograph decay, leg angles —
   means regenerating and re-pasting the whole point list. Three versions of the stipple and
   three of the harmonograph each cost a full payload.

## Principles

- **The agent supplies semantics; the server supplies geometry.** Anything deterministic
  given parameters belongs server-side.
- **Preview and draw must be the same object**, not two descriptions that happen to match.
- **Keep the escape hatch.** Novel geometry still needs a raw path in. `draw_paths` stays.
- **Never silently alter a drawing.** Out-of-reach points are reported, not clamped. This is
  already true and must remain true.

## Proposed architecture

A declarative `scene`: a list of shapes that the server compiles to strokes.

```json
{"shapes": [
  {"text": "Hello World !", "font": "timesr", "box": [4, 13, 72, 9]},
  {"circle": [33, 12.5, 5.5], "fill": {"hatch": 45, "spacing": 1.15}},
  {"ellipse": [47, 22.5, 29, 19.5]},
  {"polyline": [[20, 15.4], [12, 8], [5, 5]]},
  {"harmonograph": {"decay": 0.0085, "turns": 40}, "fit": [2, 2, 76, 41]}
]}
```

`preview_scene(scene)` returns a PNG **and a `scene_id`**. `draw_scene(scene_id)` draws that
exact compiled geometry. Preview and draw can no longer diverge, and the second call is free.

Shape primitives worth having: `polyline`, `polygon`, `rect` (with optional corner radius),
`circle`, `ellipse`, `arc`, `text`, `svg`, `grid`. Modifiers: `fill` (hatch / cross-hatch),
`transform` (translate / rotate / scale), `repeat`, `fit` (scale into a box).

## Work, by priority

### Tier 1 — the shape compiler

1. **Scene vocabulary + `scene_id`.** The core change. ~35x payload reduction on text,
   ~75x on parametric curves, and it removes the preview/draw divergence class of bug.
2. **Hatching engine.** Fill any closed shape at a given angle and spacing. This was
   hand-written three times in one session. It is what makes line art read as solid black on
   a machine with no fills.
3. **Text blocks.** `draw_text` handles one string in one font. Multi-line, multi-font,
   alignment and leading should be one call, not N calls merged by the caller.

### Tier 2 — small, high value

4. **Preview the user can see.** `preview_*` currently returns an image to the agent only.
   It should also write a PNG and return the path.
5. **`plan()` dry run.** Point count, bounding box, pen-up travel, envelope warnings and a
   time estimate, without moving. The server can calibrate the estimate from job history;
   the current fixed "0.05–0.1 s per point" hint is wrong across the speed range.
6. **Sheet state.** Track what has been drawn since `new_sheet()` and warn on overlap.
   A drawing was once laid on top of a test pattern that was still on the paper; the server
   had everything it needed to catch that.
7. **Diagnose the wedged firmware.** When `get_status` times out, probe whether TCP still
   accepts. If it does, report *"firmware wedged — power cycle required"* rather than
   "timed out". This failure happened and had to be diagnosed by hand with `ping` and `nc`.

### Tier 3 — nice to have

8. Parametric generators as first-class shapes: spiral, lissajous, harmonograph, stipple.
9. Transforms and arrays.
10. Vision feedback: `capture()` and a `verify()` that compares drawn against intended.

## Non-goals

- A full vector graphics language. `draw_svg` is the escape hatch for imported art; it
  should learn to hatch fills and accept explicit placement, not grow into a renderer.
- A GUI.
- Authentication. This is LAN-only by design, and the README says so plainly.

## Correction to the section below

The experiment described at the end of this document was run, and **both its hypotheses
were wrong**. The dot smear is not a timing artifact at all: the pen-lift axis is coupled to
radius, so the nib travels radially as it descends. Confirmed by a controlled test — the tic
grows with distance from the shoulder and shrinks with less Z over-travel, while speed at
constant radius does nothing. The fix was `PEN_DOWN_Z` (default 300), not settling time.

Corner rounding, however, *is* blending: the official spec documents one command of
lookahead. Two distinct faults that happened to look like one.

Kept below as written, because the reasoning is a fair record of how the wrong answer was
arrived at, and the experiment design is reusable.

## Open hardware question

See the section below — it should be settled **before** Tier 1, because the answer changes
`run_job` for every drawing.

---

# The unresolved experiment: why the pen lands early

## The symptom

Three oddities that look unrelated are almost certainly one fault:

- **Corners are rounded** by a roughly *constant* 1–2 mm radius — invisible on a 20 mm
  square, fatal on a 6 mm one.
- **Single-point "dots" print as ~1 mm dashes**, smeared along the direction the arm
  approached from.
- **Stroke junctions misregister** when a glyph's strokes are drawn out of order.

The dot is the clean clue. A single-point stroke sends three commands at the *same* XY:
travel with pen up, pen down, pen up. If the arm were stationary when `Z0` executes, the
mark would be a point. It is a dash. So the arm is still moving when the nib lands.

## Two candidate mechanisms

**(A) Planner blending.** The firmware looks ahead and begins the next move before
finishing the current one, so it never comes to rest at an endpoint.

**(B) Servo lag.** The firmware considers a move finished when its interpolation ends and
replies `ok`, but the servos are physically still catching up — ordinary lag and mechanical
compliance in a hobby-servo arm.

These need different fixes, and **the evidence points hard at (B)**:

- Replies are **blocking**. At `G94 S1` a single move took longer than a 20-second socket
  timeout to answer. The robot does not buffer; it answers when it believes the move is done.
- Round-trip latency to the robot is 30–145 ms, so there is *already* a substantial pause
  between consecutive moves — and corners still round. Whatever is still moving is moving
  well after the firmware has declared the move complete.

## A correction worth recording

An earlier note in this project concluded: *"`G4` dwell is UNSUPPORTED, so you cannot pause
at a corner — speed is the only lever."*

The first half is true; the conclusion does not follow. **The client can insert the dwell.**
Since replies are blocking, the server can simply sleep between the positioning move and
the pen-down. `G4` being unimplemented is irrelevant when `time.sleep()` exists.

That reframes the whole problem. It also predicts the previously-favoured fix will fail:
repeating the XY point adds a *zero-length* move, which under hypothesis (B) returns
immediately and buys no settling time at all.

## The experiment

One sheet, four groups of five dots, well separated, at a middling speed (S5):

| group | treatment | tests |
|---|---|---|
| A | plain — travel, `Z0`, `Z1000` | control |
| B | send the XY point **twice** before `Z0` | hypothesis (A) |
| C | **sleep 100 ms** between the XY move and `Z0` | hypothesis (B) |
| D | **sleep 300 ms** between the XY move and `Z0` | hypothesis (B), harder |

Then a second strip of 6 mm squares: plain, versus a sleep at each vertex.

Predictions that distinguish the hypotheses cleanly:

- **Servo lag (B):** C and D are round, D rounder than C; B is dashed exactly like A.
- **Planner blending (A):** B is round; C and D are dashed.
- **Both:** all of B, C and D improve.

Roughly a minute of drawing. It needs raw socket control to place the sleeps, so it runs as
a standalone script first; if it works it becomes a `settle_ms` option in `run_job`.

## If the dwell wins

The cost is one sleep per pen-down: 358 dots at 300 ms is about 107 s added — acceptable
for stippling, which is slow anyway.

Applying it at *every* vertex would be far too expensive (a 991-point curve at 100 ms is
99 s of pure waiting), so it should be **selective**: dwell only where the path turns
sharply. Compute the direction change at each vertex and settle only above a threshold —
say 45°. Smooth curves pay nothing; corners get their stop.

That single mechanism would then address all three symptoms: round dots, sharp corners, and
registered junctions.
