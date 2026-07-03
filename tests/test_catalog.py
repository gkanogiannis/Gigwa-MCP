"""Tool catalog: EDAM metadata coverage and the catalog://tools resource."""

from __future__ import annotations

import asyncio
import json

import gigwa_mcp.server as server


def test_catalog_matches_registered_tools():
    """Every registered tool has a catalog entry and vice versa (no drift)."""
    missing, extra = server.catalog_drift()
    assert not missing, f"tools with no TOOL_CATALOG entry: {sorted(missing)}"
    assert not extra, f"TOOL_CATALOG entries with no tool: {sorted(extra)}"


def test_every_tool_exposes_edam_meta():
    # Re-apply to cover any tool registered after a circular import elsewhere in the run.
    server._apply_catalog_meta()
    for tool in server.mcp._tool_manager.list_tools():
        assert tool.meta, f"{tool.name} has no _meta"
        # _meta is exactly {"edam": {...}} — no extra keys (matches the glama-indexing shape).
        assert set(tool.meta) == {"edam"}, f"{tool.name} _meta has unexpected keys: {set(tool.meta)}"
        edam = tool.meta["edam"]
        assert edam.get("operation") and edam.get("topic"), f"{tool.name} missing EDAM terms"
        # Bare EDAM id strings, not {id, label} objects.
        assert all(isinstance(x, str) for x in edam["operation"] + edam["topic"]), (
            f"{tool.name} EDAM terms must be bare id strings"
        )


def test_catalog_resource_lists_all_tools():
    out = asyncio.run(server.mcp.read_resource("catalog://tools"))
    content = out[0].content if isinstance(out, (list, tuple)) else out
    data = json.loads(content)
    assert data["tool_count"] == len(server.TOOL_CATALOG)
    listed = {t["name"] for cat in data["categories"] for t in cat["tools"]}
    assert listed == set(server.TOOL_CATALOG)
