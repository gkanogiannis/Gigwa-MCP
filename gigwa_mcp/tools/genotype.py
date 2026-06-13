"""Genotype import tools: DArTseq xlsx reports, plain VCF, and progress polling."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from ..importers import convert_dart_to_vcf
from ..importers.refmap import (
    extract_snp_tags,
    map_tags_to_reference,
    positions_to_dataframe,
)
from ..server import get_client, mcp


def _positions_from_rows(rows) -> dict:
    """Build the import positions map from (allele_id, chrom, pos, ref, alt, status) rows.

    Keeps only uniquely-mapped markers and at most one per genomic site: colliding
    markers (redundant tags at the same locus) are dropped so they fall back to the
    ``Unmapped`` contig, avoiding Gigwa's duplicate-variant import rejection.
    """
    positions, seen = {}, set()
    for allele_id, chrom, pos, ref, alt, status in rows:
        if status != "unique":
            continue
        site = (chrom, int(pos))
        if site in seen:
            continue
        seen.add(site)
        positions[str(allele_id)] = (str(chrom), int(pos), str(ref), str(alt))
    return positions


def _wait_and_describe(client, token: str, wait: bool) -> str:
    if not wait:
        return (
            f"Import started (progress token: {token}).\n"
            "Use get_import_progress with this token to follow it."
        )
    updates: list[str] = []

    def on_update(status) -> None:
        line = status.summary()
        if not updates or updates[-1] != line:
            updates.append(line)

    final = client.wait_for_completion(token, on_update=on_update)
    tail = updates[-3:] if updates else []
    body = "\n".join(f"  {u}" for u in tail)
    return (
        f"Import complete (token: {token}).\n"
        f"Final status: {final.summary()}"
        + (f"\nRecent progress:\n{body}" if body else "")
    )


@mcp.tool()
def import_dartseq(
    module: str,
    project: str,
    run: str,
    snp_xlsx: str | None = None,
    silico_xlsx: str | None = None,
    technology: str = "DArTseq",
    ploidy: int = 2,
    skip_monomorphic: bool = False,
    clear_project_data: bool = False,
    reference_fasta: str | None = None,
    positions_csv: str | None = None,
    min_mapq: int = 20,
    wait: bool = True,
) -> str:
    """Import DArTseq data from xlsx report(s) into Gigwa.

    Converts the DArTseq SNP and/or Silico-DArT xlsx report(s) to a standard VCF —
    doing the 2-row genotype calling in Python (so reference homozygotes are not
    mis-imported as heterozygous, as Gigwa's built-in DArT parser does) — and
    uploads it to create/append a database (``module``), ``project`` and ``run``.

    Provide at least one of ``snp_xlsx`` / ``silico_xlsx`` (absolute paths). SNP
    and Silico use different allele models; importing both into the *same* run is
    unusual — prefer separate runs unless you specifically intend to combine them.

    If ``reference_fasta`` is given (a reference genome FASTA *or* a prebuilt
    minimap2 ``.mmi`` index — an ``.mmi`` is loaded directly with no re-indexing,
    preferred for large genomes), the SNP markers' tag sequences are aligned to it
    and uniquely-mapped markers (mapq ≥ ``min_mapq``) are imported genome-anchored
    (real chromosome/position); the rest stay on an ``Unmapped`` contig. Without it,
    all markers go on ``Unmapped``.

    ``positions_csv`` reuses a mapping already produced by
    ``map_dartseq_to_reference`` (its ``dartseq_positions.csv``) instead of
    re-aligning — much faster when you've already inspected the mapping. Provide
    either ``reference_fasta`` or ``positions_csv``, not both.

    Set ``clear_project_data=True`` to replace any existing data in the project,
    ``skip_monomorphic=True`` to drop non-variant markers, and ``wait=False`` to
    return immediately with a progress token instead of blocking until done.
    """
    if not snp_xlsx and not silico_xlsx:
        raise ValueError("Provide at least one of snp_xlsx or silico_xlsx.")
    if reference_fasta and positions_csv:
        raise ValueError("Provide either reference_fasta or positions_csv, not both.")
    if reference_fasta and not Path(reference_fasta).is_file():
        raise ValueError(f"Reference FASTA not found: {reference_fasta}")
    if positions_csv and not Path(positions_csv).is_file():
        raise ValueError(f"positions_csv not found: {positions_csv}")

    csv_positions = None
    if positions_csv:
        df = pd.read_csv(positions_csv)
        csv_positions = _positions_from_rows(
            df[["allele_id", "chrom", "pos", "ref", "alt", "status"]].itertuples(index=False, name=None)
        )

    client = get_client()
    conversions = []
    with tempfile.TemporaryDirectory(prefix="gigwa_dart_") as tmp:
        data_files: list[str] = []
        for label, src in (("SNP", snp_xlsx), ("Silico", silico_xlsx)):
            if not src:
                continue
            src_path = Path(src)
            out = Path(tmp) / (src_path.stem + ".vcf")
            positions = None
            if label == "SNP" and csv_positions is not None:
                positions = csv_positions
            elif label == "SNP" and reference_fasta:
                results, _ = map_tags_to_reference(
                    extract_snp_tags(src_path), reference_fasta, min_mapq=min_mapq
                )
                positions = _positions_from_rows(
                    (r.allele_id, r.chrom, r.pos, r.ref, r.alt, r.status) for r in results
                )
            result = convert_dart_to_vcf(src_path, out, positions=positions)
            conversions.append(result)
            data_files.append(str(out))

        token = client.genotype_import(
            module=module,
            project=project,
            run=run,
            data_files=data_files,
            technology=technology,
            ploidy=ploidy,
            skip_monomorphic=skip_monomorphic,
            clear_project_data=clear_project_data,
        )
        status_text = _wait_and_describe(client, token, wait)

    header = (
        f"Target: database='{module}', project='{project}', run='{run}', "
        f"technology='{technology}', ploidy={ploidy}\n"
        + "\n".join("Converted " + c.summary() for c in conversions)
    )
    return header + "\n\n" + status_text


@mcp.tool()
def import_vcf(
    vcf_path: str,
    module: str,
    project: str,
    run: str,
    technology: str | None = None,
    ploidy: int = 2,
    skip_monomorphic: bool = False,
    clear_project_data: bool = False,
    wait: bool = True,
) -> str:
    """Import a VCF file (``.vcf`` or ``.vcf.gz``) into Gigwa.

    Uploads the VCF to create/append a database (``module``), ``project`` and
    ``run``. ``technology`` is optional free-text (e.g. 'WGS', 'GBS'). Use
    ``clear_project_data=True`` to replace existing project data and ``wait=False``
    to return a progress token instead of blocking.
    """
    path = Path(vcf_path)
    if not path.is_file():
        raise ValueError(f"VCF file not found: {path}")

    client = get_client()
    token = client.genotype_import(
        module=module,
        project=project,
        run=run,
        data_files=[str(path)],
        technology=technology,
        ploidy=ploidy,
        skip_monomorphic=skip_monomorphic,
        clear_project_data=clear_project_data,
    )
    header = (
        f"Uploading {path.name} -> database='{module}', project='{project}', "
        f"run='{run}', ploidy={ploidy}"
    )
    return header + "\n\n" + _wait_and_describe(client, token, wait)


@mcp.tool()
def map_dartseq_to_reference(
    snp_xlsx: str,
    reference_fasta: str,
    output_dir: str | None = None,
    min_mapq: int = 20,
    preset: str = "sr",
    backend: str = "auto",
) -> str:
    """Guess genomic positions for DArTseq SNP markers by aligning their tag sequences.

    Aligns each marker's ~69 bp ``AlleleSequence`` tag to ``reference_fasta`` (a
    reference genome FASTA, or a prebuilt minimap2 ``.mmi`` index) and reports the
    inferred chromosome, position and strand of each SNP. Writes
    ``dartseq_positions.csv`` (allele_id, chrom, pos, strand, mapq, ref, alt, status).
    The result can be passed to ``import_dartseq`` (``reference_fasta=``) to import
    the data genome-anchored instead of on an ``Unmapped`` contig.

    ``backend``: ``"auto"`` uses the minimap2 CLI when available (streams over
    multi-part indexes → bounded RAM, best for large multi-gigabase genomes),
    falling back to the in-process ``mappy`` binding. Markers are classified
    ``unique`` (mapq ≥ ``min_mapq``), ``multi`` (ambiguous), or ``unmapped``.
    """
    snp_path = Path(snp_xlsx)
    if not snp_path.is_file():
        raise ValueError(f"SNP report not found: {snp_path}")
    if not Path(reference_fasta).is_file():
        raise ValueError(f"Reference FASTA / index not found: {reference_fasta}")

    markers = extract_snp_tags(snp_path)
    if not markers:
        raise ValueError("No SNP tags with usable AlleleSequence/SnpPosition found in the report.")
    results, stats = map_tags_to_reference(
        markers, reference_fasta, min_mapq=min_mapq, preset=preset, backend=backend
    )

    out_dir = Path(output_dir) if output_dir else Path.cwd() / "gigwa_results" / snp_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "dartseq_positions.csv"
    positions_to_dataframe(results).to_csv(path, index=False)

    top_chroms = sorted(stats["chromosomes"].items(), key=lambda kv: -kv[1])[:10]
    chrom_lines = "\n".join(f"    {c}: {n}" for c, n in top_chroms) or "    (none)"
    return (
        f"DArTseq → reference mapping for {snp_path.name} ({stats['total']} markers)\n"
        f"Uniquely mapped (mapq ≥ {min_mapq}): {stats['unique']}\n"
        f"Multi/ambiguous: {stats['multi']}\n"
        f"Unmapped: {stats['unmapped']}\n"
        f"Markers per chromosome (top 10):\n{chrom_lines}\n"
        f"File: {path}"
    )


@mcp.tool()
def get_import_progress(progress_token: str) -> str:
    """Report the current status of a running import, given its progress token."""
    client = get_client()
    status = client.progress(progress_token)
    if status is None:
        return (
            "No active progress for this token. The import has either finished and "
            "been cleared, or has not started writing progress yet."
        )
    return status.summary()
