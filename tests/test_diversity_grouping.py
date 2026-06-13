"""Metadata-TSV driven grouping for diversity_fst / diversity_pca."""

from __future__ import annotations

import allel
import numpy as np

from gigwa_mcp.analysis.genotypes import GenotypeMatrix
from gigwa_mcp.tools.diversity import _groups_from_tsv, _sample_group_map


def _gm():
    gt = allel.GenotypeArray(np.zeros((2, 4, 2), dtype="i1"))
    return GenotypeMatrix(
        gt=gt,
        variant_ids=np.array(["m1", "m2"]),
        chrom=np.array(["1", "1"]),
        pos=np.array([1, 2]),
        sample_ids=["S1", "S2", "S3", "S4"],
        sample_names=["acc1", "acc2", "acc3", "acc4"],
        variant_set_db_id="VS§1§run",
    )


def _tsv(tmp_path):
    p = tmp_path / "meta.tsv"
    p.write_text("individual\tpop\nacc1\tnorth\nacc2\tnorth\nacc3\tsouth\nacc4\t\n")
    return str(p)


def test_sample_group_map_skips_blank(tmp_path):
    m = _sample_group_map(_tsv(tmp_path), "pop")
    assert m == {"acc1": "north", "acc2": "north", "acc3": "south"}  # acc4 blank dropped


def test_groups_from_tsv_indices(tmp_path):
    groups = _groups_from_tsv(_gm(), _tsv(tmp_path), "pop")
    assert groups == {"north": [0, 1], "south": [2]}  # matched by sample_name


def test_groups_from_tsv_matches_callset_id(tmp_path):
    # Fall back to matching the callSetDbId when the name isn't in the TSV.
    p = tmp_path / "byid.tsv"
    p.write_text("individual\tpop\nS1\tA\nS2\tB\n")
    groups = _groups_from_tsv(_gm(), str(p), "pop")
    assert groups == {"A": [0], "B": [1]}
