"""Convert DArTseq Excel reports into a standard VCF for Gigwa import.

DArTseq SNP reports use the classic **2-rows-per-marker** layout: a reference-allele
row (``SNP`` empty) followed by a SNP-allele row (``SNP`` = ``pos:REF>ALT``); each
sample cell is ``1`` (allele detected), ``0`` (not detected) or ``-`` (missing).
The diploid genotype is therefore::

    (ref=1, alt=0) -> 0/0   (ref=0, alt=1) -> 1/1
    (ref=1, alt=1) -> 0/1   otherwise      -> ./.   (missing / no allele detected)

Silico-DArT reports are 1 row per clone (dominant presence/absence): ``1`` -> ``1/1``,
``0`` -> ``0/0``, ``-`` -> ``./.``.

We do this genotype calling in Python and emit a standard VCF rather than relying on
Gigwa's built-in DArT parser, which mis-calls the 2-row format (it imports reference
homozygotes as heterozygous). The VCF is then imported through Gigwa's verified VCF
path. All markers are placed on a single ``Unmapped`` contig at sequential positions
(DArTseq markers have no genomic coordinates).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# Standard DArT marker-annotation columns (lower-cased); every other column is a sample.
_KNOWN_META_COLUMNS = {
    "alleleid", "cloneid", "allelesequence", "trimmedsequence", "chrom_", "chrompos_",
    "chrom", "chrompos", "snp", "snpposition", "callrate", "oneratioref", "oneratiosnp",
    "oneratio", "freqhomref", "freqhomsnp", "freqhets", "picref", "picsnp", "avgpic",
    "pic", "avgcountref", "avgcountsnp", "avgcount", "repavg", "rdepth", "readdepth",
    "avgreaddepth", "stdevreaddepth", "qpmr", "callratedart", "alleleseqdist", "rowsum",
    "totalsequences", "nrofalleles",
}
_ANCHORS = ("AlleleID", "CloneID", "AlleleSequence")
_SNP_RE = re.compile(r"(\d+):([ACGTN])>([ACGTN])", re.IGNORECASE)


@dataclass
class DartVcfResult:
    out_path: Path
    report_type: str  # "SNP" | "SilicoDArT"
    n_markers: int
    n_samples: int
    n_skipped: int = 0
    sample_names: list[str] = field(default_factory=list)
    n_mapped: int = 0  # markers anchored to a reference genome (rest on 'Unmapped')

    def summary(self) -> str:
        skip = f", {self.n_skipped} markers skipped (unparseable allele)" if self.n_skipped else ""
        anchor = f", {self.n_mapped} genome-anchored" if self.n_mapped else ""
        return (
            f"{self.report_type} report -> {self.out_path.name}: "
            f"{self.n_markers} markers × {self.n_samples} samples{skip}{anchor}"
        )


def call_snp_genotypes(ref: np.ndarray, alt: np.ndarray) -> np.ndarray:
    """Vectorised 2-row DArT genotype calling. ``ref``/``alt`` are presence matrices
    (markers × samples) of 1/0/NaN; returns a string matrix of VCF genotypes."""
    gt = np.full(ref.shape, "./.", dtype=object)
    gt[(ref == 1) & (alt == 0)] = "0/0"
    gt[(ref == 0) & (alt == 1)] = "1/1"
    gt[(ref == 1) & (alt == 1)] = "0/1"
    return gt


def _pick_data_sheet(xlsx: pd.ExcelFile, sheet: int | str | None) -> int | str:
    if sheet is not None:
        return sheet
    non_meta = [s for s in xlsx.sheet_names if s.strip().lower() != "metadata"]
    return non_meta[-1] if non_meta else (1 if len(xlsx.sheet_names) > 1 else 0)


def _read_report(xlsx_path: Path, sheet: int | str | None) -> pd.DataFrame:
    """Read the report sheet once and return the data matrix with proper headers."""
    xlsx = pd.ExcelFile(xlsx_path)
    sheet_id = _pick_data_sheet(xlsx, sheet)
    raw = pd.read_excel(xlsx_path, sheet_name=sheet_id, header=None)
    header_idx = None
    for idx, row in raw.iterrows():
        if {str(v).strip() for v in row.values} & set(_ANCHORS):
            header_idx = int(idx)
            break
    if header_idx is None:
        raise ValueError(
            "Could not locate the DArT header row (no AlleleID / CloneID / AlleleSequence anchor)."
        )
    data = raw.iloc[header_idx + 1 :].reset_index(drop=True)
    data.columns = [str(c).strip() for c in raw.iloc[header_idx].tolist()]
    return data


def _sample_columns(data: pd.DataFrame) -> list[str]:
    return [c for c in data.columns if str(c).strip().lower() not in _KNOWN_META_COLUMNS]


def _numeric_samples(data: pd.DataFrame, samples: list[str]) -> np.ndarray:
    """Sample cells as float (1/0, '-'/blank -> NaN), shape (n_rows, n_samples)."""
    return data[samples].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)


def _build_rows(records, gt_matrix: np.ndarray, positions: dict | None):
    """Assign each marker a (chrom, pos, id, ref, alt, gt_list) row.

    With ``positions`` (``{allele_id: (chrom, pos, ref, alt)}``), uniquely mapped
    markers get their genomic coordinate and the rows are coordinate-sorted (real
    contigs first, ``Unmapped`` last); everything else is placed on ``Unmapped`` at
    sequential positions.
    """
    rows = []
    unmapped = 0
    for k, (vid, ref, alt) in enumerate(records):
        gt_list = gt_matrix[k].tolist()
        mp = positions.get(vid) if positions else None
        if mp:
            chrom, pos, mref, malt = mp
            rows.append((str(chrom), int(pos), vid, mref, malt, gt_list))
        else:
            unmapped += 1
            rows.append(("Unmapped", unmapped, vid, ref, alt, gt_list))
    if positions:
        rows.sort(key=lambda r: (r[0] == "Unmapped", r[0], r[1]))
    return rows


def _write_vcf(out_path: Path, samples: list[str], rows) -> None:
    """Write a VCF from pre-sorted ``rows`` of (chrom, pos, id, ref, alt, gt_list)."""
    contigs: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            contigs.append(r[0])
    cols = ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", *samples]
    with out_path.open("w", newline="\n") as fh:
        fh.write("##fileformat=VCFv4.2\n##source=gigwa_mcp dartseq importer\n")
        fh.write('##FILTER=<ID=PASS,Description="All filters passed">\n')
        for c in contigs:
            fh.write(f"##contig=<ID={c}>\n")
        fh.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        fh.write("\t".join(cols) + "\n")
        for chrom, pos, vid, ref, alt, gt_list in rows:
            fixed = [chrom, str(pos), vid, ref, alt, ".", ".", ".", "GT"]
            fh.write("\t".join(fixed) + "\t" + "\t".join(gt_list) + "\n")


def _convert_snp(data: pd.DataFrame, out_path: Path, positions: dict | None = None) -> DartVcfResult:
    samples = _sample_columns(data)
    snp = data["SNP"] if "SNP" in data.columns else pd.Series([np.nan] * len(data))
    is_ref_row = snp.isna().to_numpy()

    vals = _numeric_samples(data, samples)
    alleles = data["AlleleID"].astype(str).to_numpy()
    snp_str = snp.astype(str).to_numpy()

    records, ref_rows, alt_rows, skipped = [], [], [], 0
    i = 0
    n = len(data)
    while i < n - 1:
        if is_ref_row[i] and not is_ref_row[i + 1]:
            m = _SNP_RE.search(snp_str[i + 1]) or _SNP_RE.search(alleles[i + 1])
            if m:
                records.append((alleles[i], m.group(2).upper(), m.group(3).upper()))
                ref_rows.append(i)
                alt_rows.append(i + 1)
            else:
                skipped += 1
            i += 2
        else:
            i += 1  # unexpected layout; skip a row and resync

    gt = call_snp_genotypes(vals[ref_rows], vals[alt_rows])
    n_mapped = sum(1 for vid, _, _ in records if positions and vid in positions)
    _write_vcf(out_path, samples, _build_rows(records, gt, positions))
    return DartVcfResult(out_path, "SNP", len(records), len(samples), skipped, samples[:200], n_mapped)


def _convert_silico(data: pd.DataFrame, out_path: Path) -> DartVcfResult:
    samples = _sample_columns(data)
    vals = _numeric_samples(data, samples)
    ids = data["CloneID"].astype(str).to_numpy() if "CloneID" in data.columns \
        else data[data.columns[0]].astype(str).to_numpy()

    gt = np.full(vals.shape, "./.", dtype=object)
    gt[vals == 1] = "1/1"  # presence (dominant)
    gt[vals == 0] = "0/0"  # absence
    # Placeholder alleles: presence/absence markers carry no sequence variant.
    records = [(vid, "A", "C") for vid in ids]
    _write_vcf(out_path, samples, _build_rows(records, gt, None))
    return DartVcfResult(out_path, "SilicoDArT", len(records), len(samples), 0, samples[:200])


def convert_dart_to_vcf(
    xlsx_path: str | Path,
    out_vcf_path: str | Path | None = None,
    *,
    sheet: int | str | None = None,
    positions: dict | None = None,
) -> DartVcfResult:
    """Convert a DArTseq xlsx report to a Gigwa-ready VCF with correct genotype calls.

    Auto-detects SNP (2-row, ``AlleleID`` anchor) vs Silico-DArT (1-row, ``CloneID``).
    ``positions`` (``{allele_id: (chrom, pos, ref, alt)}``, e.g. from
    ``refmap.map_tags_to_reference``) genome-anchors the matching SNP markers; the
    rest stay on the ``Unmapped`` contig. The output VCF is coordinate-sorted.
    """
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.is_file():
        raise FileNotFoundError(f"DArT report not found: {xlsx_path}")
    out_vcf_path = Path(out_vcf_path) if out_vcf_path else xlsx_path.with_suffix(".vcf")

    data = _read_report(xlsx_path, sheet)
    if "AlleleID" in data.columns and "SNP" in data.columns:
        return _convert_snp(data, out_vcf_path, positions)
    if "CloneID" in data.columns:
        return _convert_silico(data, out_vcf_path)
    raise ValueError(
        "Unrecognised DArT report: expected an AlleleID+SNP (SNP report) or CloneID (Silico) layout."
    )
