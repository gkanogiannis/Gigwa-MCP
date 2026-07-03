"""FastMCP server instance and shared Gigwa client accessor.

Tool modules import ``mcp`` and ``get_client`` from here and register themselves
via the ``@mcp.tool()`` decorator. Importing this module wires up every tool.

A central :data:`TOOL_CATALOG` annotates each tool with a category and EDAM ontology
terms (operation + topic); after the tool modules are imported these are attached as
each tool's ``_meta`` (so they ride along in ``tools/list``) and published as a
``catalog://tools`` resource for discovery (e.g. by directories such as glama.ai).
"""

from __future__ import annotations

import functools
import inspect
import json
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from .client import GigwaClient
from .config import GigwaConfig
from .progress import reset_reporter, set_reporter

try:
    __version__ = version("gigwa-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

mcp = FastMCP("gigwa")
# Report our package version as the MCP serverInfo version (FastMCP otherwise leaves it
# unset, so clients/registries show the mcp SDK version instead of gigwa-mcp's).
mcp._mcp_server.version = __version__

_client: GigwaClient | None = None


def get_client() -> GigwaClient:
    """Return a process-wide GigwaClient, created lazily from the environment."""
    global _client
    if _client is None:
        _client = GigwaClient(GigwaConfig.from_env())
    return _client


def progress_tool(**tool_kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a *synchronous* tool that can stream progress, like ``@mcp.tool()``.

    The wrapped function keeps its original (sync) body and signature; this decorator
    turns it into an async tool that (1) installs the request's ``ctx.report_progress`` as
    the active :mod:`gigwa_mcp.progress` reporter and (2) runs the body in an AnyIO worker
    thread. The body — and any client/analysis helper it calls — then reports progress by
    calling :func:`gigwa_mcp.progress.notify`, no ``Context`` threading required. Use this
    instead of ``@mcp.tool()`` for long-running tools (imports, exports, genotype loads).
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        ctx_param = inspect.Parameter(
            "ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context
        )
        new_sig = sig.replace(parameters=[*sig.parameters.values(), ctx_param])

        @functools.wraps(fn)
        async def wrapper(*args: Any, ctx: Context | None = None, **kwargs: Any) -> Any:
            token = set_reporter(ctx.report_progress if ctx is not None else None)
            try:
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
            finally:
                reset_reporter(token)

        # functools.wraps copies fn's annotations (no ctx); restore an explicit ctx: Context
        # so FastMCP's get_type_hints-based context detection excludes it from the schema.
        wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
        wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}), "ctx": Context}
        # Expose the original synchronous body (like FastMCP's Tool.fn) so tests / callers
        # can invoke the un-wrapped logic directly without the async/thread machinery.
        wrapper.fn = fn  # type: ignore[attr-defined]
        return mcp.tool(**tool_kwargs)(wrapper)

    return decorator


# --------------------------------------------------------------------------- #
# Tool catalog: category + EDAM ontology terms (id + human label) per tool.    #
# EDAM (https://edamontology.org) operation/topic mappings are best-effort but #
# stable enough for discovery/indexing; labels carry the intent if an id ever  #
# needs correcting. Kept here (not on each decorator) so the catalog is one     #
# scannable table and adding a tool without an entry fails fast (see below).    #
# --------------------------------------------------------------------------- #

_Term = tuple[str, str]  # (EDAM id, label)

# EDAM operations
_RETRIEVE: _Term = ("operation_2422", "Data retrieval")
_HANDLE: _Term = ("operation_2409", "Data handling")
_SEARCH: _Term = ("operation_2421", "Database search")
_GENOTYPING: _Term = ("operation_3196", "Genotyping")
_VARIANT_FILTER: _Term = ("operation_3675", "Variant filtering")
_ALIGN: _Term = ("operation_0292", "Sequence alignment")
_VALIDATE: _Term = ("operation_2428", "Validation")
_STATS: _Term = ("operation_2238", "Statistical calculation")
_CLUSTER: _Term = ("operation_3432", "Clustering")
_PCA: _Term = ("operation_2939", "Principal component visualisation")
_PHYLO: _Term = ("operation_0323", "Phylogenetic tree construction")
_FORMAT: _Term = ("operation_0335", "Formatting")

# EDAM topics
_T_DATA: _Term = ("topic_3071", "Data management")
_T_GENVAR: _Term = ("topic_0199", "Genetic variation")
_T_POPGEN: _Term = ("topic_3056", "Population genetics")
_T_GENOPHENO: _Term = ("topic_0625", "Genotype and phenotype")
_T_PHYLO: _Term = ("topic_0084", "Phylogenetics")
_T_MAPPING: _Term = ("topic_0102", "Mapping")


