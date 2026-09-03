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

## Where per-individual metadata lives

Passport/trait attributes sit at one of two levels depending on how the instance was
populated, so check both before concluding a run has none:

- **germplasm (accession) level** — `get_germplasm_metadata()`, the usual case.
- **call set (sample) level** — `search_callsets()`, where the attributes hang off each
  callset's `additionalInfo`. Some instances populate only this level.

`get_germplasm_metadata()` tries the germplasm level first and falls back to the callset
level automatically, so it is the right first call either way; use `search_callsets()` when
you specifically want the sample-level dump with the server's raw callset labels.

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
- **Runtime switch** — `gigwa_connect(url, profile?, anonymous?)` re-points the session at a
  different server without a restart. It reads credentials from the environment (a named
  `profile` maps to `GIGWA_USER_<PROFILE>`/`GIGWA_PASS_<PROFILE>`), so no secret is ever
  typed into the chat; the new connection is verified before it takes effect.

## EDAM annotations (from `TOOL_CATALOG`)

| Tool | operation | topic |
|---|---|---|
| `gigwa_connect` | operation_2409 Data handling | topic_3071 Data management |
| `gigwa_server_info` | operation_2422 Data retrieval | topic_3071 Data management |
| `list_content` | operation_2422 Data retrieval | topic_3071 Data management |
| `list_variant_sets` | operation_2422 Data retrieval | topic_0199 Genetic variation |
| `audit_import_quality` | operation_2428 Validation | topic_3071 Data management |
