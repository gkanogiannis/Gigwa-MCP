"""Scan a Gigwa instance for badly-imported databases via audit_import_quality.

Reads connection settings from the environment / a local .env file. Pass a single
variantSetDbId to audit just that run, or no argument to scan the whole instance.

    python scripts/run_import_audit.py
    python scripts/run_import_audit.py "MODULE§1§run"
"""
from __future__ import annotations

import sys
import time

from gigwa_mcp.tools.audit import audit_import_quality

fn = getattr(audit_import_quality, "fn", audit_import_quality)  # unwrap FastMCP tool

target = sys.argv[1] if len(sys.argv) > 1 else None

t0 = time.time()
print(fn(variant_set_db_id=target), flush=True)
print(f"[{time.time() - t0:6.1f}s] done", flush=True)
