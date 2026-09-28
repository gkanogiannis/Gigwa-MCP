"""Group, variant-subset and server-side statistics tools.

These orchestrate features Gigwa already has -- grouping individuals/samples, filtering
variants on per-group MAF / missing-data / heterozygosity, browsing the resulting
selection and charting per-interval statistics over it -- exactly as the Gigwa web UI
drives them. Nothing is computed or filtered here: every threshold is evaluated by
Gigwa, and no genotypes are downloaded.
"""

from __future__ import annotations


import pandas as pd

from .. import subsets
from ..analysis.genotypes import parse_region
from ..analysis.results import resolve_output_dir, write_csv
from ..client import CHART_ENDPOINTS
from ..identifiers import module_of
from ..selection import ENTITY_SAMPLES, VariantSelection, ga4gh_variant_set_id
from ..server import get_client, mcp, progress_tool
from . import load_json_arg


def _json_arg(value, name: str):
    try:
        return load_json_arg(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from exc


def build_selection(
    client,
    variant_set_db_id: str,
    *,
    region: str | None = None,
    reference_name: str | None = None,
    start: int | None = None,
    end: int | None = None,
    selected_variant_types: str | None = None,
    group_filters_json=None,
    entity: str | None = None,
    min_maf: float | None = None,
    max_maf: float | None = None,
    max_missing_data: float | None = None,
    min_het: float | None = None,
    max_het: float | None = None,
) -> tuple[VariantSelection, tuple[str, ...]]:
    """Assemble a :class:`VariantSelection` from tool arguments (shared by the tools)."""
    if region:
        if reference_name or start is not None or end is not None:
            raise ValueError("Give the window either as region or as reference_name/start/end, not both.")
        reference_name, start, end = parse_region(region)
    groups, samples, names = (), entity == ENTITY_SAMPLES, ()
    specs = _json_arg(group_filters_json, "group_filters_json")
    if specs:
        groups, samples, names = subsets.resolve_group_filters(
            client, variant_set_db_id, specs, entity=entity
        )
    selection = client.selection_from_filters(
        variant_set_db_id,
        reference_name=reference_name,
        start=start,
        end=end,
        min_maf=min_maf,
        max_maf=max_maf,
        max_missing_data=max_missing_data,
        min_het=min_het,
        max_het=max_het,
        selected_variant_types=selected_variant_types,
        groups=groups,
        work_with_samples=samples,
    )
    return selection, names


@mcp.tool()
def define_group(
    variant_set_db_id: str,
    name: str,
    members: list[str] | None = None,
    metadata_filters_json: str | dict | None = None,
    entity: str = "individuals",
) -> str:
    """Create (or replace) a named group of individuals or samples, for group-level filtering.

    Define the members either explicitly (``members``: individual ids, or sample ids with
    ``entity="samples"``) or from stored metadata (``metadata_filters_json``, same form as
    ``filter_individuals_by_metadata``: ``{"field": ["value", ...]}``; with samples, prefix
    parent-individual fields with ``ind.``). Members are checked against the project's own
    individuals/samples (Gigwa's GA4GH ``callsets/search``, the list the Gigwa UI shows);
    unknown ids are reported and left out.

    Gigwa has no stored "group" object -- like its web UI, this server keeps the group for
    the session and sends its members inside each query. Use the group by name in
    ``group_filters_json`` of ``define_variant_subset`` / ``count_variants`` /
    ``search_variants`` / ``export_genotypes``.
    """
    client = get_client()
    if entity not in ("individuals", "samples"):
        raise ValueError("entity must be 'individuals' or 'samples'.")
    samples = entity == ENTITY_SAMPLES
    filters = _json_arg(metadata_filters_json, "metadata_filters_json")
    if bool(members) == bool(filters):
        raise ValueError("Give exactly one of members or metadata_filters_json.")

    known = client.callset_names(variant_set_db_id, samples=samples)
    known_set = set(known)
    if filters:
        if not isinstance(filters, dict):
            raise ValueError("metadata_filters_json must be a JSON object of {field: [values]}.")
        records = client.filter_individuals_by_metadata(
            module_of(variant_set_db_id), filters, samples=samples
        )
        candidates = [str(r.get("id")) for r in records if r.get("id") is not None]
        source = f"metadata {filters}"
    else:
        candidates = [str(m).strip() for m in members if str(m).strip()]
        source = "explicit list"

    selected = [c for c in dict.fromkeys(candidates) if c in known_set]
    unknown = [c for c in dict.fromkeys(candidates) if c not in known_set]
    if not selected:
        hint = f" e.g. {', '.join(known[:5])}" if known else ""
        raise ValueError(
            f"None of the {len(candidates)} {entity} given exist in project "
            f"{ga4gh_variant_set_id(variant_set_db_id)} ({len(known)} {entity} there{',' if hint else ''}{hint})."
        )
    group, replaced = subsets.store_group(
        client, name=name, variant_set_db_id=variant_set_db_id, entity=entity,
        members=selected, source=source,
    )
    lines = [
        f"Group '{group.name}' {'replaced' if replaced else 'created'}: {len(selected)} {entity} "
        f"of {len(known)} in {group.project} (from {source})."
    ]
    if unknown:
        shown = ", ".join(unknown[:20]) + (f" … (+{len(unknown) - 20} more)" if len(unknown) > 20 else "")
        lines.append(f"Not found in this project, left out ({len(unknown)}): {shown}")
    lines.append(
        f'Use it as {{"group": "{group.name}", "min_maf": ..., "max_missing": ..., ...}} '
        "in group_filters_json."
    )
    return "\n".join(lines)


@mcp.tool()
def list_groups() -> str:
    """List the groups and variant subsets defined in this session (names, sizes, filters)."""
    groups = subsets.list_groups()
    subs = subsets.list_subsets()
    if not groups and not subs:
        return "No groups or subsets defined yet (see define_group / define_variant_subset)."
    lines = []
    if groups:
        lines.append(f"{len(groups)} group(s):")
        for g in groups:
            preview = ", ".join(g.members[:10]) + (" …" if len(g.members) > 10 else "")
            lines.append(f"  {g.name} — {len(g.members)} {g.entity} of {g.project} ({g.source}): {preview}")
    if subs:
        lines.append(f"{len(subs)} variant subset(s):")
        for s in subs:
            if s.running:
                size = "still being computed by Gigwa"
            elif s.failure is not None:
                size = f"failed ({s.failure})"
            else:
                size = f"{s.count} variant(s)"
            lines.append(f"  {s.name} — {size} in {s.selection.describe()}")
    return "\n".join(lines)


@progress_tool()
def define_variant_subset(
    variant_set_db_id: str,
    name: str | None = None,
    region: str | None = None,
    selected_variant_types: str | None = None,
    group_filters_json: str | list | None = None,
    min_maf: float | None = None,
    max_maf: float | None = None,
    max_missing_data: float | None = None,
    min_het: float | None = None,
    max_het: float | None = None,
    entity: str | None = None,
    wait: bool = True,
) -> str:
    """Run a variant filter in Gigwa and keep its result as a named, reusable subset.

    Filters are evaluated by Gigwa exactly as in its web UI: optional ``region`` and
    ``selected_variant_types`` (e.g. ``"SNP"``), plus genotype-level thresholds either for
    the whole population (flat ``min_maf``/``max_maf``/``max_missing_data``/``min_het``/
    ``max_het``, 0-1 fractions) or **per group** in ``group_filters_json`` -- a list such
    as ``[{"group": "G1", "min_maf": 0.05, "max_missing": 0.1, "max_het": 0.2}]``, where
    each entry names a ``define_group`` group (or lists ``members`` inline, or neither =
    everyone) with any of ``min_maf``, ``max_maf``, ``min_missing``, ``max_missing``,
    ``min_het``, ``max_het``, and ``discriminate_with`` (another group's name: keep only
    variants whose single dominant genotype differs between the two groups -- Gigwa's
    "Discriminate with"). Bounds are inclusive; every criterion of every group must
    hold. MAF applies to bi-allelic variants only; heterozygosity is the share of
    heterozygous calls among the group's non-missing genotypes at each variant.

    The search runs once under a token dedicated to this subset, so Gigwa keeps the
    matching variants in its temporary collection. Reuse the subset by ``name`` with
    ``search_variants`` (paged browsing), ``count_variants``, ``export_genotypes``,
    ``selection_profile`` and the QC/diversity tools' ``subset`` parameter. Redefining a
    name replaces it. ``name`` defaults to S1, S2, ...

    Genotype-level filtering can take a while on large datasets; its progress, as Gigwa
    reports it, is relayed while this call waits. Set ``wait=False`` to return as soon as
    the search is started instead: check it with ``get_subset_progress``; any tool given
    the subset meanwhile waits for it (relaying progress) before proceeding.
    """
    client = get_client()
    selection, names = build_selection(
        client, variant_set_db_id, region=region, selected_variant_types=selected_variant_types,
        group_filters_json=group_filters_json, entity=entity, min_maf=min_maf, max_maf=max_maf,
        max_missing_data=max_missing_data, min_het=min_het, max_het=max_het,
    )
    subset, replaced = subsets.create_subset(
        client, selection, name=name, group_names=names, wait=wait
    )
    if not wait:
        return (
            f"Subset '{subset.name}' {'replaced' if replaced else 'started'}: Gigwa is filtering "
            f"(not waiting).\n{selection.describe()}\n"
            f'Check status with get_subset_progress(subset="{subset.name}"); tools given '
            f'subset="{subset.name}" wait for it to finish.'
        )
    shared = "" if subset.token else (
        "\nNote: no dedicated token could be obtained (anonymous access on this build), so "
        "this subset shares Gigwa's anonymous result slot; define it again if a later search "
        "replaced it."
    )
    return (
        f"Subset '{subset.name}' {'replaced' if replaced else 'created'}: {subset.count} variant(s) "
        f"match.\n{selection.describe()}\n"
        f"Reuse it with subset=\"{subset.name}\" (search_variants to page through the variants, "
        f"export_genotypes, selection_profile, QC/diversity tools).{shared}"
    )


@mcp.tool()
def get_subset_progress(subset: str) -> str:
    """Report the status of a subset started with ``define_variant_subset(..., wait=False)``:
    still being filtered by Gigwa (with Gigwa's own progress description and percentage),
    failed (with the reason), or ready (with its variant count). Does not wait."""
    client = get_client()
    return subsets.subset_status(client, subsets.get_subset(client, subset, wait=False))


def _series_frame(stat: str, data) -> pd.DataFrame:
    """Gigwa returns {position: value} (a list of two such maps for tajima_d)."""
    if stat == "tajima_d" and isinstance(data, list):
        maps = {"tajima_d": data[0] if data else {}, "segregating_sites": data[1] if len(data) > 1 else {}}
    else:
        maps = {stat: data if isinstance(data, dict) else {}}
    positions = sorted({int(float(p)) for m in maps.values() for p in m})
    frame = {"position": positions}
    for col, m in maps.items():
        lookup = {int(float(k)): v for k, v in m.items()}
        frame[col] = [lookup.get(p) for p in positions]
    df = pd.DataFrame(frame)
    for col in maps:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@progress_tool()
def selection_profile(
    subset: str,
    statistic: str,
    sequence: str,
    start: int | None = None,
    end: int | None = None,
    intervals: int = 100,
    groups: list[str] | None = None,
    variant_type: str | None = None,
    output_dir: str | None = None,
) -> str:
    """Per-interval statistic along one sequence, computed by Gigwa over a variant subset.

    Calls the server-side endpoints behind the Gigwa web UI's charts, over the variants of
    ``subset`` (from ``define_variant_subset``): ``statistic`` is ``maf``, ``missing``
    (missing-data %), ``heterozygosity`` (%), ``fst`` (between groups; needs 2+ groups),
    ``tajima_d`` (also returns segregating sites) or ``density`` (variant count). The
    ``sequence`` (see ``list_sequences``) is split into ``intervals`` windows (50-5000;
    optionally within ``start``-``end``) and Gigwa returns one value per window. The
    statistic is computed on the genotypes of ``groups`` (names of the subset's groups;
    default: all of the subset's groups, or everyone). Writes ``profile_<statistic>_<sequence>.csv``.
    """
    client = get_client()
    if statistic not in CHART_ENDPOINTS:
        raise ValueError(f"statistic must be one of {sorted(CHART_ENDPOINTS)}.")
    intervals = max(50, min(5000, int(intervals)))
    sub = subsets.get_subset(client, subset)
    plot_groups = list(sub.selection.groups)
    if groups:
        by_name = {g.name: g for g in sub.selection.groups}
        missing = [g for g in groups if g not in by_name]
        if missing:
            raise ValueError(
                f"Subset '{sub.name}' has no group(s) {missing}; its groups are {list(by_name) or 'none'}."
            )
        plot_groups = [by_name[g] for g in groups]
    if statistic == "fst" and len(plot_groups) < 2:
        raise ValueError("fst needs at least two groups in the subset (pass groups=[...] or define them).")

    data = subsets.run_with_subset(
        client, sub,
        lambda token: client.selection_chart(
            statistic, sub.selection, token=token, sequence=sequence, variant_type=variant_type,
            range_min=start, range_max=end, intervals=intervals,
            plot_groups=plot_groups or None,
        ),
    )
    df = _series_frame(statistic, data)
    out = resolve_output_dir(sub.selection.variant_set_db_id, output_dir)
    safe_seq = "".join(c if c.isalnum() or c in "-_." else "_" for c in sequence)
    path = write_csv(df, out, f"profile_{statistic}_{safe_seq}.csv")

    value_col = df.columns[1]
    series = df[value_col].dropna()
    who = ", ".join(g.name or "all" for g in plot_groups) or "all"
    if series.empty:
        summary = "no value in any window (no variant of the subset on this sequence/range?)"
    else:
        top = df.loc[series.nlargest(3).index]
        low = df.loc[series.nsmallest(3).index]
        fmt = lambda rows: ", ".join(f"{int(r.position):,}={r[value_col]:.4g}" for _, r in rows.iterrows())  # noqa: E731
        summary = (
            f"{len(series)}/{len(df)} windows with a value; mean={series.mean():.4g}, "
            f"min={series.min():.4g}, max={series.max():.4g}\n"
            f"Highest windows (start position=value): {fmt(top)}\n"
            f"Lowest windows: {fmt(low)}"
        )
    return (
        f"Gigwa {statistic} profile of subset '{sub.name}' on {sequence} "
        f"({intervals} intervals; groups: {who})\n{summary}\nFile: {path}"
    )
