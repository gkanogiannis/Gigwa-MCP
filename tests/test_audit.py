"""Import-quality audit classifier (no live server)."""

from __future__ import annotations

import allel
import numpy as np

from gigwa_mcp.analysis.genotypes import GenotypeMatrix
from gigwa_mcp.tools.audit import _classify, _diagnose

REF, ALT, MISS = [0, 0], [0, 1], [-1, -1]
HOM_ALT = [1, 1]


def _gm(rows: list[list[list[int]]]) -> GenotypeMatrix:
    """Wrap a list of per-marker genotype rows into a GenotypeMatrix."""
    gt = allel.GenotypeArray(np.array(rows, dtype="i1"))
    n_var, n_samp = gt.shape[0], gt.shape[1]
    return GenotypeMatrix(
        gt=gt,
        variant_ids=np.array([f"m{i}" for i in range(n_var)]),
        chrom=np.array(["1"] * n_var),
        pos=np.arange(n_var),
        sample_ids=[f"S{i}" for i in range(n_samp)],
        sample_names=[f"acc{i}" for i in range(n_samp)],
        variant_set_db_id="VS§1§run",
    )


def _repeat(pattern: list[list[int]], n: int) -> list[list[list[int]]]:
    return [list(pattern) for _ in range(n)]


# 10 samples per marker; identical pattern across markers keeps the maths exact.
HEALTHY = _repeat([REF] * 3 + [ALT] * 4 + [HOM_ALT] * 2 + [MISS], 10)
HIGH_HET = _repeat([ALT] * 8 + [HOM_ALT] + [MISS], 10)
NO_HOM_ALT = _repeat([REF] * 5 + [ALT] * 5, 10)
# 20 markers: 19 monomorphic (one with a missing call to drop call rate), 1 polymorphic.
MONOMORPHIC = (
    [[MISS] + [REF] * 9]
    + _repeat([REF] * 10, 18)
    + [[REF] * 5 + [ALT] * 3 + [HOM_ALT] * 2]
)
# Rare alt allele: het present (~5%), hom-alt genuinely ~0 but HWE-consistent at low q
# (a low-MAF panel whose near-zero hom-alt is real). Must NOT be flagged lost-hom-alt.
RARE_ALT = _repeat([REF] * 18 + [ALT] + [MISS], 10)


def test_diagnose_values():
    d = _diagnose(_gm(NO_HOM_ALT))
    assert d["call_rate"] == 1.0
    assert d["het_frac"] == 0.5
    assert d["hom_alt_frac"] == 0.0
    assert d["monomorphic_frac"] == 0.0
    assert d["mean_ho"] == 0.5


def test_classify_ok():
    status, reasons = _classify(_diagnose(_gm(HEALTHY)))
    assert status == "OK"
    assert reasons == []


def test_classify_high_het():
    status, reasons = _classify(_diagnose(_gm(HIGH_HET)))
    assert status == "BROKEN"
    assert any("DArT 2-row" in r for r in reasons)
    # hom-alt present here, so the lost-hom-alt rule must NOT fire.
    assert not any("lost hom-alt" in r for r in reasons)


def test_classify_no_hom_alt_and_no_missing():
    status, reasons = _classify(_diagnose(_gm(NO_HOM_ALT)))
    assert status == "BROKEN"
    assert any("lost hom-alt" in r for r in reasons)
    assert any("no missing calls" in r for r in reasons)


def test_classify_monomorphic_is_suspect():
    status, reasons = _classify(_diagnose(_gm(MONOMORPHIC)))
    assert status == "SUSPECT"
    assert any("monomorphic" in r for r in reasons)


def test_classify_fabricated_depth_is_suspect():
    # Healthy genotypes, but AD/DP present and uniformly zero -> fabricated FORMAT.
    d = _diagnose(_gm(HEALTHY))
    assert d["depth_all_zero"] is False  # default when no depth probed
    status, reasons = _classify(d)
    assert status == "OK"
    d["depth_all_zero"] = True
    status, reasons = _classify(d)
    assert status == "SUSPECT"
    assert any("depth" in r.lower() for r in reasons)


def test_rare_alt_not_flagged_lost_hom_alt():
    # Near-zero hom-alt at low alt frequency is HWE-consistent, not a lost class.
    d = _diagnose(_gm(RARE_ALT))
    assert d["hom_alt_frac"] == 0.0 and 0 < d["het_frac"] < 0.1
    status, reasons = _classify(d)
    assert status == "OK"
    assert not any("hom-alt" in r for r in reasons)
