# Reference: region queries

## The two interval interfaces (contrasted)

This is the single most common mistake when scanning a region — the split-field tools and
the region-string tools do **not** share a parameter.

### Split fields: `count_variants`, `search_variants`

```
count_variants(variant_set_db_id, reference_name="chr1", start=1000000, end=2000000)
search_variants(variant_set_db_id, reference_name="chr1", start=1000000, end=2000000)
```

- `reference_name` — contig name (from `list_sequences`); required for a windowed query.
- `start`, `end` — optional; omit both for a whole-contig query.
- There is **no** `region` argument on these tools.

### Region string: `diversity_summary`, `qc_call_rate`

```
diversity_summary(variant_set_db_id, region="chr1:1000000-2000000")
qc_call_rate(variant_set_db_id, region="chr1:1000000-2000000")
```

- `region` — `"chrom"` (whole contig) or `"chrom:start-end"`.

### Mapping table

| User window | search/count | diversity/QC |
|---|---|---|
| whole `chr1` | `reference_name="chr1"` | `region="chr1"` |
| `chr1:1e6–2e6` | `reference_name="chr1", start=1000000, end=2000000` | `region="chr1:1000000-2000000"` |

## Coordinate convention

- **1-based, inclusive.** `start=1000000, end=2000000` includes both endpoints.
- Discover valid `reference_name` values with `list_sequences(variant_set_db_id)` — passing
  a contig that doesn't exist returns nothing, not an error.

## MAF / missingness filters

`count_variants` and `search_variants` accept (all on a **0–1** scale, all optional):

- `min_maf`, `max_maf` — keep variants within a minor-allele-frequency band. Use
  `min_maf=0.05` for the "common variant" pass.
- `max_missing_data` — drop variants with more than this fraction of missing calls.

Applying `min_maf=0.05` on the second `count_variants` call and comparing to the plain count
gives the rare-vs-common split.

## Caps

- `search_variants(..., max_variants=100000)` — the maximum number of variants written to
  `variant_search.csv`. Narrow the window or tighten the MAF band if you hit the cap.

## EDAM annotations (from `TOOL_CATALOG`)

| Tool | operation | topic |
|---|---|---|
| `list_sequences` | operation_2422 Data retrieval | topic_0102 Mapping |
| `count_variants` | operation_2421 Database search | topic_0199 Genetic variation |
| `search_variants` | operation_2421 Database search | topic_0199 Genetic variation |
| `diversity_summary` | operation_2238 Statistical calculation | topic_3056 Population genetics |
| `qc_call_rate` | operation_2238 Statistical calculation | topic_0199 Genetic variation |
