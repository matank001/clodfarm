"""The farm plugin's MCP server (stdio): the farm's own MCP tools (farm_status, farm_spawn, farm_msg, ...), reached
with this computer's connection (/farm:connect), so they need no second sign-in. See lib/clodfarm/attach.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
os.environ["CLODFARM_PLUGIN"] = "1"
from clodfarm.attach import mcp_main  # noqa: E402

mcp_main()
