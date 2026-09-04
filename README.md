<p align="center">
  <img src="docs/img/logo.png" alt="Gigwa MCP" width="460">
</p>

[![gigwa-mcp MCP server](https://glama.ai/mcp/servers/gkanogiannis/gigwa-mcp/badges/card.svg)](https://glama.ai/mcp/servers/gkanogiannis/gigwa-mcp)

[![MCP Badge](https://lobehub.com/badge/mcp/gkanogiannis-gigwa-mcp)](https://lobehub.com/mcp/gkanogiannis-gigwa-mcp)

# Gigwa MCP Server

An [MCP](https://modelcontextprotocol.io) server that drives a local or remote
[Gigwa](https://github.com/SouthGreenPlatform/Gigwa2) installation over its REST API.
It lets an MCP client (Claude Desktop / Claude Code) run the whole genotyping workflow
in plain language: **connect → import genotype data & metadata → run QC and diversity
analyses → audit databases for import artifacts**. Built for genomic-resources teams and
genebanks, but works with any Gigwa instance.

- **Import** DArTseq SNP/Silico xlsx reports (with correct 2-row genotype calling) or
  plain VCF, plus per-individual metadata.
- **Analyse** read-only: genotypes are pulled out of Gigwa and all statistics are
  computed in Python (scikit-allel / numpy / scipy). Nothing is written back.
- **Audit** an existing instance to find databases that were imported badly.
- Every analysis returns a chat summary and writes full tables as CSV under
  `./gigwa_results/<database>/`.

## Table of contents

- [Gigwa MCP Server](#gigwa-mcp-server)
  - [Table of contents](#table-of-contents)
  - [Overview](#overview)
  - [Features](#features)
  - [How it works](#how-it-works)
  - [Requirements](#requirements)
  - [Installation](#installation)
    - [Find \& try it on Glama](#find--try-it-on-glama)
    - [Install it yourself](#install-it-yourself)
    - [Add it to Claude Code (the simple version)](#add-it-to-claude-code-the-simple-version)
    - [Run with Docker](#run-with-docker)
  - [Configuration](#configuration)
  - [Connecting from an MCP client](#connecting-from-an-mcp-client)
  - [Quick start](#quick-start)
  - [Tool reference](#tool-reference)
  - [Prompts \& resources](#prompts--resources)
  - [Skills](#skills)
  - [Usage scenarios](#usage-scenarios)
  - [Output files](#output-files)
  - [Visualizing results](#visualizing-results)
    - [PCA: `pca_coords.csv`](#pca-pca_coordscsv)
    - [Population structure: `structure_clusters.csv`](#population-structure-structure_clusterscsv)
    - [Kinship: `kinship_matrix.csv`](#kinship-kinship_matrixcsv)
    - [Per-group diversity: `diversity_by_group.csv`](#per-group-diversity-diversity_by_groupcsv)
    - [Core-collection coverage: `core_collection.csv`](#core-collection-coverage-core_collectioncsv)
    - [UPGMA tree: `tree.nwk`](#upgma-tree-treenwk)
  - [Performance \& scaling](#performance--scaling)
  - [Limitations \& disadvantages](#limitations--disadvantages)
  - [Troubleshooting](#troubleshooting)
  - [DArTseq notes](#dartseq-notes)
    - [Genomic positions (optional)](#genomic-positions-optional)
  - [Project layout](#project-layout)
  - [Testing](#testing)
  - [Changelog](#changelog)
    - [v1.9.1 — export and container reliability](#v191--export-and-container-reliability)
    - [v1.9.0 — MCP SDK v2, selection-aware export \& metadata endpoints](#v190--mcp-sdk-v2-selection-aware-export--metadata-endpoints)
    - [v1.8.0 — callset-level metadata \& richer connection info](#v180--callset-level-metadata--richer-connection-info)
    - [v1.7.0 — HTTP transport](#v170--http-transport)
    - [v1.6.0 — runtime server switch](#v160--runtime-server-switch)
    - [v1.5.0 — Agent Skills](#v150--agent-skills)
    - [v1.4.16 — anonymous access \& fast-fail timeouts](#v1416--anonymous-access--fast-fail-timeouts)
    - [v1.3.4 — tool catalog, EDAM annotations \& progress reporting](#v134--tool-catalog-edam-annotations--progress-reporting)
    - [v1.2.0 — server-side search, filtered analysis \& export](#v120--server-side-search-filtered-analysis--export)
    - [v1.1.0 — Docker support](#v110--docker-support)
    - [v1.0.0 — initial release](#v100--initial-release)
  - [License \& contributing](#license--contributing)

## Overview

Gigwa is a web platform for storing and querying genotyping data. Loading data into it
and getting analyses out is normally manual (massaging xlsx into Gigwa's import format,
clicking through the web UI, uploading .dart/.vcf, exporting VCFs, running pop-gen tools separately).

This server exposes Gigwa as a set of **MCP tools**. You talk to your MCP client in
natural language; it picks the matching tool and fills in the arguments. There is no
chat API of its own, meaning the "interface" is the tool list below plus your prompts.

The analysis tools are read-only: they extract genotypes (via async VCF export or
paged BrAPI `allelematrix`), compute everything in Python, and write CSVs locally. They
never modify the data in Gigwa.

## Features

**Import pipeline**

| Tool | What it does |
|------|--------------|
| `gigwa_connect` | Switch the active Gigwa server at runtime (credentials from the environment, never the chat) |
| `gigwa_server_info` | Verify connectivity/auth and report the server version |
| `list_content` | List databases → projects → runs on the instance |
| `import_dartseq` | Call genotypes from DArTseq SNP/Silico xlsx report(s) → VCF and import (optionally genome-anchored via `reference_fasta`) |
| `import_vcf` | Import a `.vcf` / `.vcf.gz` (any technology) |
| `map_dartseq_to_reference` | Align DArT tag sequences to a reference genome to infer each marker's chromosome/position |
| `validate_metadata` | Validate an individual-metadata TSV without importing |
| `import_metadata` | Import per-individual attributes into a database |
| `get_import_progress` | Poll a running import by its progress token |
| `abort_import` | Cancel a running import (or other process) by its progress token |

**Discovery, search & export (read-only)**

| Tool | What it does |
|------|--------------|
| `list_variant_sets` | List every run with its exact BrAPI `variantSetDbId` (the id the analysis tools take) |
| `list_sequences` | List the chromosomes/contigs of a variant set (valid `reference_name` values) |
| `count_variants` | Count variants matching region / MAF / missing-data filters, server-side (no download) |
| `search_variants` | Search variants server-side and write the matching list (`variant_search.csv`) |
| `list_export_formats` | List the export formats this build actually offers, with variant-type/ploidy limits |
| `export_genotypes` | Export a variant set — or a filtered/selected subset — to a file; `wait=False` for long runs |
| `get_export_progress` | Status of this session's running export (started with `wait=False`) |
| `fetch_export_file` | Download a completed export once `get_export_progress` reports it done |
| `search_callsets` | Dump per-sample (callset) metadata — names + `additionalInfo` attributes (`sample_metadata.csv`) |
| `get_germplasm_metadata` | Pull server-stored per-individual attributes (`germplasm_metadata.csv`) |
| `list_metadata_values` | List individual-metadata field names and their distinct values |
| `filter_individuals_by_metadata` | Select individuals by metadata field/value filters (feeds `export_genotypes`) |

**QC & diversity (read-only)**

| Tool | What it does |
|------|--------------|
| `qc_call_rate` | Per-sample & per-marker call rate; flag low-call samples/markers |
| `qc_heterozygosity` | Per-sample Ho; flag outliers (contamination / off-type / selfed) |
| `qc_duplicate_accessions` | Pairwise IBS → group duplicate/clonal accessions |
| `qc_maf_filter` | Report markers that MAF / missingness filters would remove |
| `diversity_summary` | Per-marker MAF, He, Ho, PIC, Fis + dataset means |
| `diversity_pca` | PCA of population structure; variance explained + PC coords (optional `group` column) |
| `diversity_kinship` | VanRaden genomic relationship (kinship) matrix |
| `diversity_fst` | Pairwise Weir & Cockerham Fst between groups |
| `diversity_by_group` | Per-population He, Ho, Fis, MAF, % polymorphic + (rarefied) allelic richness |
| `diversity_core_collection` | Greedy allele-coverage core: smallest accession set capturing the most diversity |
| `diversity_structure` | Lightweight ancestry with PCA + K-means, pseudo-F suggests K (no ADMIXTURE binary) |
| `diversity_tree` | UPGMA dendrogram of accessions from IBS distance, written as Newick (`tree.nwk`) |

Every QC & diversity tool also accepts `region` (`"chrom"` or `"chrom:start-end"`, 1-based;
from `list_sequences`) to restrict the analysis to one genomic window.

**Import-quality audit**

| Tool | What it does |
|------|--------------|
| `audit_import_quality` | Scan a whole instance (or one run) for genotype-encoding artifacts left by a bad import; rank runs BROKEN / SUSPECT / OK |

## How it works

```
MCP client (Claude Desktop / Code)
        │  natural language → tool call
        ▼
  gigwa_mcp (this server, stdio)
        │  GigwaClient: token auth, multipart upload, async progress, BrAPI v2
        ▼
     Gigwa REST API  ──►  genotypes (async VCF export  ‖  paged search/allelematrix)
        │
        ▼
  scikit-allel / numpy / scipy  →  chat summary + CSV under ./gigwa_results/<module>/
```

Analyses load genotypes through `gigwa_mcp/analysis/genotypes.py:load_genotypes`, which
has two backends:

- **`method="vcf"` (default)** : exports the whole variant set once via async VCF and
  caches it on disk for reuse. Best for small/medium sets and when you will run several
  tools on the same run.
- **`method="allelematrix"`** : pages the genotype matrix via BrAPI
  `search/allelematrix`, honouring a server-side `max_markers` subset and sizing pages to
  the server's per-response cell cap, and caches the result in-process per `(variant set,
  caps)` so repeat tool calls reuse it. Best for large datasets where a full export is
  wasteful (see [Performance & scaling](#performance--scaling)).

Variant sets are addressed by their BrAPI `variantSetDbId`, of the form
`MODULE§projectNumber§run` (e.g. `MyDatabase§1§run1`). `list_content` shows them.

## Requirements

- **Python ≥ 3.10**
- **[`uv`](https://docs.astral.sh/uv/) (provides the `uvx` command)** is required if you launch
  the server with `uvx gigwa-mcp` (the recommended MCP-client setup below). Not needed if you
  `pip`/`pipx`-install the package and point your client at the resulting executable instead.
  Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or
  `pip install uv`, then make sure `uvx` is on your `PATH` (see the note below).
- A reachable **Gigwa** server (local or remote) and credentials.
- Optional: the **minimap2** CLI on `PATH` for DArTseq genome-anchoring of very large
  genomes (otherwise the in-process `mappy` binding is used).
- Optional: the **`[viz]`** extra (matplotlib) to run the plotting recipes / regenerate
  the example figures.

Core Python dependencies (installed automatically): `mcp`, `httpx`, `pandas`, `openpyxl`,
`numpy`, `python-dotenv`, `scikit-allel`, `scipy`, `mappy`.

## Installation

### Find & try it on Glama

`gigwa-mcp` is listed in the [Glama MCP directory](https://glama.ai/mcp/servers/gkanogiannis/gigwa-mcp) —
the quickest way to see what it does. Browse its tools, prompts and resources and **try it
live in the in-browser MCP Inspector**: it defaults to the public ICARDA instance with
anonymous access, so **no setup or credentials are needed** for a first look. Glama also
generates a ready-to-paste connection config for common MCP clients; under the hood that
just runs `uvx gigwa-mcp` (or the Docker image) — the same as the steps below.

### Install it yourself

**From PyPI** (recommended):

```bash
pip install gigwa-mcp                # core + analysis (scikit-allel/scipy)
pip install "gigwa-mcp[viz]"         # + matplotlib, for the plotting recipes
```

Or run it without installing into your environment using [pipx](https://pipx.pypa.io/)
or [uv](https://docs.astral.sh/uv/) which is handy as the `command` in an MCP client config
(see below):

```bash
pipx install gigwa-mcp        # then: gigwa-mcp
uvx gigwa-mcp                 # run on demand, no install step
```

**From source** (for development or an unreleased version):

```bash
git clone https://github.com/gkanogiannis/Gigwa-MCP.git gigwa-mcp && cd gigwa-mcp
python -m venv venv && source venv/bin/activate
pip install -e .            # core + analysis (scikit-allel/scipy)
pip install -e ".[dev]"     # + pytest, to run the test suite
pip install -e ".[viz]"     # + matplotlib, for plotting recipes / example figures
```

Run the stdio server directly to smoke-test:

```bash
python -m gigwa_mcp         # or: gigwa-mcp
```

(Normally you don't run it by hand as your MCP client launches it; see below.)

### Add it to Claude Code (the simple version)

Think of this as plugging a new tool into Claude Code so you can just *talk* to your Gigwa
server. You do it once, with a single command without editting any files by hand.

1. **Install [`uv`](https://docs.astral.sh/uv/), which provides the `uvx` command.** It's a
   small helper that downloads and runs `gigwa-mcp` for you, so you don't have to install
   anything else first:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
   # or, on Windows PowerShell:
   #   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   # or, if you already have Python/pip:
   #   pip install uv
   ```

   Then confirm it's reachable: `uvx --version` should print a version. If it says
   "command not found", `uvx` isn't on your `PATH` yet, then see the note below. (If you'd
   rather not use `uv` at all, `pipx install gigwa-mcp` works too; then use `gigwa-mcp`
   in place of `uvx gigwa-mcp` everywhere below.)

2. **Run this one command** in your terminal, swapping in your own Gigwa address, username,
   and password:

   ```bash
   claude mcp add gigwa --scope user \
     -e GIGWA_URL=http://localhost:8080/gigwa \
     -e GIGWA_USER=your_user \
     -e GIGWA_PASS=your_password \
     -- uvx gigwa-mcp
   ```

   What the pieces mean, in plain words:
   - `gigwa` : the nickname you're giving this tool.
   - `--scope user` : "make it available in all my projects" (use `--scope project` instead
     to share it with your team via a `.mcp.json` file in the repo).
   - the three `-e` lines : your Gigwa address and login, handed to the tool privately.
   - everything after `--` : the command that actually starts the server (`uvx gigwa-mcp`).

3. **Check it worked.** In Claude Code, type `/mcp`. You should see **gigwa** listed.

4. **Just ask.** Try: *"Is my Gigwa up, and what version?"* or *"List the databases."*
   Claude picks the right tool and fills in the details for you.

> **Note that `uvx` must be on your client's `PATH`.** If `/mcp` shows the server as
> **failed** with `Executable not found in $PATH: "uvx"`, the MCP client couldn't find
> `uvx`. The `uv` installer drops `uvx` in `~/.local/bin` (or `~/.cargo/bin`); make sure
> that directory is on the `PATH` of the shell/app that launches Claude (restart the app
> or your terminal after installing). As a workaround you can point the config at the
> absolute path (`"command": "/home/you/.local/bin/uvx"`), or avoid `uvx` entirely by
> `pipx install gigwa-mcp` and using `gigwa-mcp` as the `command`.

### Run with Docker

Prefer a container instead of `uvx`/`pipx`? Use the prebuilt image or build it yourself,
then let your MCP client launch it. The server speaks stdio, so the client starts it with
`docker run -i` the same way it would start `uvx gigwa-mcp`.

**Pull the prebuilt image** (published to the GitHub Container Registry, multi-arch
`linux/amd64` + `linux/arm64`):

```bash
docker pull ghcr.io/gkanogiannis/gigwa-mcp:latest
```

**…or build it yourself:**

```bash
docker build -t gigwa-mcp .
```

The examples below use the local tag `gigwa-mcp`; swap in
`ghcr.io/gkanogiannis/gigwa-mcp:latest` to run the prebuilt image instead.

The image starts in stdio mode by default. To serve Streamable HTTP instead, explicitly
set a port and publish it:

```bash
docker run -d --rm -p 8184:8184 -e GIGWA_MCP_PORT=8184 \
  -e GIGWA_URL -e GIGWA_USER -e GIGWA_PASS gigwa-mcp
```

**MCP client config** (Claude Desktop / Claude Code) — use `docker` as the command:

```json
{
  "mcpServers": {
    "gigwa": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "GIGWA_URL", "-e", "GIGWA_USER", "-e", "GIGWA_PASS",
        "-v", "/host/data:/data",
        "gigwa-mcp"
      ],
      "env": {
        "GIGWA_URL": "http://host.docker.internal:8080/gigwa",
        "GIGWA_USER": "your_user",
        "GIGWA_PASS": "your_password"
      }
    }
  }
}
```

- `-i` is required (stdio); `--rm` cleans up the container on exit.
- The bare `-e GIGWA_URL` form forwards each value from the `env` block above into the
  container, so credentials stay in your client config, not in the image.

**Files (volume mount).** Mount a host directory at `/data` (the container's working
directory). Put import inputs there and reference them by their in-container path, e.g.
`/data/report_snps.xlsx` and `/data/reference.sr.mmi`. Analysis outputs are written to
`/data/gigwa_results/<module>/`, which appears in your mounted host directory.

**Reaching Gigwa.** A Gigwa running on your host is *not* at `localhost` from inside the
container:

- **macOS / Windows:** use `http://host.docker.internal:8080/gigwa` (works out of the box).
- **Linux:** add `"--add-host=host.docker.internal:host-gateway"` to `args` and use the
  same URL, or use `"--network", "host"` and point `GIGWA_URL` at `http://localhost:8080/gigwa`.
- **Remote Gigwa:** just set `GIGWA_URL` to its address — no extra networking flags needed.

## Configuration

Connection settings come from the environment, optionally seeded from a `.env` file in
the working directory or any parent (`cp .env.example .env` and edit):

```dotenv
GIGWA_URL=http://localhost:8080/gigwa
GIGWA_USER=your_user
GIGWA_PASS=your_password
# GIGWA_TIMEOUT=120          # optional, seconds — read/request timeout
# GIGWA_CONNECT_TIMEOUT=10   # optional, seconds — TCP connect only
```

`GIGWA_URL` is the Gigwa base URL **without** the `/rest` suffix (it is appended
automatically). The target Gigwa may be local or remote. `.env` files are gitignored;
keep credentials out of version control.

**Zero config.** Every setting is optional — with **no** environment at all, the server
connects **anonymously to the public ICARDA instance** (`https://gigwa.icarda.org:8443/gigwa`),
so it works out of the box for a first look (a notice is printed to stderr). Set `GIGWA_URL`
to point at your own server.

**Anonymous access.** `GIGWA_USER`/`GIGWA_PASS` are **optional** — omit *both* to connect
as Gigwa's anonymous user, which can perform the public/read-only operations a given
instance exposes (discovery, `list_content`/`list_variant_sets`, `search_callsets`,
`count_variants`, and the read-only analyses on public data). Set both to authenticate
(required for import/write operations and private databases); setting only one is an error.

**Switching servers mid-conversation.** The `gigwa_connect` tool re-points every subsequent
tool at a different Gigwa server **without a restart** — e.g. *"connect to
`https://other.example:8443/gigwa`"*. The new connection is verified with a live round-trip
before it takes effect (a failure rolls back to the previous one), and the change lasts for
the session (env config is restored on restart). **Credentials never pass through the chat:**
to reach a server that needs credentials, pre-set a **named profile** in the environment and
reference it by name — `gigwa_connect(url, profile="prod")` reads `GIGWA_USER_PROD` /
`GIGWA_PASS_PROD`. Use `gigwa_connect(url, anonymous=true)` to force unauthenticated access.
The default `GIGWA_USER`/`GIGWA_PASS` are reused **only when reconnecting to the configured
`GIGWA_URL`** — switching to a *different* server without a profile connects anonymously, so
your home credentials are never sent to another host by accident.

```dotenv
# A named credential profile for gigwa_connect(url, profile="prod")
GIGWA_USER_PROD=your_user
GIGWA_PASS_PROD=your_password
```

## Connecting from an MCP client

Add a stdio server entry (Claude Desktop `claude_desktop_config.json` or Claude Code MCP
settings). If you `pip install`ed into a venv, point `command` at that venv's
`gigwa-mcp`; with [uv](https://docs.astral.sh/uv/) you can have it fetch and run the
published package on demand with no separate install:

```json
{
  "mcpServers": {
    "gigwa": {
      "command": "uvx",
      "args": ["gigwa-mcp"],
      "env": {
        "GIGWA_URL": "http://localhost:8080/gigwa",
        "GIGWA_USER": "your_user",
        "GIGWA_PASS": "your_password"
      }
    }
  }
}
```

Or with an explicit interpreter path (`"command": "/abs/path/to/venv/bin/gigwa-mcp"`,
no `args`) if you installed it into a virtual environment.

Credentials live in this config and every tool call authenticates on its own (token
generated and refreshed automatically), so no per-chat "connect" step is required. To drive
several Gigwa servers you can either register one entry each (e.g. `gigwa-local`,
`gigwa-remote`) with its own `GIGWA_URL`/credentials and name the one you mean in the prompt,
**or** stay in one session and switch at runtime with the `gigwa_connect` tool (see
*Switching servers mid-conversation* above) — pre-set a `GIGWA_USER_<PROFILE>` /
`GIGWA_PASS_<PROFILE>` pair per server so no secret is ever typed into the chat.

## Quick start

You talk to your MCP client in plain language; it calls the matching tool and fills in
arguments (paths, thresholds, module names) from what you say. A typical first session:

| You ask | Tool called |
|---------|-------------|
| "Is my Gigwa up, and what version?" | `gigwa_server_info` |
| "Connect and list the databases." | `list_content` |
| "Import `report_snps.xlsx` into a new database `MYDB`, anchored to `reference.sr.mmi`." | `import_dartseq(..., reference_fasta=...)` |
| "Now run call-rate QC and a PCA on that run." | `qc_call_rate` → `diversity_pca` |
| "Scan the whole instance for badly imported databases." | `audit_import_quality` |

More example prompts:

| You ask | Tool called |
|---------|-------------|
| "Load this VCF into project `trial1`." | `import_vcf` |
| "Validate then import this individual-metadata TSV." | `validate_metadata` → `import_metadata` |
| "Find duplicate / clonal accessions." | `qc_duplicate_accessions` |
| "Flag heterozygosity outliers (contamination / off-types)." | `qc_heterozygosity` |
| "Which markers would a MAF 5% / 50%-missing filter drop?" | `qc_maf_filter` |
| "Give me per-marker MAF, He, Ho, PIC." | `diversity_summary` |
| "Compute the kinship matrix." | `diversity_kinship` |
| "Compute Fst between these two groups of accessions." | `diversity_fst` |
| "Compare diversity (He/Ho/allelic richness) across my populations." | `diversity_by_group` |
| "Pick a core collection of ~10% that captures the most diversity." | `diversity_core_collection` |
| "How many genetic clusters are in this collection?" | `diversity_structure` |
| "Build a UPGMA tree of the accessions." | `diversity_tree` |

## Tool reference

All variant-set tools take `variant_set_db_id` (`MODULE§projectNumber§run`). QC/diversity
tools also accept `output_dir` (defaults to `./gigwa_results/<module>/`), the scaling
args `max_markers` / `method` (`"vcf"` | `"allelematrix"`), and `region`
(`"chrom"` / `"chrom:start-end"`); see [Performance & scaling](#performance--scaling).

**Connection & import**

| Tool | Key arguments | Returns / writes |
|------|---------------|------------------|
| `gigwa_connect` | `url`, `profile?`, `anonymous=False` | switches the active server (verified); creds from env (`GIGWA_USER[_PROFILE]`), never the chat |
| `gigwa_server_info` | (none) | server version + auth check |
| `list_content` | (none) | database → project → run hierarchy |
| `import_dartseq` | `snp_xlsx?`, `silico_xlsx?`, `module`, `project`, `run`, `ploidy=2`, `reference_fasta?`, `positions_csv?`, `wait=True` | imports a DArTseq report; marker/sample counts + final status |
| `import_vcf` | `vcf_path`, `module`, `project`, `run`, `ploidy=2`, `wait=True` | imports a `.vcf`/`.vcf.gz` |
| `map_dartseq_to_reference` | `snp_xlsx`, `reference_fasta`, `min_mapq`, `backend="auto"` | `dartseq_positions.csv` (chrom/pos/strand per marker) |
| `validate_metadata` | `tsv_path`, `module`, `metadata_type="Individual"` | validation issues (no import) |
| `import_metadata` | `tsv_path`, `module`, `metadata_type="Individual"` | imports per-individual attributes |
| `get_import_progress` | `progress_token` | current async-job status |
| `abort_import` | `progress_token` | requests cancellation of a running process |

**Discovery, search & export**

| Tool | Key arguments | Returns / writes |
|------|---------------|------------------|
| `list_variant_sets` | (none) | every run's exact `variantSetDbId` + counts |
| `list_sequences` | `variant_set_db_id` | chromosomes/contigs (valid `reference_name`s) |
| `count_variants` | `reference_name?`, `start?`, `end?`, `min_maf?`, `max_maf?`, `max_missing_data?` | server-side match count (no download) |
| `search_variants` | same filters as `count_variants`, `max_variants=100000` | `variant_search.csv` (id/chrom/pos/ref/alt) |
| `list_export_formats` | (none) | the instance's export handlers + type/ploidy limits |
| `export_genotypes` | `output_path`, `format="VCF"`, plus `region?`, `min_maf?`, `individuals?`, `metadata_fields?`, `wait=True` | writes the export file; `wait=False` returns a URL or saves an immediately returned file |
| `get_export_progress` | (none) | status of this session's export |
| `fetch_export_file` | `download_url`, `output_path` | writes the completed export |
| `search_callsets` | `variant_set_db_id` | `sample_metadata.csv` (per-sample attributes) |
| `get_germplasm_metadata` | `variant_set_db_id` | `germplasm_metadata.csv` (join on `sample_name`) |
| `list_metadata_values` | `variant_set_db_id` | metadata fields + distinct values |
| `filter_individuals_by_metadata` | `variant_set_db_id`, `filters_json` | matching individual identifiers |

**QC & diversity** (output files listed in [Output files](#output-files))

| Tool | Key arguments | Flags / interprets |
|------|---------------|--------------------|
| `qc_call_rate` | `min_sample_call_rate=0.5`, `min_marker_call_rate=0.5` | samples/markers below threshold |
| `qc_heterozygosity` | `outlier_sd=3.0` | Ho outliers; warns if cohort mean Ho implausibly high |
| `qc_duplicate_accessions` | `similarity_threshold=0.95`, `max_markers=5000` | duplicate/clone groups; warns on degenerate clustering |
| `qc_maf_filter` | `maf_threshold=0.05`, `max_missing=0.5` | counts monomorphic / low-MAF / high-missing markers |
| `diversity_summary` | (none) | dataset means; warns on strongly negative Fis |
| `diversity_pca` | `n_components=10`, `outlier_sd=6.0`, `metadata_tsv?`, `group_column?` | variance explained + PC1/PC2 outliers |
| `diversity_kinship` | `top_pairs=15` | mean off-diagonal, top related pairs, inbreeding diagonal |
| `diversity_fst` | `groups_json?` **or** `metadata_tsv`+`group_column`, `id_column="individual"` | pairwise Fst |
| `diversity_by_group` | `groups_json?` / `metadata_tsv`+`group_column` | per-group He/Ho/Fis/MAF/%poly/allelic richness |
| `diversity_core_collection` | `size?` **or** `fraction=0.1` | core set + % of diversity captured |
| `diversity_structure` | `k_min=2`, `k_max=10` | suggested K (pseudo-F) + per-K table; warns on degenerate clustering |
| `diversity_tree` | `max_markers=5000` | UPGMA Newick (`tree.nwk`) |

**Audit**

| Tool | Key arguments | Returns / writes |
|------|---------------|------------------|
| `audit_import_quality` | `variant_set_db_id?` (omit = whole instance), `max_markers=1000`, `max_samples=300`, thresholds | ranked BROKEN/SUSPECT/OK + `import_quality_scan.csv` |

## Prompts & resources

Besides tools, the server exposes MCP **prompts** and **resources** (visible in clients that
support them, and in directories like glama.ai).

**Prompts** — reusable, argument-driven workflows that chain the right tools for a task:

| Prompt | Arguments | What it walks you through |
|--------|-----------|---------------------------|
| `import_and_qc` | `data_path`, `module`, `project`, `run`, `reference?` | import a DArTseq/VCF dataset, then the standard QC + audit |
| `diversity_report` | `variant_set_db_id`, `metadata_tsv?`, `group_column?` | summary → PCA/structure → kinship → tree (+ per-group Fst) |
| `qc_triage` | `variant_set_db_id` | full QC suite + a go/no-go verdict for downstream analysis |
| `explore_instance` | (none) | server info → list content/variant sets → instance-wide audit |
| `region_scan` | `variant_set_db_id`, `region` | sequences → count/search variants → region-filtered diversity |

**Resources** — read-only endpoints a client can fetch:

| Resource | Contents |
|----------|----------|
| `catalog://tools` | categorised catalog of all tools with their EDAM operation/topic tags |
| `gigwa://server/info` | configured connection info (target URL + auth mode); no network call |

## Skills

The repo also ships **Agent Skills** (the open [`SKILL.md` standard](https://github.com/agentskills/agentskills))
under [`skills/`](skills/) — task-oriented guides that teach an agent how to drive the tools
above. They mirror the five workflow prompts and are discoverable on the
[LobeHub Skills Marketplace](https://lobehub.com/skills) and other `SKILL.md` directories.
The capability stays in the MCP server; the skills just sequence and explain the tools.

| Skill | Mirrors prompt | What it does |
|-------|----------------|--------------|
| `gigwa-import-and-qc` | `import_and_qc` | import DArTseq/VCF, then the full QC + audit and a clean/not-clean judgement |
| `gigwa-diversity-report` | `diversity_report` | diversity + structure + relatedness (PCA, structure, kinship, tree; optional by-group/Fst) |
| `gigwa-qc-triage` | `qc_triage` | full QC suite on an imported run → go/no-go verdict |
| `gigwa-explore-instance` | `explore_instance` | no-arg instance survey + health check |
| `gigwa-region-scan` | `region_scan` | variant density + local diversity within one region |

See [`skills/README.md`](skills/README.md) for the layout, prerequisites, and how to validate
or install them.

## Usage scenarios

**A. Import a DArTseq report, genome-anchored.** Map the tag sequences once, inspect, then
import reusing the positions:
> "Where do these DArT markers sit on the *X* genome at `reference.sr.mmi`?" → `map_dartseq_to_reference`
> "Looks good, import `report_snps.xlsx` into `MYDB` reusing that mapping." → `import_dartseq(..., positions_csv=...)`

**B. Vet an instance you inherited.** Before trusting any analysis, triage every run for
encoding artifacts:
> "Scan my whole Gigwa for databases that were imported badly." → `audit_import_quality`
Runs are ranked BROKEN / SUSPECT / OK with reasons, and the full table lands in
`import_quality_scan.csv`.

**C. Genebank cleaning.** Classic data-cleaning sweep on one run:
> "Check call rates, flag heterozygosity outliers, and find duplicate accessions in `MYDB§1§run1`."
→ `qc_call_rate` → `qc_heterozygosity` → `qc_duplicate_accessions`.

**D. Diversity & structure study.**
> "Give me a diversity summary, a PCA, the number of clusters, and a UPGMA tree for `MYDB§1§run1`."
→ `diversity_summary` → `diversity_pca` → `diversity_structure` → `diversity_tree`.

**E. Build a core collection.**
> "Pick a core of ~10% of accessions that captures the most allelic diversity." → `diversity_core_collection(fraction=0.1)`.

**F. Population comparisons from metadata.** Provide a metadata TSV with a grouping column
(e.g. `country`, `population`):
> "Using `meta.tsv` grouped by `population`, compare per-group diversity and compute pairwise Fst."
→ `diversity_by_group(metadata_tsv="meta.tsv", group_column="population")` → `diversity_fst(...)`.

## Output files

Each analysis writes one or more CSVs (Newick for the tree) under
`./gigwa_results/<module>/` (the audit writes to `./gigwa_results/`):

| File | Written by | Contents |
|------|------------|----------|
| `call_rate_samples.csv` / `call_rate_markers.csv` | `qc_call_rate` | per-sample / per-marker call rate + flags |
| `heterozygosity_samples.csv` | `qc_heterozygosity` | per-sample Ho, z-score, flag |
| `duplicate_pairs.csv` / `duplicate_groups.csv` | `qc_duplicate_accessions` | IBS pairs ≥ threshold, grouped |
| `marker_filter_stats.csv` | `qc_maf_filter` | per-marker MAF, missingness, would-remove flags |
| `diversity_markers.csv` | `diversity_summary` | per-marker MAF, He, Ho, PIC |
| `pca_coords.csv` | `diversity_pca` | per-sample PC coords (+ optional `group`, `outlier`) |
| `kinship_matrix.csv` | `diversity_kinship` | samples × samples GRM |
| `fst_pairwise.csv` | `diversity_fst` | Fst for every group pair |
| `diversity_by_group.csv` | `diversity_by_group` | per-group He/Ho/Fis/MAF/%poly/allelic richness |
| `core_collection.csv` | `diversity_core_collection` | rank, accession, cumulative allele coverage |
| `structure_clusters.csv` | `diversity_structure` | per-sample cluster + PC coords |
| `tree.nwk` | `diversity_tree` | UPGMA tree (Newick) |
| `import_quality_scan.csv` | `audit_import_quality` | one row per run: status + diagnostics + reasons |
| `variant_search.csv` | `search_variants` | matching variants (id, chrom, pos, ref, alt) |
| `germplasm_metadata.csv` | `get_germplasm_metadata` | server-stored per-individual attributes; join on `sample_name` |
| `sample_metadata.csv` | `search_callsets` | per-sample (callset) attributes |
| `dartseq_positions.csv` | `map_dartseq_to_reference` | per-marker chrom/pos/strand/mapq/status |

## Visualizing results

The tools output tables, not images, which keeps them composable. The figures below were
produced from a **synthetic** dataset by `docs/make_example_figures.py` (run
`pip install -e ".[viz]" && python docs/make_example_figures.py` to regenerate). The same
recipes work on the real CSVs the tools write.

### PCA: `pca_coords.csv`
![PCA](docs/img/pca.png)
```python
import pandas as pd, matplotlib.pyplot as plt
df = pd.read_csv("gigwa_results/MYDB/pca_coords.csv")
groups = df["group"] if "group" in df else pd.Series("all", index=df.index)
for g, sub in df.groupby(groups):
    plt.scatter(sub.PC1, sub.PC2, s=20, label=g)
plt.xlabel("PC1"); plt.ylabel("PC2"); plt.legend(); plt.savefig("pca.png")
```

### Population structure: `structure_clusters.csv`
![Structure](docs/img/structure.png)
```python
df = pd.read_csv("gigwa_results/MYDB/structure_clusters.csv")
plt.scatter(df.PC1, df.PC2, c=df.cluster, cmap="tab10", s=20)
plt.xlabel("PC1"); plt.ylabel("PC2"); plt.title("K-means clusters"); plt.savefig("structure.png")
```

### Kinship: `kinship_matrix.csv`
![Kinship](docs/img/kinship.png)
```python
g = pd.read_csv("gigwa_results/MYDB/kinship_matrix.csv", index_col=0)
plt.imshow(g.values, cmap="viridis"); plt.colorbar(label="relatedness"); plt.savefig("kinship.png")
```

### Per-group diversity: `diversity_by_group.csv`
![Per-group diversity](docs/img/diversity_by_group.png)
```python
d = pd.read_csv("gigwa_results/MYDB/diversity_by_group.csv").set_index("group")
d[["he", "ho", "allelic_richness"]].plot.bar(); plt.tight_layout(); plt.savefig("by_group.png")
```

### Core-collection coverage: `core_collection.csv`
![Core collection](docs/img/core_collection.png)
```python
c = pd.read_csv("gigwa_results/MYDB/core_collection.csv")
plt.plot(c["rank"], c["coverage_fraction"] * 100)
plt.xlabel("core size"); plt.ylabel("% alleles captured"); plt.savefig("core.png")
```

### UPGMA tree: `tree.nwk`
![UPGMA tree](docs/img/tree.png)

`tree.nwk` is standard Newick; open it directly in [FigTree](http://tree.bio.ed.ac.uk/software/figtree/)
or [iTOL](https://itol.embl.de/), or render in Python:
```python
from Bio import Phylo            # pip install biopython
Phylo.draw(Phylo.read("gigwa_results/MYDB/tree.nwk", "newick"))
```

## Performance & scaling

- **Small/medium runs:** the default `method="vcf"` exports once and caches; running
  several tools on the same run reuses the cached genotypes.
- **Large runs (hundreds of thousands of markers):** pass `method="allelematrix"` with a
  `max_markers` cap (e.g. 2000-20000) so genotypes are sampled **server-side** instead of
  exporting a multi-GB VCF. Statistics are estimated from the sample.
- **Many samples (thousands):** the server caps each `allelematrix` response at ~10,000
  cells, so at *N* samples a response holds ~`10000/N` markers, i.e. requests scale with
  `max_markers`. Keep `max_markers` modest on high-sample-count sets.
- **O(samples²) tools:** `diversity_kinship`, `qc_duplicate_accessions`, and
  `diversity_tree` build a samples × samples matrix (and the kinship CSV is written in
  full). Subsample markers and expect large output / slower runs beyond a few thousand
  accessions.
- The `audit_import_quality` tool is bounded by `max_markers` × `max_samples` per run, so
  it is cheap and roughly constant-cost even across a whole production instance.

## Limitations & disadvantages

- **Read-only analysis.** QC/diversity/audit never write results back to Gigwa; you get
  CSVs locally. (Import tools do write to Gigwa.)
- **No built-in plotting.** Tools emit CSV/Newick; use the
  [recipes above](#visualizing-results) (matplotlib/Bio.Phylo) to make figures.
- **`diversity_structure` is a lightweight heuristic.** It is PCA + K-means with a
  pseudo-F (Calinski-Harabasz) K suggestion; there is no true admixture model. On weakly
  or continuously structured data pseudo-F tends toward `k_max`; the per-K table is the
  real output and the tool warns when clustering is degenerate. For formal ancestry use a
  dedicated tool (ADMIXTURE / sNMF) on an exported VCF.
- **Diploid-biallelic assumptions** in places (IBS dosage 0/1/2, collapsed-token decode).
- **Grouping uses a metadata TSV, not server attributes.** Some Gigwa builds do not expose
  BrAPI germplasm/sample/attribute endpoints, so `diversity_fst` / `diversity_by_group`
  take groups from `groups_json` or a metadata TSV rather than querying Gigwa.
- **VCF export downloads the whole variant set** regardless of `max_markers`; use
  `method="allelematrix"` to subsample large sets.
- **Genome anchoring needs minimap2 + a reference**, and streaming very large indexes is
  I/O-bound.
- **Single interactive session — one operation at a time.** This is a per-user stdio
  server, not a concurrent/multi-user service. It drives Gigwa through one shared HTTP
  client, auth token and in-process genotype cache, which are not designed for parallel
  tool calls; long tools do run in a worker thread (so the connection stays responsive and
  streams progress), but heavy compute is still GIL-bound and effectively serialized —
  runs are meant to happen sequentially, and large matrices are held in RAM.

## Troubleshooting

- **Auth / "Missing required environment variable(s)".** Ensure `GIGWA_URL`, `GIGWA_USER`,
  `GIGWA_PASS` are set (env or `.env`). `GIGWA_URL` must **omit** the `/rest` suffix.
- **VCF import rejected / "not bgzipped".** Gigwa needs BGZF, not plain gzip. Recompress:
  `gunzip -c f.vcf.gz | bgzip > f.bgz.vcf.gz` (htslib `bgzip`).
- **Implausible ~95% heterozygosity after a DArT import.** That is Gigwa's built-in DArT
  parser mis-calling the 2-row format. Use `import_dartseq` (it calls genotypes in Python
  and imports a standard VCF) instead of importing the raw DArT report (see below).
- **`diversity_fst` / `diversity_by_group` report "no groups matched".** Check that
  `id_column` values in your TSV match the accession names (or callset ids) in the run.
- **Large set feels slow.** Use `method="allelematrix"` + a smaller `max_markers`, and
  avoid the O(samples²) tools on many thousands of accessions.

## DArTseq notes

DArTseq SNP reports use the classic 2-rows-per-marker layout (a reference-allele row and a
SNP-allele row, each cell `1`/`0`/`-`); Silico-DArT reports are 1 row per clone (dominant
presence/absence). `import_dartseq` does the genotype calling in Python and emits a
standard VCF, imported through Gigwa's verified VCF path:

```
(ref=1, alt=0) -> 0/0   (ref=0, alt=1) -> 1/1
(ref=1, alt=1) -> 0/1   otherwise      -> ./.   (missing / no allele detected)
```

This deliberately bypasses Gigwa's built-in DArT parser, which might mis-call the 2-row format
(there are cases that it imports reference homozygotes as heterozygous, producing implausible ~95%
heterozygosity). SNP and Silico use different allele models; import them as separate runs
unless you specifically intend to combine them.

### Genomic positions (optional)

DArTseq markers have no genomic coordinates, so by default they are placed on a single
`Unmapped` contig at sequential positions. If you have a reference genome FASTA, the marker
tag sequences (`AlleleSequence`, ~69 bp) can be aligned to it with minimap2 to infer real
chromosome/position/strand:

- `map_dartseq_to_reference(snp_xlsx, reference_fasta)` → a `dartseq_positions.csv` report
  (uniquely mapped / multi / unmapped), for inspection.
- `import_dartseq(..., reference_fasta=...)` → imports uniquely-mapped markers
  genome-anchored (minus-strand alleles complemented, output coordinate-sorted, one marker
  per genomic site); unmapped markers stay on `Unmapped`.
- `import_dartseq(..., positions_csv=...)` → reuse a `dartseq_positions.csv` from a previous
  run instead of re-aligning. Recommended for large genomes: align once, inspect, then
  import without paying the alignment cost again.

`reference_fasta` may be a FASTA (`.fa`/`.fa.gz`) or a prebuilt minimap2 `.mmi` index. By
default the **minimap2 CLI** backend is used when available: it streams over multi-part
indexes with bounded RAM, so very large (multi-gigabase) genomes work on modest machines.
The in-process `mappy` backend (`backend="mappy"`) loads the whole index into RAM instead.

Prebuild an index once (tuned for the ~69 bp tags) and reuse it:

```bash
minimap2 -x sr -d reference.sr.mmi reference.fasta   # build once
# then pass reference.sr.mmi as reference_fasta
```

## Project layout

```
gigwa_mcp/
  __main__.py           # python -m gigwa_mcp → stdio server
  config.py             # .env / env loading (GIGWA_URL/USER/PASS/TIMEOUT)
  client.py             # GigwaClient: auth, multipart upload, progress, BrAPI calls
  exports.py            # selection export options, response handling, safe downloads
  identifiers.py        # individual/sample/callset identifier resolution
  server.py             # FastMCP instance + get_client()
  importers/
    dartseq.py          # DArTseq xlsx → standard VCF (2-row genotype calling)
    refmap.py           # minimap2 tag → reference mapping
  analysis/
    genotypes.py        # load_genotypes (VCF / allelematrix backends), GenotypeMatrix
    stats.py            # pure pop-gen stats (MAF, He, PIC, IBS, GRM, allelic richness …)
    genebank.py         # core-collection + UPGMA helpers
    results.py          # output-dir resolution + CSV writing
  tools/                # @mcp.tool() wrappers: connection, genotype, metadata, qc,
                        #   diversity, audit
scripts/                # run_import_audit.py, run_qc_diversity_validation.py (generic)
docs/                   # make_example_figures.py + img/ (README figures)
skills/                 # Agent Skills (SKILL.md) mirroring the 5 prompts (for LobeHub etc.)
tests/                  # pytest suite (mocked client + synthetic fixtures)
```

## Testing

```bash
pip install -e ".[dev]"
pytest
```

`test_client.py` covers auth/token-refresh, multipart assembly and progress polling with a
mocked transport; `test_dartseq_convert.py` checks the conversion against synthetic
SNP/Silico fixtures; `test_stats.py` / `test_genebank.py` verify the pop-gen and genebank
statistics against hand-computed values; `test_genotypes.py` exercises VCF parsing +
callset-name mapping with a mock client. The suite needs no live Gigwa server.

## Changelog

### v1.9.1 — export and container reliability

- Selection exports now accept both Gigwa response styles: a queued download URL or an
  immediately returned binary file. Immediate files are written atomically and do not need
  progress polling or `fetch_export_file`.
- Docker once again defaults to clean stdio transport; entrypoint diagnostics go to stderr.
  HTTP mode requires an explicit `GIGWA_MCP_PORT`.
- Capped VCF and allele-matrix analyses now both use the first N markers in canonical
  Gigwa search order, making their sampled markers directly comparable.
- Individual/sample resolution preserves hyphenated accession names, and grouping reports
  identifiers it could not match.

### v1.9.0 — MCP SDK v2, selection-aware export & metadata endpoints

- **Migrated to the MCP Python SDK v2** (`mcp>=2.1,<3`). v1.x is upstream *maintenance mode,
  security fixes only*, and v2 carries a newer protocol revision. `FastMCP` becomes
  `MCPServer`; the transport configuration (`json_response`, `streamable_http_path`,
  `transport_security`) moved from mutable settings to `streamable_http_app()` parameters;
  and the server version is now a constructor argument, retiring the private
  `_mcp_server.version` write. Tools, prompts and resources are unchanged — the registration
  decorators are identical in v2. Verified with a scripted `initialize`/`tools/list`
  handshake over both stdio and HTTP.
- **Selection-aware export** (contributed by @GuilhemSempere, PR #2). `export_genotypes`
  gains region / variant-type / MAF / missing-data filters, `individuals` and
  `metadata_fields` selection, and `keep_on_server`; `wait=False` returns immediately with a
  download URL, pollable via the new `get_export_progress` and retrievable with
  `fetch_export_file`. `list_export_formats` reports what the instance's export-handler
  registry actually offers, including each format's variant-type/ploidy restrictions.
- **Native individual-metadata endpoints** (same PR). `list_metadata_values` and
  `filter_individuals_by_metadata` use the endpoints behind the Gigwa web UI's own
  attribute filters, and `get_germplasm` now tries them before BrAPI — so per-individual
  attributes are found on builds where BrAPI `search/germplasm` returns records with no
  `additionalInfo` at all.
- **`germplasm_metadata.csv` gained a `sample_name` column — join on it.** Gigwa's
  individual id (`germplasm_name`) is *not* the name the analysis tools give a sample: on a
  13,678-sample ICARDA database none of them agree, yet 10,090 collide numerically with a
  *different* individual, so joining on `germplasm_name` silently mis-grouped the majority of
  samples. `sample_name` bridges the two id spaces and resolves 13,678/13,678 there.
- **Removed the callset fallback from `get_germplasm_metadata`.** With the native endpoint
  tried first it no longer fires; when the germplasm level really is empty the tool now says
  so and points at `search_callsets` for sample-level attributes.
- **Hardened export downloads.** `fetch_export_file` takes a caller-supplied URL and the
  request carries the session's bearer token, so an absolute URL must now match the
  configured Gigwa origin; anything else is rejected before a request is made.
- **Declared `starlette` and `uvicorn`** explicitly — the HTTP transport imports both
  directly rather than relying on them arriving through `mcp`.

### v1.8.0 — callset-level metadata & richer connection info

- **New `search_callsets` tool.** Dumps a run's per-sample (callset) metadata — the resolved
  `sample_name`, the server's raw `callSetName`, `sampleDbId`, `callSetDbId`, and every key
  found across the callsets' `additionalInfo` — to `sample_metadata.csv`. This is the
  sample-level counterpart to `get_germplasm_metadata`.
- **`get_germplasm_metadata` falls back to the callset level.** Some instances store passport
  data on the samples rather than on BrAPI germplasm records, where the tool previously
  reported "no metadata available" despite every sample carrying a full record (observed on
  the ICARDA durum-wheat database). It now falls back to the callset level and still writes
  `germplasm_metadata.csv`, reporting an empty result only when neither level exposes any
  attribute.
- **Fallback output joins to the analysis tools.** `germplasm_name` holds the resolved sample
  name (the same rule `GenotypeMatrix.sample_names` uses) and `germplasm_db_id` the
  `callSetDbId` (`sample_names`' fallback key), so either column groups samples in
  `diversity_fst` / `diversity_by_group` without manual renaming. Note `metadata_tsv` expects
  tab-separated input — convert the CSV first.
- **Pinned the MCP SDK to the 1.x line (`mcp>=1.27,<2`).** The previous `mcp>=1.27` began
  resolving to mcp 2.x on 2026-07-28, which renamed `FastMCP` to `MCPServer` and removed
  `mcp.server.fastmcp` — so a fresh `pip install gigwa-mcp` (and any Docker image rebuild)
  failed on import. The cap restores installability. Note the 1.x line is in maintenance mode
  upstream and receives security fixes only; migrating to the v2 SDK is tracked separately.

- **`gigwa_server_info` reports account permissions.** When the build's `userInfo` supplies
  them, the connection summary now also lists the server-side identity, email, and the
  databases the account may write to / manage / administer. Builds returning an empty
  `userInfo` are unaffected — every line is guarded.

### v1.7.0 — HTTP transport

- **Streamable HTTP transport.** The server can now run over HTTP in addition to stdio:
  `python -m gigwa_mcp --port 8184` serves the MCP StreamableHTTP endpoint at `/mcp` (stdio
  stays the default; `--stdio` is explicit). Adds Docker/entrypoint wiring, DNS-rebinding /
  allowed-host protection configurable via `GIGWA_MCP_ALLOWED_HOSTS` /
  `GIGWA_MCP_ALLOWED_ORIGINS` / `GIGWA_MCP_DISABLE_DNS_REBINDING_PROTECTION`, JSON responses
  for clients that only advertise `application/json`, and tolerance for malformed
  `notifications/initialized` POSTs. HTTP mode binds loopback (`127.0.0.1`) by default; set
  `GIGWA_MCP_HOST=0.0.0.0` to accept remote connections (the Docker image sets it). Contributed
  by @guignonv (PR #1).

### v1.6.0 — runtime server switch

- **Switch servers mid-conversation.** A new `gigwa_connect(url, profile?, anonymous?)` tool
  re-points every subsequent tool at a different Gigwa instance without restarting the
  server. The switch is verified with a live round-trip before it takes effect (a failure
  rolls back to the previous connection) and lasts for the session. **Credentials never pass
  through the chat:** they are resolved from the environment — the default `GIGWA_USER`/
  `GIGWA_PASS`, or a named profile's `GIGWA_USER_<PROFILE>`/`GIGWA_PASS_<PROFILE>` — or
  omitted with `anonymous=true`. See [Configuration](#configuration).

### v1.5.0 — Agent Skills

- **Agent Skills.** A new [`skills/`](skills/) folder ships five Agent Skills (the open
  [`SKILL.md` standard](https://github.com/agentskills/agentskills)) mirroring the five
  workflow prompts — `gigwa-import-and-qc`, `gigwa-diversity-report`, `gigwa-qc-triage`,
  `gigwa-explore-instance`, `gigwa-region-scan` — discoverable on the
  [LobeHub Skills Marketplace](https://lobehub.com/skills) and other `SKILL.md` directories.
  [Skills](#skills).

### v1.4.16 — anonymous access & fast-fail timeouts

- **Anonymous access.** `GIGWA_USER`/`GIGWA_PASS` are now optional — omit *both* to connect
  as Gigwa's anonymous user and run the public/read-only operations an instance exposes
  (`list_content`, `list_variant_sets`, `search_callsets`, `count_variants`, read-only
  analyses). Verified against the public `gigwa.icarda.org` demo. Setting only one of the
  two is now an error.
- **Fast-fail, configurable connection timeout.** An unreachable/misconfigured Gigwa now
  errors in seconds instead of hanging for the full request timeout: the TCP-connect phase
  is capped separately (default 10 s, override with `GIGWA_CONNECT_TIMEOUT`), while
  read/import/export timeouts are unchanged.
- **`serverInfo` version.** The server now reports the `gigwa-mcp` package version (it
  previously surfaced the MCP SDK version).

### v1.3.4 — tool catalog, EDAM annotations & progress reporting

- **Tool catalog** in `server.py`: a central `TOOL_CATALOG` annotates all 28 tools with a
  category and [EDAM](https://edamontology.org) ontology terms (operation + topic). These
  ride along as each tool's `_meta` in `tools/list`, and are published as a
  `catalog://tools` MCP resource — improving discovery/indexing (e.g. by directories such
  as glama.ai). A test asserts every tool has a catalog entry so the two can't drift.
- **Progress reporting** for long-running tools: imports, exports, `map_dartseq_to_reference`,
  and every genotype-load-based QC/diversity tool now stream `notifications/progress` to the
  client (live import %, "Exporting VCF…", "Fetching genotypes… page k/N", "Parsing…").
  Implemented with a `@progress_tool` decorator + a small `progress.notify()` bridge, so tool
  bodies stay synchronous and no `Context` is threaded through the call stack.
- **Prompts & resources.** Five workflow **prompts** (`import_and_qc`, `diversity_report`,
  `qc_triage`, `explore_instance`, `region_scan`) and **resources** (`catalog://tools`,
  `gigwa://server/info`) — so the server advertises the full set
  of MCP capabilities (tools + prompts + resources). See [Prompts & resources](#prompts--resources).

### v1.2.0 — server-side search, filtered analysis & export

Adds 7 tools (**21 → 28**) that surface more of the Gigwa REST API, plus a genomic-region
filter on every analysis tool. Live-verified against Gigwa 2.12-RELEASE and 2.13-beta2.

- **Server-side variant search** (no full download): `count_variants` and `search_variants`
  filter by genomic region, MAF range, and missing-data fraction via Gigwa's GA4GH
  `variants/search`; `search_variants` writes `variant_search.csv`.
- **Region-restricted analysis**: every QC & diversity tool now accepts
  `region` (`"chrom"` or `"chrom:start-end"`, 1-based) to run on a single genomic window.
- **Discovery & export**: `list_variant_sets` (exact `variantSetDbId`s), `list_sequences`
  (chromosomes/contigs), and `export_genotypes` (VCF/PLINK/Flapjack; formats vary by build).
- **Robustness**: `abort_import` (cancel a running process), `get_germplasm_metadata`
  (pull server-stored per-individual attributes → `germplasm_metadata.csv`), and
  `gigwa_server_info` now reports the server-side user roles when available.

### v1.1.0 — Docker support

- `Dockerfile` (multi-stage) and `.dockerignore` to build and run the server as a
  container launched by an MCP client via `docker run -i`. See
  [Run with Docker](#run-with-docker).

### v1.0.0 — initial release

- 21 tools: connection/inventory, DArTseq/VCF import (with optional reference anchoring)
  and metadata import, read-only QC and diversity/population-structure analyses, and the
  import-quality audit.

## License & contributing

Released under the [Apache License 2.0](LICENSE) © 2026 Anestis Gkanogiannis
<anestis@gkanogiannis.com> (see also [NOTICE](NOTICE)).

Issues and pull requests are welcome. Please run `pytest` before submitting, keep new
analysis logic in pure, unit-tested helpers under `gigwa_mcp/analysis/`, and avoid
committing data, credentials, or result files (these are gitignored).
