"""Genebank helpers (core collection, UPGMA tree) + the new diversity tools.

Pure helpers are tested directly; the tools are exercised with a monkeypatched
``load_genotypes`` so no live Gigwa is needed (mirroring test_diversity_grouping.py).
"""

from __future__ import annotations

import allel
import numpy as np

from gigwa_mcp.analysis import genebank
from gigwa_mcp.analysis.genotypes import GenotypeMatrix
from gigwa_mcp.tools import diversity


# --- pure helpers --------------------------------------------------------------

def test_allele_presence():
    # V0: s0 hom-ref, s1 het, s2 hom-alt. With 2 alleles → 2 units per marker.
    gt = allel.GenotypeArray([[[0, 0], [0, 1], [1, 1]]], dtype="i1")
    pres = genebank.allele_presence(gt)
    assert pres.shape == (3, 2)  # 3 samples × (1 marker × 2 alleles)
    # s0 carries only ref; s1 carries both; s2 carries only alt.
    assert pres.tolist() == [[True, False], [True, True], [False, True]]


def test_greedy_core_picks_max_coverage_first():
    # sample 1 carries the most alleles → chosen first; coverage is non-decreasing.
    pres = np.array(
        [[1, 0, 0, 0], [1, 1, 1, 0], [0, 0, 0, 1]], dtype=bool
    )
    selected, cumulative = genebank.greedy_core(pres, 3)
    assert selected[0] == 1  # the 3-allele accession
    assert list(cumulative) == sorted(cumulative)  # monotonic non-decreasing
    assert cumulative[-1] == 4  # all four alleles captured by the full set


def test_greedy_core_saturates_then_fills():
    # one accession covers everything; the rest add nothing but the curve stays length k.
    pres = np.array([[1, 1, 1], [1, 0, 0], [0, 1, 0]], dtype=bool)
    selected, cumulative = genebank.greedy_core(pres, 3)
    assert selected[0] == 0
    assert cumulative[0] == 3 and cumulative[-1] == 3


def test_upgma_newick_groups_closest():
    # A,B close (d=1), C far (d=2) → A,B form a clade.
    dist = np.array([[0, 1, 2], [1, 0, 2], [2, 2, 0]], float)
    nwk = genebank.upgma_newick(dist, ["A", "B", "C"])
    assert nwk.endswith(";")
    assert "(A:1,B:1)" in nwk or "(B:1,A:1)" in nwk
    assert "C" in nwk


# --- tool integration (monkeypatched loader) ----------------------------------

def _two_cluster_gm(n_per=4, n_markers=40, noise=0.0):
    """Two separated clusters: A mostly hom-ref, B mostly hom-alt.

    With ``noise``>0 a random fraction of genotypes is flipped to het, giving each
    cluster genuine within-cluster variance (needed so pseudo-F has a real peak at K=2).
    With ``noise``=0 the clusters are internally monomorphic (He=0), convenient for the
    per-group and core-coverage assertions.
    """
    a = np.zeros((n_markers, n_per, 2), dtype="i1")
    b = np.ones((n_markers, n_per, 2), dtype="i1")
    arr = np.concatenate([a, b], axis=1)
    if noise:
        rng = np.random.default_rng(0)
        flip = rng.random((n_markers, 2 * n_per)) < noise
        arr[flip] = np.array([0, 1], dtype="i1")  # those genotypes become het
    gt = allel.GenotypeArray(arr)
    n = 2 * n_per
    return GenotypeMatrix(
        gt=gt,
        variant_ids=np.array([f"m{i}" for i in range(n_markers)]),
        chrom=np.array(["1"] * n_markers),
        pos=np.arange(n_markers),
        sample_ids=[f"S{i}" for i in range(n)],
        sample_names=[f"acc{i}" for i in range(n)],
        variant_set_db_id="VS§1§run",
    )


def _patch_loader(monkeypatch, gm):
    monkeypatch.setattr(diversity, "get_client", lambda: object())
    monkeypatch.setattr(diversity, "load_genotypes", lambda *a, **k: gm)


def _fn(tool):
    return getattr(tool, "fn", tool)


def test_diversity_structure_finds_two_clusters(monkeypatch, tmp_path):
    gm = _two_cluster_gm(n_per=6, noise=0.15)
    _patch_loader(monkeypatch, gm)
    out = _fn(diversity.diversity_structure)(
        "VS§1§run", k_min=2, k_max=5, output_dir=str(tmp_path)
    )
    assert "Suggested K=2" in out
    df = __import__("pandas").read_csv(tmp_path / "structure_clusters.csv")
    # the two halves land in different clusters
    assert df["cluster"].nunique() == 2
    assert df.iloc[0]["cluster"] != df.iloc[-1]["cluster"]


def test_diversity_by_group(monkeypatch, tmp_path):
    gm = _two_cluster_gm()
    _patch_loader(monkeypatch, gm)
    groups = '{"A": ["acc0","acc1","acc2","acc3"], "B": ["acc4","acc5","acc6","acc7"]}'
    out = _fn(diversity.diversity_by_group)(
        "VS§1§run", groups_json=groups, output_dir=str(tmp_path)
    )
    df = __import__("pandas").read_csv(tmp_path / "diversity_by_group.csv")
    assert set(df["group"]) == {"A", "B"}
    # each group is internally monomorphic (all hom-ref or all hom-alt) → He≈0.
    assert (df["he"] < 1e-9).all()
    assert (df["n"] == 4).all()


def test_diversity_core_collection_full_coverage(monkeypatch, tmp_path):
    gm = _two_cluster_gm()
    _patch_loader(monkeypatch, gm)
    # one accession from each cluster already covers every allele → 100% by size 2.
    out = _fn(diversity.diversity_core_collection)(
        "VS§1§run", size=2, output_dir=str(tmp_path)
    )
    assert "100.0%" in out or "100%" in out
    df = __import__("pandas").read_csv(tmp_path / "core_collection.csv")
    assert df["coverage_fraction"].iloc[-1] == 1.0
