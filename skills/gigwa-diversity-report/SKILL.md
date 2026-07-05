---
name: gigwa-diversity-report
description: "Produce a population diversity and structure report for a Gigwa variant set. Use this when a user asks to summarise genetic diversity, describe population structure, or assess relatedness for a run identified by its variantSetDbId. Walks through dataset-wide MAF/He/Ho/PIC/Fis, principal component analysis (PCA), population-structure clustering with a suggested number of clusters K, a VanRaden kinship/relatedness matrix highlighting the closest pairs, and a UPGMA dendrogram; when a metadata TSV and group column are supplied it adds per-group diversity and pairwise Weir and Cockerham Fst. Trigger on 'run a diversity analysis in Gigwa', 'show population structure / PCA / kinship / phylogenetic tree', or 'compare diversity between populations'. Keywords: Gigwa, population genetics, diversity, PCA, population structure, kinship, Fst, UPGMA tree, He, Ho, PIC, MAF."
license: Apache-2.0
metadata:
  author: "Anestis Gkanogiannis"
  version: "1.0.0"
  server: "gigwa-mcp"
  homepage: "https://github.com/gkanogiannis/Gigwa-MCP"
compatibility: "Requires the gigwa-mcp MCP server connected to the agent over stdio, driving a Gigwa 2.x instance via REST/BrAPI. Read-only: works anonymously on public instances (set GIGWA_URL; GIGWA_USER/GIGWA_PASS optional). Grouped steps need a metadata TSV readable by the server."
---

# Gigwa diversity report

## Overview

Characterise a variant set's genetic diversity, population structure and relatedness in one
coherent report: dataset-wide summary statistics, PCA, structure clustering, a kinship
matrix, a phylogenetic tree, and — when groups are defined — per-group diversity and Fst.
All steps drive the `gigwa-mcp` MCP server's read-only `diversity_*` tools.

## When to use

- After QC passes for a run (see **gigwa-qc-triage**), when the user wants the *biology*:
  how diverse, how structured, how related.
- You need a `variant_set_db_id` (`MODULE§project§run`) — get it from `list_variant_sets()`
  (or the **gigwa-explore-instance** skill).

## Prerequisites and grouping

- The `gigwa-mcp` server is connected; read-only, so anonymous access is fine.
- **Grouping is optional.** To get per-group diversity and Fst, define groups one of two
  ways:
  - a **metadata TSV** plus a `group_column` (rows keyed by `id_column`, default
    `individual`), or
  - an explicit `groups_json` mapping (accepted by `diversity_fst` / `diversity_by_group`).
- No groups → run the ungrouped core (steps 1–4) only.

## Step-by-step workflow

1. **Dataset summary** — `diversity_summary(variant_set_db_id)`: per-marker MAF, He, Ho,
   PIC and dataset means. This is the baseline everything else is read against.
2. **Structure** —
   - `diversity_pca(variant_set_db_id, n_components=10)`: variance explained + PC
     coordinates (`pca_coords.csv`). Pass `metadata_tsv` + `group_column` to colour/label
     PCs by group.
   - `diversity_structure(variant_set_db_id, k_min=2, k_max=10)`: PCA + K-means clustering
     with a suggested K.
3. **Relatedness** — `diversity_kinship(variant_set_db_id, top_pairs=15)`: VanRaden genomic
   relationship matrix; report the closest pairs (possible clones/close relatives).
4. **Tree** — `diversity_tree(variant_set_db_id, max_markers=5000)`: UPGMA dendrogram from
   IBS distance (`tree.nwk`).
5. **Grouped (only if groups defined)** —
   - `diversity_by_group(variant_set_db_id, metadata_tsv=…, group_column=…)`: per-population
     He/Ho/Fis/MAF and allelic richness.
   - `diversity_fst(variant_set_db_id, metadata_tsv=…, group_column=…)`: pairwise Weir and
     Cockerham Fst between groups.

Interpret the results **together** and flag any data-quality caveats — treat tool warnings
(e.g. high heterozygosity) as caveats on the conclusions.

## Interpreting results

- **PCA** — the first PCs' variance explained tells you how much structure exists; tight
  clusters vs a cloud indicate discrete vs continuous structure.
- **K** — the suggested K from `diversity_structure` is a heuristic; sanity-check it against
  the PCA and any known groupings.
- **Kinship** — near-1 (relative to the diagonal) pairs are clones/sibs; cross-check against
  `qc_duplicate_accessions` from QC.
- **Fst** — larger pairwise values = more differentiated populations.

See `references/popgen-metrics.md` for definitions and output-column schemas.

## Scaling

Large sets: pass `max_markers=N` to subsample markers for the heavier steps; `method` is
`"vcf"` by default; a `region` string restricts to one window. See README "Performance &
scaling".

## Output files

Under `./gigwa_results/<module>/`: `diversity_markers.csv`, `pca_coords.csv`,
`structure_clusters.csv`, `kinship_matrix.csv`, `tree.nwk`, and (grouped)
`diversity_by_group.csv`, `fst_pairwise.csv`. The README "Visualizing results" section
shows how to plot these.

## See also

- `references/popgen-metrics.md` — metric definitions, grouping-input mapping, output schema,
  EDAM annotations.
- **gigwa-qc-triage** — run first to make sure the data is worth analysing.
- **gigwa-region-scan** — restrict diversity to a single genomic window.
