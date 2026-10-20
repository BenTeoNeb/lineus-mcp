# lineus-jog

Drive a [Line-us](https://www.line-us.com) robot arm by hand, live, from the keyboard,
the mouse or a gamepad, and calibrate the pen while you do.

It is the human counterpart of [lineus-mcp](../lineus-mcp), which lets an AI agent draw.
Both sit on [lineus-core](../lineus-core): the same measured envelope, coordinate mapping
and pen depth, so what you jog lines up with what the agent draws.

## Run it

```sh
uvx --from 'git+https://github.com/BenTeoNeb/lineus-mcp#subdirectory=packages/lineus-jog' lineus-jog
```

or, from a clone, `uv run lineus-jog`. Options: `--host 192.168.1.42` (default
`line-us.local`), `--mock` to try it without a robot, `--speed 5` for the pen-down step
size, `--debug-input` to print every button and stick event.

## Controls

| | Gamepad | Keyboard | Mouse |
|---|---|---|---|
| Move | left stick | WASD / arrows | drag (pen down) |
| Fine move | right stick | hold Shift | |
| Pen | hold RT, or A to toggle | Space toggles | left button |
| Lift | B | | |
| Travel to a point | | | right-click |
| Speed down / up | LB / RB | `[` / `]` | |
| Press harder / lighter | D-pad down / up | `-` / `=` | |
| Home | Start | H | |
| Clear the trail | Back | C | |
| Save the drawing | Y | Enter | |
| Quit | | Esc | |

Any gamepad SDL recognises works; an 8BitDo Pro paired in X-input mode shows up as an
Xbox controller.

## How it behaves

The window shows the reachable envelope in green and the page in grey. Moves into the
edge slide along it rather than being sent: the firmware would clamp them and lift the
pen.

Each `G01` blocks until the arm has finished the move, so the arm cannot keep up with a
60 Hz input loop. A background thread sends only the newest target whenever the arm is
free; inputs that arrive while it is busy are dropped, not queued. The cursor (circle)
leads the arm (dot) by one move at most, and never by more than 6 mm.

Enter saves what was drawn as a `jog-*.json` scene, which lineus-mcp's `preview_scene`
and `draw_scene` replay.

## Calibrating the pen

After a pen change, put the pen down and trim the depth in steps of 25 while you draw:
lower Z presses harder. The change applies at once, even with the arm standing still.
Stop where the line is solid and the stroke ends are clean. The status line names the
setting that keeps it, `LINEUS_PEN_DOWN_Z` (or `LINEUS_PEN_MARGIN` when a pen plane is
set); put it in your MCP server's environment too. The trim lasts only for that session.

## Sharing the arm with lineus-mcp

The arm takes one connection at a time. lineus-mcp lets go of it after every drawing, so
the two take turns: draw with the agent, jog, draw again. While a drawing is running the
jog window shows `no link` and connects as soon as the drawing ends.
