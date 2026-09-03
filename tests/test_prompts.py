"""Prompts, resources and advertised capabilities (the glama Capabilities/Prompts/Resources
sections come straight from these MCP protocol responses)."""

from __future__ import annotations

import asyncio

import gigwa_mcp.server as server

EXPECTED_PROMPTS = {
    "import_and_qc",
    "diversity_report",
    "qc_triage",
    "explore_instance",
    "region_scan",
}
EXPECTED_RESOURCES = {
    "catalog://tools",
    "gigwa://server/info",
}


def test_prompts_registered():
    prompts = asyncio.run(server.mcp.list_prompts())
    assert {p.name for p in prompts} == EXPECTED_PROMPTS
    # every prompt carries a description (shown in prompts/list)
    assert all(p.description for p in prompts)


def test_prompt_renders_with_arguments():
    got = asyncio.run(
        server.mcp.get_prompt("region_scan", {"variant_set_db_id": "VS§1§r", "region": "chr1:1-100"})
    )
    text = got.messages[0].content.text
    assert "chr1:1-100" in text and "VS§1§r" in text
    assert "count_variants" in text  # it chains the relevant tools


def test_resources_registered():
    resources = asyncio.run(server.mcp.list_resources())
    assert {str(r.uri) for r in resources} == EXPECTED_RESOURCES


def test_capabilities_advertise_all_sections():
    caps = server.mcp._lowlevel_server.create_initialization_options().capabilities
    assert caps.tools is not None
    assert caps.prompts is not None
    assert caps.resources is not None
