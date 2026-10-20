# Line-us, revived

Software for the [Line-us](https://www.line-us.com) robot drawing arm: a small pen
plotter that started on Kickstarter, a three-servo arm that speaks G-code over TCP on the
local network.

There are two ways to drive it here, for two different hands on the pen:

```
an AI agent   ──▶  lineus-mcp  ──┐
                                 ├──▶  lineus-core  ──▶  TCP 1337  ──▶  Line-us
a person      ──▶  lineus-jog  ──┘
```

| Package | For | What it is |
|---|---|---|
| [lineus-mcp](packages/lineus-mcp) | agents | An MCP server: a scene language, single-stroke handwriting, doodles, and a preview that simulates what the pen will really do |
| [lineus-jog](packages/lineus-jog) | people | A window that moves the arm live from the keyboard, mouse or a gamepad, and calibrates the pen depth while you draw |
| [lineus-core](packages/lineus-core) | both | The arm as measured: its reach, pen model, coordinate mapping and connection |

The arm takes one connection at a time. Both tools let go of it when they are not using
it, so you can draw with an agent and jog by hand without restarting either.

## Install

Each tool installs on its own, straight from GitHub; neither pulls in the other's
dependencies.

```sh
uvx --from 'git+https://github.com/BenTeoNeb/lineus-mcp#subdirectory=packages/lineus-mcp' lineus-mcp
```

```sh
uvx --from 'git+https://github.com/BenTeoNeb/lineus-mcp#subdirectory=packages/lineus-jog' lineus-jog
```

How to register the MCP server with Claude Code, Claude Desktop or another client is in
[its README](packages/lineus-mcp/README.md#install). Both read the same `LINEUS_*`
environment variables, so set `LINEUS_HOST` (and any pen calibration) once for both.

## Development

A [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/): one lockfile,
one environment, one test run.

```sh
uv sync --all-packages      # every package, with pytest and ruff
uv run pytest               # all three packages: no robot, no network, a few seconds
uv run ruff check packages
```

## Safety

- Preview before drawing.
- Never expose port 1337 to the internet. There is no authentication.

## Licence

MIT — see [LICENSE](LICENSE).
