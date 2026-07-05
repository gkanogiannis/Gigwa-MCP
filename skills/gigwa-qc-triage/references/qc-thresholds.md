# Reference: QC thresholds and verdict

## Per-tool tunable thresholds (defaults)

| Tool | Threshold params (default) | What it controls |
|---|---|---|
| `qc_call_rate` | `min_sample_call_rate=0.5`, `min_marker_call_rate=0.5` | Flags samples/markers below the fraction of non-missing calls. |
| `qc_heterozygosity` | `outlier_sd=3.0` | Flags samples whose observed heterozygosity is more than this many SD from the mean. |
| `qc_duplicate_accessions` | `similarity_threshold=0.95`, `max_markers=5000` | IBS similarity above which a pair is called duplicate/clonal (subsampled to `max_markers`). |
| `qc_maf_filter` | `maf_threshold=0.05`, `max_missing=0.5` | Previews how many markers a MAF + missingness filter would remove. |
| `audit_import_quality` | `het_threshold=0.6`, `complete_call_rate=0.999`, `monomorphic_threshold=0.9` | Detects genotype-encoding artifacts (see below). |

All QC/audit tools also accept `max_markers`, `method="vcf"`, `region`, and `output_dir`.

## Interpretation guide

- **Low call rate** — a handful of bad samples/markers is normal; drop them. Pervasive low
  call rate (or the opposite, ~100% with nothing missing) points at an import/encoding
  problem, not biology.
- **High Ho** — observed heterozygosity well above expectation flags contamination,
  sample mix-ups, or heterozygous miscalls. A whole-dataset high-Ho warning is a strong
  NO-GO signal.
- **Duplicates** — clusters in `duplicate_groups.csv` are clones or mislabels; expected in
  clonal crops, suspicious in a diversity panel.
- **MAF/missingness** — the filter preview trades marker count for quality; a huge drop at
  MAF 0.05 means many rare/near-monomorphic markers.

## Audit ranking

`audit_import_quality` combines the three thresholds into a rough rank:

- **OK** — heterozygosity, call rate and monomorphic fraction all plausible.
- **SUSPECT** — one signal off (e.g. Ho above `het_threshold`).
- **BROKEN** — call rate ≥ `complete_call_rate` (nothing missing) and/or monomorphic
  fraction ≥ `monomorphic_threshold`: alleles almost certainly collapsed/miscoded on import.

## Go/no-go decision table

| Signal | GO | GO WITH CAVEATS | NO-GO |
|---|---|---|---|
| Call rate | healthy across the board | a few low entities → drop them | pervasively low or pinned ~100% |
| Heterozygosity | no outlier cluster | isolated outliers → drop them | dataset-wide high-Ho warning |
| Duplicates | none unexpected | de-duplicate first | — (rarely fatal alone) |
| MAF filter | reasonable retention | many rare markers dropped | — |
| Audit | OK | SUSPECT (documented) | BROKEN |

Report the numbers behind the call and, for GO WITH CAVEATS, the exact samples/markers to
remove.

## EDAM annotations (from `TOOL_CATALOG`)

| Tool | operation | topic |
|---|---|---|
| `qc_call_rate` | operation_2238 Statistical calculation | topic_0199 Genetic variation |
| `qc_heterozygosity` | operation_2238 Statistical calculation | topic_3056 Population genetics |
| `qc_duplicate_accessions` | operation_3432 Clustering | topic_3056 Population genetics |
| `qc_maf_filter` | operation_3675 Variant filtering | topic_0199 Genetic variation |
| `audit_import_quality` | operation_2428 Validation | topic_3071 Data management |
