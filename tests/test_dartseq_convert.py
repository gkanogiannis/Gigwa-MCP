"""DArTseq -> VCF conversion: genotype-calling correctness on small synthetic reports."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gigwa_mcp.importers import call_snp_genotypes, convert_dart_to_vcf


def test_call_snp_genotypes_mapping():
    ref = np.array([[1, 0, 1, 0, np.nan]])
    alt = np.array([[0, 1, 1, 0, np.nan]])
    gt = call_snp_genotypes(ref, alt)
    assert list(gt[0]) == ["0/0", "1/1", "0/1", "./.", "./."]


def _write_xlsx(path, rows, sheet="Report"):
    pd.DataFrame(rows).to_excel(path, header=False, index=False, sheet_name=sheet)


def _parse_vcf(path):
    """Return (samples, {variant_id: (ref, alt, [gt,...])})."""
    samples, records = [], {}
    for line in path.read_text().splitlines():
        if line.startswith("##"):
            continue
        f = line.split("\t")
        if line.startswith("#CHROM"):
            samples = f[9:]
        else:
            records[f[2]] = (f[3], f[4], f[9:])
    return samples, records


def test_convert_snp_report_genotypes(tmp_path):
    xlsx = tmp_path / "snp.xlsx"
    _write_xlsx(xlsx, [
        ["*", "*", "*", "*", "*", "*", "*"],
        ["AlleleID", "AlleleSequence", "SNP", "SnpPosition", "S1", "S2", "S3"],
        ["m1|F|0--10:A>G", "ACGT", np.nan, 10, 1, 0, 1],
        ["m1|F|0-10:A>G-10:A>G", "ACGT", "10:A>G", 10, 0, 1, 1],
        ["m2|F|0--20:C>T", "ACGT", np.nan, 20, 0, 1, "-"],
        ["m2|F|0-20:C>T-20:C>T", "ACGT", "20:C>T", 20, 1, 0, "-"],
    ])
    out = tmp_path / "snp.vcf"
    res = convert_dart_to_vcf(xlsx, out)
    assert res.report_type == "SNP"
    assert res.n_markers == 2 and res.n_samples == 3

    samples, rec = _parse_vcf(out)
    assert samples == ["S1", "S2", "S3"]
    assert rec["m1|F|0--10:A>G"][:2] == ("A", "G")
    assert rec["m1|F|0--10:A>G"][2] == ["0/0", "1/1", "0/1"]
    assert rec["m2|F|0--20:C>T"][:2] == ("C", "T")
    assert rec["m2|F|0--20:C>T"][2] == ["1/1", "0/0", "./."]


def test_convert_snp_with_positions_anchors_and_sorts(tmp_path):
    xlsx = tmp_path / "snp.xlsx"
    _write_xlsx(xlsx, [
        ["AlleleID", "AlleleSequence", "SNP", "SnpPosition", "S1", "S2"],
        ["m1|F|0--10:A>G", "ACGT", np.nan, 10, 1, 0],
        ["m1|F|0-10:A>G-10:A>G", "ACGT", "10:A>G", 10, 0, 1],
        ["m2|F|0--20:C>T", "ACGT", np.nan, 20, 1, 0],
        ["m2|F|0-20:C>T-20:C>T", "ACGT", "20:C>T", 20, 0, 1],
    ])
    out = tmp_path / "snp.vcf"
    # Only m1 is mapped (to chr5:1000); m2 stays Unmapped.
    positions = {"m1|F|0--10:A>G": ("chr5", 1000, "A", "G")}
    res = convert_dart_to_vcf(xlsx, out, positions=positions)
    assert res.n_mapped == 1

    text = out.read_text()
    assert "##contig=<ID=chr5>" in text and "##contig=<ID=Unmapped>" in text
    body = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    # coordinate-sorted: mapped chr5 row first, Unmapped last
    assert body[0].split("\t")[0] == "chr5" and body[0].split("\t")[1] == "1000"
    assert body[-1].split("\t")[0] == "Unmapped"
    # genotypes preserved for m1: S1 hom-ref, S2 hom-alt
    assert body[0].split("\t")[9:] == ["0/0", "1/1"]


def test_convert_silico_report_genotypes(tmp_path):
    xlsx = tmp_path / "silico.xlsx"
    _write_xlsx(xlsx, [
        ["CloneID", "AlleleSequence", "CallRate", "S1", "S2", "S3"],
        ["clone1", "ACGT", 1.0, 1, 0, "-"],
        ["clone2", "ACGT", 1.0, 0, 1, 1],
    ])
    out = tmp_path / "silico.vcf"
    res = convert_dart_to_vcf(xlsx, out)
    assert res.report_type == "SilicoDArT"
    assert res.n_markers == 2 and res.n_samples == 3

    _, rec = _parse_vcf(out)
    assert rec["clone1"][2] == ["1/1", "0/0", "./."]
    assert rec["clone2"][2] == ["0/0", "1/1", "1/1"]
