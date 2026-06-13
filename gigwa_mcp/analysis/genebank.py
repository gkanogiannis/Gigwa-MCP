"""Genebank-specific analysis helpers: core-collection selection and distance trees.

Pure functions over numpy arrays (no I/O), so they unit-test against hand-built inputs
the same way ``stats.py`` does.
"""

from __future__ import annotations

import numpy as np


def allele_presence(gt) -> np.ndarray:
    """Per-accession allele-presence matrix for core-collection coverage.

    Returns a boolean array of shape ``(n_samples, n_variants * n_alleles)``: entry
    ``(s, marker*A + a)`` is True when sample ``s`` carries allele ``a`` at that marker
    (i.e. the allele appears in its called genotype). Missing genotypes contribute no
    presence. This is the unit of "diversity to capture" a core collection maximises.
    """
    # gt: scikit-allel GenotypeArray (n_variants, n_samples, ploidy)
    g = np.asarray(gt)  # (m, n, ploidy)
    m, n, _ = g.shape
    n_alleles = int(g.max()) + 1 if g.size and g.max() >= 0 else 1
    pres = np.zeros((n, m * n_alleles), dtype=bool)
    for a in range(n_alleles):
        has_a = (g == a).any(axis=2)  # (m, n) sample carries allele a at marker
        pres[:, a::n_alleles] = has_a.T
    return pres


def greedy_core(presence: np.ndarray, k: int) -> tuple[list[int], np.ndarray]:
    """Greedily pick ``k`` accessions maximising cumulative allele coverage.

    ``presence`` is a boolean ``(n_samples, n_units)`` matrix (see :func:`allele_presence`).
    At each step the accession adding the most not-yet-covered units is selected (ties →
    lowest index). Returns ``(selected_indices, cumulative_coverage)`` where
    ``cumulative_coverage[i]`` is the number of distinct units captured after the first
    ``i+1`` picks. Core-Hunter-style A-coverage objective.
    """
    presence = np.asarray(presence, dtype=bool)
    n_samples, n_units = presence.shape
    k = int(min(k, n_samples))
    covered = np.zeros(n_units, dtype=bool)
    available = np.ones(n_samples, dtype=bool)
    selected: list[int] = []
    cumulative = np.zeros(k, dtype=int)

    for step in range(k):
        gains = (presence & ~covered).sum(axis=1)
        gains[~available] = -1
        pick = int(np.argmax(gains))
        if gains[pick] <= 0:
            # No accession adds new alleles — coverage saturated; fill the rest with
            # arbitrary remaining accessions so the curve length stays ``k``.
            remaining = np.where(available)[0]
            for r in remaining[: k - step]:
                selected.append(int(r))
                cumulative[len(selected) - 1] = int(covered.sum())
            break
        covered |= presence[pick]
        available[pick] = False
        selected.append(pick)
        cumulative[step] = int(covered.sum())

    return selected, cumulative[: len(selected)]


def upgma_newick(distance: np.ndarray, labels: list[str]) -> str:
    """UPGMA tree from a square distance matrix, returned as a Newick string.

    Uses ``scipy.cluster.hierarchy.linkage`` (average linkage = UPGMA) on the condensed
    distances, then walks the tree emitting branch lengths (ultrametric: a child's branch
    is the parent height minus the child height).
    """
    from scipy.cluster.hierarchy import linkage, to_tree
    from scipy.spatial.distance import squareform

    d = np.asarray(distance, dtype=float)
    d = np.nan_to_num(0.5 * (d + d.T), nan=float(np.nanmax(d)) if np.isfinite(d).any() else 0.0)
    np.fill_diagonal(d, 0.0)
    if d.shape[0] < 2:
        return f"({labels[0] if labels else 'leaf'});"

    z = linkage(squareform(d, checks=False), method="average")
    tree = to_tree(z)

    def emit(node, parent_height: float) -> str:
        height = node.dist
        bl = max(parent_height - height, 0.0)
        if node.is_leaf():
            return f"{labels[node.id]}:{bl:.6g}"
        left = emit(node.get_left(), height)
        right = emit(node.get_right(), height)
        return f"({left},{right}):{bl:.6g}"

    return f"({emit(tree.get_left(), tree.dist)},{emit(tree.get_right(), tree.dist)});"
