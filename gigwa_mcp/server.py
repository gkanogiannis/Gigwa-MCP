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


def set_client(client: GigwaClient | None) -> GigwaClient | None:
    """Install *client* as the process-wide client and return the previous one, if any.

    Backs the runtime connection switch (``gigwa_connect``): every tool and the
    ``gigwa://server/info`` resource resolve the connection through :func:`get_client`,
    so replacing the singleton here re-points them all at once. The displaced client is
    **returned but not closed** — the caller owns its lifecycle, so a failed switch can
    restore it (roll back) and a successful one can close the old HTTP pool. Passing
    ``None`` resets to lazy env construction on the next :func:`get_client`.
    """
    global _client
    previous = _client
    _client = client
    return previous


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
        """The tool's ``_meta`` payload: EDAM operation/topic as **bare id strings**.

        Matches the exact shape other bioinformatics MCP servers use and what directory
        indexers (glama.ai) expect — ``{"edam": {"operation": ["operation_xxxx"],
        "topic": ["topic_xxxx"]}}`` — with no extra keys. The category and human-readable
        EDAM labels are published separately in the ``catalog://tools`` resource.
        """
        return {
            "edam": {
                "operation": [self.operation[0]],
                "topic": [self.topic[0]],
            },
        }


TOOL_CATALOG: dict[str, ToolInfo] = {
    # -- Connection & discovery --
    "gigwa_connect": ToolInfo(
        "Connection & discovery",
        "Switch the active Gigwa server at runtime (credentials resolved from the environment).",
        _HANDLE, _T_DATA),
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


# Descriptions for tool input parameters, applied to each tool's inputSchema (see
# _annotate_input_schemas). Centralised here so the 28 tool signatures stay untouched;
# FastMCP/pydantic only auto-adds a ``title`` per property, so this supplies the
# human-readable ``description`` that hand-written MCP schemas carry.
_PARAM_DESCRIPTIONS: dict[str, str] = {
    "url": "Target Gigwa base URL to connect to (e.g. https://host:port/gigwa); a bare host:port assumes https.",
    "profile": "Optional credential profile: reads GIGWA_USER_<PROFILE>/GIGWA_PASS_<PROFILE> from the environment (never typed in chat). Omit to use the default GIGWA_USER/GIGWA_PASS.",
    "anonymous": "Connect without credentials (Gigwa's anonymous public/read-only access).",
    "variant_set_db_id": "BrAPI variantSetDbId identifying the run (MODULE§project§run); from list_variant_sets / list_content.",
    "method": "Genotype source: 'vcf' (full export, cached) or 'allelematrix' (paged, server-side subset).",
    "max_markers": "Cap the number of markers analysed (evenly-spaced subsample); omit to use all.",
    "max_samples": "Cap the number of samples/callsets sampled (allelematrix path).",
    "region": "Restrict analysis to a genomic window: 'chrom' or 'chrom:start-end' (1-based).",
    "output_dir": "Directory for the output CSV(s) (default ./gigwa_results/<module>/).",
    "output_path": "Destination file path for the export.",
    "module": "Target Gigwa database (module) name.",
    "project": "Target project name within the database.",
    "run": "Target run name within the project.",
    "technology": "Free-text genotyping technology label (e.g. 'DArTseq', 'WGS', 'GBS').",
    "ploidy": "Sample ploidy (default 2).",
    "skip_monomorphic": "Drop non-variant (monomorphic) markers during import.",
    "clear_project_data": "Replace any existing data in the project before importing.",
    "wait": "Block until the import finishes (True) or return a progress token immediately (False).",
    "snp_xlsx": "Path to a DArTseq SNP xlsx report.",
    "silico_xlsx": "Path to a Silico-DArT xlsx report.",
    "vcf_path": "Path to the VCF file (.vcf or .vcf.gz) to import.",
    "reference_fasta": "Path to a reference genome FASTA or a prebuilt minimap2 .mmi index, for genome-anchoring.",
    "positions_csv": "Path to a dartseq_positions.csv (from map_dartseq_to_reference) to reuse instead of re-aligning.",
    "min_mapq": "Minimum mapping quality for a tag to count as uniquely mapped.",
    "preset": "minimap2 preset (default 'sr' for short reads).",
    "backend": "Aligner backend: 'auto' (minimap2 CLI if available, else mappy), 'cli', or 'mappy'.",
    "progress_token": "Progress token returned by an import (import::<user>::<uuid>).",
    "tsv_path": "Path to the metadata TSV file.",
    "metadata_type": "Metadata entity type / id-column name (default 'individual').",
    "validate_first": "Validate the metadata file before importing.",
    "reference_name": "Chromosome/contig name to restrict the search to (see list_sequences).",
    "start": "Region start position, 1-based inclusive.",
    "end": "Region end position, 1-based inclusive.",
    "min_maf": "Minimum minor-allele frequency (0-1).",
    "max_maf": "Maximum minor-allele frequency (0-1).",
    "max_missing_data": "Maximum per-variant missing-data fraction (0-1).",
    "max_variants": "Maximum number of matching variants to retrieve.",
    "format": "Export format: VCF, PLINK or Flapjack (availability varies by Gigwa build).",
    "timeout": "Maximum seconds to wait for the export to complete.",
    "min_sample_call_rate": "Flag samples with call rate below this (0-1).",
    "min_marker_call_rate": "Flag markers with call rate below this (0-1).",
    "outlier_sd": "Flag points more than this many standard deviations from the mean.",
    "similarity_threshold": "IBS similarity (0-1) at/above which accessions are grouped as duplicates.",
    "maf_threshold": "Minor-allele-frequency threshold below which markers are flagged.",
    "max_missing": "Maximum per-marker missing-data fraction (0-1) before a marker is flagged.",
    "n_components": "Number of principal components to compute.",
    "top_pairs": "How many most-related sample pairs to report.",
    "groups_json": "JSON object mapping each group name to a list of accession names/ids.",
    "metadata_tsv": "Path to a metadata TSV (import_metadata format) used to define groups.",
    "group_column": "Column in the metadata TSV holding the group/population label.",
    "id_column": "Column in the metadata TSV holding the individual/accession id (default 'individual').",
    "size": "Explicit core-collection size (number of accessions); overrides fraction.",
    "fraction": "Core-collection size as a fraction of all accessions (default 0.1).",
    "k_min": "Smallest number of clusters (K) to evaluate.",
    "k_max": "Largest number of clusters (K) to evaluate.",
    "het_threshold": "Mean observed-heterozygosity above which a run is flagged BROKEN (mis-called heterozygotes).",
    "complete_call_rate": "Call-rate above which a run is flagged as suspiciously complete (no missing data).",
    "monomorphic_threshold": "Monomorphic-marker fraction above which a run is flagged for low informativeness.",
}


def _strip_titles(node: object) -> None:
    """Recursively remove every ``title`` key from a JSON-schema dict/list, in place."""
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for value in node:
            _strip_titles(value)


def _normalize_tool_schemas() -> None:
    """Shape every tool's schemas like a hand-written MCP tool.

    FastMCP/pydantic derive schemas from the function signature and add ``title`` keys
    (top-level and per-property) but no parameter ``description``. This removes every
    ``title`` from both the input and output schemas, attaches descriptions to input
    properties from :data:`_PARAM_DESCRIPTIONS`, and sets ``additionalProperties: false``
    on the inputSchema. The outputSchema is kept (like the reference servers), just
    title-free.
    """
    for tool in mcp._tool_manager.list_tools():
        schema = tool.parameters
        if isinstance(schema, dict):
            _strip_titles(schema)
            schema.setdefault("additionalProperties", False)
            for pname, pschema in (schema.get("properties") or {}).items():
                if not isinstance(pschema, dict):
                    continue
                desc = _PARAM_DESCRIPTIONS.get(pname)
                if desc and "description" not in pschema:
                    pschema["description"] = desc
        _strip_titles(tool.output_schema)


_apply_catalog_meta()
_normalize_tool_schemas()


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
    """Configured connection info — the target URL and auth mode.

    Deliberately makes **no network call**: reading a resource must be side-effect-free,
    and the server should not generate outbound traffic during directory inspection. Use
    the ``gigwa_server_info`` tool to actually test the live connection and fetch the
    server version.
    """
    try:
        client = get_client()  # constructs the client only; opens no connection
        auth = "anonymous (public/read-only)" if client.anonymous else f"user '{client.config.username}'"
        return (
            f"Gigwa server: {client.config.base_url}\n"
            f"REST base: {client.rest}\n"
            f"Authentication: {auth}\n"
            "(Run the gigwa_server_info tool to test the live connection and version.)"
        )
    except Exception as exc:  # noqa: BLE001 - resource read must not raise
        return f"Gigwa not configured: {exc}"


# Register the workflow prompts (attaches their @mcp.prompt() functions to `mcp`).
from . import prompts  # noqa: E402,F401

__all__ = ["mcp", "get_client", "progress_tool", "TOOL_CATALOG", "ToolInfo", "catalog_drift"]
