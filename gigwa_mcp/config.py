"""Configuration loading for the Gigwa MCP server.

Reads connection settings from the environment (optionally seeded from a local
``.env`` file): ``GIGWA_URL`` (defaults to the public ICARDA instance when unset),
plus the optional ``GIGWA_USER``/``GIGWA_PASS`` (omit both for anonymous access),
``GIGWA_TIMEOUT`` and ``GIGWA_CONNECT_TIMEOUT`` (seconds).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import GigwaConfigError

# Fallback when GIGWA_URL is unset: the public ICARDA Gigwa instance, reachable
# anonymously. Lets the server run with zero configuration (good for demos, directory
# inspection, and a first look) while any real deployment overrides GIGWA_URL.
DEFAULT_GIGWA_URL = "https://gigwa.icarda.org:8443/gigwa"

try:  # python-dotenv is a declared dependency, but stay importable without it.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def _find_dotenv(start: Path | None = None) -> Path | None:
    """Walk up from *start* (cwd by default) looking for a ``.env`` file."""
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class GigwaConfig:
    base_url: str
    # Credentials are optional: leave both empty to use Gigwa's **anonymous** access
    # (public/read-only operations on public instances). When empty, the client sends no
    # ``Authorization`` header rather than trying to generate a token.
    username: str = ""
    password: str = ""
    timeout: float = 120.0
    # Cap on TCP connection establishment only (the read/write timeout stays ``timeout``),
    # so an unreachable/misconfigured Gigwa fails in seconds instead of hanging. Connecting
    # to a reachable server is sub-second even across regions; raise this if you sit behind
    # a very high-latency link.
    connect_timeout: float = 10.0

    @property
    def rest_url(self) -> str:
        """Base URL of the REST API (``<base_url>/rest``)."""
        url = self.base_url.rstrip("/")
        if not url.endswith("/rest"):
            url = f"{url}/rest"
        return url

    @classmethod
    def from_env(cls) -> "GigwaConfig":
        if load_dotenv is not None:
            env_path = _find_dotenv()
            if env_path is not None:
                load_dotenv(env_path)

        base_url = os.environ.get("GIGWA_URL")
        if not base_url:
            # Zero-config fallback: the public ICARDA instance (anonymous). Notice goes to
            # stderr — never stdout, which is the MCP stdio transport.
            base_url = DEFAULT_GIGWA_URL
            print(
                f"[gigwa-mcp] GIGWA_URL not set; defaulting to the public ICARDA instance "
                f"({base_url}) with anonymous access. Set GIGWA_URL to use your own server.",
                file=sys.stderr,
            )
        # Both must be present to authenticate; otherwise fall back to anonymous access.
        username = os.environ.get("GIGWA_USER") or ""
        password = os.environ.get("GIGWA_PASS") or ""
        if bool(username) != bool(password):
            raise GigwaConfigError(
                "Set both GIGWA_USER and GIGWA_PASS to authenticate, or neither for "
                "anonymous access — got only one."
            )

        try:
            timeout = float(os.environ.get("GIGWA_TIMEOUT", "120"))
        except ValueError:
            timeout = 120.0

        try:
            connect_timeout = float(os.environ.get("GIGWA_CONNECT_TIMEOUT", "10"))
        except ValueError:
            connect_timeout = 10.0

        return cls(
            base_url=base_url,
            username=username,
            password=password,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )
