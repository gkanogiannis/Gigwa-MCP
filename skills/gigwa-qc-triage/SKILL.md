---
name: gigwa-qc-triage
description: "Run the full quality-control suite on a Gigwa variant set and give a go/no-go verdict for downstream analysis. Use this when a user wants to triage or vet the quality of an already-imported run (identified by its variantSetDbId) before trusting diversity or association results. Runs call rate (worst samples and markers), heterozygosity outliers that flag contamination or off-types, duplicate/clonal accession detection, a MAF/missingness filter preview, and an import-quality audit for genotype-encoding artifacts, then summarises the key numbers into a clear go/no-go recommendation. Trigger on 'QC this Gigwa run', 'is this dataset good enough to analyse', 'check call rates and flag bad samples/markers', or 'triage data quality'. Keywords: Gigwa, quality control, QC, call rate, heterozygosity, duplicate accessions, MAF filter, missingness, import audit, go/no-go."
license: Apache-2.0
metadata:
  author: "Anestis Gkanogiannis"
  version: "1.0.0"
  server: "gigwa-mcp"
  homepage: "https://github.com/gkanogiannis/Gigwa-MCP"
compatibility: "Requires the gigwa-mcp MCP server connected to the agent over stdio, driving a Gigwa 2.x instance via REST/BrAPI. Read-only: works anonymously on public instances (set GIGWA_URL; GIGWA_USER/GIGWA_PASS optional). Needs a variantSetDbId for an already-imported run."
---

# Gigwa QC triage

## Overview

Vet an **already-imported** variant set and return a go/no-go verdict for downstream
analysis. Runs the same read-only QC tools as the import skill, minus the import, then
turns the numbers into a recommendation. Drives the `gigwa-mcp` MCP server's `qc_*` and
`audit_import_quality` tools.

## When to use vs gigwa-import-and-qc

- **This skill** — the data is *already in Gigwa*; you just need to know if it's trustworthy.
- **gigwa-import-and-qc** — you still need to *load* the data first (it imports, then runs
  exactly these QC steps).

## Prerequisites

- The `gigwa-mcp` server is connected; read-only, so anonymous access on a public instance
  is fine.
- A `variant_set_db_id` (`MODULE§project§run`) from `list_variant_sets()` (or the
  **gigwa-explore-instance** skill).

## Step-by-step workflow

Run each step on the same `variant_set_db_id`, noting the flag to watch:

1. `qc_call_rate(variant_set_db_id)` — overall call rate and the **worst samples/markers**
   (writes `call_rate_samples.csv`, `call_rate_markers.csv`). Watch: entities far below the
   `min_*_call_rate` defaults (0.5).
2. `qc_heterozygosity(variant_set_db_id)` — per-sample observed heterozygosity; **heed the
   high-Ho warning** (contamination, off-types, or a het-miscoding import).
3. `qc_duplicate_accessions(variant_set_db_id)` — clones / mislabelled duplicates via
   pairwise IBS (writes `duplicate_pairs.csv`, `duplicate_groups.csv`).
4. `qc_maf_filter(variant_set_db_id)` — how many markers a MAF/missingness filter would drop
   (default `maf_threshold=0.05`, `max_missing=0.5`).
5. `audit_import_quality(variant_set_db_id)` — confirm there is no genotype-encoding
   artifact (writes `import_quality_scan.csv`).

## Verdict rubric

Turn the numbers into one of three calls (see `references/qc-thresholds.md` for the table):

- **GO** — call rates healthy, no Ho outlier cluster, few/no unexpected duplicates, MAF
  filter drops a reasonable fraction, audit **OK**.
- **GO WITH CAVEATS** — usable after dropping the worst samples/markers or de-duplicating;
  state exactly what to remove.
- **NO-GO** — audit **BROKEN/SUSPECT**, pervasive low call rate, or an implausible
  heterozygosity/monomorphic profile → fix the import (see **gigwa-import-and-qc**) before
  any diversity analysis.

Always report the **key numbers** behind the verdict, not just the verdict.

## Output files

Under `./gigwa_results/<module>/`: `call_rate_samples.csv`, `call_rate_markers.csv`,
`heterozygosity_samples.csv`, `duplicate_pairs.csv`, `duplicate_groups.csv`,
`marker_filter_stats.csv`, `import_quality_scan.csv`.

## See also

- `references/qc-thresholds.md` — per-tool thresholds, interpretation, decision table,
  EDAM annotations.
- **gigwa-diversity-report** — the downstream analysis this verdict gates.
- **gigwa-import-and-qc** — if the verdict is NO-GO because of the import.
