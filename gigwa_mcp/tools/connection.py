"""Connection / inventory tools: check the server and list its content."""

from __future__ import annotations

from typing import Any

from ..server import get_client, mcp


@mcp.tool()
def gigwa_server_info() -> str:
    """Check connectivity to the configured Gigwa server.

    Generates an auth token with the configured credentials and reports the
    server URL and (best-effort) version. Use this first to confirm the
    connection works before importing data.
    """
    client = get_client()
    version = client.server_version()
    # Force a token round-trip so we fail fast on bad credentials / unreachable host.
    client.instance_content_summary()
    lines = [
        f"Connected to Gigwa at {client.config.base_url}",
        f"REST base: {client.rest}",
        f"Version: {version or 'unknown'}",
        f"User: {client.config.username}",
        "Authentication: OK",
    ]
    return "\n".join(lines)


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
