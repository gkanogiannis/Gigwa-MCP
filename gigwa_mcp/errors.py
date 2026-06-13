"""Typed errors for the Gigwa MCP server.

Tool functions catch these and surface clean, human-readable messages to the
MCP client instead of leaking raw tracebacks or httpx internals.
"""

from __future__ import annotations


class GigwaError(Exception):
    """Base class for all Gigwa MCP errors."""


class GigwaConfigError(GigwaError):
    """Missing or invalid configuration (e.g. unset GIGWA_* env vars)."""


class GigwaAuthError(GigwaError):
    """Authentication against Gigwa failed (bad credentials or token)."""


class GigwaAPIError(GigwaError):
    """A Gigwa REST call returned an error status."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        self.status_code = status_code
        self.body = body
        detail = message
        if status_code is not None:
            detail = f"{detail} (HTTP {status_code})"
        if body:
            snippet = body.strip()
            if len(snippet) > 500:
                snippet = snippet[:500] + "..."
            detail = f"{detail}: {snippet}"
        super().__init__(detail)


class GigwaImportError(GigwaError):
    """An import job failed or was aborted on the server."""
