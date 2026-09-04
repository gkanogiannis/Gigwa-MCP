#!/bin/sh
# Entrypoint script for Gigwa MCP server container.
# If GIGWA_MCP_PORT is set, runs in HTTP mode on that port.
# Otherwise, runs in STDIO mode (default).

set -e

# Check if GIGWA_MCP_PORT is set and not empty
if [ -n "$GIGWA_MCP_PORT" ]; then
    echo "Starting Gigwa MCP server in HTTP mode on port $GIGWA_MCP_PORT" >&2
    exec gigwa-mcp --port "$GIGWA_MCP_PORT"
else
    echo "Starting Gigwa MCP server in STDIO mode" >&2
    exec gigwa-mcp --stdio
fi
