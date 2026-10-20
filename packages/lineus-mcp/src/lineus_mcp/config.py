"""Settings of the agent drawing tools: bundled data, caches and the preview. The arm's own
settings are in lineus_core.config."""
from __future__ import annotations

import os

# Bundled data (fonts, doodles, example scenes) ships inside the package.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# Fetched doodles and the preview image live outside the package: an installed package
# directory is not ours to write to.
CACHE_DIR = os.environ.get("LINEUS_CACHE",
                           os.path.join(os.path.expanduser("~"), ".cache", "lineus-mcp"))
# Where preview_* writes a PNG THE USER CAN ACTUALLY LOOK AT. MCP image content goes to
# the agent, not to the person at the terminal -- "where do i see it" was a real question
# and the honest answer was "you cannot". The filename is STABLE so an open viewer
# (macOS Preview, VS Code) reloads it in place as you iterate on a scene; every preview
# note prints the path.
PREVIEW_PATH = os.environ.get("LINEUS_PREVIEW", os.path.join(CACHE_DIR, "preview.png"))


NIB_MM = float(os.environ.get("LINEUS_NIB_MM", "0.5"))
