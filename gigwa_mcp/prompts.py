"""Workflow prompts: reusable, argument-driven guides that walk the assistant through a
multi-tool Gigwa task (import + QC, a diversity report, region scans, …).

Registered via ``@mcp.prompt()`` and surfaced through the MCP ``prompts/list`` /
``prompts/get`` calls (and shown as the server's "Prompts" in directories like glama.ai).
Each returns a single user-message string that chains the relevant tools.
"""

from __future__ import annotations

from .server import mcp


@mcp.prompt()
def import_and_qc(
    data_path: str,
    module: str,
    project: str,
    run: str,
    reference: str = "",
) -> str:
    """Import a genotype dataset (DArTseq xlsx or VCF) into Gigwa, then run standard QC."""
    ref = (
        f" Anchor markers to the reference at '{reference}' — for a DArTseq report, align the"
        " tag sequences with map_dartseq_to_reference first (or pass reference_fasta)."
        if reference
        else ""
    )
    return (
        f"Import the dataset at '{data_path}' into Gigwa database '{module}', project "
        f"'{project}', run '{run}'.\n"
        f"1. Use import_dartseq for a DArTseq SNP/Silico xlsx report, or import_vcf for a "
        f".vcf/.vcf.gz.{ref}\n"
        "2. Confirm the imported counts with list_variant_sets.\n"
        "3. Run QC: qc_call_rate, qc_heterozygosity, qc_maf_filter and qc_duplicate_accessions.\n"
        "4. Run audit_import_quality on the new run to catch genotype-encoding artifacts.\n"
        "5. Summarise flagged samples/markers and whether the import looks clean."
    )


@mcp.prompt()
def diversity_report(
    variant_set_db_id: str,
    metadata_tsv: str = "",
    group_column: str = "",
) -> str:
    """Produce a population diversity / structure report for a variant set."""
    grouped = bool(metadata_tsv and group_column)
    g = (
        f" Use metadata_tsv='{metadata_tsv}' with group_column='{group_column}' for the "
        "group-aware steps."
        if grouped
        else ""
    )
    lines = [
        f"Produce a population-diversity report for variant set '{variant_set_db_id}'.{g}",
        "1. diversity_summary — dataset-wide MAF/He/Ho/PIC/Fis.",
        "2. diversity_pca and diversity_structure — describe structure (variance explained, suggested K).",
        "3. diversity_kinship — relatedness; highlight the closest pairs.",
        "4. diversity_tree — a UPGMA dendrogram.",
    ]
    if grouped:
        lines.append(
            "5. diversity_by_group and diversity_fst — compare within/between-group diversity."
        )
    lines.append(
        "Interpret the results together and flag any data-quality caveats (e.g. tool warnings)."
    )
    return "\n".join(lines)


@mcp.prompt()
def qc_triage(variant_set_db_id: str) -> str:
    """Run the full QC suite on a variant set and give a go/no-go verdict."""
    return (
        f"Triage the quality of variant set '{variant_set_db_id}'.\n"
        "1. qc_call_rate — overall call rate and the worst samples/markers.\n"
        "2. qc_heterozygosity — flag contamination/off-types (heed the high-Ho warning).\n"
        "3. qc_duplicate_accessions — find clones / mislabelled duplicates.\n"
        "4. qc_maf_filter — how many markers a MAF/missingness filter would drop.\n"
        "5. audit_import_quality — confirm there is no genotype-encoding artifact.\n"
        "Give a go/no-go verdict for downstream diversity analysis, with the key numbers."
    )


@mcp.prompt()
def explore_instance() -> str:
    """Get an overview of the whole Gigwa instance and flag anything that needs attention."""
    return (
        "Give me an overview of this Gigwa instance.\n"
        "1. gigwa_server_info — confirm connectivity, version and user.\n"
        "2. list_content and list_variant_sets — enumerate databases/projects/runs and sizes.\n"
        "3. audit_import_quality (whole instance) — flag any badly imported runs.\n"
        "Summarise what's available and anything that needs attention."
    )


@mcp.prompt()
def region_scan(variant_set_db_id: str, region: str) -> str:
    """Characterise variants and diversity within one genomic region."""
    return (
        f"Investigate region '{region}' in variant set '{variant_set_db_id}'.\n"
        "1. list_sequences — confirm the contig name.\n"
        f"2. count_variants for '{region}', then again with min_maf=0.05 to see common variants.\n"
        "3. search_variants — list the matching variants (writes a CSV).\n"
        f"4. diversity_summary and qc_call_rate with region='{region}' — characterise just that window.\n"
        "Report variant density, common vs rare, and diversity in the region."
    )
