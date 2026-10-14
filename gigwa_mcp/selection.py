"""Translate group-level variant filters into the request bodies Gigwa itself expects.

Gigwa has no server-side "group" entity: its web UI (``main.js`` ``buildSearchQuery``)
describes groups entirely inside each GA4GH ``variants/search`` / ``exportData`` request,
as **parallel per-group arrays**:

* ``callSetIds`` holds group 1's members and ``additionalCallSetIds`` those of groups
  2..N, each member written ``module§<individual or sample id>`` (an empty list means
  "every individual/sample of the project");
* ``groupName``, ``gtPattern``, ``mostSameRatio``, ``discriminate``,
  ``annotationFieldThresholds`` and the ``min``/``max`` ``Maf`` / ``MissingData`` /
  ``HeZ`` arrays carry one element per group -- the server's genotype-filter path
  indexes all of them, so every array must be present and full length.

Server-side semantics (``GenotypingDataQueryBuilder``): thresholds are **percentages**
(MAF 0-50, missing data and heterozygosity 0-100), bounds are **inclusive**, and every
criterion of every group must hold (logical AND). MAF is only defined for bi-allelic
variants. ``discriminate`` is ``null`` per group unless the group is discriminated with
another: its value is then that group's 1-based number, and Gigwa keeps only variants
where both groups have exactly one dominant genotype and those differ (it evaluates a
mutual pair once).

The tools take 0-1 fractions; the conversion to Gigwa's percentages happens here only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

ENTITY_INDIVIDUALS = "individuals"
ENTITY_SAMPLES = "samples"
ENTITIES = (ENTITY_INDIVIDUALS, ENTITY_SAMPLES)

# Keys accepted in a group_filters JSON entry, mapped to GroupFilter fields.
_THRESHOLD_KEYS = ("min_maf", "max_maf", "min_missing", "max_missing", "min_het", "max_het")


def ga4gh_variant_set_id(variant_set_db_id: str) -> str:
    """GA4GH's ``variantSetId`` is ``module§project`` -- the BrAPI variantSetDbId with its
    trailing ``§run`` dropped (Gigwa's GA4GH endpoints fail on the three-part id)."""
    return "§".join(str(variant_set_db_id).split("§")[:2])


def _fraction(name: str, value: Any, upper: float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number between 0 and {upper}, got {value!r}.") from exc
    if not 0.0 <= number <= upper:
        raise ValueError(
            f"{name} must be a fraction between 0 and {upper} (e.g. 0.05 for 5%), got {value!r}."
        )
    return number


@dataclass(frozen=True)
class GroupFilter:
    """One group of individuals/samples and the genotype-level thresholds applied to it.

    ``members`` are bare individual (or sample) ids; empty means the whole project.
    Thresholds are 0-1 fractions; ``None`` leaves that bound at Gigwa's default.
    """

    name: str = ""
    members: tuple[str, ...] = ()
    min_maf: float | None = None
    max_maf: float | None = None
    min_missing: float | None = None
    max_missing: float | None = None
    min_het: float | None = None
    max_het: float | None = None
    # Name of another group of the same query: keep only variants whose single dominant
    # genotype in this group differs from that group's (Gigwa's "Discriminate with").
    discriminate_with: str | None = None

    def __post_init__(self) -> None:
        for key in _THRESHOLD_KEYS:
            upper = 0.5 if key.endswith("maf") else 1.0
            object.__setattr__(self, key, _fraction(key, getattr(self, key), upper))
        for low, high in (("min_maf", "max_maf"), ("min_missing", "max_missing"), ("min_het", "max_het")):
            lo, hi = getattr(self, low), getattr(self, high)
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"{low} ({lo}) is greater than {high} ({hi}).")
        object.__setattr__(self, "members", tuple(dict.fromkeys(str(m) for m in self.members)))

    @property
    def has_maf(self) -> bool:
        return self.min_maf is not None or self.max_maf is not None

    @property
    def has_thresholds(self) -> bool:
        """Whether this group needs Gigwa's genotype-level filtering (thresholds or
        discrimination) -- which is what builds the token's temporary collection."""
        return self.discriminate_with is not None or any(
            getattr(self, key) is not None for key in _THRESHOLD_KEYS
        )

    def describe(self) -> str:
        """Human-readable summary, e.g. ``G1 (12 members): MAF≥5%, missing≤10%``."""
        who = f"{len(self.members)} members" if self.members else "all"
        parts = []
        for label, low, high in (("MAF", "min_maf", "max_maf"),
                                 ("missing", "min_missing", "max_missing"),
                                 ("het", "min_het", "max_het")):
            lo, hi = getattr(self, low), getattr(self, high)
            if lo is not None:
                parts.append(f"{label}≥{lo * 100:g}%")
            if hi is not None:
                parts.append(f"{label}≤{hi * 100:g}%")
        if self.discriminate_with:
            parts.append(f"dominant genotype differs from {self.discriminate_with}")
        return f"{self.name or 'group'} ({who})" + (f": {', '.join(parts)}" if parts else "")


def group_filter_from_dict(spec: dict[str, Any], *, members: Iterable[str] = (), name: str = "") -> GroupFilter:
    """Build a :class:`GroupFilter` from one ``group_filters`` JSON entry (thresholds only;
    membership is resolved by the caller)."""
    unknown = set(spec) - set(_THRESHOLD_KEYS) - {"group", "members", "name", "discriminate_with"}
    if unknown:
        raise ValueError(
            f"Unknown key(s) {sorted(unknown)} in group filter; expected 'group' or 'members' "
            f"plus any of {list(_THRESHOLD_KEYS) + ['discriminate_with']}."
        )
    return GroupFilter(
        name=name,
        members=tuple(members),
        discriminate_with=str(spec["discriminate_with"]) if spec.get("discriminate_with") else None,
        **{key: spec.get(key) for key in _THRESHOLD_KEYS},
    )


def discrimination_indexes(groups: list[GroupFilter]) -> list[int | None]:
    """Gigwa's ``discriminate`` array: per group, the **1-based** number of the group it
    is discriminated with (the UI's "Discriminate with" select), or ``None``."""
    positions = {g.name: i for i, g in enumerate(groups) if g.name}
    out: list[int | None] = []
    for i, g in enumerate(groups):
        if g.discriminate_with is None:
            out.append(None)
            continue
        other = positions.get(g.discriminate_with)
        if other is None:
            raise ValueError(
                f"Group '{g.name or i + 1}' is to be discriminated with '{g.discriminate_with}', "
                f"which is not one of this query's groups ({[x.name for x in groups]})."
            )
        if other == i:
            raise ValueError(f"Group '{g.name}' cannot be discriminated with itself.")
        out.append(other + 1)
    return out


def group_arrays(module: str, groups: Iterable[GroupFilter]) -> dict[str, Any]:
    """The per-group array fields of a Gigwa search/export/chart body, in UI form."""
    groups = list(groups)
    if not groups:
        return {"callSetIds": []}

    def ids(group: GroupFilter) -> list[str]:
        return [f"{module}§{member}" for member in group.members]

    def pct(value: float | None, default: float) -> float:
        return round(value * 100, 6) if value is not None else default

    return {
        "callSetIds": ids(groups[0]),
        "additionalCallSetIds": [ids(g) for g in groups[1:]],
        "groupName": [g.name for g in groups],
        "discriminate": discrimination_indexes(groups),
        "gtPattern": [""] * len(groups),
        "mostSameRatio": ["100"] * len(groups),
        "minMaf": [pct(g.min_maf, 0.0) for g in groups],
        "maxMaf": [pct(g.max_maf, 50.0) for g in groups],
        "minMissingData": [pct(g.min_missing, 0.0) for g in groups],
        "maxMissingData": [pct(g.max_missing, 100.0) for g in groups],
        "minHeZ": [pct(g.min_het, 0.0) for g in groups],
        "maxHeZ": [pct(g.max_het, 100.0) for g in groups],
        "annotationFieldThresholds": [{} for _ in groups],
    }


@dataclass(frozen=True)
class VariantSelection:
    """A complete Gigwa variant query: variant-level filters plus per-group filters.

    This is exactly what the Gigwa web UI sends on "Search"; the same body drives
    counting, browsing, exporting and the server-side chart statistics.
    """

    variant_set_db_id: str
    reference_name: str | None = None
    start: int | None = None
    end: int | None = None
    selected_variant_types: str | None = None
    groups: tuple[GroupFilter, ...] = field(default_factory=tuple)
    work_with_samples: bool = False

    @property
    def module(self) -> str:
        return str(self.variant_set_db_id).split("§", 1)[0]

    @property
    def has_genotype_filters(self) -> bool:
        return any(g.has_thresholds for g in self.groups)

    def headers(self) -> dict[str, str]:
        """Extra request headers: Gigwa switches to sample-level ids on ``workWithSamples``."""
        return {"workWithSamples": "true"} if self.work_with_samples else {}

    def body(self, *, search_mode: int, page_size: int = 0, page_token: str = "",
             get_gt: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "variantSetId": ga4gh_variant_set_id(self.variant_set_db_id),
            "searchMode": search_mode,
            "getGT": get_gt,
            "pageSize": page_size,
            "pageToken": page_token,
        }
        if self.reference_name:
            body["referenceName"] = self.reference_name
        if self.start is not None:
            body["start"] = int(self.start)
        if self.end is not None:
            body["end"] = int(self.end)
        if self.selected_variant_types:
            body["selectedVariantTypes"] = self.selected_variant_types
        if self.bi_allelic_only:
            body["alleleCount"] = "2"
        body.update(group_arrays(self.module, self.groups))
        return body

    @property
    def bi_allelic_only(self) -> bool:
        """MAF is only defined on bi-allelic variants: Gigwa refuses a MAF filter on a
        project holding other variants ("MAF is only supported on biallelic data!")
        unless the query also restricts to 2 alleles -- the UI's "number of alleles"
        filter, sent as ``alleleCount``. Any MAF bound therefore implies it."""
        return any(g.has_maf for g in self.groups)

    def chart_body(self, *, sequence: str, variant_type: str | None, range_min: int | None,
                   range_max: int | None, intervals: int,
                   plot_groups: Iterable[GroupFilter] | None = None) -> dict[str, Any]:
        """Body for Gigwa's per-interval statistics endpoints (``MgdbChartRequest``).

        Mirrors the UI's ``buildChartDataPayLoad``: the variant-level filters plus the
        groups whose genotypes the statistic is computed on, **without** the genotype
        thresholds -- those were already applied when the search built the token's
        temporary variant collection, which these endpoints read.
        """
        groups = list(plot_groups) if plot_groups is not None else list(self.groups) or [GroupFilter()]
        arrays = group_arrays(self.module, groups)
        body: dict[str, Any] = {
            "variantSetId": ga4gh_variant_set_id(self.variant_set_db_id),
            "callSetIds": arrays["callSetIds"],
            "additionalCallSetIds": arrays.get("additionalCallSetIds", []),
            "groupName": arrays.get("groupName", []),
            "discriminate": arrays.get("discriminate", []),
            "annotationFieldThresholds": arrays.get("annotationFieldThresholds", []),
            "displayedSequence": sequence,
            "displayedVariantType": variant_type or None,
            "displayedRangeIntervalCount": int(intervals),
        }
        if range_min is not None:
            body["displayedRangeMin"] = int(range_min)
        if range_max is not None:
            body["displayedRangeMax"] = int(range_max)
        if self.start is not None:
            body["start"] = int(self.start)
        if self.end is not None:
            body["end"] = int(self.end)
        if self.selected_variant_types:
            body["selectedVariantTypes"] = self.selected_variant_types
        if self.bi_allelic_only:
            body["alleleCount"] = "2"
        return body

    def exported_members(self) -> list[str]:
        """Union of the groups' members, in order (empty = everyone)."""
        if any(not g.members for g in self.groups):
            return []
        return list(dict.fromkeys(m for g in self.groups for m in g.members))

    def describe(self) -> str:
        where = []
        if self.reference_name:
            region = self.reference_name
            if self.start is not None or self.end is not None:
                region += f":{self.start or ''}-{self.end or ''}"
            where.append(f"region={region}")
        if self.selected_variant_types:
            where.append(f"types={self.selected_variant_types}")
        if self.bi_allelic_only:
            where.append("bi-allelic only (implied by MAF)")
        if self.work_with_samples:
            where.append("entity=samples")
        lines = [f"{self.variant_set_db_id}" + (f" ({', '.join(where)})" if where else "")]
        lines += [f"  - {g.describe()}" for g in self.groups]
        return "\n".join(lines)
