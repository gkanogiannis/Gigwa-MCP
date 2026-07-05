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


def _maybe_load_dotenv() -> None:
    """Load a nearby ``.env`` into the process environment, if python-dotenv is present."""
    if load_dotenv is not None:
        env_path = _find_dotenv()
        if env_path is not None:
            load_dotenv(env_path)


def _env_float(name: str, default: float) -> float:
    """Read *name* from the environment as a float, falling back to *default* on error."""
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_timeouts() -> tuple[float, float]:
    """(read/write timeout, connect timeout) from ``GIGWA_TIMEOUT``/``GIGWA_CONNECT_TIMEOUT``."""
    return _env_float("GIGWA_TIMEOUT", 120.0), _env_float("GIGWA_CONNECT_TIMEOUT", 10.0)


def _profile_key(profile: str) -> str:
    """Env-var suffix for a named credential *profile* (upper-cased, non-alnum -> ``_``)."""
    return "".join(ch if ch.isalnum() else "_" for ch in profile.strip()).upper()


def _normalize_base_url(url: str) -> str:
    """Normalise a user-supplied Gigwa base URL.

    Accepts what a person types conversationally — a bare ``host:port`` (e.g. from
    "connect to abc.xyz:123") gets an ``https://`` scheme prepended; an explicit
    ``http://``/``https://`` is left as-is. Trailing slashes are trimmed (``rest_url``
    re-adds the ``/rest`` suffix).
    """
    url = (url or "").strip()
    if not url:
        raise GigwaConfigError("A Gigwa base URL is required (e.g. https://host:port/gigwa).")
    if "://" not in url:
        url = f"https://{url}"
    return url.rstrip("/")


def resolve_credentials(profile: str | None = None, *, anonymous: bool = False) -> tuple[str, str]:
    """Resolve ``(username, password)`` for a connection, **without touching the chat**.

    Credentials are read from the *environment* (never passed through the model):
    ``anonymous=True`` -> ``("", "")``; a named *profile* -> ``GIGWA_USER_<PROFILE>`` /
    ``GIGWA_PASS_<PROFILE>``; otherwise the default ``GIGWA_USER`` / ``GIGWA_PASS``.
    Both of a pair must be present (or neither) — a lone one raises, as does a named
    profile with no matching variables (likely a typo, so we surface it).
    """
    if anonymous:
        return "", ""
    if profile:
        key = _profile_key(profile)
        user_var, pass_var = f"GIGWA_USER_{key}", f"GIGWA_PASS_{key}"
    else:
        user_var, pass_var = "GIGWA_USER", "GIGWA_PASS"
    username = os.environ.get(user_var) or ""
    password = os.environ.get(pass_var) or ""
    if bool(username) != bool(password):
        raise GigwaConfigError(
            f"Set both {user_var} and {pass_var} to authenticate, or neither for "
            "anonymous access — got only one."
        )
    if profile and not username:
        raise GigwaConfigError(
            f"No credentials found for profile '{profile}': set {user_var} and {pass_var} "
            "in the environment, or pass anonymous=True."
        )
    return username, password


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
        _maybe_load_dotenv()

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
        username, password = resolve_credentials()
        timeout, connect_timeout = _env_timeouts()

        return cls(
            base_url=base_url,
            username=username,
            password=password,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )

    @classmethod
    def for_connection(
        cls, base_url: str, *, profile: str | None = None, anonymous: bool = False
    ) -> "GigwaConfig":
        """Build a config for a runtime connection switch (the ``gigwa_connect`` tool).

        Same shape as :meth:`from_env` — the URL is normalised, timeouts come from the
        environment — but the target *base_url* is given explicitly and credentials are
        resolved out-of-band via :func:`resolve_credentials` (``profile``/``anonymous``),
        so no secret is ever passed as a tool argument.
        """
        _maybe_load_dotenv()
        url = _normalize_base_url(base_url)
        if not anonymous and profile is None:
            # No profile named: the default GIGWA_USER/GIGWA_PASS belong to the *configured*
            # server, so only reuse them when reconnecting to that same URL. Pointing at any
            # other server without an explicit profile defaults to anonymous — the home
            # credentials are never transmitted to a different host unasked.
            home = _normalize_base_url(os.environ.get("GIGWA_URL") or DEFAULT_GIGWA_URL)
            if url != home:
                anonymous = True
        username, password = resolve_credentials(profile, anonymous=anonymous)
        timeout, connect_timeout = _env_timeouts()
        return cls(
            base_url=url,
            username=username,
            password=password,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )
