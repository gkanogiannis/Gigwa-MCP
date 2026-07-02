"""Individual-metadata tools: validate and import per-individual attributes."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..analysis.genotypes import module_of
from ..analysis.results import resolve_output_dir, write_csv
from ..client import ProgressStatus
from ..errors import GigwaAPIError
from ..server import get_client, mcp


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


@mcp.tool()
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


@mcp.tool()
def get_germplasm_metadata(
    variant_set_db_id: str,
    output_dir: str | None = None,
) -> str:
    """Fetch server-stored per-individual metadata (germplasm attributes) for a database.

    Reads the attributes already stored in Gigwa (imported earlier via ``import_metadata``
    or a BrAPI source) for the module of ``variant_set_db_id``, via BrAPI germplasm. Writes
    ``germplasm_metadata.csv`` (one row per accession, attribute columns) that can be fed
    back to the grouping tools (``diversity_fst`` / ``diversity_by_group`` via
    ``metadata_tsv``). Returns an empty-result note when the Gigwa build does not expose
    germplasm attributes (some 2.12 builds do not).
    """
    client = get_client()
    records = client.get_germplasm(variant_set_db_id)
    if not records:
        return (
            f"No server-stored germplasm metadata available for "
            f"{module_of(variant_set_db_id)} (the Gigwa build may not expose it — "
            "import metadata with import_metadata, or supply a local TSV instead)."
        )
    df = pd.DataFrame(_germplasm_row(g) for g in records)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "germplasm_metadata.csv")
    attr_cols = [c for c in df.columns if c not in ("germplasm_name", "germplasm_db_id")]
    return (
        f"Germplasm metadata for {module_of(variant_set_db_id)}: "
        f"{len(df)} accession(s), {len(attr_cols)} attribute(s)\n"
        f"Attributes: {', '.join(attr_cols) or '(none)'}\n"
        f"File: {path}"
    )
