import asyncio
import os
import sys

from lineus_mcp.server import get_example, mcp

DRAWS = {"draw_paths", "draw_svg", "draw_text", "draw_scene", "draw_orientation_test"}
READ_ONLY = {"get_status", "get_job", "list_fonts", "get_example", "doodles", "plan_scene",
             "preview_paths", "preview_svg", "preview_text", "preview_scene"}


def content(r):
    return r[0] if isinstance(r, tuple) else r


def test_every_tool_is_annotated_honestly(run):
    tools = {t.name: t.annotations for t in run(mcp.list_tools())}
    assert len(tools) == 17 and all(a is not None for a in tools.values())
    assert all(tools[n].readOnlyHint for n in READ_ONLY)
    for n in DRAWS:                       # physical, additive, not repeatable
        a = tools[n]
        assert not a.readOnlyHint and not a.destructiveHint and not a.idempotentHint
        assert a.openWorldHint
    assert tools["abort"].destructiveHint and tools["home"].idempotentHint


def test_preview_returns_diagnostic_simulated_and_checks(run):
    scene = {"shapes": [{"paths": [[[10, 10], [40, 10]], [[10, 10.6], [40, 10.6]]]}]}
    out = content(run(mcp.call_tool("preview_scene", {"scene": scene})))
    assert [type(c).__name__ for c in out] == ["ImageContent", "ImageContent", "TextContent"]
    assert "DRAWING CHECKS" in out[-1].text
    assert os.path.exists(os.environ["LINEUS_PREVIEW"])


def test_a_bad_scene_comes_back_as_a_message(run):
    bad = {"shapes": [{"param": {"t": [0, 1, 10], "x": "wobble", "y": "t"}}]}
    out = content(run(mcp.call_tool("preview_scene", {"scene": bad})))
    assert "unknown name 'wobble'" in out[0].text


def test_doodle_browser_returns_a_contact_sheet(run):
    out = content(run(mcp.call_tool("doodles", {"category": "owl"})))
    assert [type(c).__name__ for c in out] == ["ImageContent", "TextContent"]
    assert "chosen by eye" in out[-1].text


def test_examples_as_tool_and_resources(run):
    names = {e["name"] for e in get_example()["examples"]}
    assert {"one_line_cat", "geometric_fox"} <= names
    assert "error" in get_example("../server")
    assert "lineus://examples" in {str(r.uri) for r in run(mcp.list_resources())}
    assert any("{name}" in str(t.uriTemplate) for t in run(mcp.list_resource_templates()))


def test_instructions_carry_the_drawing_lessons():
    text = mcp.instructions
    assert "SIMULATED" in text and "One-line art is ONE designed path" in text


def test_the_server_speaks_mcp_over_stdio():
    """Spawn the real server and talk to it with the real client. Anything the server
    wrote to stdout would corrupt the protocol stream and fail this."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def talk():
        params = StdioServerParameters(command=sys.executable, args=["-m", "lineus_mcp"],
                                       env=dict(os.environ))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            plan = await session.call_tool("plan_scene", {"scene": {
                "shapes": [{"doodle": "cat", "pick": 0, "box": [10, 5, 30, 30]}]}})
            return len(tools.tools), plan.content[0].text
    n, plan = asyncio.run(talk())
    assert n == 17 and '"strokes"' in plan
