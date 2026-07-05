---
name: gigwa-region-scan
description: "Characterise the variants and diversity within a single genomic region of a Gigwa variant set. Use this when a user asks about a specific chromosome, contig or coordinate window (e.g. 'chr1' or 'chr1:1000000-2000000') in a run identified by its variantSetDbId. Confirms the contig name, counts variants in the window and again at a common-variant MAF threshold, lists the matching variants to a CSV, and characterises just that window with a diversity summary and call-rate QC. Trigger on 'scan this region in Gigwa', 'how many variants are in chr1:...', 'is this locus variable', or 'diversity around a gene or window'. Note the interval interface differs by tool: count_variants and search_variants take reference_name/start/end, while diversity_summary and qc_call_rate take a single region string. Keywords: Gigwa, genomic region, variant density, region scan, chromosome, contig, MAF, variant search, call rate."
license: Apache-2.0
metadata:
  author: "Anestis Gkanogiannis"
  version: "1.0.0"
  server: "gigwa-mcp"
  homepage: "https://github.com/gkanogiannis/Gigwa-MCP"
compatibility: "Requires the gigwa-mcp MCP server connected to the agent over stdio, driving a Gigwa 2.x instance via REST/BrAPI. Read-only: works anonymously on public instances (set GIGWA_URL; GIGWA_USER/GIGWA_PASS optional). Needs a variantSetDbId and a chromosome/contig name or coordinate window."
---

# Gigwa region scan

## Overview

Zoom into one genomic window of a variant set and report its variant density, common vs
rare composition, and local diversity/quality. Drives the `gigwa-mcp` MCP server's search,
diversity and QC tools against a single region.

## When to use

- A user names a chromosome/contig (`chr1`) or a coordinate range (`chr1:1000000-2000000`)
  and wants to know what's there.
- You need a `variant_set_db_id` (`MODULE§project§run`) — from `list_variant_sets()` or the
  **gigwa-explore-instance** skill.

## Region syntax — the two interfaces (important)

**The tools take the interval two different ways.** Get this right or the filters silently
apply to the whole set:

| Tool(s) | How to pass the window |
|---|---|
| `count_variants`, `search_variants` | split into `reference_name`, `start`, `end` (there is **no** `region` param) |
| `diversity_summary`, `qc_call_rate` | one `region` string: `"chrom"` or `"chrom:start-end"` |

Mapping example for `chr1:1000000-2000000`:

- search/count → `reference_name="chr1", start=1000000, end=2000000`
- diversity/QC → `region="chr1:1000000-2000000"`

Coordinates are **1-based inclusive**. A whole chromosome is `reference_name="chr1"` (no
start/end) or `region="chr1"`.

## Step-by-step workflow

1. **Validate the contig** — `list_sequences(variant_set_db_id)` to confirm the exact
   `reference_name` exists (names must match the run's contigs).
2. **Count** —
   - `count_variants(variant_set_db_id, reference_name, start, end)` — total variants in the
     window.
   - `count_variants(variant_set_db_id, reference_name, start, end, min_maf=0.05)` — again,
     restricted to common variants; the gap = rare/low-frequency variants.
3. **List** — `search_variants(variant_set_db_id, reference_name, start, end)` writes the
   matching variants to `variant_search.csv` (respects the same MAF/missingness filters;
   capped by `max_variants=100000`).
4. **Characterise the window** (note the `region=` string form):
   - `diversity_summary(variant_set_db_id, region="chr1:1000000-2000000")`
   - `qc_call_rate(variant_set_db_id, region="chr1:1000000-2000000")`
5. **Report** variant density (per bp / per window), common-vs-rare split, and local
   diversity/call-rate.

## Interpreting results

- **Density** — variants per window vs the dataset average flags hyper-variable or
  conserved loci.
- **Common vs rare** — a large drop from the plain count to the `min_maf=0.05` count means
  the region is dominated by rare variants.
- **Local diversity/QC** — low call rate in the window can explain apparent low diversity;
  read them together.

## Output files

Under `./gigwa_results/<module>/`: `variant_search.csv`, plus `diversity_markers.csv` and
`call_rate_samples.csv` / `call_rate_markers.csv` (scoped to the region).

## See also

- `references/region-queries.md` — both interfaces with copy-paste examples, coordinate
  convention, MAF/missingness semantics, EDAM annotations.
- **gigwa-diversity-report** — whole-run diversity instead of one window.
