---
name: gigwa-import-and-qc
description: "Import a genotype dataset into a Gigwa server and run standard quality control. Use this when a user wants to load a DArTseq SNP/Silico xlsx report or a VCF (.vcf/.vcf.gz) into a Gigwa database, project and run, then verify the import is clean. Covers optional genome-anchoring of DArTseq tag sequences to a reference FASTA, confirming variant and sample counts, and the full QC sweep: per-sample and per-marker call rate, heterozygosity outliers, MAF and missingness filtering, duplicate or clonal accessions, plus an import-quality audit that catches genotype-encoding artifacts. Trigger on requests like 'import this DArTseq report into Gigwa', 'load a VCF and QC it', or 'ingest genotypes and tell me if the import looks clean'. Keywords: Gigwa, DArTseq, VCF import, genotyping, call rate, MAF, heterozygosity, duplicate accessions, import audit."
license: Apache-2.0
metadata:
  author: "Anestis Gkanogiannis"
  version: "1.0.0"
  server: "gigwa-mcp"
  homepage: "https://github.com/gkanogiannis/Gigwa-MCP"
compatibility: "Requires the gigwa-mcp MCP server connected to the agent over stdio, driving a Gigwa 2.x instance via REST/BrAPI. Import needs write access, so set GIGWA_URL and GIGWA_USER/GIGWA_PASS (anonymous is read-only). Input files (xlsx/VCF/FASTA) must be readable by the server."
---

# Gigwa import + QC

## Overview

Load a genotype dataset into a Gigwa instance and immediately check whether the import
is trustworthy. This chains a single import call with the full read-only QC suite and an
import-quality audit, then ends with a clean / not-clean judgement. It drives the
`gigwa-mcp` MCP server's tools — the tools do the work; this skill sequences them.

## When to use / when not

- **Use** when the user is *ingesting new data*: a DArTseq SNP/Silico xlsx report or a
  `.vcf`/`.vcf.gz`, and wants confidence the import worked.
- **Don't use** to re-QC a run that is *already imported* — use **gigwa-qc-triage**
  instead (same QC steps, no import). For diversity/structure after QC passes, use
  **gigwa-diversity-report**.

## Prerequisites

- The `gigwa-mcp` server is connected and `gigwa_server_info` succeeds.
- Import **writes** data, so the server must be authenticated: `GIGWA_USER` /
  `GIGWA_PASS` (anonymous access is read-only and cannot import).
- Input files are on a path the **server** process can read.
- Decide the target coordinates up front: `module` (database), `project`, `run`.

## Decide the branch

| Input | Tool | Key file param |
|---|---|---|
| DArTseq xlsx (SNP and/or SilicoDArT) | `import_dartseq` | `snp_xlsx`, `silico_xlsx` |
| VCF (`.vcf` / `.vcf.gz`) | `import_vcf` | `vcf_path` |

## Optional: genome-anchor DArTseq first

DArTseq tags are unplaced by default. To assign genomic positions, align the tag
sequences to a reference **before** import:

1. `map_dartseq_to_reference(snp_xlsx, reference_fasta, min_mapq=20, preset="sr", backend="auto")`
   → writes `dartseq_positions.csv`.
2. Pass that CSV back as `import_dartseq(..., positions_csv="…/dartseq_positions.csv")`,
   **or** let `import_dartseq(..., reference_fasta=…)` do the alignment inline.

Skip this whole step for VCF (already positioned) or when unplaced markers are fine.

## Step-by-step workflow

1. **Import.**
   - DArTseq: `import_dartseq(module, project, run, snp_xlsx=…, silico_xlsx=…, ploidy=2,
     skip_monomorphic=False, clear_project_data=False)`.
   - VCF: `import_vcf(vcf_path, module, project, run, ploidy=2)`.
   - Long imports: pass `wait=False` to get a **progress token**, then poll with
     `get_import_progress(progress_token)` and cancel with `abort_import(progress_token)`
     if needed.
2. **Confirm counts** with `list_variant_sets()`. Read back the new run's variant and
   call-set counts, and **capture its exact `variantSetDbId`** (`MODULE§project§run`) —
   every step below needs it.
3. **QC sweep** (all take the `variant_set_db_id` from step 2):
   - `qc_call_rate(variant_set_db_id)` — worst samples/markers.
   - `qc_heterozygosity(variant_set_db_id)` — outliers (heed the high-Ho warning:
     inflated heterozygosity often means contamination or a miscoded import).
   - `qc_maf_filter(variant_set_db_id)` — how many markers a MAF/missingness filter drops.
   - `qc_duplicate_accessions(variant_set_db_id)` — clones / mislabelled duplicates.
4. **Audit** with `audit_import_quality(variant_set_db_id)` — catches genotype-encoding
   artifacts (e.g. everything called, no heterozygotes, suspicious monomorphic fraction).
5. **Summarise** flagged samples/markers and give a clear verdict on whether the import
   looks clean.

## Interpreting results

A *bad* import typically shows one or more of: dataset-wide observed heterozygosity that
is implausibly high, a call rate pinned at ~100% (nothing missing — real genotyping never
is), a very high monomorphic fraction, or the audit ranking a run **BROKEN**/**SUSPECT**.
See `references/import-and-qc.md` for the audit thresholds and how to read them.

## Output files

CSVs land under `./gigwa_results/<module>/`: `call_rate_samples.csv`,
`call_rate_markers.csv`, `heterozygosity_samples.csv`, `marker_filter_stats.csv`,
`duplicate_pairs.csv`, `duplicate_groups.csv`, `import_quality_scan.csv`, and
`dartseq_positions.csv` (if anchored).

## See also

- `references/import-and-qc.md` — id format, DArTseq calling/anchoring, progress-token
  semantics, audit thresholds, EDAM annotations.
- **gigwa-qc-triage** — the same QC on an already-imported run.
- **gigwa-diversity-report** — diversity/structure once QC passes.
