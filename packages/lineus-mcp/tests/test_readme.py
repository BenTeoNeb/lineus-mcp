"""The README is checked like code: every name it mentions must exist, every scene it
shows must compile."""
import glob
import json
import os
import re

import lineus_mcp.server as server
from lineus_mcp.scene import compile_scene

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
# Settings live in all three packages: the arm's in lineus-core, the rest beside their code.
SOURCE = "".join(open(f, encoding="utf-8").read() for f in
                 glob.glob(os.path.join(ROOT, "..", "*", "src", "*", "*.py")))


def test_every_tool_named_exists(run):
    tools = {t.name for t in run(server.mcp.list_tools())}
    named = set(re.findall(r"`(get_status|preview_\w+|draw_\w+|plan_scene|list_fonts|"
                           r"get_example|doodles|get_job|abort|home)`", README))
    assert named and named <= tools


def test_every_env_var_named_exists():
    named = set(re.findall(r"`(LINEUS_\w+)`", README))
    assert named and all(n in SOURCE for n in named)


def test_every_scene_shown_compiles():
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", README, re.S)]
    scenes = [b for b in blocks if "shapes" in b]
    assert scenes and all(compile_scene(s) for s in scenes)
