"""FastMCP server instance and shared Gigwa client accessor.

Tool modules import ``mcp`` and ``get_client`` from here and register themselves
via the ``@mcp.tool()`` decorator. Importing this module wires up every tool.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .client import GigwaClient
from .config import GigwaConfig

mcp = FastMCP("gigwa")

_client: GigwaClient | None = None


def get_client() -> GigwaClient:
    """Return a process-wide GigwaClient, created lazily from the environment."""
    global _client
    if _client is None:
        _client = GigwaClient(GigwaConfig.from_env())
    return _client


# Registering the tool modules attaches their @mcp.tool() functions to `mcp`.
from .tools import connection, genotype, metadata  # noqa: E402,F401
from .tools import qc, diversity, audit  # noqa: E402,F401

__all__ = ["mcp", "get_client"]
