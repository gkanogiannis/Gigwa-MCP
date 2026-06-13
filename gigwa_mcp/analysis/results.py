"""Helpers for writing analysis result tables to disk."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .genotypes import _safe_name, module_of


def resolve_output_dir(variant_set_db_id: str, output_dir: str | Path | None) -> Path:
    """Directory for a variant set's result files.

    Defaults to ``./gigwa_results/<module>/`` (created if needed).
    """
    if output_dir:
        out = Path(output_dir)
    else:
        out = Path.cwd() / "gigwa_results" / _safe_name(module_of(variant_set_db_id))
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_csv(df: pd.DataFrame, out_dir: Path, filename: str, **kwargs) -> Path:
    path = out_dir / filename
    df.to_csv(path, index=kwargs.pop("index", False), **kwargs)
    return path
