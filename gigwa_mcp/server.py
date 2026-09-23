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
from typing import Any, Callable

import anyio
import httpx
from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import GigwaClient
from .config import GigwaConfig
from .errors import GigwaError
from . import __version__
from .progress import reset_reporter, set_reporter

# ``version`` reaches the MCP serverInfo, so clients and registries report gigwa-mcp's
# version rather than the mcp SDK's. The StreamableHTTP transport mounts at /mcp, which is
# the SDK's own default in v2 and what MCP clients expect.
mcp = MCPServer("gigwa", version=__version__)

# Prefer JSON responses for StreamableHTTP POST requests. Many MCP clients (including the
# Drupal mcp_client integration) only advertise application/json and otherwise hit the SDK's
# 406 Not Acceptable path when the server expects SSE negotiation. In the v2 SDK this is a
# parameter of the app factory rather than a setting, so it is applied in
# :func:`_streamable_http_app` below (and mirrored by tests that build the app directly).
STREAMABLE_HTTP_JSON_RESPONSE = True

# Where the StreamableHTTP transport mounts. This is also the v2 SDK's own default; it is
# named here because the malformed-notification normaliser below has to recognise the path,
# and because tests build the app directly.
STREAMABLE_HTTP_PATH = "/mcp"
# Normalize malformed notification requests from clients that send
# notifications/initialized with an id field. This is tolerated here to
# remain compatible with buggy MCP clients while preserving normal behavior.


# The only body we rewrite (notifications/initialized) is a tiny fixed-shape message, so we
# only buffer bodies up to this size. A larger POST is streamed straight through to the SDK
# untouched — this wrapper must not become an unbounded in-memory buffer sitting in front of
# the transport's Host/DNS-rebinding checks (else a large body could exhaust memory even from
# a disallowed host, before those checks run).
_MAX_NORMALIZE_BODY_BYTES = 64 * 1024


