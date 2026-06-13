"""Diversity / population-structure tools (read-only): summary, PCA, kinship, Fst.

Each tool loads the variant set's genotypes once (cached), computes the statistic
in Python (scikit-allel / numpy), writes full tables as CSV, and returns a concise
summary. Nothing is written back to Gigwa.
"""

from __future__ import annotations

import json

import allel
import numpy as np
import pandas as pd

from ..analysis import genebank, load_genotypes, stats
from ..analysis.results import resolve_output_dir, write_csv
from ..server import get_client, mcp


@mcp.tool()
def diversity_summary(
    variant_set_db_id: str,
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Per-marker diversity statistics (MAF, He, Ho, PIC) and dataset means.

    He is Nei's gene diversity (1 - Σpᵢ²), Ho is observed heterozygosity, PIC is
    polymorphism information content. Writes ``diversity_markers.csv``. For large sets
    pass ``method="allelematrix"`` + ``max_markers`` to sample server-side.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    ac = gm.gt.count_alleles()
    maf_v = stats.maf(ac)
    he = stats.expected_heterozygosity(ac)
    pic_v = stats.pic(ac)
    n_called_m = np.asarray(gm.gt.is_called().sum(axis=1), dtype=float)
    n_het_m = np.asarray(gm.gt.is_het().sum(axis=1), dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        ho = np.where(n_called_m > 0, n_het_m / n_called_m, np.nan)

    df = pd.DataFrame({
        "variant_id": gm.variant_ids, "chrom": gm.chrom, "pos": gm.pos,
        "maf": maf_v, "he": he, "ho": ho, "pic": pic_v,
    })
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "diversity_markers.csv")

    def m(x):
        return float(np.nanmean(x))

    he_mean, ho_mean = m(he), m(ho)
    # Fis = 1 - Ho/He. Strongly negative => excess heterozygosity (possible encoding
    # issue or contamination); strongly positive => inbreeding / Wahlund effect.
    fis = 1 - ho_mean / he_mean if he_mean > 0 else float("nan")
    note = ""
    if np.isfinite(fis) and fis < -0.3:
        note = (
            f"\n⚠ Fis={fis:.2f} (Ho≫He) indicates strong excess heterozygosity — "
            "check the genotype encoding / DArT import before interpreting diversity."
        )
    return (
        f"Diversity summary for {variant_set_db_id} "
        f"({gm.n_variants} markers × {gm.n_samples} samples)\n"
        f"Mean MAF={m(maf_v):.3f}, He={he_mean:.3f}, Ho={ho_mean:.3f}, "
        f"PIC={m(pic_v):.3f}, Fis={fis:.3f}\n"
        f"Polymorphic markers (MAF>0): {int(np.nansum(maf_v > 0))}{note}\n"
        f"File: {path}"
    )


@mcp.tool()
def diversity_pca(
    variant_set_db_id: str,
    n_components: int = 10,
    max_markers: int | None = None,
    outlier_sd: float = 6.0,
    metadata_tsv: str | None = None,
    group_column: str | None = None,
    id_column: str = "individual",
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Principal component analysis of population structure.

    Runs PCA on the alt-allele dosage matrix (monomorphic markers dropped, missing
    mean-imputed, Patterson scaling). Writes ``pca_coords.csv`` (per-sample PC
    coordinates) and reports variance explained plus any PC1/PC2 outlier samples
    (beyond ``outlier_sd`` SD). Pass ``metadata_tsv`` + ``group_column`` to add a
    ``group`` column (population label per sample) for colouring the PC plot. For large
    sets pass ``method="allelematrix"`` + ``max_markers`` to avoid a full VCF export.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    gn = stats.imputed_alt_counts(gm.gt, drop_invariant=True)
    if gn.shape[0] < 2:
        return f"Not enough polymorphic markers for PCA in {variant_set_db_id}."

    k = int(min(n_components, gm.n_samples - 1, gn.shape[0]))
    coords, model = allel.pca(gn, n_components=k, scaler="patterson")
    evr = np.asarray(model.explained_variance_ratio_)

    cols = {"sample_id": gm.sample_ids, "sample_name": gm.sample_names}
    for j in range(k):
        cols[f"PC{j + 1}"] = coords[:, j]
    df = pd.DataFrame(cols)

    # Flag outliers on PC1/PC2.
    flags = []
    for _, r in df.iterrows():
        bad = []
        for pc in ("PC1", "PC2"):
            col = df[pc]
            sd = col.std()
            if sd > 0 and abs(r[pc] - col.mean()) > outlier_sd * sd:
                bad.append(pc)
        flags.append(",".join(bad))
    df["outlier"] = flags

    group_line = ""
    if metadata_tsv and group_column:
        gmap = _sample_group_map(metadata_tsv, group_column, id_column)
        df["group"] = [
            gmap.get(str(n)) or gmap.get(str(s)) or ""
            for n, s in zip(gm.sample_names, gm.sample_ids)
        ]
        counts = df[df["group"] != ""]["group"].value_counts().to_dict()
        sizes = ", ".join(f"{g}={n}" for g, n in counts.items()) or "(none matched)"
        group_line = f"\nGroups ({group_column}): {sizes}"

    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "pca_coords.csv")

    var_line = ", ".join(f"PC{j + 1}={evr[j] * 100:.1f}%" for j in range(min(k, 5)))
    outliers = df[df["outlier"] != ""]["sample_name"].tolist()
    return (
        f"PCA for {variant_set_db_id} ({gn.shape[0]} polymorphic markers × {gm.n_samples} samples)\n"
        f"Variance explained: {var_line}\n"
        f"PC1/PC2 outliers ({len(outliers)}): {', '.join(outliers[:20]) or '(none)'}"
        f"{group_line}\n"
        f"File: {path}"
    )


@mcp.tool()
def diversity_kinship(
    variant_set_db_id: str,
    max_markers: int | None = None,
    top_pairs: int = 15,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """VanRaden genomic relationship (kinship) matrix.

    Computes G = ZZ'/(2 Σp(1-p)) from alt dosage. Writes the full matrix as
    ``kinship_matrix.csv`` (samples × samples) and reports the most-related pairs
    and the diagonal (self-relationship / inbreeding) range. For large sets pass
    ``method="allelematrix"`` + ``max_markers`` to avoid a full VCF export.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    dosage = stats.alt_dosage(gm.gt)
    grm = stats.vanraden_grm(dosage)

    names = gm.sample_names
    mat = pd.DataFrame(grm, index=names, columns=names)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(mat, out, "kinship_matrix.csv", index=True)

    n = grm.shape[0]
    iu = np.triu_indices(n, k=1)
    off = grm[iu]
    order = np.argsort(off)[::-1][:top_pairs]
    pair_lines = "\n".join(
        f"    {names[iu[0][k]]} ~ {names[iu[1][k]]}: {off[k]:.3f}" for k in order
    )
    diag = np.diag(grm)
    return (
        f"Kinship (VanRaden GRM) for {variant_set_db_id} ({gm.n_variants} markers × {n} samples)\n"
        f"Off-diagonal: mean={off.mean():.3f}, max={off.max():.3f}\n"
        f"Diagonal (self): mean={diag.mean():.3f}, range=[{diag.min():.3f}, {diag.max():.3f}]\n"
        f"Top related pairs:\n{pair_lines}\n"
        f"File: {path}"
    )


def _sample_group_map(tsv_path: str, group_column: str, id_column: str = "individual") -> dict[str, str]:
    """Read a metadata TSV (the ``import_metadata`` format) into {individual -> group}."""
    df = pd.read_csv(tsv_path, sep="\t", dtype=str)
    for col in (id_column, group_column):
        if col not in df.columns:
            raise ValueError(f"Column '{col}' not in {tsv_path} (have: {list(df.columns)}).")
    return {
        str(k): str(v)
        for k, v in zip(df[id_column], df[group_column])
        if v is not None and str(v).strip() and str(v).lower() != "nan"
    }


def _groups_from_tsv(gm, tsv_path: str, group_column: str, id_column: str = "individual") -> dict[str, list[int]]:
    """Build {group: [sample indices]} by matching a metadata TSV to loaded samples."""
    gmap = _sample_group_map(tsv_path, group_column, id_column)
    groups: dict[str, list[int]] = {}
    for i, (sid, sname) in enumerate(zip(gm.sample_ids, gm.sample_names)):
        g = gmap.get(str(sname)) or gmap.get(str(sid))
        if g:
            groups.setdefault(g, []).append(i)
    return groups


def _resolve_groups(gm, groups_json: str | None) -> dict[str, list[int]]:
    """Map a {group: [accession names/ids]} JSON object to sample-index lists."""
    mapping = json.loads(groups_json)
    name_to_idx: dict[str, int] = {}
    for i, (sid, sname) in enumerate(zip(gm.sample_ids, gm.sample_names)):
        name_to_idx.setdefault(str(sname), i)
        name_to_idx.setdefault(str(sid), i)
    groups: dict[str, list[int]] = {}
    for gname, members in mapping.items():
        idx = [name_to_idx[str(m)] for m in members if str(m) in name_to_idx]
        if idx:
            groups[gname] = idx
    return groups


@mcp.tool()
def diversity_fst(
    variant_set_db_id: str,
    groups_json: str | None = None,
    metadata_tsv: str | None = None,
    group_column: str | None = None,
    id_column: str = "individual",
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Pairwise Weir & Cockerham Fst between groups of samples.

    Define the groups one of two ways:
    - ``groups_json`` — a JSON object mapping each group name to a list of accession
      names (or callset ids), e.g. ``{"north": ["112","156"], "south": ["11","42"]}``.
    - ``metadata_tsv`` + ``group_column`` — read groups from a metadata TSV (the same
      file format used by ``import_metadata``), keyed on ``id_column`` (default
      ``individual``) and grouped by ``group_column``.

    Writes ``fst_pairwise.csv`` with the Fst for every group pair. (Server-side BrAPI
    attributes are not used for grouping — that endpoint is unavailable on the target
    Gigwa 2.12 build.)
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    if groups_json:
        groups = _resolve_groups(gm, groups_json)
    elif metadata_tsv and group_column:
        groups = _groups_from_tsv(gm, metadata_tsv, group_column, id_column)
    else:
        return (
            "No groups specified. Provide either groups_json={\"grp\": [names]} or "
            "metadata_tsv=<path> + group_column=<column>."
        )
    if len(groups) < 2:
        return (
            "Need at least two non-empty groups (matched to sample names/ids). "
            f"Resolved groups: {{ {', '.join(f'{k}:{len(v)}' for k, v in groups.items())} }}"
        )

    names = list(groups)
    rows = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            subpops = [groups[names[i]], groups[names[j]]]
            a, b, c = allel.weir_cockerham_fst(gm.gt, subpops)
            denom = np.nansum(a) + np.nansum(b) + np.nansum(c)
            fst = float(np.nansum(a) / denom) if denom > 0 else float("nan")
            rows.append({"group_a": names[i], "group_b": names[j], "fst": fst})

    df = pd.DataFrame(rows)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "fst_pairwise.csv")

    sizes = ", ".join(f"{k}={len(v)}" for k, v in groups.items())
    pair_lines = "\n".join(f"    {r['group_a']} vs {r['group_b']}: Fst={r['fst']:.4f}" for r in rows)
    return (
        f"Pairwise Fst for {variant_set_db_id} (groups: {sizes})\n"
        f"{pair_lines}\n"
        f"File: {path}"
    )


@mcp.tool()
def diversity_by_group(
    variant_set_db_id: str,
    groups_json: str | None = None,
    metadata_tsv: str | None = None,
    group_column: str | None = None,
    id_column: str = "individual",
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Per-population diversity: He, Ho, Fis, MAF, % polymorphic, allelic richness.

    Define groups the same way as ``diversity_fst`` — either ``groups_json``
    ``{group: [names]}`` or ``metadata_tsv`` + ``group_column``. For each group computes
    n, % polymorphic markers, mean MAF, Nei's He, observed Ho, Fis (1−Ho/He), mean
    observed allelic richness, and **rarefied** allelic richness (rarefied to the
    smallest group's gene-copy count so unequal group sizes are comparable). Writes
    ``diversity_by_group.csv``.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    if groups_json:
        groups = _resolve_groups(gm, groups_json)
    elif metadata_tsv and group_column:
        groups = _groups_from_tsv(gm, metadata_tsv, group_column, id_column)
    else:
        return (
            "No groups specified. Provide either groups_json={\"grp\": [names]} or "
            "metadata_tsv=<path> + group_column=<column>."
        )
    if not groups:
        return "No groups matched the loaded samples (check id_column / names)."

    # Rarefy to the smallest group's diploid gene-copy count for comparability.
    rar_n = max(2 * min(len(idx) for idx in groups.values()), 2)
    rows = []
    for gname, idx in groups.items():
        sub = gm.gt[:, idx]
        ac = sub.count_alleles()
        maf_v = stats.maf(ac)
        he = stats.expected_heterozygosity(ac)
        ho, _ = stats.heterozygosity_per_sample(sub)
        he_mean = float(np.nanmean(he))
        ho_mean = float(np.nanmean(ho)) if ho.size and not np.all(np.isnan(ho)) else float("nan")
        rows.append({
            "group": gname,
            "n": len(idx),
            "pct_polymorphic": float(np.nanmean(np.nan_to_num(maf_v, nan=0.0) > 0.0)),
            "mean_maf": float(np.nanmean(maf_v)),
            "he": he_mean,
            "ho": ho_mean,
            "fis": (1.0 - ho_mean / he_mean) if he_mean > 0 else float("nan"),
            "allelic_richness": float(np.nanmean(stats.allelic_richness(ac))),
            "rarefied_ar": float(np.nanmean(stats.rarefied_allelic_richness(ac, rar_n))),
        })

    df = pd.DataFrame(rows).sort_values("he", ascending=False).reset_index(drop=True)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "diversity_by_group.csv")

    lines = "\n".join(
        f"    {r['group']} (n={r['n']}): He={r['he']:.3f} Ho={r['ho']:.3f} "
        f"Fis={r['fis']:.2f} MAF={r['mean_maf']:.3f} AR={r['allelic_richness']:.2f} "
        f"rAR={r['rarefied_ar']:.2f}"
        for _, r in df.iterrows()
    )
    return (
        f"Per-group diversity for {variant_set_db_id} "
        f"({gm.n_variants} markers, {len(groups)} groups; rarefied to {rar_n} gene copies)\n"
        f"{lines}\n"
        f"File: {path}"
    )


@mcp.tool()
def diversity_core_collection(
    variant_set_db_id: str,
    size: int | None = None,
    fraction: float = 0.1,
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Select a core collection that maximises captured allelic diversity.

    Greedy allele-coverage selection (Core-Hunter style): repeatedly add the accession
    that contributes the most not-yet-captured marker-alleles. Pick the core ``size``
    directly, or as ``fraction`` of all accessions (default 10%). Writes
    ``core_collection.csv`` (rank, accession, cumulative allele coverage) and reports the
    fraction of total allelic diversity the core captures.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    presence = genebank.allele_presence(gm.gt)
    total_units = int(presence.any(axis=0).sum())  # alleles present in ≥1 accession
    k = int(size) if size else max(1, int(round(fraction * gm.n_samples)))
    k = min(k, gm.n_samples)

    selected, cumulative = genebank.greedy_core(presence, k)
    rows = [
        {
            "rank": i + 1,
            "sample_name": gm.sample_names[s],
            "sample_id": gm.sample_ids[s],
            "cumulative_alleles": int(cumulative[i]),
            "coverage_fraction": float(cumulative[i] / total_units) if total_units else float("nan"),
        }
        for i, s in enumerate(selected)
    ]
    df = pd.DataFrame(rows)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "core_collection.csv")

    captured = int(cumulative[-1]) if len(cumulative) else 0
    frac = captured / total_units if total_units else float("nan")
    return (
        f"Core collection for {variant_set_db_id} "
        f"({gm.n_variants} markers × {gm.n_samples} accessions)\n"
        f"Core of {len(selected)} accessions captures {captured}/{total_units} "
        f"marker-alleles ({frac:.1%} of total diversity)\n"
        f"First picks: {', '.join(gm.sample_names[s] for s in selected[:10])}\n"
        f"File: {path}"
    )


@mcp.tool()
def diversity_structure(
    variant_set_db_id: str,
    k_min: int = 2,
    k_max: int = 10,
    max_markers: int | None = None,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """Lightweight population-structure clustering (PCA + K-means, in-Python).

    Reduces the alt-dosage matrix with PCA (Patterson scaling), then runs K-means for
    K in ``k_min..k_max`` and picks the K with the highest pseudo-F (Calinski-Harabasz)
    between/within variance ratio — a clear maximum when groups are well separated.
    Writes ``structure_clusters.csv`` (sample, assigned cluster at the best K, PC coords)
    and reports the chosen K with cluster sizes. (No external ADMIXTURE binary — computed
    entirely in Python, consistent with the rest of the analysis layer.)
    """
    from scipy.cluster.vq import kmeans2

    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    gn = stats.imputed_alt_counts(gm.gt, drop_invariant=True)
    if gn.shape[0] < 2 or gm.n_samples < 4:
        return f"Not enough polymorphic markers / samples for structure in {variant_set_db_id}."

    n_pc = int(min(10, gm.n_samples - 1, gn.shape[0]))
    coords, _ = allel.pca(gn, n_components=n_pc, scaler="patterson")
    total_ss = float(((coords - coords.mean(axis=0)) ** 2).sum())

    n = coords.shape[0]
    best = None  # (pseudo_f, k, labels)
    per_k = []
    for k in range(max(2, k_min), min(k_max, n - 1) + 1):
        centroids, labels = kmeans2(coords, k, minit="++", seed=0, missing="warn")
        if len(np.unique(labels)) < k or np.isnan(centroids).any():
            # Empty cluster(s): K is too large for the data — don't select it.
            per_k.append((k, float("-inf")))
            continue
        within = float(((coords - centroids[labels]) ** 2).sum())
        between = total_ss - within
        pseudo_f = (between / (k - 1)) / (within / (n - k)) if within > 0 and n > k else float("inf")
        per_k.append((k, pseudo_f))
        # Iterate K ascending and use strict `>` so the smallest K wins on ties.
        if best is None or pseudo_f > best[0]:
            best = (pseudo_f, k, labels)

    _, best_k, labels = best
    cols = {"sample_id": gm.sample_ids, "sample_name": gm.sample_names, "cluster": labels}
    for j in range(min(n_pc, 5)):
        cols[f"PC{j + 1}"] = coords[:, j]
    df = pd.DataFrame(cols)
    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = write_csv(df, out, "structure_clusters.csv")

    counts = np.array([int((labels == c).sum()) for c in range(best_k)])
    sizes = ", ".join(f"K{c}={n}" for c, n in enumerate(counts))
    f_line = ", ".join(f"K={k}:F={f:.1f}" for k, f in per_k)
    # Degenerate clustering (one giant cluster + singletons) means there is no clear
    # discrete structure — pseudo-F is just rewarding the peeling-off of outliers. Warn
    # rather than present a misleading K (common on low-MAF / continuously-varying data).
    note = ""
    if counts.max() > 0.5 * gm.n_samples and (counts == 1).sum() >= 2:
        note = (
            "\n⚠ One cluster holds most samples while others are singletons — there is "
            "likely no clear discrete structure here (pseudo-F is driven by outliers). "
            "Treat K as exploratory and inspect the PCA / per-K table instead."
        )
    return (
        f"Population structure for {variant_set_db_id} "
        f"({gn.shape[0]} polymorphic markers × {gm.n_samples} samples)\n"
        f"Suggested K={best_k} (highest pseudo-F); cluster sizes: {sizes}\n"
        f"pseudo-F by K (inspect for an elbow; rerun with k_min=k_max to fix K): {f_line}"
        f"{note}\n"
        f"File: {path}"
    )


@mcp.tool()
def diversity_tree(
    variant_set_db_id: str,
    max_markers: int | None = 5000,
    method: str = "vcf",
    output_dir: str | None = None,
) -> str:
    """UPGMA dendrogram of accessions from IBS allele-sharing distance (Newick).

    Builds a pairwise IBS similarity matrix, converts to distance (1 − IBS), and writes a
    UPGMA tree as ``tree.nwk`` (standard Newick, loadable in FigTree / iTOL / ape). Marker
    subsampling (``max_markers``) keeps it tractable on large sets.
    """
    client = get_client()
    gm = load_genotypes(client, variant_set_db_id, max_markers=max_markers or None, method=method)
    sim = stats.ibs_similarity(stats.alt_dosage(gm.gt))
    dist = 1.0 - sim
    newick = genebank.upgma_newick(dist, list(gm.sample_names))

    out = resolve_output_dir(variant_set_db_id, output_dir)
    path = out / "tree.nwk"
    path.write_text(newick + "\n")
    return (
        f"UPGMA tree for {variant_set_db_id} "
        f"({gm.n_variants} markers × {gm.n_samples} accessions)\n"
        f"Mean pairwise IBS distance: {float(np.nanmean(dist[np.triu_indices(gm.n_samples, 1)])):.3f}\n"
        f"File: {path}"
    )
