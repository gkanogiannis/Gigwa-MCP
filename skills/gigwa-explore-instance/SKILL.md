---
name: gigwa-explore-instance
description: "Get an overview of an entire Gigwa instance and flag anything that needs attention. Use this when a user asks what data is available on a Gigwa server, wants to enumerate databases/projects/runs, or has inherited an instance and needs a health check before analysis. Confirms connectivity, server version and authenticated user; lists all databases, projects and runs with their exact BrAPI variantSetDbIds and variant/call-set counts; and runs an instance-wide import-quality audit that ranks runs BROKEN/SUSPECT/OK so badly imported data is caught early. Trigger on 'what is on this Gigwa server', 'give me an overview of the instance', 'list the databases and runs', or 'which runs were imported badly'. Keywords: Gigwa, instance overview, server info, list content, variant sets, variantSetDbId, import audit, data catalog."
license: Apache-2.0
metadata:
  author: "Anestis Gkanogiannis"
  version: "1.0.0"
  server: "gigwa-mcp"
  homepage: "https://github.com/gkanogiannis/Gigwa-MCP"
compatibility: "Requires the gigwa-mcp MCP server connected to the agent over stdio, driving a Gigwa 2.x instance via REST/BrAPI. Read-only and takes no arguments: works anonymously against the configured GIGWA_URL (defaults to the public ICARDA instance); GIGWA_USER/GIGWA_PASS optional."
---

# Gigwa explore instance

## Overview

A one-shot survey of a whole Gigwa instance plus a health check: confirm you can reach it,
enumerate everything on it with the exact ids later analyses need, and flag any run that was
imported badly. Drives the `gigwa-mcp` MCP server's discovery and audit tools. No arguments
required.

## When to use

- First contact with an unknown or inherited Gigwa server; "what's here and is any of it
  broken?"
- To obtain the `variantSetDbId`s that every other skill (**gigwa-qc-triage**,
  **gigwa-diversity-report**, **gigwa-region-scan**) needs.

## Prerequisites

- The `gigwa-mcp` server is connected. Read-only; works anonymously against the configured
  `GIGWA_URL` (defaults to the public ICARDA instance when unset).

## Step-by-step workflow

0. **(Optional) Point at a different server** — `gigwa_connect(url, profile?, anonymous?)`
   switches the active Gigwa instance at runtime (no restart). Credentials come from the
   environment (default `GIGWA_USER`/`GIGWA_PASS`, or `GIGWA_USER_<PROFILE>`/
   `GIGWA_PASS_<PROFILE>` when a `profile` is named) — never from the chat. The switch is
   verified before it takes effect and lasts for the session.
1. **Connectivity** — `gigwa_server_info()`: confirms the URL is reachable and reports the
   server version and the authenticated user (or anonymous).
2. **Enumerate** —
   - `list_content()`: human-readable databases → projects → runs.
   - `list_variant_sets()`: every run with its **exact `variantSetDbId`** and variant/
     call-set counts. This is where you copy ids for downstream skills.
3. **Health check** — `audit_import_quality()` with **no** `variant_set_db_id` scans the
   *whole instance* and ranks every run OK / SUSPECT / BROKEN (writes
   `import_quality_scan.csv`).
4. **Summarise** what data is available (databases, projects, runs, sizes) and call out
   anything that needs attention (broken/suspect runs, empty projects).

## Interpreting results

- `list_content` is the human view; `list_variant_sets` is the id-bearing view — use both.
- In the audit, **BROKEN** runs should not be analysed until re-imported; **SUSPECT** runs
  warrant a closer QC pass (**gigwa-qc-triage**).
- The `catalog://tools` and `gigwa://server/info` MCP **resources** are complementary,
  network-free ways to inspect capabilities and configured connection info.

## Output files

`./gigwa_results/<module>/import_quality_scan.csv` (the instance-wide audit).

## See also

- `references/instance-anatomy.md` — the instance→module→project→run model, id format,
  resources, auth modes, EDAM annotations.
- **gigwa-qc-triage** / **gigwa-region-scan** / **gigwa-diversity-report** — drill into a
  specific run once you have its id.
