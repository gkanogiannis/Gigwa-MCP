"""Individual-metadata tools: validate and import per-individual attributes."""

from __future__ import annotations

from pathlib import Path

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