def _normalize_streamable_http_message_body(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return body

    if (
        isinstance(payload, dict)
        and payload.get("method") == "notifications/initialized"
        and "id" in payload
    ):
        payload.pop("id", None)
        return json.dumps(payload).encode("utf-8")

    return body


def _replay_receive(prefix: bytes, more_body: bool, receive: Receive) -> Receive:
    """A receive() that re-emits an already-consumed *prefix*, then defers to *receive*.

    Lets us hand the buffered-so-far bytes plus the untouched remainder of the stream to the
    downstream app without holding the whole body in memory.
    """
    emitted = False

    async def _recv() -> Any:
        nonlocal emitted
        if not emitted:
            emitted = True
            return {"type": "http.request", "body": prefix, "more_body": more_body}
        return await receive()

    return _recv


def _normalize_streamable_http_app(app: ASGIApp) -> ASGIApp:
    async def wrapper(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == STREAMABLE_HTTP_PATH:
            body = bytearray()
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] != "http.request":
                    await app(scope, receive, send)
                    return
                body.extend(message.get("body", b""))
                more_body = message.get("more_body", False)
                if len(body) > _MAX_NORMALIZE_BODY_BYTES:
                    # Far larger than any notification we rewrite: stop buffering and stream
                    # the rest through untouched (bounded memory; no normalization needed).
                    await app(scope, _replay_receive(bytes(body), more_body, receive), send)
                    return

            normalized_body = _normalize_streamable_http_message_body(bytes(body))

            async def receive_with_normalized_body() -> dict[str, Any]:
                return {"type": "http.request", "body": normalized_body, "more_body": False}

            await app(scope, receive_with_normalized_body, send)
            return
        await app(scope, receive, send)

    return wrapper


# Wrap the StreamableHTTP app so malformed MCP notification POSTs are normalized.
# This does not change the underlying SDK behavior for valid clients. The v2 SDK takes the
# transport configuration as app-factory parameters, so pass them through and default
# ``json_response`` to our preference while still letting a caller override it.
mcp._original_streamable_http_app = mcp.streamable_http_app  # type: ignore[attr-defined]


def _streamable_http_app(**kwargs: Any) -> ASGIApp:
    kwargs.setdefault("json_response", STREAMABLE_HTTP_JSON_RESPONSE)
    kwargs.setdefault("streamable_http_path", STREAMABLE_HTTP_PATH)
    return _normalize_streamable_http_app(mcp._original_streamable_http_app(**kwargs))

mcp.streamable_http_app = _streamable_http_app  # type: ignore[attr-defined]

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
            # The SDK reports anything but a ToolError as a crash and hides its text: the
            # model sees only "Error executing tool <name>" -- confirmed live, a
            # diversity_admixture whose export download died gave no hint why. Gigwa and
            # network failures are anticipated, so hand their message through.
            except GigwaError as exc:
                raise ToolError(str(exc)) from exc
            except httpx.TransportError as exc:
                raise ToolError(
                    f"Connection to Gigwa failed ({type(exc).__name__}: {exc or 'no detail'}). "
                    f"Check the network and retry."
                ) from exc
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
    "search_callsets": ToolInfo(
        "Metadata", "Dump per-sample (callset) metadata: names + additionalInfo attributes.",
        _RETRIEVE, _T_GENOPHENO),
    "list_metadata_values": ToolInfo(
        "Metadata", "List individual-metadata field names and their distinct values.",
        _RETRIEVE, _T_GENOPHENO),
    "filter_individuals_by_metadata": ToolInfo(
        "Metadata", "Select individuals matching metadata field/value filters.",
        _SEARCH, _T_GENOPHENO),
    # -- Variant search --
    "count_variants": ToolInfo(
        "Variant search", "Count variants matching region/MAF/missing filters, server-side.",
        _SEARCH, _T_GENVAR),
    "search_variants": ToolInfo(
        "Variant search", "Search variants server-side and write the matching list to CSV.",
        _SEARCH, _T_GENVAR),
    # -- Export --
    "list_export_formats": ToolInfo(
        "Export", "List export formats this instance supports, with type/ploidy compatibility.",
        _RETRIEVE, _T_GENVAR),
    "export_genotypes": ToolInfo(
        "Export", "Export a variant set, or a filtered/selected subset, to a file.",
        _FORMAT, _T_GENVAR),
    "get_export_progress": ToolInfo(
        "Export", "Report the status of the current session's running export.",
        _HANDLE, _T_DATA),
    "fetch_export_file": ToolInfo(
        "Export", "Retrieve a completed export started with export_genotypes(wait=False).",
        _RETRIEVE, _T_DATA),
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
    "diversity_admixture": ToolInfo(
        "Diversity & structure", "Model-based ancestry (Q matrix) via the real ADMIXTURE binary.",
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
    "variant_set_db_id": "BrAPI variantSetDbId identifying the run (MODULE§project§run) -- copy the exact string from list_variant_sets / list_content, never assemble one by hand: the middle segment is a numeric project index, not the project's name, and a wrong guess fails with an opaque HTTP 500 rather than a clear error.",
    "method": "Genotype source: 'vcf' (full export, cached) or 'allelematrix' (paged, server-side subset).",
    "max_markers": "Cap analysis to the first N markers in canonical Gigwa search order; omit to use all.",
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
    "wait": "Block until the job finishes (True, default) or return immediately once it's kicked off (False) -- an import returns a progress token to poll with get_import_progress, an export returns a download URL to poll with get_export_progress and retrieve with fetch_export_file.",
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
    "format": "Export format name, e.g. VCF (default), PLINK, FLAPJACK or VCF.gz; see list_export_formats for what this instance offers (and each format's type/ploidy restrictions).",
    "timeout": "Maximum seconds to wait for the export to complete.",
    "selected_variant_types": "Restrict the export to these variant types, ';'-joined (e.g. 'SNP' or 'SNP;INDEL'); omit for all types.",
    "individuals": "Individual-level identifiers to include in the export (e.g. from filter_individuals_by_metadata), not sample/callset ids; omit for all. Gigwa resolves each individual to all of its samples/callsets across runs server-side, including when an individual has more than one, so no manual sample mapping or dedup is needed.",
    "metadata_fields": "Individual metadata columns to embed in the export (from get_germplasm_metadata); omit for none.",
    "keep_on_server": "Also leave a copy of the export in the user's Gigwa temp-output area after downloading it here.",
    "filters_json": "JSON object mapping each metadata field name to a list of acceptable values, e.g. {\"GroupK4\": [\"cA\"]} (see list_metadata_values for field/value names). Multiple fields AND together; multiple values for one field OR together.",
    "download_url": "Download URL returned by export_genotypes(..., wait=False), once get_export_progress reports the export complete.",
    "export_token": "The export's own token, from export_genotypes(..., wait=False)'s reply -- pins the check to that specific export. Omit only when at most one export is in flight at a time; Gigwa tracks one \"current export\" per token, so with no argument this falls back to the session's shared token and could report a different export's status.",
    "min_sample_call_rate": "Flag samples with call rate below this (0-1).",
    "min_marker_call_rate": "Flag markers with call rate below this (0-1).",
    "outlier_sd": "Flag points more than this many standard deviations from the mean.",
    "similarity_threshold": "IBS similarity (0-1) at/above which accessions are grouped as duplicates.",
    "maf_threshold": "Minor-allele-frequency threshold below which markers are flagged.",
    "max_missing": "Maximum per-marker missing-data fraction (0-1) before a marker is flagged.",
    "n_components": "Number of principal components to compute.",
    "top_pairs": "How many most-related sample pairs to report.",
    "groups_json": "JSON object mapping each group name to a list of accession names/ids.",
    "reference_groups_json": "Supervised ADMIXTURE: JSON object mapping each reference population name to its member individuals, e.g. {\"XI\": [...], \"GJ\": [...]}. Runs once with K = number of groups (k_min/k_max ignored); every other analysed sample is a target. Members are auto-added to individuals.",
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
    "seed": "Random seed for ADMIXTURE's initialization (reproducibility).",
    "threads": "Number of CPU threads ADMIXTURE should use; omit to let it choose.",
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
