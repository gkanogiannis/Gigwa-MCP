<p align="center">
  <img src="docs/img/logo.png" alt="Gigwa MCP" width="460">
</p>

[![gigwa-mcp MCP server](https://glama.ai/mcp/servers/gkanogiannis/gigwa-mcp/badges/card.svg)](https://glama.ai/mcp/servers/gkanogiannis/gigwa-mcp)

[![MCP Badge](https://lobehub.com/badge/mcp/gkanogiannis-gigwa-mcp)](https://lobehub.com/mcp/gkanogiannis-gigwa-mcp)

# Gigwa MCP Server

An [MCP](https://modelcontextprotocol.io) server that drives a local or remote
[Gigwa](https://github.com/SouthGreenPlatform/Gigwa2) installation over its REST API.
It lets an MCP client (Codex CLI / Claude Desktop / Claude Code) run the whole genotyping workflow
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
    - [Try it first (no install)](#try-it-first-no-install)
    - [1. Get it](#1-get-it)
    - [2. Point your client at it](#2-point-your-client-at-it)
    - [Installing from the MCPB bundle](#installing-from-the-mcpb-bundle)
  - [Run with Docker](#run-with-docker)
  - [Configuration](#configuration)
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
    - [v1.9.2 — Windows dependency compatibility](#v192--windows-dependency-compatibility)
    - [v1.9.1 — export and container reliability](#v191--export-and-container-reliability)
    - [v1.9.0 — MCP SDK v2, selection-aware export \& metadata endpoints](#v190--mcp-sdk-v2-selection-aware-export--metadata-endpoints)
    - [Earlier releases](#earlier-releases)
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

#### Import pipeline

| Tool | What it does |
| ------ | -------------- |
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

#### Discovery, search & export (read-only)

| Tool | What it does |
| ------ | -------------- |
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

#### QC & diversity (read-only)

| Tool | What it does |
| ------ | -------------- |
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
| `diversity_structure` | Lightweight ancestry with PCA + K-means, pseudo-F suggests K (pure Python, all platforms) |
| `diversity_admixture` | Model-based ancestry (Q matrix) via the real ADMIXTURE binary, CV-picked K, or supervised against reference groups (Linux/macOS) |
| `diversity_tree` | UPGMA dendrogram of accessions from IBS distance, written as Newick (`tree.nwk`) |

Every QC & diversity tool also accepts `region` (`"chrom"` or `"chrom:start-end"`, 1-based;
from `list_sequences`) to restrict the analysis to one genomic window.

#### Import-quality audit

| Tool | What it does |
| ------ | -------------- |
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
- A reachable **Gigwa** server (local or remote). Credentials are optional for public
  data; the default ICARDA connection is anonymous.
- Optional: the **minimap2** CLI on `PATH` for DArTseq genome-anchoring of very large
  genomes (otherwise the in-process `mappy` binding is used).
- Optional: the **`[viz]`** extra (matplotlib) to run the plotting recipes / regenerate
  the example figures.

Core Python dependencies (installed automatically): `mcp`, `httpx`, `pandas`, `openpyxl`,
`numpy`, `python-dotenv`, `scikit-allel`, `scipy`, `mappy`.

Starting with 1.9.2, `mappy` is installed automatically only on non-Windows platforms
(including Linux and macOS). Windows installations skip it. Reference mapping still
requires a usable minimap2 CLI or mappy backend; use Linux/Docker when neither is
available. Other tools do not require mappy. Native Windows installation of the full
dependency set has not yet been verified.

## Installation

Two steps: get the server, then point your MCP client at it. The client launches it for you —
you never run it by hand. With no configuration it connects to the public ICARDA instance
anonymously, so you can try everything before setting up credentials.

### Try it first (no install)

`gigwa-mcp` is listed in the [Glama MCP directory](https://glama.ai/mcp/servers/gkanogiannis/gigwa-mcp),
where you can browse its tools and **run it live in the in-browser MCP Inspector** — no setup
or credentials needed. Glama also generates a ready-to-paste config for common clients; under
the hood it runs `uvx gigwa-mcp`, exactly like the steps below.

### 1. Get it

Pick one. **uvx is the simplest** — it downloads and runs the published package on demand,
with no install step and nothing to update.

| Route | Command | Notes |
| ----- | ------- | ----- |
| **uvx** (recommended) | `uvx gigwa-mcp` | Needs [uv](https://docs.astral.sh/uv/getting-started/installation/). Pin a release with `uvx gigwa-mcp==1.9.2`. |
| **pip** | `pip install gigwa-mcp` | Into a venv you manage; `"gigwa-mcp[viz]"` adds plotting. |
| **pipx** | `pipx install gigwa-mcp` | Isolated environment, managed for you. |
| **Docker** | `docker pull ghcr.io/gkanogiannis/gigwa-mcp:latest` | Multi-arch `linux/amd64` + `linux/arm64`. See [Run with Docker](#run-with-docker). |
| **From source** | `git clone …` then `pip install .` | For development; see below. |
| **MCPB bundle** | [download](https://github.com/gkanogiannis/Gigwa-MCP/releases/latest/download/GigwaMCP.mcpb) | Desktop clients that support MCPB. Linux x86-64 + Python 3.13 only. |

A dedicated virtual environment, if you prefer an explicit path to point your client at:

```bash
python3 -m venv "$HOME/.local/share/gigwa-mcp-venv"
"$HOME/.local/share/gigwa-mcp-venv/bin/python" -m pip install gigwa-mcp
"$HOME/.local/share/gigwa-mcp-venv/bin/gigwa-mcp" --help
```

**From source**, for development or to run an unreleased checkout:

```bash
git clone https://github.com/gkanogiannis/Gigwa-MCP.git
cd Gigwa-MCP
python3 -m venv venv
venv/bin/python -m pip install -e ".[dev]"     # drop -e and [dev] for a plain install
venv/bin/gigwa-mcp --help
```

The server looks for `.env` in its **working directory and parent directories**, not beside
its executable — so with a source checkout, launch it from the repository root (see the Codex
example below) or set the variables in your client config.

> **If your client reports `Executable not found in $PATH: "uvx"`**, the `uv` installer put
> `uvx` in `~/.local/bin` (or `~/.cargo/bin`) and the app launching your client cannot see it.
> Restart the app after installing, use the absolute path from `command -v uvx`, or install
> with `pipx install gigwa-mcp` and use `gigwa-mcp` as the command instead.

### 2. Point your client at it

| Client | Command |
| ------ | ------- |
| **Claude Code** | `claude mcp add gigwa --scope user -- uvx gigwa-mcp` |
| **Codex CLI** | `codex mcp add gigwa -- uvx gigwa-mcp` |
| **Claude Desktop** and other JSON configs | see the snippet below |

Add credentials with `-e`, or leave them out to use the public ICARDA instance:

```bash
claude mcp add gigwa --scope user \
  -e GIGWA_URL=http://localhost:8080/gigwa \
  -e GIGWA_USER=your_user -e GIGWA_PASS=your_password \
  -- uvx gigwa-mcp
```

In plain words: `gigwa` is the name you are giving this tool; `--scope user` makes it
available in all your projects (`--scope project` shares it with your team through a
`.mcp.json` in the repo); everything after `--` is the command that actually starts the
server. Values passed with `-e` are stored in your client's configuration, and a literal
password also lands in your shell history — a `.env` file or inherited environment variables
avoid both. See [Configuration](#configuration).

**Check it worked.** In Claude Code type `/mcp` and look for **gigwa**; with Codex run
`codex mcp get gigwa`. Registration alone does not test connectivity, so ask
*"Is my Gigwa up, and what version?"* to make a real tool call. Then try *"List the
databases."*

**For a different install route**, swap the command after `--`: an absolute path for pip or
pipx (`command -v gigwa-mcp`), or, for a source checkout, `env --chdir` so the server finds
the repository's `.env`:

```bash
codex mcp add gigwa -- env --chdir="$(pwd)" "$(pwd)/venv/bin/gigwa-mcp"
```

**JSON config** (Claude Desktop, or any client configured by file):

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

Use `"command": "/abs/path/to/venv/bin/gigwa-mcp"` with no `args` for a virtual-environment
install. Every tool call authenticates on its own, so there is no per-chat "connect" step. To
drive several servers, either register one entry each (`gigwa-local`, `gigwa-remote`) and name
the one you mean in the prompt, or switch at runtime with `gigwa_connect` — pre-set a
`GIGWA_USER_<PROFILE>` / `GIGWA_PASS_<PROFILE>` pair per server so no secret is typed into the
chat.

### Installing from the MCPB bundle

[MCPB desktop extensions](https://github.com/modelcontextprotocol/mcpb) install by opening the
file in a desktop client that supports the format. Download
[GigwaMCP.mcpb](https://github.com/gkanogiannis/Gigwa-MCP/releases/latest/download/GigwaMCP.mcpb)
from the latest release, or a specific version from the
[releases page](https://github.com/gkanogiannis/Gigwa-MCP/releases). It is a release asset
rather than a file in the repository because it is ~86 MB.

The bundle carries **CPython 3.13 Linux x86-64 native libraries** and needs a matching system
with Python 3.13 on `PATH` — Python itself is not bundled, and its manifest launches `python3`,
so that command must resolve to 3.13. Upstream documents Claude Desktop MCPB support for macOS
and Windows, which this Linux bundle cannot serve; a successful `mcpb pack` does not verify
desktop-client compatibility. It does not include your `.env` credentials.

**Codex CLI cannot import MCPB files.** On a compatible Linux machine, extract the bundle into
a fresh directory (to avoid mixing old and new dependencies) and register its Python module:

```bash
curl -LO https://github.com/gkanogiannis/Gigwa-MCP/releases/latest/download/GigwaMCP.mcpb
mkdir -p "$HOME/.local/share/gigwa-mcp-bundle"
unzip GigwaMCP.mcpb -d "$HOME/.local/share/gigwa-mcp-bundle"
codex mcp add gigwa \
  --env "PYTHONPATH=$HOME/.local/share/gigwa-mcp-bundle:$HOME/.local/share/gigwa-mcp-bundle/venv/server/lib" \
  -- python3.13 -m gigwa_mcp
```

For Codex the uvx route above is usually simpler.

## Run with Docker

Run the server as a container instead of `uvx`/`pipx`. It speaks stdio either way, so your
MCP client starts it with `docker run -i` exactly as it would start `uvx gigwa-mcp`.

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

## Quick start

You talk to your MCP client in plain language; it calls the matching tool and fills in
arguments (paths, thresholds, module names) from what you say. A typical first session:

| You ask | Tool called |
| --------- | ------------- |
| "Is my Gigwa up, and what version?" | `gigwa_server_info` |
| "Connect and list the databases." | `list_content` |
| "Import `report_snps.xlsx` into a new database `MYDB`, anchored to `reference.sr.mmi`." | `import_dartseq(..., reference_fasta=...)` |
| "Now run call-rate QC and a PCA on that run." | `qc_call_rate` → `diversity_pca` |
| "Scan the whole instance for badly imported databases." | `audit_import_quality` |

More example prompts:

| You ask | Tool called |
| --------- | ------------- |
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
| "Run ADMIXTURE / give me ancestry proportions per accession." | `diversity_admixture` |
| "Estimate the admixed accessions' ancestry using the other groups as reference populations." | `diversity_admixture` with `reference_groups_json` |
| "Build a UPGMA tree of the accessions." | `diversity_tree` |

## Tool reference

All variant-set tools take `variant_set_db_id` (`MODULE§projectNumber§run`). QC/diversity
tools also accept `output_dir` (defaults to `./gigwa_results/<module>/`), the scaling
args `max_markers` / `method` (`"vcf"` | `"allelematrix"`), and `region`
(`"chrom"` / `"chrom:start-end"`); see [Performance & scaling](#performance--scaling).

#### Connection & import

| Tool | Key arguments | Returns / writes |
| ------ | --------------- | ------------------ |
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

#### Discovery, search & export

| Tool | Key arguments | Returns / writes |
| ------ | --------------- | ------------------ |
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
| ------ | --------------- | -------------------- |
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
| `diversity_admixture` | `k_min=2`, `k_max=6`, `seed=1`, `threads?`, `reference_groups_json?` | Q matrix at CV-best K + CV error per K; with `reference_groups_json`, one `--supervised` run at K = number of groups (Linux/macOS only) |
| `diversity_tree` | `max_markers=5000` | UPGMA Newick (`tree.nwk`) |

#### Audit

| Tool | Key arguments | Returns / writes |
| ------ | --------------- | ------------------ |
| `audit_import_quality` | `variant_set_db_id?` (omit = whole instance), `max_markers=1000`, `max_samples=300`, thresholds | ranked BROKEN/SUSPECT/OK + `import_quality_scan.csv` |

## Prompts & resources

Besides tools, the server exposes MCP **prompts** and **resources** (visible in clients that
support them, and in directories like glama.ai).

**Prompts** — reusable, argument-driven workflows that chain the right tools for a task:

| Prompt | Arguments | What it walks you through |
| -------- | ----------- | --------------------------- |
| `import_and_qc` | `data_path`, `module`, `project`, `run`, `reference?` | import a DArTseq/VCF dataset, then the standard QC + audit |
| `diversity_report` | `variant_set_db_id`, `metadata_tsv?`, `group_column?` | summary → PCA/structure → kinship → tree (+ per-group Fst) |
| `qc_triage` | `variant_set_db_id` | full QC suite + a go/no-go verdict for downstream analysis |
| `explore_instance` | (none) | server info → list content/variant sets → instance-wide audit |
| `region_scan` | `variant_set_db_id`, `region` | sequences → count/search variants → region-filtered diversity |

**Resources** — read-only endpoints a client can fetch:

| Resource | Contents |
| ---------- | ---------- |
| `catalog://tools` | categorised catalog of all tools with their EDAM operation/topic tags |
| `gigwa://server/info` | configured connection info (target URL + auth mode); no network call |

## Skills

The repo also ships **Agent Skills** (the open [`SKILL.md` standard](https://github.com/agentskills/agentskills))
under [`skills/`](skills/) — task-oriented guides that teach an agent how to drive the tools
above. They mirror the five workflow prompts and are discoverable on the
[LobeHub Skills Marketplace](https://lobehub.com/skills) and other `SKILL.md` directories.
The capability stays in the MCP server; the skills just sequence and explain the tools.

| Skill | Mirrors prompt | What it does |
| ------- | ---------------- | -------------- |
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
| ------ | ------------ | ---------- |
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
| `admixture_Q_K<k>.csv` | `diversity_admixture` | per-sample ancestry fractions at the CV-best K |
| `admixture_cv.csv` | `diversity_admixture` | cross-validation error / log-likelihood per K evaluated |
| `admixture_supervised_Q.csv` | `diversity_admixture` (supervised) | per-sample ancestry fractions, one column per reference group, plus each sample's `reference_group` (blank for targets) |
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
  real output and the tool warns when clustering is degenerate. `diversity_admixture` runs
  the real model-based ADMIXTURE tool instead, when that stronger result is needed.
- **`diversity_admixture` needs Linux or macOS.** It fetches the official ADMIXTURE
  binary (statically linked, no installer) into `~/.cache/gigwa-mcp/admixture` on first
  use; the author publishes no native Windows build, so on Windows run it via WSL, Linux,
  or Docker. ADMIXTURE is free for academic/non-profit use — commercial use requires a
  license from the author (see https://dalexander.github.io/admixture).
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
  server.py             # MCPServer instance + get_client()
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

The three most recent releases in detail; older ones in brief. See the
[releases page](https://github.com/gkanogiannis/Gigwa-MCP/releases) or `git log` for
commit-level history.

### v1.9.2 — Windows dependency compatibility

- `mappy` is no longer installed automatically on Windows, where it has no wheels and used
  to make `pip install gigwa-mcp` fail outright. Linux and macOS are unchanged.
- Reference mapping reports an actionable error when `mappy` is absent; the minimap2 CLI
  remains available as a backend, and no other tool needs it.

### v1.9.1 — export and container reliability

- Selection exports accept both Gigwa response styles — a queued download URL, or the file
  returned immediately. Immediate files are written atomically and skip progress polling.
- Docker defaults to clean stdio again; HTTP requires an explicit `GIGWA_MCP_PORT`, and
  entrypoint diagnostics go to stderr.
- Capped VCF and allele-matrix analyses sample the same first N markers in canonical Gigwa
  search order, so results from the two backends are directly comparable.
- Individual/sample resolution preserves hyphenated accession names, and grouping reports
  identifiers it could not match instead of dropping them silently.

### v1.9.0 — MCP SDK v2, selection-aware export & metadata endpoints

- Migrated to the MCP Python SDK v2 (`mcp>=2.1,<3`); the 1.x line is upstream maintenance
  mode. Tools, prompts and resources are unchanged.
- **Selection-aware export** (@GuilhemSempere, PR #2): `export_genotypes` gains region,
  variant-type, MAF and missing-data filters plus `individuals` / `metadata_fields`
  selection. `wait=False` returns a download URL for `get_export_progress` and
  `fetch_export_file`, and `list_export_formats` reports what the instance actually offers.
- **Native metadata endpoints** (same PR): `list_metadata_values` and
  `filter_individuals_by_metadata` use the endpoints behind Gigwa's own attribute filters,
  so per-individual attributes are found on builds where BrAPI returns none.
- **`germplasm_metadata.csv` gained a `sample_name` column — join on that, not
  `germplasm_name`.** Gigwa's individual id is not the name the analysis tools give a
  sample, so the old join could silently mis-group samples.
- `fetch_export_file` rejects download URLs that do not match the configured Gigwa origin,
  so the session's bearer token cannot be sent elsewhere.

### Earlier releases

- **v1.8.0** — `search_callsets` writes per-sample `sample_metadata.csv`;
  `get_germplasm_metadata` falls back to the callset level; `gigwa_server_info` reports
  account permissions; SDK pinned to `mcp<2`.
- **v1.7.0** — Streamable HTTP transport (`--port`) with DNS-rebinding and allowed-host
  protection; stdio stays the default. Contributed by @guignonv (PR #1).
- **v1.6.0** — `gigwa_connect` re-points the session at another Gigwa instance
  mid-conversation, verified before it takes effect. Credentials resolve from the
  environment, never from the chat.
- **v1.5.0** — five Agent Skills in [`skills/`](skills/) mirroring the workflow prompts.
- **v1.4.16** — anonymous access when `GIGWA_USER`/`GIGWA_PASS` are both omitted;
  fast-fail `GIGWA_CONNECT_TIMEOUT`; `serverInfo` reports the package version.
- **v1.3.4** — `TOOL_CATALOG` with EDAM annotations and the `catalog://tools` resource;
  `notifications/progress` from long-running tools; five prompts and two resources.
- **v1.2.0** — server-side `count_variants` / `search_variants`, a `region` filter on every
  analysis tool, `list_variant_sets` / `list_sequences` / `export_genotypes`, and
  `abort_import` (21 → 28 tools).
- **v1.1.0** — Docker support: a multi-stage `Dockerfile` for `docker run -i`.
- **v1.0.0** — initial release: 21 tools covering import, QC, diversity and the
  import-quality audit.

## License & contributing

Released under the [Apache License 2.0](LICENSE) © 2026 Anestis Gkanogiannis
<anestis@gkanogiannis.com> (see also [NOTICE](NOTICE)).

Issues and pull requests are welcome. Please run `pytest` before submitting, keep new
analysis logic in pure, unit-tested helpers under `gigwa_mcp/analysis/`, and avoid
committing data, credentials, or result files (these are gitignored).
