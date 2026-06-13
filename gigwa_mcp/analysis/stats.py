"""Pure population-genetics statistics over a scikit-allel GenotypeArray.

These functions take a ``GenotypeArray`` (shape ``(n_variants, n_samples,
ploidy)``) or derived arrays and return numpy results. They do no I/O, so they
are straightforward to unit-test against hand-computed values.
"""

from __future__ import annotations

import numpy as np


def call_rate_per_sample(gt) -> np.ndarray:
    """Fraction of markers called, per sample (length n_samples)."""
    return np.asarray(gt.is_called().mean(axis=0))


def call_rate_per_marker(gt) -> np.ndarray:
    """Fraction of samples called, per marker (length n_variants)."""
    return np.asarray(gt.is_called().mean(axis=1))


def heterozygosity_per_sample(gt) -> tuple[np.ndarray, np.ndarray]:
    """Observed heterozygosity (het/called) and called count, per sample."""
    n_called = np.asarray(gt.is_called().sum(axis=0), dtype=float)
    n_het = np.asarray(gt.is_het().sum(axis=0), dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        ho = np.where(n_called > 0, n_het / n_called, np.nan)
    return ho, n_called


def _allele_freqs(ac) -> np.ndarray:
    ac = np.asarray(ac, dtype=float)
    tot = ac.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        freqs = np.where(tot[:, None] > 0, ac / tot[:, None], np.nan)
    return freqs


def maf(ac) -> np.ndarray:
    """Minor-allele frequency per marker (1 - frequency of the most common allele)."""
    freqs = _allele_freqs(ac)
    return 1.0 - np.nanmax(freqs, axis=1)


def expected_heterozygosity(ac) -> np.ndarray:
    """Nei's gene diversity He = 1 - Σ pᵢ² per marker."""
    freqs = _allele_freqs(ac)
    return 1.0 - np.nansum(freqs**2, axis=1)


def pic(ac) -> np.ndarray:
    """Polymorphism Information Content per marker.

    PIC = 1 - Σ pᵢ² - Σ_{i<j} 2 pᵢ² pⱼ², using
    Σ_{i<j} 2 pᵢ² pⱼ² = (Σ pᵢ²)² - Σ pᵢ⁴.
    """
    freqs = _allele_freqs(ac)
    sum_p2 = np.nansum(freqs**2, axis=1)
    sum_p4 = np.nansum(freqs**4, axis=1)
    cross = sum_p2**2 - sum_p4
    return 1.0 - sum_p2 - cross


def alt_dosage(gt) -> np.ndarray:
    """Alternate-allele dosage matrix (n_variants, n_samples), missing as NaN."""
    gn = np.asarray(gt.to_n_alt(fill=-1), dtype=float)
    gn[gn < 0] = np.nan
    return gn


def ibs_similarity(dosage: np.ndarray) -> np.ndarray:
    """Pairwise identity-by-state similarity among samples from a dosage matrix.

    Similarity = mean over co-called markers of (1 - |dᵢ - dⱼ| / 2), i.e. the
    proportion of shared alleles. Diagonal is set to 1.0. Input dosage is
    (n_variants, n_samples) with NaN for missing (diploid biallelic; dosage 0/1/2).

    Fully vectorised via BLAS matrix products so it scales to thousands of samples:
    for integer dosages, |a−b| = Σ_{k=1,2} |1(a≥k) − 1(b≥k)|, and the pairwise sum of
    a binary threshold indicator P (masked to called genotypes) over co-called markers
    is ``Pᵀ·C + Cᵀ·P − 2·Pᵀ·P`` where C is the called-mask. This avoids the O(n²·m)
    Python loop of the naive implementation.
    """
    called = (~np.isnan(dosage)).astype(np.float64)  # (m, n)
    d = np.nan_to_num(dosage, nan=0.0)
    n_co = called.T @ called  # co-called marker count per pair (n, n)

    abs_sum = np.zeros_like(n_co)
    for k in (1.0, 2.0):
        # P = threshold indicator (dosage >= k), masked to called genotypes (0 elsewhere).
        p = (d >= k).astype(np.float64) * called
        ptc = p.T @ called
        abs_sum += ptc + ptc.T - 2.0 * (p.T @ p)

    with np.errstate(invalid="ignore", divide="ignore"):
        sim = np.where(n_co > 0, 1.0 - 0.5 * abs_sum / n_co, np.nan)
    np.fill_diagonal(sim, 1.0)
    return sim


def allelic_richness(ac) -> np.ndarray:
    """Observed number of distinct alleles per marker (alleles with count > 0)."""
    ac = np.asarray(ac)
    return (ac > 0).sum(axis=1).astype(float)


def rarefied_allelic_richness(ac, n: int) -> np.ndarray:
    """Expected allele count per marker rarefied to ``n`` gene copies.

    El Mousadik & Petit (1996) rarefaction: for each marker with N total gene copies
    and allele counts nᵢ, the expected number of alleles in a random sample of ``n``
    gene copies is Σᵢ [1 − C(N−nᵢ, n) / C(N, n)]. Lets groups of unequal size be
    compared on a common basis. Markers with N < n yield NaN (cannot rarefy that deep).
    Computed in log space (``gammaln``) for numerical stability at large N.
    """
    from scipy.special import gammaln

    ac = np.asarray(ac, dtype=float)
    big_n = ac.sum(axis=1)  # (m,) total gene copies per marker

    def log_comb(a, b):
        # log C(a, b); valid for a >= b >= 0, else -inf (C = 0).
        a = np.asarray(a, dtype=float)
        with np.errstate(invalid="ignore"):
            out = gammaln(a + 1.0) - gammaln(b + 1.0) - gammaln(a - b + 1.0)
        out = np.where(a >= b, out, -np.inf)
        return out

    log_cNn = log_comb(big_n, n)  # (m,)
    # P(allele i absent in sample of n) = C(N-nᵢ, n)/C(N, n); present-prob = 1 - that.
    with np.errstate(invalid="ignore"):
        absent = np.exp(log_comb(big_n[:, None] - ac, n) - log_cNn[:, None])
    absent = np.where(np.isfinite(absent), absent, 0.0)
    present = np.where(ac > 0, 1.0 - absent, 0.0)
    rar = present.sum(axis=1)
    return np.where(big_n >= n, rar, np.nan)


def vanraden_grm(dosage: np.ndarray) -> np.ndarray:
    """VanRaden genomic relationship matrix from an alt-dosage matrix.

    Z = M - 2p, G = ZZ' / (2 Σ p(1-p)). Missing genotypes are imputed to 2p
    (so their centred value is 0). Monomorphic markers are dropped.
    """
    p = np.nanmean(dosage, axis=1) / 2.0  # (n_variants,)
    keep = np.isfinite(p) & (p > 0) & (p < 1)
    d = dosage[keep]
    p = p[keep]
    if d.shape[0] == 0:
        raise ValueError("No polymorphic markers available for kinship.")
    # Impute missing to 2p so centred contribution is zero.
    rows = np.where(np.isnan(d))
    d = d.copy()
    d[rows] = (2.0 * p)[rows[0]]
    z = d - 2.0 * p[:, None]
    denom = 2.0 * np.sum(p * (1.0 - p))
    return (z.T @ z) / denom


def imputed_alt_counts(gt, drop_invariant: bool = True) -> np.ndarray:
    """Alt-count matrix (n_variants, n_samples) with missing mean-imputed; for PCA."""
    gn = np.asarray(gt.to_n_alt(fill=-1), dtype=float)
    gn[gn < 0] = np.nan
    row_mean = np.nanmean(gn, axis=1)
    rows = np.where(np.isnan(gn))
    gn[rows] = row_mean[rows[0]]
    if drop_invariant:
        gn = gn[gn.std(axis=1) > 0]
    return gn
