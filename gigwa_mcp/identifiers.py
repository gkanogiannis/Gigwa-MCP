"""Canonical conversions between Gigwa individual, sample, and callset identifiers."""

from __future__ import annotations

from typing import Any


def module_of(variant_set_db_id: str) -> str:
    return variant_set_db_id.split("§", 1)[0]


def accession_name(entity_id: str | None, variant_set_db_id: str) -> str | None:
    """Remove only Gigwa's exact module and ``-project-run`` framing.

    Unlike splitting at the first hyphen, this preserves hyphens belonging to the
    accession itself.
    """
    if not entity_id:
        return None
    value = str(entity_id)
    body = value.split("§", 1)[1] if "§" in value else value
    parts = str(variant_set_db_id).split("§")
    if len(parts) == 3:
        suffix = f"-{parts[1]}-{parts[2]}"
        if body.endswith(suffix) and len(body) > len(suffix):
            body = body[: -len(suffix)]
    return body or None


def sample_name_map(callsets: list[dict[str, Any]], variant_set_db_id: str) -> dict[str, str]:
    """Map callSetDbId to the best human-readable sample/accession name."""
    out: dict[str, str] = {}
    for callset in callsets:
        callset_id = callset.get("callSetDbId")
        if callset_id is None:
            continue
        out[str(callset_id)] = (
            accession_name(callset.get("sampleDbId"), variant_set_db_id)
            or accession_name(callset.get("callSetName"), variant_set_db_id)
            or str(callset_id)
        )
    return out


def individual_name(callset: dict[str, Any], variant_set_db_id: str) -> str | None:
    """Recover the individual while preserving hyphens inside its name."""
    callset_name = str(callset.get("callSetName") or "")
    parts = callset_name.rsplit("-", 2)
    run = str(variant_set_db_id).split("§")[-1]
    if len(parts) == 3 and parts[0] and parts[1].isdigit() and parts[2] == run:
        return parts[0]
    return accession_name(callset.get("sampleDbId"), variant_set_db_id) or callset_name or None


def individual_to_sample_names(
    callsets: list[dict[str, Any]], variant_set_db_id: str
) -> dict[str, list[str]]:
    """Map individual identifiers to all analysis sample names representing them."""
    names = sample_name_map(callsets, variant_set_db_id)
    out: dict[str, list[str]] = {}
    for callset in callsets:
        individual = individual_name(callset, variant_set_db_id)
        resolved = names.get(str(callset.get("callSetDbId")))
        if individual and resolved and resolved not in out.setdefault(individual, []):
            out[individual].append(resolved)
    return out
