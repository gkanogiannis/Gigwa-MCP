# Agent Skills for gigwa-mcp

Task-oriented **Agent Skills** for the [`gigwa-mcp`](https://github.com/gkanogiannis/Gigwa-MCP)
MCP server. Each skill teaches an agent how to drive the server's tools to accomplish a
common Gigwa workflow — import + QC, a diversity report, a QC verdict, an instance overview,
or a region scan.

## What Agent Skills are

Agent Skills are the open [`SKILL.md` standard](https://github.com/agentskills/agentskills):
a folder per skill containing a `SKILL.md` (YAML frontmatter with `name` + `description`,
plus a Markdown body) and optional `references/` notes loaded on demand. Their `name` +
`description` are cheap to keep in context, so an agent can pick the right skill for a task
and only then read the full body. These skills are **advisory** — the capability lives in
the gigwa-mcp MCP server's tools; the skills just sequence and explain them. They are
discoverable on the [LobeHub Skills Marketplace](https://lobehub.com/skills) and other
`SKILL.md` directories.

## The skills

| Skill | Mirrors prompt | What it does | Key tools |
|---|---|---|---|
| [`gigwa-import-and-qc`](gigwa-import-and-qc/SKILL.md) | `import_and_qc` | Import DArTseq/VCF, then run the full QC suite + audit and judge if the import is clean. | `import_dartseq`/`import_vcf`, `list_variant_sets`, `qc_*`, `audit_import_quality` |
| [`gigwa-diversity-report`](gigwa-diversity-report/SKILL.md) | `diversity_report` | Diversity + population structure + relatedness (PCA, structure, kinship, tree; optional by-group/Fst). | `diversity_summary`, `diversity_pca`, `diversity_structure`, `diversity_kinship`, `diversity_tree`, `diversity_by_group`, `diversity_fst` |
| [`gigwa-qc-triage`](gigwa-qc-triage/SKILL.md) | `qc_triage` | Full QC suite on an already-imported run → go/no-go verdict. | `qc_call_rate`, `qc_heterozygosity`, `qc_duplicate_accessions`, `qc_maf_filter`, `audit_import_quality` |
| [`gigwa-explore-instance`](gigwa-explore-instance/SKILL.md) | `explore_instance` | No-arg instance survey + health check; enumerates runs with their variantSetDbIds. | `gigwa_server_info`, `list_content`, `list_variant_sets`, `audit_import_quality` |
| [`gigwa-region-scan`](gigwa-region-scan/SKILL.md) | `region_scan` | Variant density + local diversity within one chromosome/window. | `list_sequences`, `count_variants`, `search_variants`, `diversity_summary`, `qc_call_rate` |

## Prerequisite

These skills assume the **gigwa-mcp MCP server is connected** to your agent (see the main
README's [Installation](../README.md#installation) and
[Configuration](../README.md#configuration)). Read-only skills (explore, QC triage,
diversity, region scan) work **anonymously** against the configured `GIGWA_URL` (defaults to
the public ICARDA instance). **`gigwa-import-and-qc`** additionally needs write access —
set `GIGWA_USER` / `GIGWA_PASS`.

## Layout

```text
skills/
  gigwa-import-and-qc/     SKILL.md  references/import-and-qc.md
  gigwa-diversity-report/  SKILL.md  references/popgen-metrics.md
  gigwa-qc-triage/         SKILL.md  references/qc-thresholds.md
  gigwa-explore-instance/  SKILL.md  references/instance-anatomy.md
  gigwa-region-scan/       SKILL.md  references/region-queries.md
```

Each skill's `name` (in `SKILL.md`) equals its folder name; deeper domain notes live one
level down in `references/`.

## Validate / install

- Validate a skill against the standard: `skills-ref validate ./skills/<skill-name>`
  (from [github.com/agentskills/agentskills](https://github.com/agentskills/agentskills)).
- Once published, these are discoverable via the LobeHub marketplace CLI — e.g.
  `npx -y @lobehub/market-cli skills search --q "gigwa"`, then install by the id/slug the
  marketplace assigns (check `market-cli skills --help` for the exact form).
