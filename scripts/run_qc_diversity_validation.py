"""Run the QC/diversity tools against a variant set (read-only smoke test).

Reads connection settings from the environment / a local .env file. Pass a
variantSetDbId and an optional marker cap; with method="allelematrix" the tools
estimate from a bounded, server-side marker subset instead of a full VCF export.

The O(samples²) tools (kinship, duplicates, tree) are only run when --heavy is given,
since their output matrix is impractical on multi-thousand-sample sets.

    python scripts/run_qc_diversity_validation.py "MODULE§1§run"
    python scripts/run_qc_diversity_validation.py "MODULE§1§run" --max-markers 300
    python scripts/run_qc_diversity_validation.py "MODULE§1§run" --heavy
"""
from __future__ import annotations

import argparse
import time
import traceback

from gigwa_mcp.tools import diversity, qc

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("variant_set_db_id", help="e.g. MODULE§1§run")
parser.add_argument("--max-markers", type=int, default=2000)
parser.add_argument("--method", default="allelematrix", choices=["vcf", "allelematrix"])
parser.add_argument("--heavy", action="store_true", help="also run O(samples^2) tools")
args = parser.parse_args()


def fn(tool):
    return getattr(tool, "fn", tool)


def run(label, tool, **kwargs):
    t0 = time.time()
    try:
        out = fn(tool)(
            variant_set_db_id=args.variant_set_db_id,
            max_markers=args.max_markers,
            method=args.method,
            **kwargs,
        )
        head = "\n".join(out.splitlines()[:4])
        print(f"\n### {label}  [{time.time() - t0:6.1f}s]\n{head}", flush=True)
    except Exception as exc:  # keep going so one failure doesn't abort the sweep
        print(f"\n### {label}  [{time.time() - t0:6.1f}s]  ERROR: {exc}", flush=True)
        traceback.print_exc()


print("=" * 70, f"\n{args.variant_set_db_id}  (method={args.method}, max_markers={args.max_markers})", flush=True)
run("qc_call_rate", qc.qc_call_rate)
run("qc_heterozygosity", qc.qc_heterozygosity)
run("qc_maf_filter", qc.qc_maf_filter)
run("diversity_summary", diversity.diversity_summary)
run("diversity_pca", diversity.diversity_pca)
run("diversity_structure", diversity.diversity_structure)
run("diversity_core_collection", diversity.diversity_core_collection, fraction=0.1)

if args.heavy:
    run("qc_duplicate_accessions", qc.qc_duplicate_accessions)
    run("diversity_kinship", diversity.diversity_kinship)
    run("diversity_tree", diversity.diversity_tree)
else:
    print("\n(skipping O(samples^2) tools: kinship, duplicates, tree — pass --heavy to run them)")

print("\nDONE", flush=True)
