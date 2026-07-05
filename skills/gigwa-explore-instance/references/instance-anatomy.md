# Reference: Gigwa instance anatomy

## The data model

A Gigwa instance nests four levels:

```
instance  →  database (module)  →  project  →  run
```

- **database / module** — a MongoDB database holding one or more projects.
- **project** — a study grouping runs and germplasm.
- **run** — one import (a variant set + its call sets).

A single run is addressed by a BrAPI **variantSetDbId**:

```
MODULE§project§run          (§ = section sign, U+00A7)
```

`list_variant_sets()` returns these strings verbatim — copy one and pass it as
`variant_set_db_id` to any QC/diversity/search tool. Do not construct it by hand.

## BrAPI / GA4GH terms and counts

- **variant set** — the run's set of variants (markers).
- **call set** — one genotyped sample within the run.
- The variant and call-set counts reported by `list_variant_sets()` come from the server's
  BrAPI/GA4GH endpoints and tell you the shape (markers × samples) of each run.

## Complementary discovery: MCP resources

Besides the tools, the `gigwa-mcp` server exposes two **resources** (network-free):

| Resource | Purpose |
|---|---|
| `catalog://tools` | Categorised catalog of all tools with EDAM operation/topic ids + labels; intended for directory indexers. |
| `gigwa://server/info` | The configured connection info (URL, REST base, auth mode) — makes no network call. |

## Auth modes

- **Anonymous** — read-only browsing/analysis; the default when `GIGWA_USER`/`GIGWA_PASS`
  are unset. Sufficient for this skill and all read-only analysis skills.
- **Authenticated** — needed only for writes (imports, metadata). Set `GIGWA_USER` /
  `GIGWA_PASS`.
- `GIGWA_URL` defaults to the public ICARDA instance (`https://gigwa.icarda.org:8443/gigwa`)
  when unset.

## EDAM annotations (from `TOOL_CATALOG`)

| Tool | operation | topic |
|---|---|---|
| `gigwa_server_info` | operation_2422 Data retrieval | topic_3071 Data management |
| `list_content` | operation_2422 Data retrieval | topic_3071 Data management |
| `list_variant_sets` | operation_2422 Data retrieval | topic_0199 Genetic variation |
| `audit_import_quality` | operation_2428 Validation | topic_3071 Data management |
