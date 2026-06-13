"""Entry point: run the Gigwa MCP server over stdio.

    python -m gigwa_mcp        # or the installed `gigwa-mcp` console script
"""

from __future__ import annotations

from .server import mcp


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
