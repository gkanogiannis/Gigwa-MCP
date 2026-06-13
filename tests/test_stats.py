"""Statistics correctness on a tiny synthetic GenotypeArray with hand-computed values."""

from __future__ import annotations

import allel
import numpy as np
import pytest

from gigwa_mcp.analysis import stats

# 4 variants x 3 samples, diploid; -1 = missing.
#   V0: s0 hom-ref, s1 het, s2 hom-alt
#   V1: s0 hom-ref, s1 hom-ref, s2 het
#   V2: s0 MISSING, s1 hom-ref, s2 hom-alt
#   V3: monomorphic ref
GT = allel.GenotypeArray(
    [
        [[0, 0], [0, 1], [1, 1]],
        [[0, 0], [0, 0], [0, 1]],
        [[-1, -1], [0, 0], [1, 1]],
        [[0, 0], [0, 0], [0, 0]],
    ],
    dtype="i1",
)


def test_call_rate():
    assert stats.call_rate_per_sample(GT) == pytest.approx([0.75, 1.0, 1.0])
    assert stats.call_rate_per_marker(GT) == pytest.approx([1.0, 1.0, 2 / 3, 1.0])


def test_heterozygosity():
    ho, n_called = stats.heterozygosity_per_sample(GT)
    assert ho == pytest.approx([0.0, 0.25, 0.25])
    assert list(n_called) == [3, 4, 4]


def test_maf_he_pic():
    ac = GT.count_alleles()
    assert stats.maf(ac) == pytest.approx([0.5, 1 / 6, 0.5, 0.0])
    assert stats.expected_heterozygosity(ac) == pytest.approx([0.5, 10 / 36, 0.5, 0.0])
    # PIC for the balanced biallelic V0: 1 - 0.5 - 0.125 = 0.375
    assert stats.pic(ac)[0] == pytest.approx(0.375)


def test_ibs_similarity():
    sim = stats.ibs_similarity(stats.alt_dosage(GT))
    assert sim.shape == (3, 3)
    assert np.allclose(np.diag(sim), 1.0)
    assert sim[0, 1] == pytest.approx(2.5 / 3)
    assert sim[0, 2] == pytest.approx(0.5)
    assert sim[1, 2] == pytest.approx(0.5)
    assert sim[1, 0] == pytest.approx(sim[0, 1])  # symmetric


def test_vanraden_grm_shape_and_symmetry():
    grm = stats.vanraden_grm(stats.alt_dosage(GT))
    assert grm.shape == (3, 3)
    assert np.allclose(grm, grm.T)
    assert np.all(np.isfinite(grm))


def test_allelic_richness():
    ac = GT.count_alleles()
    # V0/V1/V2 biallelic (2 alleles observed), V3 monomorphic (1 allele).
    assert list(stats.allelic_richness(ac)) == [2.0, 2.0, 2.0, 1.0]


def test_rarefied_allelic_richness():
    ac = GT.count_alleles()
    n_full = [int(a.sum()) for a in ac]  # gene copies per marker
    # Rarefying to the full gene-copy count reproduces observed allelic richness.
    rar_full = [
        float(stats.rarefied_allelic_richness(ac[i : i + 1], n_full[i])[0]) for i in range(4)
    ]
    assert rar_full == pytest.approx([2.0, 2.0, 2.0, 1.0])
    # Rarefying deeper than available gene copies is undefined (NaN).
    assert np.isnan(stats.rarefied_allelic_richness(ac, max(n_full) + 2)).all()
    # Rarefied richness never exceeds observed and is ≥ 1 for a polymorphic marker.
    rar2 = stats.rarefied_allelic_richness(ac, 2)
    assert rar2[0] <= 2.0 and rar2[0] >= 1.0
    assert rar2[3] == pytest.approx(1.0)  # monomorphic stays 1
