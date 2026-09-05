"""mcp_server — FastMCP server exposing the OpAMP server as typed MCP tools.

Run with ``python -m mcp_server`` (stdio) or ``python -m mcp_server --transport sse``.
See :mod:`mcp_server.server` for details.
"""

from mcp_server.server import main, mcp

__all__ = ["mcp", "main"]
