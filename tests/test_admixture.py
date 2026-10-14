"""ADMIXTURE integration: PLINK BED encoding, binary fetch guard, run/Q parsing."""

from __future__ import annotations

import platform
import shutil
import subprocess

import allel
import numpy as np
import pytest

from gigwa_mcp.analysis import admixture
from gigwa_mcp.analysis.genotypes import GenotypeMatrix


def _calls(n_alt_row):
    """Map a row of desired n_alt values (0/1/2/-1) to diploid allele-call pairs."""
    lut = {0: (0, 0), 1: (0, 1), 2: (1, 1), -1: (-1, -1)}
    return [lut[v] for v in n_alt_row]


def _gm():
    # variant0 and variant2 are monomorphic (all 0, all 1) -- write_plink_bed must drop
    # them. variant1 and variant3 carry the byte patterns hand-verified below.
    rows = [
        [0, 0, 0, 0, 0],       # dropped: monomorphic
        [0, 1, 2, -1, 2],      # kept
        [1, 1, 1, 1, 1],       # dropped: monomorphic
        [2, 0, 1, 0, -1],      # kept
    ]
    gt = allel.GenotypeArray(np.array([_calls(r) for r in rows], dtype="i1"))
    return GenotypeMatrix(
        gt=gt,
        variant_ids=np.array(["m0", "m1", "m2", "m3"]),
        chrom=np.array(["1", "1", "2", "2"]),
        pos=np.array([10, 20, 30, 40]),
        sample_ids=["S0", "S1", "S2", "S3", "S4"],
        sample_names=["acc 1", "acc2", "acc2", "acc3", "acc4"],
        variant_set_db_id="VS§1§run",
    )


def test_write_plink_bed_drops_monomorphic_and_encodes_correctly(tmp_path):
    dataset = admixture.write_plink_bed(_gm(), tmp_path / "data")

    assert dataset.n_variants == 2  # variant0, variant2 dropped
    assert dataset.n_samples == 5

    raw = dataset.bed_path.read_bytes()
    assert raw[:3] == bytes([0x6C, 0x1B, 0x01])
    # Hand-computed from n_alt rows [0,1,2,-1,2] and [2,0,1,0,-1] via the documented
    # PLINK 2-bit codes (missing=0b01, hom-ref=0b11, het=0b10, hom-alt=0b00),
    # 4 samples/byte LSB-first, one padding byte per row for the 5th sample.
    assert list(raw[3:]) == [75, 0, 236, 1]

    bim_lines = dataset.bed_path.with_suffix(".bim").read_text().splitlines()
    assert bim_lines == ["1\tm1\t0\t20\tA\tC", "2\tm3\t0\t40\tA\tC"]

    fam_lines = dataset.bed_path.with_suffix(".fam").read_text().splitlines()
    # Whitespace sanitised ("acc 1" -> "acc_1") and the duplicate "acc2" de-duplicated.
    ids = [line.split("\t")[0] for line in fam_lines]
    assert ids == ["acc_1", "acc2", "acc2_1", "acc3", "acc4"]
    assert len(set(ids)) == 5


@pytest.mark.parametrize(
    "name, code",
    [("chr10", "10"), ("Chr01", "1"), ("CHR3", "3"), ("7", "7"), ("scaffold_12", "0"),
     ("chrUn", "0"), ("", "0"), (None, "0")],
)
def test_plink_chrom_codes_are_integers(name, code):
    """ADMIXTURE rejects non-integer chromosome codes ("Invalid chromosome code!  Use
    integers.") -- confirmed live on a rice VCF whose CHROM is ``chr10``."""
    assert admixture._plink_chrom(name) == code


def test_write_plink_bed_no_polymorphic_markers_raises(tmp_path):
    gt = allel.GenotypeArray(np.array([_calls([0, 0, 0, 0, 0])], dtype="i1"))
    gm = GenotypeMatrix(
        gt=gt, variant_ids=np.array(["m0"]), chrom=np.array(["1"]), pos=np.array([1]),
        sample_ids=["S0", "S1", "S2", "S3", "S4"], sample_names=["a", "b", "c", "d", "e"],
        variant_set_db_id="VS§1§run",
    )
    with pytest.raises(ValueError, match="No polymorphic markers"):
        admixture.write_plink_bed(gm, tmp_path / "data")


def test_ensure_admixture_binary_windows_has_no_build(tmp_path, monkeypatch):
    monkeypatch.setattr(admixture, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    with pytest.raises(ValueError, match="no native Windows build"):
        admixture.ensure_admixture_binary()


def test_ensure_admixture_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/admixture" if name == "admixture" else None)
    assert admixture.ensure_admixture_binary() == admixture.Path("/usr/local/bin/admixture")


def test_run_admixture_parses_cv_and_loglikelihood(tmp_path, monkeypatch):
    dataset = admixture.PlinkDataset(bed_path=tmp_path / "data.bed", n_variants=10, n_samples=3)
    dataset.bed_path.write_bytes(b"\x6c\x1b\x01")
    q_path = tmp_path / "data.3.Q"
    q_path.write_text("0.7 0.2 0.1\n0.1 0.8 0.1\n0.2 0.2 0.6\n")

    stdout = "some log lines\nLoglikelihood: -1234.5\nCV error (K=3): 0.4567\nWriting output files.\n"
    monkeypatch.setattr(
        admixture.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=""),
    )

    run = admixture.run_admixture(dataset, 3, binary=tmp_path / "admixture")
    assert run.k == 3
    assert run.cv_error == pytest.approx(0.4567)
    assert run.log_likelihood == pytest.approx(-1234.5)
    assert run.q.shape == (3, 3)
    assert run.q[1].tolist() == pytest.approx([0.1, 0.8, 0.1])


