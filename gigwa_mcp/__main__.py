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
import os
import sys

from mcp.server.transport_security import TransportSecuritySettings

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


def _normalize_allowed_hosts(values: list[str]) -> list[str]:
    """Expand simple hostnames to cover both bare-host and host:port requests."""
    normalized: list[str] = []
    for value in values:
        item = value.strip()
        if not item:
            continue
        if item == "*":
            normalized.append(item)
            continue
        if item.endswith(":*"):
            normalized.append(item)
            normalized.append(item[:-2])
            continue
        if ":" not in item:
            normalized.append(item)
            normalized.append(f"{item}:*")
            continue
        normalized.append(item)
    return normalized


def configure_http_transport_security() -> None:
    """Allow container/service hostnames and localhost to pass the SDK's Host checks."""
    disable_dns_rebinding = os.getenv("GIGWA_MCP_DISABLE_DNS_REBINDING_PROTECTION", "").strip().lower()
    disable_dns_rebinding = disable_dns_rebinding in {"1", "true", "yes", "on"}

    default_allowed_hosts = ["127.0.0.1", "localhost", "[::1]"]
    container_hostname = os.getenv("HOSTNAME")
    if container_hostname:
        default_allowed_hosts.append(container_hostname)

    allowed_hosts_raw = os.getenv("GIGWA_MCP_ALLOWED_HOSTS", ",".join(default_allowed_hosts))
    allowed_origins_raw = os.getenv(
        "GIGWA_MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1,http://localhost,http://[::1]",
    )

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=not disable_dns_rebinding,
        allowed_hosts=_normalize_allowed_hosts(
            [host for host in allowed_hosts_raw.split(",") if host.strip()]
        ),
        allowed_origins=_normalize_allowed_hosts(
            [origin for origin in allowed_origins_raw.split(",") if origin.strip()]
        ),
    )


def run_http_server(port: int) -> None:
    """Run the MCP server with StreamableHTTP transport using uvicorn."""
    import uvicorn

    configure_http_transport_security()
    app = mcp.streamable_http_app()

    # Bind loopback by default so a local `--port` run is not exposed on every network
    # interface. Containers/remote deployments opt into all-interfaces by setting
    # GIGWA_MCP_HOST=0.0.0.0 (the Docker image sets it; see Dockerfile).
    host = os.getenv("GIGWA_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"

    # Print to stdout before uvicorn takes over logging
    sys.stdout.write(f"Starting Gigwa MCP server on http://{host}:{port}/mcp\n")
    sys.stdout.write("Press Ctrl+C to stop\n")
    sys.stdout.flush()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        # Disable uvicorn's default server header for cleaner logs
        server_header=False,
    )


if __name__ == "__main__":
    main()
