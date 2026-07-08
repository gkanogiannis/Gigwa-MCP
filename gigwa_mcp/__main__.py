"""Entry point: run the Gigwa MCP server over stdio or HTTP.

    python -m gigwa_mcp        # stdio transport (default)
    python -m gigwa_mcp --stdio
    python -m gigwa_mcp --port 8080

Usage:
    gigwa-mcp [--stdio] [--port PORT]

Options:
    --stdio     Run with STDIO transport (default)
    --port PORT Run with StreamableHTTP transport on the specified port
"""

from __future__ import annotations

import argparse
import sys

from .server import mcp


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Gigwa MCP server with STDIO or HTTP transport."
    )

    # Mutually exclusive group for transport options
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--stdio",
        action="store_true",
        default=True,
        help="Run with STDIO transport (default)",
    )
    group.add_argument(
        "--port",
        type=int,
        default=None,
        help="Run with StreamableHTTP transport on the specified port",
    )

    args = parser.parse_args()

    if args.port is not None:
        run_http_server(args.port)
    else:
        mcp.run(transport="stdio")


def run_http_server(port: int) -> None:
    """Run the MCP server with StreamableHTTP transport using uvicorn."""
    import uvicorn

    app = mcp.streamable_http_app()

    # Print to stdout before uvicorn takes over logging
    sys.stdout.write(f"Starting Gigwa MCP server on http://localhost:{port}/mcp\n")
    sys.stdout.write("Press Ctrl+C to stop\n")
    sys.stdout.flush()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        # Disable uvicorn's default server header for cleaner logs
        server_header=False,
    )


if __name__ == "__main__":
    main()
