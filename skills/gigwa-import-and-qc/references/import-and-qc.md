# Reference: import + QC

## The `variantSetDbId` (a.k.a. `variant_set_db_id`)

Gigwa's data model nests **instance → database (module) → project → run**. A single run is
addressed by a BrAPI id of the form:

```
MODULE§project§run
```

where `§` is the section sign (U+00A7). You do not build this by hand — after import, call
`list_variant_sets()` and copy the exact `variantSetDbId` string it returns; pass it
verbatim to every QC/diversity/search tool as `variant_set_db_id`.

## DArTseq genotype calling and reference anchoring

DArTseq reports come as two-row-per-marker allele tables (SNP report and/or SilicoDArT
report). `import_dartseq` calls diploid genotypes from those rows. Tags are **unplaced**
unless you supply positions:

- `map_dartseq_to_reference(snp_xlsx, reference_fasta, output_dir=None, min_mapq=20,
  preset="sr", backend="auto")` aligns the tag sequences and writes `dartseq_positions.csv`.
  - `min_mapq` — minimum mapping quality to accept a placement (default 20).
  - `preset` — minimap2 preset (`sr` = short read, the default).
  - `backend` — `auto` picks an available aligner.
- Feed the CSV into `import_dartseq(..., positions_csv=…)`, or pass
  `import_dartseq(..., reference_fasta=…)` to align during import.

### `import_dartseq` parameters

| Param | Default | Meaning |
|---|---|---|
| `module`, `project`, `run` | — | Target coordinates (required). |
| `snp_xlsx` | None | DArTseq SNP report path. |
| `silico_xlsx` | None | SilicoDArT report path. |
| `technology` | `"DArTseq"` | Assay label stored on the run. |
| `ploidy` | 2 | Sample ploidy. |
| `skip_monomorphic` | False | Drop non-variant markers on import. |
| `clear_project_data` | False | Wipe existing project data first (destructive). |
| `reference_fasta` | None | Align tags inline against this FASTA. |
| `positions_csv` | None | Reuse a precomputed `dartseq_positions.csv`. |
| `min_mapq` | 20 | Min MAPQ when aligning inline. |
| `wait` | True | Block until done; `False` returns a progress token. |

### `import_vcf` parameters

`import_vcf(vcf_path, module, project, run, technology=None, ploidy=2,
skip_monomorphic=False, clear_project_data=False, wait=True)` — VCFs are already
positioned, so no anchoring step.

## Progress-token semantics (long imports)

With `wait=False`, import returns a **progress token** instead of blocking:

- `get_import_progress(progress_token)` — poll status/percentage.
- `abort_import(progress_token)` — cancel a running import (or other process).

## Audit thresholds (`audit_import_quality`)

`audit_import_quality(variant_set_db_id=None, max_markers=1000, max_samples=300,
het_threshold=0.6, complete_call_rate=0.999, monomorphic_threshold=0.9)`:

- `het_threshold` (0.6) — mean observed heterozygosity above this is suspicious
  (contamination or a het-miscoding import bug).
- `complete_call_rate` (0.999) — a call rate this high means essentially nothing is
  missing, which real genotyping never achieves → likely a missing-data encoding bug.
- `monomorphic_threshold` (0.9) — a fraction of monomorphic markers above this suggests
  alleles collapsed on import.

Each run is ranked roughly **OK / SUSPECT / BROKEN** from these signals. Called with **no**
`variant_set_db_id`, the audit scans the whole instance. Output: `import_quality_scan.csv`.

## EDAM annotations (from `TOOL_CATALOG`)

| Tool | operation | topic |
|---|---|---|
| `map_dartseq_to_reference` | operation_0292 Sequence alignment | topic_0102 Mapping |
| `import_dartseq` | operation_3196 Genotyping | topic_0199 Genetic variation |
| `import_vcf` | operation_3196 Genotyping | topic_0199 Genetic variation |
| `get_import_progress` | operation_2409 Data handling | topic_3071 Data management |
| `abort_import` | operation_2409 Data handling | topic_3071 Data management |
| `list_variant_sets` | operation_2422 Data retrieval | topic_0199 Genetic variation |
| `qc_call_rate` | operation_2238 Statistical calculation | topic_0199 Genetic variation |
| `qc_heterozygosity` | operation_2238 Statistical calculation | topic_3056 Population genetics |
| `qc_maf_filter` | operation_3675 Variant filtering | topic_0199 Genetic variation |
| `qc_duplicate_accessions` | operation_3432 Clustering | topic_3056 Population genetics |
| `audit_import_quality` | operation_2428 Validation | topic_3071 Data management |
