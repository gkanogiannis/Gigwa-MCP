"""Connection / inventory tools: connect/switch server, check it, and list its content."""

from __future__ import annotations

from typing import Any

from ..client import GigwaClient
from ..config import GigwaConfig
from ..server import get_client, mcp, set_client


def _verify_and_describe(client: GigwaClient) -> str:
    """Force a live round-trip against *client* and render its connection summary.

    Raises if the server is unreachable or rejects the credentials (so callers can treat
    a return value as "the connection works"). Shared by ``gigwa_server_info`` and the
    ``gigwa_connect`` switch so both report identically.
    """
    version = client.server_version()
    # Force a round-trip so we fail fast on bad credentials / unreachable host (and, when
    # authenticated, exercise token generation).
    client.instance_content_summary()
    lines = [
        f"Connected to Gigwa at {client.config.base_url}",
        f"REST base: {client.rest}",
        f"Version: {version or 'unknown'}",
    ]
    if client.anonymous:
        lines += ["User: (anonymous)", "Authentication: anonymous (public/read-only access)"]
    else:
        lines += [f"User: {client.config.username}", "Authentication: OK"]
    # Best-effort: report server-side user identity / roles when the build supports it.
    info = client.user_info()
    if info:
        roles = info.get("authorities") or info.get("roles") or info.get("permissions")
        if isinstance(roles, (list, tuple)) and roles:
            lines.append("Roles: " + ", ".join(str(r) for r in roles))
        if info.get("administrator") or info.get("admin"):
            lines.append("Administrator: yes")
    return "\n".join(lines)


@mcp.tool()
def gigwa_connect(url: str, profile: str | None = None, anonymous: bool = False) -> str:
    """Switch the active Gigwa server at runtime — no restart needed.

    Re-points every subsequent tool (and the gigwa:// resources) at *url* for the rest of
    the session. Credentials are **never passed through the chat**: they are resolved from
    the environment — a named *profile* reads GIGWA_USER_<PROFILE>/GIGWA_PASS_<PROFILE>;
    anonymous=True sends none. With neither, the default GIGWA_USER/GIGWA_PASS are used
    **only when reconnecting to the configured GIGWA_URL** — switching to any other server
    without a profile connects anonymously, so your home credentials are never transmitted
    to a different host unasked. The new connection is verified with a live round-trip before
    this returns; on failure the previous connection is restored.
    """
    config = GigwaConfig.for_connection(url, profile=profile, anonymous=anonymous)
    new_client = GigwaClient(config)
    previous = set_client(new_client)
    try:
        summary = _verify_and_describe(new_client)
    except Exception:
        # Roll back to the previous connection and discard the failed one.
        set_client(previous)
        new_client.close()
        raise
    # Success: tear down the old connection's HTTP pool (the singleton is otherwise never closed).
    if previous is not None and previous is not new_client:
        previous.close()
    return "Switched Gigwa connection.\n" + summary


@mcp.tool()
def gigwa_server_info() -> str:
    """Check connectivity to the configured Gigwa server.

    Generates an auth token with the configured credentials and reports the
    server URL and (best-effort) version. Use this first to confirm the
    connection works before importing data.
    """
    return _verify_and_describe(get_client())


def _render_summary(summary: dict[str, Any]) -> str:
    """Render Gigwa's instanceContentSummary.

    Shape (verified on 2.12): top-level keys are positional slots ("Database1",
    ...); each holds scalar fields (``database`` = real name, ``individuals``,
    ``markers``, ``taxon``) plus nested ``ProjectN`` dicts (``name``,
    ``variantType``, ``ploidy``, ``samples``, ``runs``).
    """
    if not summary:
        return "The Gigwa instance currently has no databases."
    lines: list[str] = [f"{len(summary)} database(s):"]
    for slot, db in summary.items():
        if not isinstance(db, dict):
            continue
        name = db.get("database", slot)
        meta: list[str] = []
        if db.get("individuals") is not None:
            meta.append(f"{db['individuals']} individuals")
        if db.get("markers") is not None:
            meta.append(f"{db['markers']} markers")
        if db.get("taxon"):
            meta.append(f"taxon={db['taxon']}")
        lines.append(f"- {name}" + (f" ({', '.join(meta)})" if meta else ""))
        for proj in (v for v in db.values() if isinstance(v, dict)):
            bits: list[str] = []
            vtype = proj.get("variantType")
            if vtype:
                bits.append("/".join(map(str, vtype)) if isinstance(vtype, list) else str(vtype))
            if proj.get("ploidy") is not None:
                bits.append(f"ploidy {proj['ploidy']}")
            if proj.get("samples") is not None:
                bits.append(f"{proj['samples']} samples")
            runs = proj.get("runs") or []
            if runs:
                bits.append("runs: " + ", ".join(map(str, runs)))
            pname = proj.get("name", "?")
            lines.append(f"    - project '{pname}'" + (f" — {'; '.join(bits)}" if bits else ""))
    return "\n".join(lines)


@mcp.tool()
def list_content() -> str:
    """List the databases, projects and runs currently hosted on the Gigwa server."""
    client = get_client()
    summary = client.instance_content_summary()
    return _render_summary(summary)