def test_run_admixture_nonzero_exit_raises(tmp_path, monkeypatch):
    dataset = admixture.PlinkDataset(bed_path=tmp_path / "data.bed", n_variants=10, n_samples=3)
    dataset.bed_path.write_bytes(b"\x6c\x1b\x01")
    monkeypatch.setattr(
        admixture.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="boom"),
    )
    with pytest.raises(ValueError, match="boom"):
        admixture.run_admixture(dataset, 2, binary=tmp_path / "admixture")


def test_q_dataframe_reports_dominant_cluster():
    gm = _gm()
    run = admixture.AdmixtureRun(
        k=2,
        q=np.array([[0.9, 0.1], [0.2, 0.8], [0.5, 0.5], [0.1, 0.9], [0.6, 0.4]]),
        cv_error=0.1,
        log_likelihood=-100.0,
    )
    df = admixture.q_dataframe(gm, run)
    assert list(df["dominant_cluster"]) == [1, 2, 1, 2, 1]
    assert list(df.columns) == ["sample_id", "sample_name", "Q1", "Q2", "dominant_cluster"]


def test_write_pop_file_marks_targets_and_returns_first_appearance_order(tmp_path):
    dataset = admixture.write_plink_bed(_gm(), tmp_path / "data")
    order = admixture.write_pop_file(dataset, ["zeta", None, "alpha", "zeta", None])
    assert order == ["zeta", "alpha"]  # ADMIXTURE's Q column order, not alphabetical
    assert dataset.bed_path.with_suffix(".pop").read_text().splitlines() == [
        "zeta", "-", "alpha", "zeta", "-",
    ]


def test_write_pop_file_length_mismatch_raises(tmp_path):
    dataset = admixture.write_plink_bed(_gm(), tmp_path / "data")
    with pytest.raises(ValueError, match="4 population labels for 5 samples"):
        admixture.write_pop_file(dataset, ["a", "b", None, None])


def test_run_admixture_supervised_passes_flag(tmp_path, monkeypatch):
    dataset = admixture.PlinkDataset(bed_path=tmp_path / "data.bed", n_variants=10, n_samples=2)
    dataset.bed_path.write_bytes(b"\x6c\x1b\x01")
    (tmp_path / "data.2.Q").write_text("0.7 0.3\n0.1 0.9\n")
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="Loglikelihood: -1.0\n", stderr="")

    monkeypatch.setattr(admixture.subprocess, "run", fake_run)
    run = admixture.run_admixture(dataset, 2, cv=False, supervised=True, binary=tmp_path / "admixture")
    assert "--supervised" in seen["args"] and "--cv" not in seen["args"]
    assert run.cv_error is None


def test_q_dataframe_names_columns_after_populations():
    run = admixture.AdmixtureRun(
        k=2,
        q=np.array([[0.9, 0.1], [0.2, 0.8], [0.5, 0.5], [0.1, 0.9], [0.6, 0.4]]),
        cv_error=None,
        log_likelihood=-100.0,
    )
    df = admixture.q_dataframe(_gm(), run, ["GJ", "XI"])
    assert list(df.columns) == ["sample_id", "sample_name", "GJ", "XI", "dominant_cluster"]
    assert list(df["dominant_cluster"]) == ["GJ", "XI", "GJ", "XI", "GJ"]


@pytest.mark.skipif(shutil.which("admixture") is None and not (
    admixture._cache_dir() / "admixture").exists(), reason="ADMIXTURE binary not available")
def test_supervised_end_to_end_recovers_reference_ancestry(tmp_path):
    rng = np.random.default_rng(0)
    nv = 1000
    pz, pa = rng.uniform(0.0, 0.2, nv), rng.uniform(0.8, 1.0, nv)

    def draw(p, n):
        return rng.binomial(1, p[:, None, None], size=(nv, n, 2))

    g = np.concatenate([draw(pz, 15), draw(pa, 15), draw(pa, 2), draw(pz, 2)], axis=1).astype("i1")
    n = g.shape[1]
    gm = GenotypeMatrix(
        gt=allel.GenotypeArray(g), variant_ids=np.array([f"m{i}" for i in range(nv)]),
        chrom=np.array(["1"] * nv), pos=np.arange(1, nv + 1),
        sample_ids=[f"s{i}" for i in range(n)], sample_names=[f"s{i}" for i in range(n)],
        variant_set_db_id="VS§1§run",
    )
    dataset = admixture.write_plink_bed(gm, tmp_path / "data")
    order = admixture.write_pop_file(dataset, ["zeta"] * 15 + ["alpha"] * 15 + [None] * 4)
    run = admixture.run_admixture(dataset, 2, cv=False, supervised=True)
    df = admixture.q_dataframe(gm, run, order)
    assert list(df["dominant_cluster"].iloc[30:]) == ["alpha", "alpha", "zeta", "zeta"]
