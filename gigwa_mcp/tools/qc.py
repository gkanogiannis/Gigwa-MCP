"""Quality-control tools (read-only): call rate, heterozygosity, duplicates, MAF.

Each tool loads the variant set's genotypes once (cached), computes per-sample /
per-marker statistics, writes full tables as CSV under the output directory, and
returns a concise summary. Nothing is written back to Gigwa.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..analysis import load_genotypes
from ..analysis import stats
from ..analysis.results import resolve_output_dir, write_csv
from ..server import get_client, mcp


def _worst(df: pd.DataFrame, col: str, n: int, ascending: bool = True) -> str:
    head = df.sort_values(col, ascending=ascending).head(n)
    return "\n".join(
        f"    {r['sample_name'] if 'sample_name' in r else r['variant_id']}: {r[col]:.3f}"
        for _, r in head.iterrows()
    )


@mcp.tool()
def qc_call_rate(
    variant_set_db_id: str,
    min_sample_call_rate: float = 0.5,
    min_marker_call_rate: float = 0.5,
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Per-sample and per-marker call rate (missingness) QC for a variant set.

    Flags samples/markers below the given thresholds. Writes
    ``call_rate_samples.csv`` and ``call_rate_markers.csv`` and returns a summary
    with the overall call rate and the worst offenders. ``variant_set_db_id`` is a
    BrAPI variantSetDbId (from ``list_content`` / BrAPI variantsets). For large
    production sets pass ``method="allelematrix"`` with ``max_markers`` (e.g. 20000)
    to estimate from a server-side marker subset instead of a full VCF export.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    cr_s = stats.call_rate_per_sample(gm.gt)
    cr_m = stats.call_rate_per_marker(gm.gt)

    samples = pd.DataFrame({
        "sample_id": gm.sample_ids,
        "sample_name": gm.sample_names,
        "call_rate": cr_s,
        "below_threshold": cr_s < min_sample_call_rate,
    })
    markers = pd.DataFrame({
        "variant_id": gm.variant_ids,
        "chrom": gm.chrom,
        "pos": gm.pos,
        "call_rate": cr_m,
        "below_threshold": cr_m < min_marker_call_rate,
    })

    out = resolve_output_dir(variant_set_db_id, output_dir)
    sp = write_csv(samples, out, "call_rate_samples.csv")
    mp = write_csv(markers, out, "call_rate_markers.csv")

    n_bad_s = int(samples["below_threshold"].sum())
    n_bad_m = int(markers["below_threshold"].sum())
    return (
        f"Call-rate QC for {variant_set_db_id} ({gm.n_variants} markers × {gm.n_samples} samples)\n"
        f"Overall call rate: {float(gm.gt.is_called().mean()):.3f}\n"
        f"Samples below {min_sample_call_rate}: {n_bad_s}/{gm.n_samples}\n"
        f"{_worst(samples, 'call_rate', 10)}\n"
        f"Markers below {min_marker_call_rate}: {n_bad_m}/{gm.n_variants}\n"
        f"Files: {sp}, {mp}"
    )


@mcp.tool()
def qc_heterozygosity(
    variant_set_db_id: str,
    outlier_sd: float = 3.0,
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Per-sample observed heterozygosity QC, flagging outliers.

    High Ho relative to the cohort suggests contamination or off-types; very low
    Ho suggests selfed/inbred or duplicated material. Flags samples more than
    ``outlier_sd`` standard deviations from the mean. Writes
    ``heterozygosity_samples.csv``. For large sets pass ``method="allelematrix"`` +
    ``max_markers`` to avoid a full VCF export.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    ho, n_called = stats.heterozygosity_per_sample(gm.gt)

    mean, sd = float(np.nanmean(ho)), float(np.nanstd(ho))
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (ho - mean) / sd if sd > 0 else np.zeros_like(ho)
    flag = np.where(np.abs(z) > outlier_sd, np.where(z > 0, "high", "low"), "")

    df = pd.DataFrame({
        "sample_id": gm.sample_ids,
        "sample_name": gm.sample_names,
        "heterozygosity": ho,
        "n_called": n_called.astype(int),
        "z_score": z,
        "flag": flag,
    })
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "heterozygosity_samples.csv")

    flagged = df[df["flag"] != ""]
    lines = "\n".join(
        f"    {r['sample_name']}: Ho={r['heterozygosity']:.3f} ({r['flag']}, z={r['z_score']:.1f})"
        for _, r in flagged.sort_values("z_score").iterrows()
    )
    # Absolute sanity check: a cohort mean Ho this high is biologically implausible
    # for diploids and usually points to a genotype-encoding problem (e.g. a DArT
    # 2-row report importing most calls as heterozygous), not real biology.
    note = ""
    if mean > 0.6:
        note = (
            f"\n⚠ Cohort mean Ho={mean:.2f} is implausibly high — likely a genotype-"
            "encoding issue in the source data (e.g. DArT 2-row import) rather than "
            "true biology. Review the import before trusting downstream analyses."
        )
    return (
        f"Heterozygosity QC for {variant_set_db_id}\n"
        f"Mean Ho={mean:.3f}, SD={sd:.3f}; flagged {len(flagged)}/{gm.n_samples} "
        f"samples beyond {outlier_sd} SD\n"
        f"{lines or '    (none)'}{note}\n"
        f"File: {path}"
    )


def _union_find(n: int, pairs: list[tuple[int, int]]) -> dict[int, list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return {root: members for root, members in groups.items() if len(members) > 1}


@mcp.tool()
def qc_duplicate_accessions(
    variant_set_db_id: str,
    similarity_threshold: float = 0.95,
    max_markers: int | None = 5000,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Detect duplicate / clonal accessions via pairwise identity-by-state (IBS).

    Computes IBS allele-sharing similarity between every pair of samples and groups
    pairs at or above ``similarity_threshold`` into duplicate sets — the core
    genebank "cleaning" check for mislabelled duplicates and clones. By default
    subsamples to ``max_markers`` evenly-spaced markers for speed (set to 0/None to
    use all). Writes ``duplicate_pairs.csv`` and ``duplicate_groups.csv``. For large
    sets pass ``method="allelematrix"`` to fetch the marker subset without a full export.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    dosage = stats.alt_dosage(gm.gt)
    sim = stats.ibs_similarity(dosage)

    n = gm.n_samples
    iu = np.triu_indices(n, k=1)
    pair_sim = sim[iu]
    keep = np.where(pair_sim >= similarity_threshold)[0]
    pairs = [(int(iu[0][k]), int(iu[1][k])) for k in keep]

    pairs_df = pd.DataFrame([
        {
            "sample_a": gm.sample_names[a],
            "sample_b": gm.sample_names[b],
            "sample_a_id": gm.sample_ids[a],
            "sample_b_id": gm.sample_ids[b],
            "ibs_similarity": float(sim[a, b]),
        }
        for a, b in pairs
    ])

    groups = _union_find(n, pairs)
    group_rows = []
    for gi, (_, members) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), 1):
        for m in members:
            group_rows.append({
                "group_id": gi,
                "sample_name": gm.sample_names[m],
                "sample_id": gm.sample_ids[m],
            })
    groups_df = pd.DataFrame(group_rows, columns=["group_id", "sample_name", "sample_id"])

    out = resolve_output_dir(variant_set_db_id, output_dir)
    pp = write_csv(pairs_df, out, "duplicate_pairs.csv")
    gp = write_csv(groups_df, out, "duplicate_groups.csv")

    group_lines = []
    for gi, members in enumerate(sorted(groups.values(), key=len, reverse=True)[:15], 1):
        shown = [gm.sample_names[m] for m in members[:30]]
        extra = f" … (+{len(members) - 30} more)" if len(members) > 30 else ""
        group_lines.append(f"    group {gi} ({len(members)}): {', '.join(shown)}{extra}")

    # Degenerate clustering (one group swallowing most samples) means the markers
    # can't tell samples apart — usually low informativeness / excess heterozygosity,
    # not hundreds of real duplicates.
    largest = max((len(m) for m in groups.values()), default=0)
    note = ""
    if largest > 0.25 * gm.n_samples:
        note = (
            f"\n⚠ One group contains {largest}/{gm.n_samples} samples — this is almost "
            "certainly a data-quality artifact (low marker informativeness / excess "
            "heterozygosity), not real duplicates. Run qc_heterozygosity and review the import."
        )
    return (
        f"Duplicate detection for {variant_set_db_id} "
        f"({gm.n_variants} markers × {gm.n_samples} samples, IBS ≥ {similarity_threshold})\n"
        f"{len(pairs)} duplicate pair(s) in {len(groups)} group(s):\n"
        f"{chr(10).join(group_lines) or '    (none)'}{note}\n"
        f"Files: {pp}, {gp}"
    )


@mcp.tool()
def qc_maf_filter(
    variant_set_db_id: str,
    maf_threshold: float = 0.05,
    max_missing: float = 0.5,
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Report markers that would be filtered by MAF / missingness (no changes applied).

    Computes per-marker minor-allele frequency and missing rate, and counts how many
    markers are monomorphic, below ``maf_threshold``, or above ``max_missing`` missing.
    Writes ``marker_filter_stats.csv``. This is a report only — it does not modify Gigwa.
    For large sets pass ``method="allelematrix"`` + ``max_markers`` to sample server-side.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    ac = gm.gt.count_alleles()
    maf_v = stats.maf(ac)
    missing = 1.0 - stats.call_rate_per_marker(gm.gt)

    monomorphic = np.nan_to_num(maf_v, nan=0.0) <= 0.0
    low_maf = (~monomorphic) & (maf_v < maf_threshold)
    high_missing = missing > max_missing
    would_remove = monomorphic | low_maf | high_missing

    df = pd.DataFrame({
        "variant_id": gm.variant_ids,
        "chrom": gm.chrom,
        "pos": gm.pos,
        "maf": maf_v,
        "missing_rate": missing,
        "monomorphic": monomorphic,
        "low_maf": low_maf,
        "high_missing": high_missing,
        "would_remove": would_remove,
    })
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "marker_filter_stats.csv")

    return (
        f"MAF/missingness filter report for {variant_set_db_id} ({gm.n_variants} markers)\n"
        f"Monomorphic: {int(monomorphic.sum())}\n"
        f"MAF < {maf_threshold}: {int(low_maf.sum())}\n"
        f"Missing > {max_missing}: {int(high_missing.sum())}\n"
        f"Would remove (union): {int(would_remove.sum())} -> "
        f"{int((~would_remove).sum())} markers retained\n"
        f"File: {path}"
    )
