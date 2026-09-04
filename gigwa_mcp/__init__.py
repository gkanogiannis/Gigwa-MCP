"""Gigwa MCP server.

An MCP (Model Context Protocol) server that drives a local or remote Gigwa
installation over its REST API: connect, list content, import genotype data
(DArTseq xlsx reports or plain VCF) and individual metadata, and (Phase 2) run
QC and diversity analyses. Targeted at CGIAR genomic-resources teams and
genebanks.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gigwa-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
