"""Generate synthetic, known-truth genotype data for the local benchmark harness.

Everything here is dataset-agnostic and fully reproducible (seeded). It builds a
structured multi-population diploid panel and emits the inputs every import path of
the MCP server consumes:

- ``clean.vcf``              well-formed multi-population VCF (the OK control).
- ``dartseq_snp.xlsx``       2-row DArTseq SNP report encoding the *same* calls, so
                             the importer's 2-row genotype calling can be round-tripped.
- ``silico_dart.xlsx``       1-row presence/absence Silico-DArT report.
- ``reference.fasta``        tiny synthetic genome with each marker's tag planted at a
                             known coordinate, so minimap2 maps the tags back uniquely.
- ``metadata.tsv``           per-individual population labels (+ a couple attributes);
  ``metadata_bad.tsv``       same data but a wrong id-column header (validation path).
- adversarial VCFs           ``broken_allhet.vcf`` / ``broken_losthomalt.vcf`` /
                             ``suspect_monomorphic.vcf`` to trip the anomaly auditor.
- ``scale.vcf``              a larger panel for the scaling tier (paging / O(n^2) tools).
- ``manifest.json``          the known-truth summary (sizes, expected K, expected audit
                             class per file) for the harness to assert against.

    python scripts/gen_synthetic_data.py
    python scripts/gen_synthetic_data.py --out data/synthetic --seed 0
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

# Genotype codes used internally: 0=hom-ref, 1=het, 2=hom-alt, -1=missing.
_BASES = np.array(["A", "C", "G", "T"])
TAG_LEN = 69
SNP_OFFSET = 33
N_CHROMS = 3


# --------------------------------------------------------------------------- #
# core structured-panel construction
# --------------------------------------------------------------------------- #
def build_panel(
    *,
    n_pops: int = 3,
    n_per_pop: int = 80,
    n_markers: int = 1500,
    fst: float = 0.15,
    missing: float = 0.05,
    mono_frac: float = 0.10,
    seed: int = 0,
) -> dict:
    """A Balding-Nichols structured diploid panel with known population labels.

    Per marker an ancestral frequency p is drawn, then each population's frequency is
    sampled from Beta(p(1-F)/F, (1-p)(1-F)/F) so populations are genuinely differentiated
    (drives PCA / Fst / structure / tree). A ``mono_frac`` of markers is forced
    monomorphic to mimic an uninformative tail.
    """
    rng = np.random.default_rng(seed)
    n_samples = n_pops * n_per_pop
    pop_of = np.repeat(np.arange(n_pops), n_per_pop)

    p_anc = rng.uniform(0.1, 0.9, size=n_markers)
    a = p_anc * (1 - fst) / fst
    b = (1 - p_anc) * (1 - fst) / fst
    # freq[m, pop]
    freq = rng.beta(a[:, None], b[:, None] * np.ones(n_pops)[None, :])

    mono_mask = rng.random(n_markers) < mono_frac
    freq[mono_mask] = rng.choice([0.0, 1.0], size=(mono_mask.sum(), n_pops))

    # Sample two alleles per genotype from the per-(marker, sample) frequency.
    samp_freq = freq[:, pop_of]  # (n_markers, n_samples)
    a1 = (rng.random((n_markers, n_samples)) < samp_freq).astype(np.int8)
    a2 = (rng.random((n_markers, n_samples)) < samp_freq).astype(np.int8)
    gt = (a1 + a2).astype(np.int8)  # 0/1/2
    gt[rng.random((n_markers, n_samples)) < missing] = -1

    chrom = np.array([f"chr{i % N_CHROMS + 1}" for i in range(n_markers)])
    pos = np.zeros(n_markers, dtype=int)
    for c in range(N_CHROMS):
        idx = np.where(np.arange(n_markers) % N_CHROMS == c)[0]
        pos[idx] = np.arange(1, len(idx) + 1) * 1000

    ref_alt = rng.integers(0, 4, size=(n_markers, 2))
    ref_alt[:, 1] = (ref_alt[:, 0] + 1 + rng.integers(0, 3, n_markers)) % 4  # alt != ref
    ref = _BASES[ref_alt[:, 0]]
    alt = _BASES[ref_alt[:, 1]]

    tags = _make_tags(ref, n_markers, rng)
    sample_names = [f"ACC{i:04d}" for i in range(n_samples)]
    pop_names = [f"POP_{chr(ord('A') + p)}" for p in pop_of]

    return {
        "gt": gt, "chrom": chrom, "pos": pos, "ref": ref, "alt": alt, "tags": tags,
        "sample_names": sample_names, "pop_names": pop_names, "n_pops": n_pops,
    }


def _make_tags(ref: np.ndarray, n_markers: int, rng) -> np.ndarray:
    """A unique ~69 bp tag per marker with the REF base planted at SNP_OFFSET."""
    raw = _BASES[rng.integers(0, 4, size=(n_markers, TAG_LEN))]
    raw[:, SNP_OFFSET] = ref
    return np.array(["".join(row) for row in raw])


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #
def _gt_to_vcf_tokens(gt_row: np.ndarray) -> list[str]:
    out = np.empty(gt_row.shape, dtype=object)
    out[gt_row == 0] = "0/0"
    out[gt_row == 1] = "0/1"
    out[gt_row == 2] = "1/1"
    out[gt_row == -1] = "./."
    return out.tolist()


def write_vcf(
    path: Path,
    gt: np.ndarray,
    samples: list[str],
    chrom: np.ndarray,
    pos: np.ndarray,
    ref: np.ndarray,
    alt: np.ndarray,
    *,
    fmt_suffix: str | None = None,
    fmt_header: str = "GT",
) -> None:
    """Write a minimal VCF. ``fmt_suffix`` (e.g. ``:0,0:0`` with fmt_header ``GT:AD:DP``)
    appends fabricated FORMAT fields to every call."""
    contigs = list(dict.fromkeys(chrom.tolist()))
    with path.open("w", newline="\n") as fh:
        fh.write("##fileformat=VCFv4.2\n##source=gigwa_mcp synthetic generator\n")
        fh.write('##FILTER=<ID=PASS,Description="All filters passed">\n')
        for c in contigs:
            fh.write(f"##contig=<ID={c}>\n")
        fh.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        if "AD" in fmt_header:
            fh.write('##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allele depths">\n')
        if "DP" in fmt_header:
            fh.write('##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Read depth">\n')
        fh.write("\t".join(["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER",
                            "INFO", "FORMAT", *samples]) + "\n")
        for i in range(gt.shape[0]):
            toks = _gt_to_vcf_tokens(gt[i])
            if fmt_suffix:
                toks = [t + fmt_suffix for t in toks]
            fixed = [chrom[i], str(int(pos[i])), f"snp{i}", str(ref[i]), str(alt[i]),
                     ".", ".", ".", fmt_header]
            fh.write("\t".join(fixed) + "\t" + "\t".join(toks) + "\n")


def write_dart_snp_xlsx(path: Path, panel: dict) -> None:
    """2-row DArTseq SNP report. ref-row cell=1 if ref allele detected, alt-row cell=1
    if alt detected; ``-`` for missing — the exact encoding the importer inverts."""
    gt, samples = panel["gt"], panel["sample_names"]
    n_markers = gt.shape[0]
    ref_cell = np.where(np.isin(gt, [0, 1]), "1", "0").astype(object)
    alt_cell = np.where(np.isin(gt, [1, 2]), "1", "0").astype(object)
    ref_cell[gt == -1] = "-"
    alt_cell[gt == -1] = "-"

    header = ["AlleleID", "AlleleSequence", "SNP", "SnpPosition", "CallRate", *samples]
    rows = [header]
    for i in range(n_markers):
        snp = f"{SNP_OFFSET}:{panel['ref'][i]}>{panel['alt'][i]}"
        mid = f"m{i}"
        cr = float((gt[i] != -1).mean())
        rows.append([f"{mid}|F|0", panel["tags"][i], None, SNP_OFFSET, cr, *ref_cell[i].tolist()])
        rows.append([f"{mid}|F|0-{snp}", panel["tags"][i], snp, SNP_OFFSET, cr, *alt_cell[i].tolist()])
    pd.DataFrame(rows).to_excel(path, header=False, index=False, sheet_name="Report")


def write_silico_xlsx(path: Path, panel: dict) -> None:
    """1-row presence/absence Silico-DArT report (clone present if any alt allele)."""
    gt, samples = panel["gt"], panel["sample_names"]
    present = np.where(np.isin(gt, [1, 2]), "1", "0").astype(object)
    present[gt == -1] = "-"
    header = ["CloneID", "AlleleSequence", "CallRate", *samples]
    rows = [header]
    for i in range(gt.shape[0]):
        cr = float((gt[i] != -1).mean())
        rows.append([f"clone{i}", panel["tags"][i], cr, *present[i].tolist()])
    pd.DataFrame(rows).to_excel(path, header=False, index=False, sheet_name="Report")


def write_reference_fasta(path: Path, panel: dict, seed: int = 1) -> dict:
    """Plant each marker's tag on a synthetic chromosome at a recorded coordinate."""
    rng = np.random.default_rng(seed)
    tags, chrom = panel["tags"], panel["chrom"]
    planted: dict[str, list] = {}
    by_chrom: dict[str, list[str]] = {}
    for i in range(len(tags)):
        c = chrom[i]
        seq_parts = by_chrom.setdefault(c, [])
        filler = "".join(_BASES[rng.integers(0, 4, size=rng.integers(120, 200))])
        seq_parts.append(filler)
        seq_parts.append(tags[i])
    with path.open("w", newline="\n") as fh:
        for c, parts in by_chrom.items():
            fh.write(f">{c}\n")
            seq = "".join(parts)
            for j in range(0, len(seq), 70):
                fh.write(seq[j:j + 70] + "\n")
    return planted


