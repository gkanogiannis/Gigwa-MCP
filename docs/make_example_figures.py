"""Generate the example figures embedded in the README from a SYNTHETIC dataset.

No live Gigwa server and no real data are used — a small structured genotype matrix is
simulated so the figures are illustrative and dataset-agnostic. The plots mirror what
you would produce from the tools' CSV/Newick outputs (see the README "Visualizing
results" section for the matching copy-paste recipes).

    pip install -e ".[viz]"
    python docs/make_example_figures.py
"""
from __future__ import annotations

from pathlib import Path

import allel
import numpy as np
from scipy.cluster.hierarchy import dendrogram, linkage
from scipy.cluster.vq import kmeans2
from scipy.spatial.distance import squareform

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from gigwa_mcp.analysis import genebank, stats  # noqa: E402

OUT = Path(__file__).resolve().parent / "img"
OUT.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(7)


def simulate(n_markers=400, sizes=(60, 45, 30)):
    """Three populations with differing allele frequencies + a little missingness."""
    groups, blocks = [], []
    for gi, n in enumerate(sizes):
        # population-specific alt-allele frequency per marker
        p = rng.beta(1.2 + 0.6 * gi, 1.2 + 0.6 * (len(sizes) - gi), size=n_markers)
        a0 = (rng.random((n_markers, n)) < p[:, None]).astype("i1")
        a1 = (rng.random((n_markers, n)) < p[:, None]).astype("i1")
        gt = np.stack([a0, a1], axis=2)
        miss = rng.random((n_markers, n)) < 0.04
        gt[miss] = -1
        blocks.append(gt)
        groups += [f"pop{gi + 1}"] * n
    arr = np.concatenate(blocks, axis=1)
    return allel.GenotypeArray(arr), np.array(groups)


gt, groups = simulate()
uniq = sorted(set(groups))
colors = {g: c for g, c in zip(uniq, plt.cm.tab10.colors)}
gcol = [colors[g] for g in groups]
n = gt.shape[1]


def save(fig, name):
    fig.tight_layout()
    fig.savefig(OUT / name, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print("wrote", OUT / name)


# 1) PCA (pca_coords.csv) ------------------------------------------------------
gn = stats.imputed_alt_counts(gt, drop_invariant=True)
coords, model = allel.pca(gn, n_components=4, scaler="patterson")
evr = model.explained_variance_ratio_ * 100
fig, ax = plt.subplots(figsize=(5, 4))
for g in uniq:
    m = groups == g
    ax.scatter(coords[m, 0], coords[m, 1], s=22, alpha=0.8, label=g, color=colors[g])
ax.set_xlabel(f"PC1 ({evr[0]:.1f}%)")
ax.set_ylabel(f"PC2 ({evr[1]:.1f}%)")
ax.set_title("PCA of population structure")
ax.legend(title="group", frameon=False)
save(fig, "pca.png")

# 2) Kinship heatmap (kinship_matrix.csv) --------------------------------------
grm = stats.vanraden_grm(stats.alt_dosage(gt))
order = np.argsort(groups, kind="stable")
fig, ax = plt.subplots(figsize=(5, 4.2))
im = ax.imshow(grm[np.ix_(order, order)], cmap="viridis", aspect="auto")
ax.set_title("VanRaden kinship (GRM)")
ax.set_xticks([])
ax.set_yticks([])
fig.colorbar(im, ax=ax, shrink=0.8, label="relatedness")
save(fig, "kinship.png")

# 3) Structure: K-means on PCA (structure_clusters.csv) ------------------------
_, labels = kmeans2(coords, len(uniq), minit="++", seed=0, missing="warn")
fig, ax = plt.subplots(figsize=(5, 4))
sc = ax.scatter(coords[:, 0], coords[:, 1], c=labels, cmap="tab10", s=22, alpha=0.85)
ax.set_xlabel(f"PC1 ({evr[0]:.1f}%)")
ax.set_ylabel(f"PC2 ({evr[1]:.1f}%)")
ax.set_title(f"K-means clusters (K={len(uniq)})")
save(fig, "structure.png")

# 4) Core-collection coverage curve (core_collection.csv) ----------------------
pres = genebank.allele_presence(gt)
selected, cum = genebank.greedy_core(pres, n)
total = int(pres.any(axis=0).sum())
frac = np.array(cum) / total
fig, ax = plt.subplots(figsize=(5, 4))
ax.plot(np.arange(1, len(frac) + 1), frac * 100, color="#1f77b4")
ax.axhline(95, ls="--", lw=0.8, color="grey")
k95 = int(np.argmax(frac >= 0.95) + 1)
ax.axvline(k95, ls="--", lw=0.8, color="grey")
ax.set_xlabel("core size (accessions)")
ax.set_ylabel("% of alleles captured")
ax.set_title(f"Core-collection coverage ( {k95} capture 95% )")
save(fig, "core_collection.png")

# 5) Per-group diversity bars (diversity_by_group.csv) -------------------------
he, ho, ar = [], [], []
for g in uniq:
    idx = [i for i in range(n) if groups[i] == g]
    sub = gt[:, idx]
    ac = sub.count_alleles()
    he.append(float(np.nanmean(stats.expected_heterozygosity(ac))))
    hovals, _ = stats.heterozygosity_per_sample(sub)
    ho.append(float(np.nanmean(hovals)))
    ar.append(float(np.nanmean(stats.allelic_richness(ac))))
x = np.arange(len(uniq))
fig, ax = plt.subplots(figsize=(5.4, 4))
ax.bar(x - 0.25, he, 0.25, label="He")
ax.bar(x, ho, 0.25, label="Ho")
ax.bar(x + 0.25, ar, 0.25, label="allelic richness")
ax.set_xticks(x)
ax.set_xticklabels(uniq)
ax.set_title("Per-group diversity")
ax.legend(frameon=False)
save(fig, "diversity_by_group.png")

# 6) UPGMA tree (tree.nwk) — subsample accessions so the dendrogram is readable -
sub_idx = np.sort(rng.choice(n, size=24, replace=False))
sim = stats.ibs_similarity(stats.alt_dosage(gt[:, sub_idx]))
dist = 1.0 - sim
np.fill_diagonal(dist, 0.0)
z = linkage(squareform(0.5 * (dist + dist.T), checks=False), method="average")
labels6 = [f"{groups[i]}-{i}" for i in sub_idx]
lab_colors = [colors[groups[i]] for i in sub_idx]
fig, ax = plt.subplots(figsize=(6, 5))
dn = dendrogram(z, labels=labels6, orientation="left", ax=ax, color_threshold=0)
ax.set_title("UPGMA tree (IBS distance)")
for lbl in ax.get_ymajorticklabels():
    g = lbl.get_text().split("-")[0]
    lbl.set_color(colors.get(g, "black"))
save(fig, "tree.png")

print("\nAll figures written to", OUT)
