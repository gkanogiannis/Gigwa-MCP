"""Translation of group-level filters into Gigwa's per-group request arrays.

The expected shapes mirror what the Gigwa web UI sends (``main.js`` ``buildSearchQuery``
and ``index.jsp`` ``buildChartDataPayLoad``), which the server indexes per group.
"""

from __future__ import annotations

import pytest

from gigwa_mcp.client import GigwaClient
from gigwa_mcp.selection import GroupFilter, VariantSelection, group_arrays


def test_two_groups_become_parallel_percentage_arrays():
    g1 = GroupFilter(name="G1", members=("ind1", "ind2"), min_maf=0.05, max_missing=0.1)
    g2 = GroupFilter(name="G2", members=("ind3",), max_het=0.2)
    arrays = group_arrays("MOD", [g1, g2])

    # Group 1 in callSetIds, the others in additionalCallSetIds, all module§id-framed.
    assert arrays["callSetIds"] == ["MOD§ind1", "MOD§ind2"]
    assert arrays["additionalCallSetIds"] == [["MOD§ind3"]]
    assert arrays["groupName"] == ["G1", "G2"]
    # Fractions go to Gigwa as percentages; unset bounds get the UI defaults.
    assert arrays["minMaf"] == [5.0, 0.0]
    assert arrays["maxMaf"] == [50.0, 50.0]
    assert arrays["minMissingData"] == [0.0, 0.0]
    assert arrays["maxMissingData"] == [10.0, 100.0]
    assert arrays["minHeZ"] == [0.0, 0.0]
    assert arrays["maxHeZ"] == [100.0, 20.0]
    # Every companion array is full length (the server indexes them per group).
    for key in ("discriminate", "gtPattern", "mostSameRatio", "annotationFieldThresholds"):
        assert len(arrays[key]) == 2
    assert arrays["discriminate"] == [None, None]


def test_group_with_no_members_means_everyone():
    arrays = group_arrays("MOD", [GroupFilter(min_maf=0.1)])
    assert arrays["callSetIds"] == []
    assert arrays["additionalCallSetIds"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_maf": 0.6},          # MAF cannot exceed 0.5
        {"max_missing": 1.5},      # fractions, not percentages
        {"min_het": -0.1},
        {"min_maf": 0.3, "max_maf": 0.1},
        {"max_missing": "ten"},
    ],
)
def test_invalid_thresholds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        GroupFilter(**kwargs)


def test_search_body_carries_region_types_and_groups():
    selection = VariantSelection(
        variant_set_db_id="MOD§3§run1",
        reference_name="chr1",
        start=10,
        end=500,
        selected_variant_types="SNP",
        groups=(GroupFilter(name="A", members=("x",), min_maf=0.05),),
    )
    body = selection.body(search_mode=3, page_size=100, page_token="0")
    assert body["variantSetId"] == "MOD§3"  # GA4GH id: run dropped
    assert body["referenceName"] == "chr1" and body["start"] == 10 and body["end"] == 500
    assert body["selectedVariantTypes"] == "SNP"
    assert body["callSetIds"] == ["MOD§x"]
    assert body["minMaf"] == [5.0]
    assert body["searchMode"] == 3


def test_samples_mode_sets_the_workwithsamples_header():
    selection = VariantSelection(variant_set_db_id="MOD§1§r", work_with_samples=True)
    assert selection.headers() == {"workWithSamples": "true"}
    assert VariantSelection(variant_set_db_id="MOD§1§r").headers() == {}


def test_chart_body_sends_groups_but_not_thresholds():
    selection = VariantSelection(
        variant_set_db_id="MOD§1§r",
        groups=(
            GroupFilter(name="A", members=("a1",), min_maf=0.05),
            GroupFilter(name="B", members=("b1", "b2"), max_missing=0.1),
        ),
    )
    body = selection.chart_body(sequence="chr2", variant_type=None, range_min=None,
                                range_max=None, intervals=200)
    assert body["displayedSequence"] == "chr2"
    assert body["displayedRangeIntervalCount"] == 200
    assert body["callSetIds"] == ["MOD§a1"]
    assert body["additionalCallSetIds"] == [["MOD§b1", "MOD§b2"]]
    # Thresholds were applied when the search built the temporary collection.
    assert "minMaf" not in body and "maxMissingData" not in body
    assert "displayedRangeMin" not in body  # let Gigwa find the default range


def test_exported_members_is_the_union_or_everyone():
    both = VariantSelection(
        variant_set_db_id="M§1§r",
        groups=(GroupFilter(members=("a", "b")), GroupFilter(members=("b", "c"))),
    )
    assert both.exported_members() == ["a", "b", "c"]
    with_all = VariantSelection(
        variant_set_db_id="M§1§r", groups=(GroupFilter(members=("a",)), GroupFilter()),
    )
    assert with_all.exported_members() == []


def test_flat_thresholds_and_groups_cannot_be_mixed():
    with pytest.raises(ValueError):
        GigwaClient.selection_from_filters(
            "M§1§r", min_maf=0.05, groups=[GroupFilter(members=("a",))]
        )


def test_flat_thresholds_make_one_whole_population_group():
    selection = GigwaClient.selection_from_filters("M§1§r", min_het=0.1)
    assert len(selection.groups) == 1
    assert selection.groups[0].members == ()
    assert selection.body(search_mode=0)["minHeZ"] == [10.0]


def test_maf_bound_implies_bi_allelic_only():
    maf = VariantSelection(variant_set_db_id="M§1§r", groups=(GroupFilter(min_maf=0.05),))
    assert maf.body(search_mode=0)["alleleCount"] == "2"
    assert maf.chart_body(sequence="c", variant_type=None, range_min=None, range_max=None,
                          intervals=50)["alleleCount"] == "2"
    other = VariantSelection(variant_set_db_id="M§1§r", groups=(GroupFilter(max_missing=0.1),))
    assert "alleleCount" not in other.body(search_mode=0)


def test_discriminate_with_names_the_other_group_one_based():
    a = GroupFilter(name="XI", members=("x1",), max_missing=0.05, discriminate_with="GJ")
    b = GroupFilter(name="GJ", members=("g1",), max_missing=0.05, discriminate_with="XI")
    arrays = group_arrays("M", [a, b])
    assert arrays["discriminate"] == [2, 1]  # the UI's "Discriminate with" group numbers
    one_way = group_arrays("M", [a, GroupFilter(name="GJ", members=("g1",))])
    assert one_way["discriminate"] == [2, None]
    assert GroupFilter(name="A", discriminate_with="B").has_thresholds


@pytest.mark.parametrize("target", ["nope", "XI"])
def test_discriminate_with_must_name_another_group_of_the_query(target):
    with pytest.raises(ValueError):
        group_arrays("M", [GroupFilter(name="XI", discriminate_with=target), GroupFilter(name="GJ")])