def write_metadata(good: Path, bad: Path, panel: dict, seed: int = 2) -> None:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "individual": panel["sample_names"],
        "population": panel["pop_names"],
        "collection_year": rng.integers(1990, 2020, len(panel["sample_names"])),
    })
    df.to_csv(good, sep="\t", index=False)
    # Wrong id-column header so validate_metadata has something to complain about.
    df.rename(columns={"individual": "sample_code"}).to_csv(bad, sep="\t", index=False)


# --------------------------------------------------------------------------- #
# adversarial panels (one VCF each) — designed to trip audit_import_quality
# --------------------------------------------------------------------------- #
def _flat_meta(n_markers: int, rng):
    chrom = np.array([f"chr{i % N_CHROMS + 1}" for i in range(n_markers)])
    pos = np.zeros(n_markers, dtype=int)
    for c in range(N_CHROMS):
        idx = np.where(np.arange(n_markers) % N_CHROMS == c)[0]
        pos[idx] = np.arange(1, len(idx) + 1) * 1000
    ra = rng.integers(0, 4, size=(n_markers, 2))
    ra[:, 1] = (ra[:, 0] + 1) % 4
    return chrom, pos, _BASES[ra[:, 0]], _BASES[ra[:, 1]]


def write_adversarial(out: Path, *, n_samples=100, n_markers=200, seed=7) -> dict:
    rng = np.random.default_rng(seed)
    samples = [f"S{i:03d}" for i in range(n_samples)]

    def emit(name, gt, **kw):
        chrom, pos, ref, alt = _flat_meta(gt.shape[0], rng)
        write_vcf(out / name, gt, samples, chrom, pos, ref, alt, **kw)

    # all-het: nearly every call 0/1 -> mean Ho ~ 1 (DArT 2-row mis-call signature).
    allhet = np.ones((n_markers, n_samples), dtype=np.int8)
    allhet[rng.random((n_markers, n_samples)) < 0.02] = -1
    emit("broken_allhet.vcf", allhet)

    # lost-hom-alt: het ~0.6, hom-ref ~0.4, *zero* hom-alt, no missing, depth all zero.
    r = rng.random((n_markers, n_samples))
    losthomalt = np.where(r < 0.6, 1, 0).astype(np.int8)  # only 0/0 and 0/1
    emit("broken_losthomalt.vcf", losthomalt, fmt_suffix=":0,0:0", fmt_header="GT:AD:DP")

    # monomorphic: ~95% markers fixed hom-alt (kept by Gigwa), ~5% polymorphic.
    mono = np.full((n_markers, n_samples), 2, dtype=np.int8)
    poly_idx = rng.choice(n_markers, size=max(1, n_markers // 20), replace=False)
    mono[poly_idx] = rng.integers(0, 3, size=(len(poly_idx), n_samples))
    mono[rng.random((n_markers, n_samples)) < 0.10] = -1  # ~10% missing
    emit("suspect_monomorphic.vcf", mono)

    return {"n_samples": n_samples, "n_markers": n_markers}


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/synthetic")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n-per-pop", type=int, default=80)
    ap.add_argument("--n-markers", type=int, default=1500)
    ap.add_argument("--scale-per-pop", type=int, default=167)  # ~500 samples
    ap.add_argument("--scale-markers", type=int, default=5000)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Generating main panel ({3 * args.n_per_pop} samples x {args.n_markers} markers)...")
    panel = build_panel(n_per_pop=args.n_per_pop, n_markers=args.n_markers, seed=args.seed)
    write_vcf(out / "clean.vcf", panel["gt"], panel["sample_names"],
              panel["chrom"], panel["pos"], panel["ref"], panel["alt"])
    print("  clean.vcf")
    write_dart_snp_xlsx(out / "dartseq_snp.xlsx", panel)
    print("  dartseq_snp.xlsx")
    write_silico_xlsx(out / "silico_dart.xlsx", panel)
    print("  silico_dart.xlsx")
    write_reference_fasta(out / "reference.fasta", panel)
    print("  reference.fasta")
    write_metadata(out / "metadata.tsv", out / "metadata_bad.tsv", panel)
    print("  metadata.tsv + metadata_bad.tsv")

    print("Generating adversarial panels...")
    adv = write_adversarial(out)
    print("  broken_allhet.vcf, broken_losthomalt.vcf, suspect_monomorphic.vcf")

    # Tiny panel: 3 samples, 1 polymorphic marker — trips the PCA "<2 polymorphic" and
    # structure "<4 samples" guards (edge-case path).
    tiny_gt = np.array([[0, 2, 0], [0, 0, 0], [2, 2, 2], [0, 0, 0]], dtype=np.int8)
    tchrom = np.array(["chr1"] * 4)
    tpos = np.array([1000, 2000, 3000, 4000])
    tref, talt = np.array(list("ACGT")), np.array(list("CGTA"))
    write_vcf(out / "tiny.vcf", tiny_gt, ["T0", "T1", "T2"], tchrom, tpos, tref, talt)
    print("  tiny.vcf")

    print(f"Generating scale panel ({3 * args.scale_per_pop} samples x {args.scale_markers} markers)...")
    scale = build_panel(n_per_pop=args.scale_per_pop, n_markers=args.scale_markers,
                        seed=args.seed + 1)
    write_vcf(out / "scale.vcf", scale["gt"], scale["sample_names"],
              scale["chrom"], scale["pos"], scale["ref"], scale["alt"])
    print("  scale.vcf")

    manifest = {
        "seed": args.seed,
        "main": {
            "n_samples": len(panel["sample_names"]),
            "n_markers": int(panel["gt"].shape[0]),
            "n_pops": panel["n_pops"],
            "expected_k": panel["n_pops"],
            "populations": sorted(set(panel["pop_names"])),
        },
        "scale": {
            "n_samples": len(scale["sample_names"]),
            "n_markers": int(scale["gt"].shape[0]),
        },
        "adversarial": adv,
        "expected_audit_class": {
            "clean.vcf": "OK",
            "broken_allhet.vcf": "BROKEN",
            "broken_losthomalt.vcf": "BROKEN",
            "suspect_monomorphic.vcf": "SUSPECT",
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest.json\nDone -> {out.resolve()}")


if __name__ == "__main__":
    main()
