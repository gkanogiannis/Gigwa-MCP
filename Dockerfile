# Gigwa MCP server  stdio or HTTP transport.
#
#   docker build -t gigwa-mcp .
#
# STDIO transport (default, for MCP client via docker run -i):
#   docker run -i --rm -e GIGWA_URL -e GIGWA_USER -e GIGWA_PASS -v "$PWD:/data" gigwa-mcp
#
# HTTP transport (expose port, e.g. 8184):
#   docker run -d --rm -p 8184:8184 -e GIGWA_URL -e GIGWA_USER -e GIGWA_PASS gigwa-mcp
#   # or with custom port:
#   docker run -d --rm -p 9000:9000 -e GIGWA_MCP_PORT=9000 -e GIGWA_URL -e GIGWA_USER -e GIGWA_PASS gigwa-mcp
#
# See README ("Run with Docker") for MCP client config, volume mounts, and networking.
#
# Multi-stage: the builder has a C toolchain (some deps, e.g. mappy, may build from
# source); the final image copies just the installed venv, so no compiler ships in it.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1

# Toolchain for any deps without a matching wheel (mappy compiles against zlib).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install the package (and its deps) into a self-contained venv we can copy out.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
# LICENSE/NOTICE/README are referenced by pyproject.toml (license-files / readme),
# so they must be present at install time.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY gigwa_mcp ./gigwa_mcp
RUN pip install .


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Bring in the ready-built venv from the builder (no compiler in this layer).
COPY --from=builder /opt/venv /opt/venv

# Run as non-root. WORKDIR is a mount point: analysis results are written to
# ./gigwa_results/<module>/ relative to the working directory, so mounting a host
# dir at /data persists outputs and lets imports read host files by their /data path.
RUN useradd --create-home --uid 1000 gigwa \
    && mkdir -p /data \
    && chown gigwa:gigwa /data
USER gigwa
WORKDIR /data

# Default port for HTTP transport (can be overridden via -e GIGWA_MCP_PORT)
ENV GIGWA_MCP_PORT=8184

# Entrypoint script to handle both STDIO and HTTP modes
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gigwa-mcp"]
