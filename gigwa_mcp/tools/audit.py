"""Import-quality audit (read-only): find databases imported with encoding artifacts.

Two real-world failures motivated this detective tool: the DArTseq 2-row report whose
reference homozygotes were imported as heterozygous (~95% het), and a remapped VCF that
lost its homozygous-alt class and forced missing calls to ``0/0`` (call rate 1.0, zero
``1/1``). Both are visible from genotype classes alone, so this tool walks the instance
(or a single variant set), computes cheap GT diagnostics per run, classifies each, writes
a CSV and returns a ranked summary. Nothing is written back to Gigwa.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..analysis import load_genotypes, stats
from ..analysis.genotypes import GenotypeMatrix
from ..analysis.results import write_csv
from ..server import get_client, mcp

# CSV / DataFrame column order (also the per-run diagnostic keys, plus status/reasons).
_COLUMNS = [
    "variant_set_db_id",
    "status",
    "n_variants",
    "n_samples",
    "call_rate",
    "het_frac",
    "hom_ref_frac",
    "hom_alt_frac",
    "mean_ho",
    "monomorphic_frac",
    "depth_all_zero",
    "reasons",
]

_STATUS_ORDER = {"BROKEN": 0, "SUSPECT": 1, "OK": 2, "ERROR": 3}


def _diagnose(gm: GenotypeMatrix) -> dict:
    """Per-run genotype-class diagnostics, all as fractions of *called* genotypes."""
    gt = gm.gt
    called = np.asarray(gt.is_called())
    n_called = int(called.sum())
    call_rate = float(called.mean()) if called.size else 0.0
    ho, _ = stats.heterozygosity_per_sample(gt)
    maf_v = stats.maf(gt.count_alleles())
    safe = n_called or 1
    return {
        "n_variants": int(gm.n_variants),
        "n_samples": int(gm.n_samples),
        "call_rate": call_rate,
        "het_frac": int(np.asarray(gt.is_het()).sum()) / safe,
        "hom_ref_frac": int(np.asarray(gt.is_hom_ref()).sum()) / safe,
        "hom_alt_frac": int(np.asarray(gt.is_hom_alt()).sum()) / safe,
        "mean_ho": float(np.nanmean(ho)) if ho.size and not np.all(np.isnan(ho)) else float("nan"),
        "monomorphic_frac": float(np.mean(np.nan_to_num(maf_v, nan=0.0) <= 0.0)) if maf_v.size else 0.0,
        "depth_all_zero": bool(gm.depth_all_zero),
    }


def _classify(
    d: dict,
    *,
    het_threshold: float = 0.6,
    complete_call_rate: float = 0.999,
    monomorphic_threshold: float = 0.9,
    homalt_deficit_ratio: float = 0.2,
    homalt_min_expected: float = 0.01,
) -> tuple[str, list[str]]:
    """Classify one run's diagnostics into OK / SUSPECT / BROKEN with reasons.

    Pure function (no I/O) so it is unit-testable against hand-built diagnostics.
    """
    reasons: list[str] = []
    broken = suspect = False

    het, hom_alt = d["het_frac"], d["hom_alt_frac"]
    mean_ho, call_rate, mono = d["mean_ho"], d["call_rate"], d["monomorphic_frac"]

    # Lost-hom-alt detection via an HWE deficit, not a raw threshold: derive the alt
    # allele frequency q from the observed genotype fractions and compare hom-alt to
    # its HWE expectation q². We only flag when hom-alt is far below expectation AND
    # that expectation is non-negligible — so a mostly-monomorphic / low-MAF panel
    # (where near-zero hom-alt is genuine biology) is NOT mistaken for the
    # collapsed-homozygote artifact.
    q = (het + 2.0 * hom_alt) / 2.0
    exp_hom_alt = q * q
    no_hom_alt = exp_hom_alt > homalt_min_expected and hom_alt < homalt_deficit_ratio * exp_hom_alt

    if not np.isnan(mean_ho) and mean_ho > het_threshold:
        broken = True
        reasons.append(
            f"implausibly high heterozygosity (mean Ho={mean_ho:.2f}) — reference "
            "homozygotes likely mis-called as het (DArT 2-row artifact)"
        )
    if no_hom_alt:
        broken = True
        reasons.append(
            f"homozygous-alt genotypes far below HWE expectation (observed "
            f"{hom_alt:.4f} vs expected {exp_hom_alt:.3f} at alt freq {q:.2f}) — "
            "lost hom-alt class"
        )
    if call_rate > complete_call_rate:
        # On its own a suspicious "too perfect" signal; with no-hom-alt it's the
        # collapsed-homozygote signature (missing forced to 0/0), so it corroborates BROKEN.
        suspect = suspect or not no_hom_alt
        reasons.append(
            f"no missing calls (call rate={call_rate:.4f}) — missing genotypes likely "
            "forced to 0/0"
        )
    if mono > monomorphic_threshold:
        suspect = True
        reasons.append(f"{mono:.0%} of markers monomorphic — low informativeness")
    if d.get("depth_all_zero"):
        # Depth present everywhere but all zero (AD=0,0 / DP=0): the VCF was synthesised
        # from genotype calls with fabricated FORMAT fields — depth/likelihoods are
        # unusable and the same converter often miscalls GT (see the high-het / lost
        # hom-alt rules above, which mark BROKEN when they also fire).
        suspect = True
        reasons.append(
            "AD/DP depth fields present but uniformly zero (e.g. 0,0:0) — VCF "
            "synthesised from genotype calls with fabricated depth/likelihoods"
        )

    status = "BROKEN" if broken else ("SUSPECT" if suspect else "OK")
    return status, reasons


@mcp.tool()
def audit_import_quality(
    variant_set_db_id: str | None = None,
    max_markers: int = 1000,
    max_samples: int = 300,
    het_threshold: float = 0.6,
    complete_call_rate: float = 0.999,
    monomorphic_threshold: float = 0.9,
    output_dir: str | None = None,
) -> str:
    """Scan a Gigwa instance for databases imported with genotype-encoding artifacts.

    With no ``variant_set_db_id`` this audits **every run on the instance**; pass one to
    audit a single variant set. For each run it pulls a bounded genotype sample (up to
    ``max_markers`` markers × ``max_samples`` callsets) via paged BrAPI
    ``search/allelematrix`` — cheap and constant-cost regardless of how large the variant
    set is, so it is safe to run across a whole production instance without exporting
    multi-GB VCFs. The aggregate genotype-class fractions it needs are estimated tightly
    from the sample (a *true* zero hom-alt class stays zero; a rare-but-real one shows up).
    It flags two import failure modes plus two weaker signals:

    - **BROKEN** — cohort mean Ho above ``het_threshold`` (DArT 2-row mis-call), or
      homozygous-alt genotypes far below their HWE expectation given the alt-allele
      frequency (lost hom-alt class; the HWE test avoids false positives on low-MAF /
      mostly-monomorphic panels where near-zero hom-alt is genuine).
    - **SUSPECT** — call rate above ``complete_call_rate`` (no missing data, often missing
      forced to 0/0), monomorphic fraction above ``monomorphic_threshold``, or AD/DP depth
      fields present but uniformly zero (a VCF synthesised from genotype calls with
      fabricated depth/likelihoods — the same converter often miscalls GT too).

    Writes ``import_quality_scan.csv`` (one row per run) under ``output_dir`` (default
    ``./gigwa_results/``) and returns a summary ranked worst-first. Read-only — it never
    modifies Gigwa.
    """
    client = get_client()
    if variant_set_db_id:
        targets = [variant_set_db_id]
    else:
        targets = [
            vs.get("variantSetDbId")
            for vs in client.list_variantsets()
            if vs.get("variantSetDbId")
        ]
    if not targets:
        return "No variant sets found on the Gigwa instance to audit."

    rows: list[dict] = []
    for vs in targets:
        try:
            gm = load_genotypes(
                client,
                vs,
                max_markers=max_markers or None,
                max_samples=max_samples or None,
                with_depth=True,
                method="allelematrix",
            )
        except Exception as exc:  # one bad run shouldn't sink the whole scan
            rows.append(
                {c: None for c in _COLUMNS}
                | {"variant_set_db_id": vs, "status": "ERROR", "reasons": str(exc)}
            )
            continue
        diag = _diagnose(gm)
        status, reasons = _classify(
            diag,
            het_threshold=het_threshold,
            complete_call_rate=complete_call_rate,
            monomorphic_threshold=monomorphic_threshold,
        )
        rows.append(
            {"variant_set_db_id": vs, "status": status, "reasons": "; ".join(reasons), **diag}
        )

    df = pd.DataFrame(rows, columns=_COLUMNS)
    df = df.sort_values(by="status", key=lambda s: s.map(_STATUS_ORDER)).reset_index(drop=True)

    out = Path(output_dir) if output_dir else Path.cwd() / "gigwa_results"
    out.mkdir(parents=True, exist_ok=True)
    path = write_csv(df, out, "import_quality_scan.csv")

    counts = df["status"].value_counts()
    tally = ", ".join(f"{int(counts[s])} {s}" for s in _STATUS_ORDER if s in counts)
    lines = [f"Import-quality audit: {len(df)} run(s) scanned — {tally}"]

    flagged = df[df["status"].isin(("BROKEN", "SUSPECT", "ERROR"))]
    for _, r in flagged.iterrows():
        if r["status"] == "ERROR":
            lines.append(f"  [ERROR] {r['variant_set_db_id']}: {r['reasons']}")
            continue
        lines.append(
            f"  [{r['status']}] {r['variant_set_db_id']} "
            f"(sampled {int(r['n_variants'])}×{int(r['n_samples'])}): "
            f"call_rate={r['call_rate']:.3f} het={r['het_frac']:.2f} "
            f"hom_alt={r['hom_alt_frac']:.3f} mean_Ho={r['mean_ho']:.2f} "
            f"monomorphic={r['monomorphic_frac']:.2f}"
            + (" depth=all-zero" if r["depth_all_zero"] else "")
        )
        lines.append(f"      {r['reasons']}")
    if flagged.empty:
        lines.append("  No encoding artifacts detected — all runs look OK.")

    lines.append(f"File: {path}")
    return "\n".join(lines)