@dataclass(frozen=True)
class ToolInfo:
    category: str
    summary: str
    operation: _Term
    topic: _Term

    def meta(self) -> dict:
        """The tool's ``_meta`` payload: category + EDAM operation/topic (id + label)."""
        return {
            "category": self.category,
            "edam": {
                "operation": [{"id": self.operation[0], "label": self.operation[1]}],
                "topic": [{"id": self.topic[0], "label": self.topic[1]}],
            },
        }


TOOL_CATALOG: dict[str, ToolInfo] = {
    # -- Connection & discovery --
    "gigwa_server_info": ToolInfo(
        "Connection & discovery", "Check connectivity/auth and report the server version.",
        _RETRIEVE, _T_DATA),
    "list_content": ToolInfo(
        "Connection & discovery", "List databases, projects and runs on the instance.",
        _RETRIEVE, _T_DATA),
    "list_variant_sets": ToolInfo(
        "Connection & discovery", "List every run with its exact BrAPI variantSetDbId.",
        _RETRIEVE, _T_GENVAR),
    "list_sequences": ToolInfo(
        "Connection & discovery", "List the chromosomes/contigs of a variant set.",
        _RETRIEVE, _T_MAPPING),
    # -- Import --
    "import_dartseq": ToolInfo(
        "Import", "Call genotypes from DArTseq xlsx report(s) and import as VCF.",
        _GENOTYPING, _T_GENVAR),
    "import_vcf": ToolInfo(
        "Import", "Import a VCF (.vcf/.vcf.gz) into a database/project/run.",
        _GENOTYPING, _T_GENVAR),
    "get_import_progress": ToolInfo(
        "Import", "Report the status of a running import by its progress token.",
        _HANDLE, _T_DATA),
    "abort_import": ToolInfo(
        "Import", "Cancel a running import (or other process) by its progress token.",
        _HANDLE, _T_DATA),
    # -- Reference mapping --
    "map_dartseq_to_reference": ToolInfo(
        "Reference mapping", "Align DArT tag sequences to a reference to infer positions.",
        _ALIGN, _T_MAPPING),
    # -- Metadata --
    "validate_metadata": ToolInfo(
        "Metadata", "Validate an individual-metadata TSV without importing.",
        _VALIDATE, _T_DATA),
    "import_metadata": ToolInfo(
        "Metadata", "Import per-individual attributes into an existing database.",
        _HANDLE, _T_GENOPHENO),
    "get_germplasm_metadata": ToolInfo(
        "Metadata", "Fetch server-stored per-individual (germplasm) attributes.",
        _RETRIEVE, _T_GENOPHENO),
    # -- Variant search --
    "count_variants": ToolInfo(
        "Variant search", "Count variants matching region/MAF/missing filters, server-side.",
        _SEARCH, _T_GENVAR),
    "search_variants": ToolInfo(
        "Variant search", "Search variants server-side and write the matching list to CSV.",
        _SEARCH, _T_GENVAR),
    # -- Export --
    "export_genotypes": ToolInfo(
        "Export", "Export a variant set to a file (VCF/PLINK/Flapjack; varies by build).",
        _FORMAT, _T_GENVAR),
    # -- Quality control --
    "qc_call_rate": ToolInfo(
        "Quality control", "Per-sample & per-marker call rate; flag low-call entities.",
        _STATS, _T_GENVAR),
    "qc_heterozygosity": ToolInfo(
        "Quality control", "Per-sample observed heterozygosity; flag outliers.",
        _STATS, _T_POPGEN),
    "qc_duplicate_accessions": ToolInfo(
        "Quality control", "Group duplicate/clonal accessions via pairwise IBS.",
        _CLUSTER, _T_POPGEN),
    "qc_maf_filter": ToolInfo(
        "Quality control", "Report markers a MAF/missingness filter would remove.",
        _VARIANT_FILTER, _T_GENVAR),
    # -- Diversity & structure --
    "diversity_summary": ToolInfo(
        "Diversity & structure", "Per-marker MAF, He, Ho, PIC + dataset means.",
        _STATS, _T_POPGEN),
    "diversity_pca": ToolInfo(
        "Diversity & structure", "PCA of population structure; variance + PC coordinates.",
        _PCA, _T_POPGEN),
    "diversity_kinship": ToolInfo(
        "Diversity & structure", "VanRaden genomic relationship (kinship) matrix.",
        _STATS, _T_POPGEN),
    "diversity_fst": ToolInfo(
        "Diversity & structure", "Pairwise Weir & Cockerham Fst between sample groups.",
        _STATS, _T_POPGEN),
    "diversity_by_group": ToolInfo(
        "Diversity & structure", "Per-population He/Ho/Fis/MAF and allelic richness.",
        _STATS, _T_POPGEN),
    "diversity_core_collection": ToolInfo(
        "Diversity & structure", "Greedy allele-coverage core collection selection.",
        _STATS, _T_POPGEN),
    "diversity_structure": ToolInfo(
        "Diversity & structure", "Population-structure clustering (PCA + K-means).",
        _CLUSTER, _T_POPGEN),
    "diversity_tree": ToolInfo(
        "Diversity & structure", "UPGMA dendrogram of accessions from IBS distance.",
        _PHYLO, _T_PHYLO),
    # -- Audit --
    "audit_import_quality": ToolInfo(
        "Audit", "Scan runs for genotype-encoding artifacts from bad imports.",
        _VALIDATE, _T_DATA),
}


