---
name: gigwa-group-filtering
description: "Turn natural-language requests about groups of individuals or samples into Gigwa's own group-level variant filtering. Use this when a user wants to create a group from a list of individuals/samples or from metadata, keep variants with a given minor-allele frequency (MAF), missing-data rate or heterozygosity rate within one or several groups, combine these criteria, page through or export the resulting variants, chart per-window MAF / missingness / heterozygosity / Fst / Tajima's D over them, or reuse that subset in a later analysis. Every filter and statistic is evaluated by Gigwa itself (no genotypes downloaded, nothing recomputed locally). Trigger on 'create a group with these individuals', 'in this group give me the variants with MAF > 5%', 'variants with less than 10% missing data in this group', 'combine MAF, missingness and heterozygosity', 'use that subset for the next analysis', 'Fst between these two groups along chr3'. Keywords: Gigwa, group, population, subset, MAF, missing data, missingness, heterozygosity, genotype filter, Fst, Tajima's D, samples."
license: Apache-2.0
metadata:
  author: "Guilhem Sempéré"
  version: "1.0.0"
  server: "gigwa-mcp"
  homepage: "https://github.com/gkanogiannis/Gigwa-MCP"
compatibility: "Requires the gigwa-mcp MCP server connected to the agent, driving a Gigwa 2.x instance via its REST/GA4GH API. Read-only; works anonymously on public instances. Needs a variantSetDbId."
---

# Gigwa group filtering

## Overview

Gigwa can already filter variants on genotype statistics **per group of individuals or
samples** — MAF, missing data and heterozygosity rate — and chart per-window statistics
over the result. This skill translates a user's words into those Gigwa operations through
the `gigwa-mcp` tools. The MCP server only orchestrates: **Gigwa evaluates every filter and
statistic**; no genotypes are downloaded to compute them.

## Concepts

- **Group** — a named list of individuals (or samples) of one project. Gigwa stores no
  group object; `define_group` keeps it for the session and sends its members in each query.
- **Subset** — the result of one Gigwa search (region, variant types, per-group thresholds),
  kept server-side in Gigwa's temporary collection under a token dedicated to the subset.
  Browsing, exporting, charting and the QC/diversity tools reuse it by name, without
  re-filtering. Several subsets coexist. An expired subset is rebuilt automatically.

## Translating the request

| User says | Parameter (fractions 0-1) |
|---|---|
| "MAF > 5 %", "common variants" | `min_maf: 0.05` |
| "MAF below 1 %", "rare" | `max_maf: 0.01` (MAF ≤ 0.5 by definition) |
| "less than 10 % missing data", "call rate ≥ 90 %" | `max_missing: 0.10` |
| "heterozygosity below 20 %" | `max_het: 0.20` |
| "at least 30 % heterozygotes" | `min_het: 0.30` |
| "variants that discriminate A from B", "different major genotype" | `discriminate_with: "B"` in A's entry (B's entry needed too; setting it both ways is equivalent) |
| "SNPs only" | `selected_variant_types: "SNP"` |
| "on chr3 between 1 and 2 Mb" | `region: "chr3:1000000-2000000"` |

Semantics to keep in mind (and to tell the user when relevant):
- Gigwa bounds are **inclusive**: "MAF > 5 %" is applied as MAF ≥ 5 %.
- Every criterion of every group must hold (**AND**). For "A or B", run two subsets.
- MAF is computed on **bi-allelic** variants only.
- Discrimination keeps variants where **each** group has exactly one dominant (most frequent)
  genotype and the two differ — ties within a group exclude the variant.
- Heterozygosity rate = share of heterozygous calls among the group's **non-missing**
  genotypes at each variant (not per-sample Ho: that is `qc_heterozygosity`).
- Thresholds apply **within each group** — "in this group" means the group's entry.
- Filters apply to the whole **project** (all its runs), not just one run.

## Step-by-step workflow

1. **Get the ids** — `list_variant_sets()` for the `variant_set_db_id`.
2. **Create groups** — `define_group(variant_set_db_id, name, members=[...])`, or from
   metadata: `list_metadata_values` then `define_group(..., metadata_filters_json={"GroupK4": ["cA"]})`.
   Sample-level groups: `entity="samples"` (all groups of one query must be the same kind).
   Check the reply for ids "not found in this project".
3. **Size it (optional)** — `count_variants(variant_set_db_id, group_filters_json=[...])`.
4. **Create the subset** —
   `define_variant_subset(variant_set_db_id, name="S1", group_filters_json=[{"group": "G1", "min_maf": 0.05, "max_missing": 0.1, "max_het": 0.2}])`.
   Several groups: one entry each, e.g. `[{"group": "G1", "min_maf": 0.05}, {"group": "G2", "max_missing": 0.2}]`.
   Whole population instead of a group: flat `min_maf=`, `max_missing_data=`, `min_het=`, `max_het=`.
   Gigwa's progress is relayed while it filters. On large datasets pass `wait=False` to
   return at once, then follow it with `get_subset_progress("S1")` (as with
   `export_genotypes(wait=False)` / `get_export_progress`); any tool given the subset
   waits for it to finish.
5. **Look at the variants** — `search_variants(variant_set_db_id, subset="S1")` (pages of
   ≤1000; the reply gives the next `page`).
6. **Use the subset next** —
   - `export_genotypes(variant_set_db_id, output_path, subset="S1", format=...)`
   - `selection_profile("S1", statistic="maf"|"missing"|"heterozygosity"|"fst"|"tajima_d"|"density", sequence="chr1")`
     — Gigwa-computed per-window values (Fst needs 2+ groups).
   - any QC/diversity tool with `subset="S1"` (e.g. `diversity_pca(variant_set_db_id, subset="S1")`)
     — Gigwa exports only the subset's variants for the groups' members.
7. `list_groups()` recalls what exists in the session.

## Do / don't

- **Do** filter with the tools above; **don't** use `qc_maf_filter` / `qc_heterozygosity`
  to select variants — they compute locally on downloaded genotypes and only report.
- **Don't** combine `subset` with new filters on export — define a new subset instead.
- Group and subset names are session-scoped; after `gigwa_connect` to another server,
  define them again.

## See also

- `references/group-filtering.md` — the Gigwa request fields behind each tool and the
  call sequences.
- **gigwa-diversity-report** / **gigwa-region-scan** — accept `subset=` too.
