"""Server-side variant search, sequence listing, and multi-format export tools.

These wrap Gigwa's server-side filtering (GA4GH ``variants/search``), reference/contig
listing (GA4GH ``references/search``) and BrAPI export, so callers can count/filter/export
without pulling a whole variant set through the analysis layer.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..analysis.results import resolve_output_dir, write_csv
from ..server import get_client, mcp, progress_tool


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


@progress_tool()
def export_genotypes(
    variant_set_db_id: str,
    output_path: str,
    format: str = "VCF",
    timeout: float = 1800.0,
) -> str:
    """Export a variant set to a file in the given format.

    ``format`` is one of Gigwa's export formats. Which are available depends on the Gigwa
    build — ``VCF`` (default), ``PLINK`` and ``FLAPJACK`` are commonly supported; others
    (``HAPMAP``, ``DARWIN``, …) may not be, in which case the tool reports the formats this
    instance actually offers. The export runs server-side and is streamed to
    ``output_path``. For large sets this can take a while; raise ``timeout`` (seconds).
    """
    client = get_client()
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = client.export_data(variant_set_db_id, dest, fmt=format, timeout=timeout)
    size = written.stat().st_size if written.is_file() else 0
    return (
        f"Exported {variant_set_db_id} as {format.upper()} -> {written} "
        f"({size:,} bytes)."
    )
