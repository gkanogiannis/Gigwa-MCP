"""Configuration loading for the Gigwa MCP server.

Reads connection settings from the environment (optionally seeded from a local
``.env`` file): ``GIGWA_URL``, ``GIGWA_USER``, ``GIGWA_PASS`` and the optional
``GIGWA_TIMEOUT`` (seconds).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import GigwaConfigError

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
    username: str
    password: str
    timeout: float = 120.0

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

        values = {
            "GIGWA_URL": os.environ.get("GIGWA_URL"),
            "GIGWA_USER": os.environ.get("GIGWA_USER"),
            "GIGWA_PASS": os.environ.get("GIGWA_PASS"),
        }
        missing = [key for key, val in values.items() if not val]
        if missing:
            raise GigwaConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Set them in your environment or a .env file "
                "(GIGWA_URL, GIGWA_USER, GIGWA_PASS)."
            )

        try:
            timeout = float(os.environ.get("GIGWA_TIMEOUT", "120"))
        except ValueError:
            timeout = 120.0

        return cls(
            base_url=values["GIGWA_URL"],
            username=values["GIGWA_USER"],
            password=values["GIGWA_PASS"],
            timeout=timeout,
        )
