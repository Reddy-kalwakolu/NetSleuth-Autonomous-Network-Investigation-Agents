"""Read only investigation tools, as plain Python. The MCP server wraps these in milestone 3."""

from netsleuth.tools import telemetry as _telemetry  # noqa: F401  registers the tools
from netsleuth.tools import topology as _topology  # noqa: F401  registers the tools
from netsleuth.tools.base import TOOLS, ToolError, ToolResult, call_tool, make_ref, rerun, tool
from netsleuth.tools.inventory import Inventory, inventory, lowest_common_ancestor, offline_now

__all__ = [
    "TOOLS",
    "Inventory",
    "ToolError",
    "ToolResult",
    "call_tool",
    "inventory",
    "lowest_common_ancestor",
    "make_ref",
    "offline_now",
    "rerun",
    "tool",
]
