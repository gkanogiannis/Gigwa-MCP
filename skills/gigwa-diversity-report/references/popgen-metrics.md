# Reference: population-genetics metrics

## Metric definitions

- **MAF** — minor allele frequency; the frequency of the less common allele at a marker.
- **He** — expected heterozygosity (gene diversity), `1 − Σ p_i²`; diversity under
  Hardy–Weinberg.
- **Ho** — observed heterozygosity; the actual fraction of heterozygous calls. Ho ≫ He is a
  contamination / miscalling red flag; Ho ≪ He suggests inbreeding or a selfing crop.
- **PIC** — polymorphism information content; a marker's usefulness for distinguishing
  genotypes.
- **Fis** — inbreeding coefficient within a group, from the He/Ho gap.
- **Allelic richness** — number of alleles per marker, rarefied so groups of different size
  are comparable.
- **Kinship (VanRaden GRM)** — genomic relationship matrix estimating realised relatedness
  between every pair of samples from marker genotypes.
- **Fst (Weir & Cockerham)** — proportion of total genetic variance due to differences
  *between* the defined groups; 0 = panmictic, higher = more differentiated.
- **UPGMA / IBS** — the tree is built by UPGMA clustering on an identity-by-state (IBS)
  distance between accessions.

## Grouping inputs → tools

Group-aware tools (`diversity_pca`, `diversity_by_group`, `diversity_fst`) accept groups two
ways:

| Input | Params | Notes |
|---|---|---|
| Metadata TSV | `metadata_tsv`, `group_column`, `id_column="individual"` | Rows keyed by `id_column`; the value in `group_column` is the group label. |
| Explicit map | `groups_json` | JSON mapping (group → members, or member → group) for `diversity_fst` / `diversity_by_group`. |

`id_column` must match the sample identifiers used in the variant set (default
`individual`).

## Output-file schema (quick reference)

Written under `./gigwa_results/<module>/`:

| File | Tool | Contents |
|---|---|---|
| `diversity_markers.csv` | `diversity_summary` | Per-marker MAF/He/Ho/PIC (+ dataset means). |
| `pca_coords.csv` | `diversity_pca` | Per-sample PC coordinates (and group label if given). |
| `structure_clusters.csv` | `diversity_structure` | Per-sample cluster assignment across tested K. |
| `kinship_matrix.csv` | `diversity_kinship` | Square VanRaden relatedness matrix. |
| `tree.nwk` | `diversity_tree` | UPGMA dendrogram in Newick format. |
| `diversity_by_group.csv` | `diversity_by_group` | Per-group He/Ho/Fis/MAF, allelic richness. |
| `fst_pairwise.csv` | `diversity_fst` | Pairwise Weir & Cockerham Fst between groups. |
| `core_collection.csv` | `diversity_core_collection` | (Related tool) greedy allele-coverage core set. |

## Scaling knobs

- `max_markers` — subsample markers (the tree defaults to `max_markers=5000`); lower for
  speed, raise for resolution.
- `method` — `"vcf"` (default) genotype loading path.
- `region` — restrict to `"chrom"` or `"chrom:start-end"` (1-based).
- `diversity_pca(outlier_sd=6.0)` — flags PC outliers beyond this many SD.

## EDAM annotations (from `TOOL_CATALOG`)

| Tool | operation | topic |
|---|---|---|
| `diversity_summary` | operation_2238 Statistical calculation | topic_3056 Population genetics |
| `diversity_pca` | operation_2939 Principal component visualisation | topic_3056 Population genetics |
| `diversity_structure` | operation_3432 Clustering | topic_3056 Population genetics |
| `diversity_kinship` | operation_2238 Statistical calculation | topic_3056 Population genetics |
| `diversity_by_group` | operation_2238 Statistical calculation | topic_3056 Population genetics |
| `diversity_fst` | operation_2238 Statistical calculation | topic_3056 Population genetics |
| `diversity_tree` | operation_0323 Phylogenetic tree construction | topic_0084 Phylogenetics |
| `diversity_core_collection` | operation_2238 Statistical calculation | topic_3056 Population genetics |