# Registering the tool modules attaches their @mcp.tool() functions to `mcp`.
from .tools import connection, genotype, metadata  # noqa: E402,F401
from .tools import qc, diversity, audit  # noqa: E402,F401
from .tools import search  # noqa: E402,F401


def _apply_catalog_meta() -> None:
    """Attach each catalog entry as its tool's ``_meta``.

    Idempotent and tolerant of tools not yet registered: the canonical server-first
    import registers every tool before this runs, but a test that imports a single tool
    module can trigger a circular import where a tool is still mid-initialisation — those
    simply get their meta on the next call. ``test_catalog.py`` asserts full parity so a
    new @mcp.tool() without a TOOL_CATALOG entry is still caught.
    """
    for name, info in TOOL_CATALOG.items():
        tool = mcp._tool_manager.get_tool(name)
        if tool is not None:
            tool.meta = info.meta()


def catalog_drift() -> tuple[set[str], set[str]]:
    """Return ``(tools_without_catalog_entry, catalog_entries_without_tool)``."""
    registered = {t.name for t in mcp._tool_manager.list_tools()}
    return registered - TOOL_CATALOG.keys(), TOOL_CATALOG.keys() - registered


_apply_catalog_meta()


@mcp.resource("catalog://tools", name="Tool catalog", mime_type="application/json")
def tool_catalog() -> str:
    """A categorised catalog of every tool with its EDAM operation/topic annotations."""
    categories: dict[str, list[dict]] = {}
    for name, info in TOOL_CATALOG.items():
        categories.setdefault(info.category, []).append(
            {
                "name": name,
                "summary": info.summary,
                "edam_operation": {"id": info.operation[0], "label": info.operation[1]},
                "edam_topic": {"id": info.topic[0], "label": info.topic[1]},
            }
        )
    payload = {
        "server": "gigwa",
        "tool_count": len(TOOL_CATALOG),
        "categories": [
            {"name": cat, "tools": tools} for cat, tools in categories.items()
        ],
    }
    return json.dumps(payload, indent=2)


@mcp.resource("gigwa://server/info", name="Gigwa server info", mime_type="text/plain")
def server_info_resource() -> str:
    """Live connection status: server URL, version and authenticated user."""
    try:
        client = get_client()
        return (
            f"Gigwa {client.server_version() or 'unknown'} at {client.config.base_url}\n"
            f"REST base: {client.rest}\n"
            f"User: {client.config.username}"
        )
    except Exception as exc:  # noqa: BLE001 - resource read must not raise
        return f"Gigwa server not reachable / not configured: {exc}"


@mcp.resource("gigwa://instance/summary", name="Gigwa instance content", mime_type="application/json")
def instance_summary_resource() -> str:
    """Live inventory of the instance (databases → projects → runs), as JSON."""
    try:
        return json.dumps(get_client().instance_content_summary(), indent=2)
    except Exception as exc:  # noqa: BLE001 - resource read must not raise
        return json.dumps({"error": str(exc)})


# Register the workflow prompts (attaches their @mcp.prompt() functions to `mcp`).
from . import prompts  # noqa: E402,F401

__all__ = ["mcp", "get_client", "progress_tool", "TOOL_CATALOG", "ToolInfo", "catalog_drift"]
