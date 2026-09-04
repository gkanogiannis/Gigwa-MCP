"""Server-side variant search, sequence listing, and multi-format export tools.

These wrap Gigwa's server-side filtering (GA4GH ``variants/search``), reference/contig
listing (GA4GH ``references/search``) and BrAPI export, so callers can count/filter/export
without pulling a whole variant set through the analysis layer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from ..analysis.genotypes import parse_region
from ..analysis.results import resolve_output_dir, write_csv
from ..exports import ExportSelection
from ..server import get_client, mcp, progress_tool

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _variant_row(v: dict) -> dict:
    """Flatten a GA4GH Variant dict into id/chrom/pos/ref/alt columns."""
    alt = v.get("alternateBases")
    if isinstance(alt, (list, tuple)):
        alt = ",".join(str(a) for a in alt)
    return {
        "variant_id": v.get("variantDbId") or v.get("id"),
        "chrom": v.get("referenceName"),
        "pos": v.get("start"),
        "ref": v.get("referenceBases"),
        "alt": alt,
    }


@mcp.tool()
def count_variants(
    variant_set_db_id: str,
    reference_name: str | None = None,
    start: int | None = None,
    end: int | None = None,
    min_maf: float | None = None,
    max_maf: float | None = None,
    max_missing_data: float | None = None,
) -> str:
    """Count variants matching filters, computed server-side (nothing is downloaded).

    Fast way to size a query before pulling data. Filter by genomic region
    (``reference_name`` + optional ``start``/``end``, from ``list_sequences``), minor-
    allele frequency (``min_maf``/``max_maf``) and/or ``max_missing_data`` (0–1 fraction).
    With no filters this returns the total variant count of the set. ``variant_set_db_id``
    is a BrAPI variantSetDbId (from ``list_variant_sets`` / ``list_content``).
    """
    client = get_client()
    n = client.count_variants(
        variant_set_db_id,
        reference_name=reference_name,
        start=start,
        end=end,
        min_maf=min_maf,
        max_maf=max_maf,
        max_missing_data=max_missing_data,
    )
    filt = []
    if reference_name:
        region = reference_name + (f":{start or ''}-{end or ''}" if (start or end) else "")
        filt.append(f"region={region}")
    if min_maf is not None:
        filt.append(f"MAF≥{min_maf}")
    if max_maf is not None:
        filt.append(f"MAF≤{max_maf}")
    if max_missing_data is not None:
        filt.append(f"missing≤{max_missing_data}")
    where = f" ({', '.join(filt)})" if filt else ""
    return f"{n} variant(s) match in {variant_set_db_id}{where}."


@progress_tool()
def search_variants(
    variant_set_db_id: str,
    reference_name: str | None = None,
    start: int | None = None,
    end: int | None = None,
    min_maf: float | None = None,
    max_maf: float | None = None,
    max_missing_data: float | None = None,
    max_variants: int = 100000,
    output_dir: str | None = None,
) -> str:
    """Search variants matching filters server-side and write the matching list to CSV.

    Same filters as ``count_variants`` (region / MAF / missing-data). Returns variant
    metadata only (id, chrom, pos, ref, alt) — no genotypes are fetched — and writes
    ``variant_search.csv``. Use ``count_variants`` first to size the result; ``max_variants``
    caps how many are retrieved. For downstream genotype analysis on a filtered subset, use
    the ``region``/``min_maf`` options on the QC/diversity tools instead.
    """
    client = get_client()
    variants = client.search_variants(
        variant_set_db_id,
        reference_name=reference_name,
        start=start,
        end=end,
        min_maf=min_maf,
        max_maf=max_maf,
        max_missing_data=max_missing_data,
        max_variants=max_variants,
    )
    if not variants:
        return f"No variants matched the filters in {variant_set_db_id}."

    df = pd.DataFrame(_variant_row(v) for v in variants)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "variant_search.csv")

    by_chrom = df["chrom"].value_counts().head(10).to_dict()
    chrom_line = ", ".join(f"{c}={n}" for c, n in by_chrom.items()) or "(none)"
    return (
        f"Found {len(df)} variant(s) in {variant_set_db_id}\n"
        f"By sequence (top 10): {chrom_line}\n"
        f"File: {path}"
    )


@mcp.tool()
def list_sequences(variant_set_db_id: str) -> str:
    """List the reference sequences (chromosomes/contigs) available in a variant set.

    Use this to discover valid ``reference_name`` values for the region filters on
    ``count_variants`` / ``search_variants`` / the QC & diversity tools.
    """
    client = get_client()
    refs = client.list_sequences(variant_set_db_id)
    if not refs:
        return f"No sequences reported for {variant_set_db_id}."
    lines = []
    for r in refs[:200]:
        name = r.get("name") or r.get("referenceName") or r.get("md5checksum")
        length = r.get("length")
        lines.append(f"  {name}" + (f" ({length:,} bp)" if isinstance(length, int) else ""))
    extra = f"\n  … (+{len(refs) - 200} more)" if len(refs) > 200 else ""
    return f"{len(refs)} sequence(s) in {variant_set_db_id}:\n" + "\n".join(lines) + extra


@mcp.tool()
def list_variant_sets() -> str:
    """List every variant set (run) with its exact BrAPI variantSetDbId.

    The other tools take a ``variant_set_db_id``; this returns those ids directly (plus
    name and variant/callset counts when the server provides them), complementing the
    human-readable database/project/run view from ``list_content``.
    """
    client = get_client()
    sets = client.list_variantsets()
    if not sets:
        return "No variant sets found on the Gigwa instance."
    lines = []
    for vs in sets:
        vid = vs.get("variantSetDbId")
        name = vs.get("variantSetName") or ""
        vc, cc = vs.get("variantCount"), vs.get("callSetCount")
        meta = []
        if vc is not None:
            meta.append(f"{vc} variants")
        if cc is not None:
            meta.append(f"{cc} callsets")
        lines.append(f"  {vid}" + (f" — {name}" if name else "") + (f" ({', '.join(meta)})" if meta else ""))
    return f"{len(sets)} variant set(s):\n" + "\n".join(lines)


@mcp.tool()
def list_export_formats() -> str:
    """List the export formats this Gigwa build supports, with per-format compatibility info.

    Queried live from the server's export-handler registry rather than a hardcoded list, so
    it reflects exactly what this build/instance offers. For each format shows the variant
    types it accepts and the ploidy levels it supports — check this before picking a
    ``format`` for ``export_genotypes``: several formats (e.g. EIGENSTRAT, FASTA, NEXUS,
    PHYLIP, PCA, ASD, JUKES-CANTOR) are SNP-only and/or diploid-only, silently dropping
    INDEL/MIXED sites or rejecting other-ploidy runs rather than erroring.
    """
    client = get_client()
    formats = client.get_export_formats()
    if not formats:
        return "No export formats reported by this Gigwa instance."
    lines = []
    for name, info in sorted(formats.items()):
        types = info.get("supportedVariantTypes") or "any"
        ploidy = info.get("supportedPloidyLevels") or "any"
        ext = info.get("dataFileExtensions") or ""
        desc = _HTML_TAG_RE.sub("", info.get("desc") or "").strip()
        if len(desc) > 160:
            desc = desc[:157].rstrip() + "..."
        lines.append(
            f"  {name}: variant types={types}, ploidy={ploidy}"
            + (f", extension(s)={ext}" if ext else "")
            + (f" — {desc}" if desc else "")
        )
    return f"{len(formats)} export format(s) on this instance:\n" + "\n".join(lines)


@progress_tool()
def export_genotypes(
    variant_set_db_id: str,
    output_path: str,
    format: str = "VCF",
    region: str | None = None,
    selected_variant_types: str | None = None,
    min_maf: float | None = None,
    max_maf: float | None = None,
    max_missing_data: float | None = None,
    individuals: list[str] | None = None,
    metadata_fields: list[str] | None = None,
    keep_on_server: bool = False,
    wait: bool = True,
    timeout: float = 1800.0,
) -> str:
    """Export a variant set — or a filtered/selected subset of it — to a file.

    With none of the filter/selection parameters set, exports the whole set via Gigwa's
    plain per-format export (``format`` one of ``VCF`` (default), ``PLINK`` or
    ``FLAPJACK``; availability varies by build — check ``list_export_formats`` for what
    this instance actually offers, and each format's variant-type/ploidy restrictions).

    Passing any of ``region``, ``selected_variant_types``, ``min_maf``/``max_maf``,
    ``max_missing_data``, ``individuals`` or ``metadata_fields`` instead drives Gigwa's
    selection-aware export — the same endpoint the Gigwa web UI uses for a filtered
    download — which additionally accepts any format the server advertises, including the
    bgzipped ``"VCF.gz"``. ``keep_on_server`` leaves a copy in the user's Gigwa temp-output
    area after this downloads it (default False).

    For large sets this can take a while; raise ``timeout`` (seconds), or set
    ``wait=False`` to return immediately once the export is kicked off instead of blocking
    for the whole thing. A server that queues the work returns a download URL; check progress
    with ``get_export_progress`` and, once it reports complete, retrieve the file with
    ``fetch_export_file`` (that URL, plus ``output_path``). ``wait=False`` always goes
    through the selection-aware endpoint, even with no filters set. Some Gigwa builds
    return the completed bytes immediately; in that case they are written to
    ``output_path`` and no progress/fetch step is needed.
    """
    client = get_client()
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    filtered = any([
        region, selected_variant_types, min_maf is not None, max_maf is not None,
        max_missing_data is not None, individuals, metadata_fields, keep_on_server,
    ])
    chrom, start, end = parse_region(region) if region else (None, None, None)

    if not wait:
        result = client.start_export_result(
            variant_set_db_id,
            selection=ExportSelection(
                fmt=format,
                reference_name=chrom,
                start=start,
                end=end,
                selected_variant_types=selected_variant_types,
                min_maf=min_maf,
                max_maf=max_maf,
                max_missing_data=max_missing_data,
                exported_individuals=individuals,
                metadata_fields=metadata_fields,
                keep_on_server=keep_on_server,
            ),
            immediate_dest_path=dest,
        )
        if result.completed_path is not None:
            size = result.completed_path.stat().st_size
            media = f"; media type {result.media_type}" if result.media_type else ""
            return (
                f"Export completed immediately -> {result.completed_path} "
                f"({size:,} bytes{media}); no progress polling or fetch is needed."
            )
        export_url = result.download_url
        return (
            f"Export started for {variant_set_db_id} as {format} (not waiting).\n"
            f"Check status with get_export_progress().\n"
            f'Once complete: fetch_export_file(download_url="{export_url}", output_path="{output_path}").'
        )

    if filtered:
        written = client.export_selection(
            variant_set_db_id,
            dest,
            fmt=format,
            reference_name=chrom,
            start=start,
            end=end,
            selected_variant_types=selected_variant_types,
            min_maf=min_maf,
            max_maf=max_maf,
            max_missing_data=max_missing_data,
            exported_individuals=individuals,
            metadata_fields=metadata_fields,
            keep_on_server=keep_on_server,
            timeout=timeout,
        )
    else:
        written = client.export_data(variant_set_db_id, dest, fmt=format, timeout=timeout)

    size = written.stat().st_size if written.is_file() else 0
    detail = (
        f"\nFilters: region={region or 'all'}, types={selected_variant_types or 'all'}, "
        f"individuals={len(individuals) if individuals else 'all'}"
        if filtered else ""
    )
    return f"Exported {variant_set_db_id} as {format} -> {written} ({size:,} bytes).{detail}"


@mcp.tool()
def get_export_progress() -> str:
    """Report the status of the current session's most recent export (started with
    ``export_genotypes(..., wait=False)``).

    Unlike imports, an export isn't tracked by a token you pass around — Gigwa ties it to
    the session's own auth token, so there is exactly one "current export" per connection
    and this takes no arguments.
    """
    client = get_client()
    status = client.export_progress()
    if status is None:
        return "No export progress reported (none started this session, or it already finished)."
    if status.error:
        return f"Export failed: {status.error}"
    if status.aborted:
        return "Export was aborted on the server."
    if status.complete:
        return "Export complete — retrieve it with fetch_export_file."
    return f"Export in progress: {status.summary()}"


@mcp.tool()
def fetch_export_file(download_url: str, output_path: str) -> str:
    """Download a completed export — the URL ``export_genotypes(..., wait=False)`` returned
    — once ``get_export_progress`` reports it complete."""
    client = get_client()
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = client.download_export(download_url, dest)
    size = written.stat().st_size if written.is_file() else 0
    return f"Downloaded export -> {written} ({size:,} bytes)."
