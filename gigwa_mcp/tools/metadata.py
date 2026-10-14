"""Individual-metadata tools: validate and import per-individual attributes."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..identifiers import individual_to_sample_names, module_of, sample_name_map as _name_map
from ..analysis.results import resolve_output_dir, write_csv
from ..client import ProgressStatus
from ..errors import GigwaAPIError, GigwaError
from ..server import get_client, mcp, progress_tool
from . import load_json_arg


def _render_validation(result: object) -> str:
    if isinstance(result, list):
        if not result:
            return "Validation passed: no issues found."
        return "Validation reported:\n" + "\n".join(f"  - {item}" for item in result)
    if isinstance(result, dict):
        return "Validation result: " + str(result)
    return f"Validation result: {result}"


@mcp.tool()
def validate_metadata(
    tsv_path: str,
    module: str,
    metadata_type: str = "individual",
) -> str:
    """Validate an individual-metadata file against a Gigwa database without importing.

    ``metadata_type`` is the name of the ID column in the file that links rows to
    genotype entities — for individual metadata this is the ``individual`` column
    (the header must match exactly, case-sensitive). ``tsv_path`` is a TSV whose
    first column header equals ``metadata_type``.
    """
    path = Path(tsv_path)
    if not path.is_file():
        raise ValueError(f"Metadata file not found: {path}")
    client = get_client()
    try:
        result = client.metadata_validation(
            module=module, file_path=path, metadata_type=metadata_type
        )
    except GigwaAPIError as exc:
        return f"Validation failed: {exc}"
    return _render_validation(result)


@progress_tool()
def import_metadata(
    tsv_path: str,
    module: str,
    metadata_type: str = "individual",
    validate_first: bool = True,
) -> str:
    """Import individual metadata (per-individual attributes) into an existing Gigwa database.

    The file is a TSV whose first column header equals ``metadata_type``
    (``individual`` for individual metadata) and whose values match the
    individual/sample names already present in the database. Remaining columns
    become searchable attributes. By default the file is validated first; set
    ``validate_first=False`` to skip that check.
    """
    path = Path(tsv_path)
    if not path.is_file():
        raise ValueError(f"Metadata file not found: {path}")
    client = get_client()

    prefix = ""
    if validate_first:
        try:
            issues = client.metadata_validation(
                module=module, file_path=path, metadata_type=metadata_type
            )
        except GigwaAPIError as exc:
            return f"Aborted: metadata validation failed: {exc}"
        if isinstance(issues, list) and issues:
            prefix = "Validation warnings:\n" + "\n".join(f"  - {i}" for i in issues) + "\n\n"

    try:
        result = client.metadata_import(
            module=module, file_path=path, metadata_type=metadata_type
        )
    except GigwaAPIError as exc:
        return f"{prefix}Metadata import failed: {exc}"

    # Some Gigwa builds return a progress token for async metadata import.
    if isinstance(result, str) and "::" in result and len(result) < 120:
        final = client.wait_for_completion(result)
        status = final.summary() if isinstance(final, ProgressStatus) else str(final)
        return f"{prefix}Metadata import complete ({status})."
    return f"{prefix}Metadata import response: {result}"


def _germplasm_row(g: dict) -> dict:
    """Flatten a BrAPI germplasm record into name + its additionalInfo attributes."""
    row = {
        "germplasm_name": g.get("germplasmName") or g.get("defaultDisplayName"),
        "germplasm_db_id": g.get("germplasmDbId"),
    }
    extra = g.get("additionalInfo")
    if isinstance(extra, dict):
        for k, v in extra.items():
            row[str(k)] = v
    return row


def _callset_metadata(
    callsets: list[dict], variant_set_db_id: str
) -> tuple[list[str], list[dict]]:
    """Flatten callsets into ``(attribute_columns, rows)``.

    Each row carries ``sample_name``, ``callSetName``, ``sampleDbId``, ``callSetDbId`` plus
    every key found across the callsets' ``additionalInfo`` (missing values filled with "").

    ``sample_name`` is resolved with :func:`~gigwa_mcp.identifiers.sample_name_map`, the
    single naming rule the loaded genotype matrix uses (accession recovered from
    ``sampleDbId``, else ``callSetName``, else ``callSetDbId``), so it lines up with
    ``GenotypeMatrix.sample_names`` in the analysis tools. The raw ``callSetName`` is kept
    alongside it because on some builds it is the human-readable label.
    """
    names = _name_map(callsets, variant_set_db_id)
    attr_cols = sorted({k for cs in callsets for k in (cs.get("additionalInfo") or {})})
    rows: list[dict] = []
    for cs in callsets:
        info = cs.get("additionalInfo") or {}
        cid = cs.get("callSetDbId")
        row = {
            "sample_name": names.get(cid) or cs.get("callSetName") or cid,
            "callSetName": cs.get("callSetName"),
            "sampleDbId": cs.get("sampleDbId"),
            "callSetDbId": cid,
        }
        row.update({k: info.get(k, "") for k in attr_cols})
        rows.append(row)
    return attr_cols, rows


@mcp.tool()
def search_callsets(
    variant_set_db_id: str,
    output_dir: str | None = None,
) -> str:
    """Dump per-sample (callset) metadata for a run: names + ``additionalInfo`` attributes.

    Retrieves the run's callsets via BrAPI ``search/callsets`` and writes
    ``sample_metadata.csv`` (one row per sample) with ``sample_name``, ``callSetName``,
    ``sampleDbId``, ``callSetDbId`` and every attribute present in the callsets'
    ``additionalInfo`` (e.g. ICARDA_IG, SeedID, Country, Latitude, Longitude, SiteCode,
    PopulationType).

    ``sample_name`` is the same name the analysis tools use for the sample, so the file
    joins to their outputs; ``callSetName`` keeps the server's raw label, which on some
    builds is the more human-readable of the two.

    This is the sample/callset-level counterpart to ``get_germplasm_metadata``: use it
    when the germplasm (accession) level exposes no attributes but the samples do.
    """
    client = get_client()
    callsets = client.search_callsets(variant_set_db_id)
    if not callsets:
        return f"No callsets found for {module_of(variant_set_db_id)}."
    attr_cols, rows = _callset_metadata(callsets, variant_set_db_id)
    cols = ["sample_name", "callSetName", "sampleDbId", "callSetDbId"] + attr_cols
    df = pd.DataFrame(rows, columns=cols)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "sample_metadata.csv")
    return (
        f"Sample (callset) metadata for {module_of(variant_set_db_id)}: "
        f"{len(df)} sample(s), {len(attr_cols)} attribute(s)\n"
        f"Attributes: {', '.join(attr_cols) or '(none)'}\n"
        f"File: {path}"
    )


def _individual_to_sample_names(client, variant_set_db_id: str) -> dict[str, list[str]]:
    """Map Gigwa's individual id -> the sample name(s) the analysis tools use.

    Gigwa keys individual metadata by the individual id, which is the leading segment of a
    callset's ``callSetName`` (``4970`` in ``4970-1-Run1``). The genotype matrix, however,
    names samples with :func:`~gigwa_mcp.analysis.genotypes._name_map`, which derives them
    from ``sampleDbId``. On real data those two id spaces do not agree, so a join needs this
    bridge. Returns ``{}`` if the callsets cannot be read (the mapping is best-effort).
    """
    try:
        callsets = client.search_callsets(variant_set_db_id)
    except GigwaError:  # metadata is still worth writing without the bridge column
        return {}
    return individual_to_sample_names(callsets, variant_set_db_id)


@mcp.tool()
def get_germplasm_metadata(
    variant_set_db_id: str,
    output_dir: str | None = None,
) -> str:
    """Fetch server-stored per-individual metadata (germplasm attributes) for a database.

    Reads the attributes already stored in Gigwa (imported earlier via ``import_metadata``
    or a BrAPI source) for the module of ``variant_set_db_id`` and writes
    ``germplasm_metadata.csv`` — one row per accession, with attribute columns.

    **Which column to join on.** ``germplasm_name`` is Gigwa's *individual* id, which on
    real data does **not** match the sample names the analysis tools use. Join on the
    ``sample_name`` column instead: it is the name ``diversity_fst`` /
    ``diversity_by_group`` match against (via ``metadata_tsv``, which expects **tab**-
    separated input — convert the CSV first). Where one individual carries several samples
    the column holds them ``;``-joined and needs splitting first. It is blank when the
    callsets could not be read.

    For the sample/callset level itself — raw ``callSetName``, ``sampleDbId`` and per-sample
    ``additionalInfo`` — use ``search_callsets`` instead.
    """
    client = get_client()
    records = client.get_germplasm(variant_set_db_id)
    df = pd.DataFrame(_germplasm_row(g) for g in records) if records else pd.DataFrame()
    attr_cols = [c for c in df.columns if c not in ("germplasm_name", "germplasm_db_id")]

    if not records or not attr_cols:
        return (
            f"No server-stored metadata available for {module_of(variant_set_db_id)} "
            "(the germplasm level exposes no attributes — import metadata with "
            "import_metadata, try search_callsets for sample-level attributes, or supply a "
            "local TSV instead)."
        )

    # Bridge Gigwa's individual id space to the analysis tools' sample names so the file can
    # actually be joined; see _individual_to_sample_names.
    bridge = _individual_to_sample_names(client, variant_set_db_id)
    df.insert(0, "sample_name", [
        ";".join(bridge.get(str(n), [])) for n in df["germplasm_name"]
    ])
    matched = int((df["sample_name"] != "").sum())

    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "germplasm_metadata.csv")
    return (
        f"Germplasm metadata for {module_of(variant_set_db_id)}: "
        f"{len(df)} accession(s), {len(attr_cols)} attribute(s)\n"
        f"Attributes: {', '.join(attr_cols)}\n"
        f"Join on sample_name ({matched}/{len(df)} resolved to analysis sample names); "
        "germplasm_name is Gigwa's individual id and does not match them.\n"
        f"File: {path}"
    )


@mcp.tool()
def list_metadata_values(variant_set_db_id: str) -> str:
    """List the individual-metadata fields available for a database and their distinct values.

    Backed by the endpoint the Gigwa web UI itself uses to populate its metadata-based
    selection filters, so it works even on builds where ``get_germplasm_metadata``'s BrAPI
    fallback returns no attributes. Use this to discover field names/values before calling
    ``filter_individuals_by_metadata``.
    """
    client = get_client()
    module = module_of(variant_set_db_id)
    fields = client.distinct_individual_metadata(module)
    if not fields:
        return f"No individual-metadata fields found for {module}."
    lines = []
    for name, values in fields.items():
        vals = [v for v in values if v]
        if not vals:
            lines.append(f"  {name}: (blank only)")
        elif len(vals) > 20:
            # Near-unique fields (e.g. accession names/ids) would otherwise dump hundreds
            # of values; a sample is enough to see the field is an identifier, not a
            # category, and to use with filter_individuals_by_metadata if actually needed.
            lines.append(f"  {name}: {len(vals)} distinct values, e.g. {', '.join(vals[:20])}, ...")
        else:
            lines.append(f"  {name}: {', '.join(vals)}")
    return f"{len(fields)} metadata field(s) for {module}:\n" + "\n".join(lines)


@mcp.tool()
def filter_individuals_by_metadata(variant_set_db_id: str, filters_json: str | dict) -> str:
    """Select individuals whose stored metadata matches the given field/value filters.

    ``filters_json`` is a JSON object mapping each metadata field name (see
    ``list_metadata_values``) to a list of acceptable values, e.g. ``{"GroupK4": ["cA"]}``.
    Multiple fields combine with AND; multiple values for one field combine with OR.
    Returns the matching **individual**-level identifiers, ready to pass directly to
    ``export_genotypes``'s ``individuals`` parameter. Diversity ``groups_json`` instead
    matches analysis sample names; obtain those from ``get_germplasm_metadata``'s
    ``sample_name`` column. Gigwa's export resolves each individual to all of its samples
    across runs server-side (verified against the Gigwa server source), including when an
    individual has more than one sample.
    """
    client = get_client()
    module = module_of(variant_set_db_id)
    try:
        filters = load_json_arg(filters_json)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"filters_json is not valid JSON: {exc}") from exc
    if not isinstance(filters, dict):
        raise ValueError("filters_json must be a JSON object of {field: [values]}.")

    records = client.filter_individuals_by_metadata(module, filters)
    if not records:
        return f"No individuals in {module} match {filters}."
    names = [str(r.get("id")) for r in records if r.get("id") is not None]
    return f"{len(names)} individual(s) in {module} match {filters}:\n" + ", ".join(names)
