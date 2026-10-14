# Group-level filtering: what the tools send to Gigwa

The tools reproduce the requests of the Gigwa web UI (`main.js` `buildSearchQuery`,
`index.jsp` `buildChartDataPayLoad`), verified against `GigwaRestController`,
`Ga4ghRestController`, `GenotypingDataQueryBuilder` and `VisualizationService`.

## Per-group arrays (GA4GH `POST /ga4gh/variants/search`, `POST /gigwa/exportData`)

| Field | Group *i* |
|---|---|
| `callSetIds` | members of group 1, `module§<id>` (empty = whole project) |
| `additionalCallSetIds` | list of member lists for groups 2..N |
| `groupName[i]` | group name (population label in exports) |
| `minMaf[i]` / `maxMaf[i]` | percent, 0-50 (bi-allelic only) |
| `minMissingData[i]` / `maxMissingData[i]` | percent, 0-100 |
| `minHeZ[i]` / `maxHeZ[i]` | percent of heterozygous among called genotypes, 0-100 |
| `discriminate[i]` | `null`, or the 1-based number of the group *i* is discriminated with (`discriminate_with`) |
| `gtPattern`, `mostSameRatio`, `annotationFieldThresholds` | UI defaults (`""`, `"100"`, `{}`) — required full length |

Header `workWithSamples: true` switches ids to samples.

## Search modes and the temporary collection

| `searchMode` | Effect |
|---|---|
| 0 | count only |
| 2 | browse a page of the token's temporary collection (`pageToken` = page index) |
| 3 | count + search (fills the token's temporary collection) + first page |

The temporary collection is **per token**; each subset therefore gets its own token
(`POST /gigwa/generateToken`, without a body when anonymous).

## Call sequences

| Request | Calls |
|---|---|
| "Create a group with these individuals" | `define_group` → `POST /ga4gh/callsets/search` (validation) |
| "…from metadata" | `define_group` → `POST /gigwa/filterIndividualsFromMetadata/{module}` (or `filterSamplesFromMetadata`) + callsets/search |
| "In this group, MAF > 5 %" | `define_variant_subset` → `generateToken` → `variants/search` mode 3 with `minMaf=[5]` |
| "< 10 % missing in this group" | same with `maxMissingData=[10]` |
| "Combine MAF, missingness, heterozygosity" | one group entry with `minMaf`, `maxMissingData`, `maxHeZ` |
| "List them" | `search_variants(subset=)` → `variants/search` mode 2 pages, subset token |
| "Use the subset for the next analysis" | `export_genotypes(subset=)` / QC-diversity `subset=` → `POST /gigwa/exportData`, subset token, `exportedIndividuals` = group members |
| "MAF / missing / het / Fst / Tajima's D along chrN" | `selection_profile` → `POST /gigwa/{mafData,missingData,heterozygosityData,fstData,tajimaDData,densityData}`, subset token |

On HTTP 401, or "Unable to find temporary records" reported on the token's progress, the
subset is searched again under a new token and the call retried once.
