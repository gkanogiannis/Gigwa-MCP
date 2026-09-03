"""Metadata tools: callset (sample) attribute dumps and the germplasm→callset fallback.

Uses synthetic callset/germplasm payloads shaped like the two naming conventions Gigwa
builds use in the wild — no live server. See tests/test_genotypes.py for the same split.
"""

from __future__ import annotations

import pandas as pd
import pytest

from gigwa_mcp.analysis.genotypes import _name_map
from gigwa_mcp.tools import metadata

# Variant set ids matching each fixture's ``<module>§<project>§<run>`` shape.
ICARDA_VS = "WD§1§Run1"
DISTINCT_VS = "VS§1§run"

# The ICARDA shape: sampleDbId is an opaque numeric id and callSetName carries the label.
ICARDA_CALLSETS = [
    {
        "callSetDbId": "WD§9764",
        "callSetName": "4970-1-Run1",
        "sampleDbId": "WD§9764",
        "additionalInfo": {"Country": "TUR", "ICARDA_IG": "IG96138", "SeedID": "SEEDICAR4970"},
    },
    {
        "callSetDbId": "WD§8441",
        "callSetName": "3640-1-Run1",
        "sampleDbId": "WD§8441",
        # No SeedID -> exercises the fill-missing-with-"" path.
        "additionalInfo": {"Country": "ETH", "ICARDA_IG": "IG76912"},
    },
]

# The other shape: callSetName is a meaningless index, the accession is in sampleDbId,
# and callSetDbId differs from sampleDbId -- which is what pins down the fallback's keys.
DISTINCT_CALLSETS = [
    {
        "callSetDbId": "S1",
        "callSetName": "1",
        "sampleDbId": "VS§ACC0001-1-run",
        "additionalInfo": {"Country": "SYR"},
    },
    {
        "callSetDbId": "S2",
        "callSetName": "2",
        "sampleDbId": "VS§ACC0002-1-run",
        "additionalInfo": {"Country": "MAR"},
    },
]


class FakeClient:
    """Stands in for GigwaClient; ``germplasm`` and ``callsets`` are set per test."""

    def __init__(self, germplasm=None, callsets=None, callsets_forbidden=False):
        self._germplasm = germplasm or []
        self._callsets = callsets or []
        self._callsets_forbidden = callsets_forbidden
        self.callset_calls = 0

    def get_germplasm(self, variant_set_db_id):
        return self._germplasm

    def search_callsets(self, variant_set_db_id):
        if self._callsets_forbidden:
            raise AssertionError("search_callsets must not be called on the germplasm path")
        self.callset_calls += 1
        return self._callsets


def _fn(tool):
    return getattr(tool, "fn", tool)


def _patch(monkeypatch, client):
    monkeypatch.setattr(metadata, "get_client", lambda: client)
    return client


def _read(tmp_path, name):
    return pd.read_csv(tmp_path / name, dtype=str, keep_default_na=False)


# -- _callset_metadata ------------------------------------------------------

def test_callset_metadata_unions_attributes_and_fills_missing():
    attr_cols, rows = _callset_metadata_call(ICARDA_CALLSETS, ICARDA_VS)
    assert attr_cols == ["Country", "ICARDA_IG", "SeedID"]  # sorted union
    assert rows[0]["SeedID"] == "SEEDICAR4970"
    assert rows[1]["SeedID"] == ""  # absent on that callset


def _callset_metadata_call(callsets, vs):
    return metadata._callset_metadata(callsets, vs)


def test_callset_metadata_sample_name_matches_the_analysis_naming_rule():
    """sample_name must be what GenotypeMatrix.sample_names would hold, in both shapes."""
    for callsets, vs in ((ICARDA_CALLSETS, ICARDA_VS), (DISTINCT_CALLSETS, DISTINCT_VS)):
        _, rows = _callset_metadata_call(callsets, vs)
        expected = _name_map(callsets, vs)
        assert [r["sample_name"] for r in rows] == [expected[r["callSetDbId"]] for r in rows]

    # On the ICARDA shape the resolved name is *not* the callSetName -- the distinction
    # this column exists for.
    _, rows = _callset_metadata_call(ICARDA_CALLSETS, ICARDA_VS)
    assert rows[0]["sample_name"] == "9764"
    assert rows[0]["callSetName"] == "4970-1-Run1"

    # On the other shape it is recovered from sampleDbId, not the numeric callSetName.
    _, rows = _callset_metadata_call(DISTINCT_CALLSETS, DISTINCT_VS)
    assert rows[0]["sample_name"] == "ACC0001"


def test_callset_metadata_falls_back_to_callset_name_then_id():
    """A callset the naming rule cannot resolve still gets a usable sample_name."""
    _, rows = _callset_metadata_call([{"callSetDbId": "X1", "callSetName": "labelled"}], "M§1§r")
    assert rows[0]["sample_name"] == "labelled"
    _, rows = _callset_metadata_call([{"callSetDbId": "X1"}], "M§1§r")
    assert rows[0]["sample_name"] == "X1"


# -- search_callsets --------------------------------------------------------

def test_search_callsets_writes_sample_metadata(monkeypatch, tmp_path):
    _patch(monkeypatch, FakeClient(callsets=ICARDA_CALLSETS))
    out = _fn(metadata.search_callsets)(ICARDA_VS, output_dir=str(tmp_path))

    assert "2 sample(s), 3 attribute(s)" in out
    assert "Country, ICARDA_IG, SeedID" in out
    df = _read(tmp_path, "sample_metadata.csv")
    assert list(df.columns) == [
        "sample_name", "callSetName", "sampleDbId", "callSetDbId",
        "Country", "ICARDA_IG", "SeedID",
    ]
    assert df["sample_name"].tolist() == ["9764", "8441"]
    assert df["callSetName"].tolist() == ["4970-1-Run1", "3640-1-Run1"]
    assert df["SeedID"].tolist() == ["SEEDICAR4970", ""]


def test_search_callsets_reports_when_there_are_none(monkeypatch, tmp_path):
    _patch(monkeypatch, FakeClient(callsets=[]))
    out = _fn(metadata.search_callsets)(ICARDA_VS, output_dir=str(tmp_path))
    assert "No callsets found" in out
    assert not (tmp_path / "sample_metadata.csv").exists()


def test_search_callsets_dumps_names_even_without_attributes(monkeypatch, tmp_path):
    bare = [{"callSetDbId": "S1", "callSetName": "acc1", "sampleDbId": "VS§acc1-1-run"}]
    _patch(monkeypatch, FakeClient(callsets=bare))
    out = _fn(metadata.search_callsets)(DISTINCT_VS, output_dir=str(tmp_path))
    assert "0 attribute(s)" in out
    assert "Attributes: (none)" in out
    df = _read(tmp_path, "sample_metadata.csv")
    assert df["sample_name"].tolist() == ["acc1"]


# -- get_germplasm_metadata: germplasm level --------------------------------

def test_get_germplasm_metadata_prefers_the_germplasm_level(monkeypatch, tmp_path):
    """With germplasm attributes present the callset level is never consulted."""
    records = [
        {"germplasmName": "acc1", "germplasmDbId": "G1", "additionalInfo": {"Country": "SYR"}},
        {"germplasmName": "acc2", "germplasmDbId": "G2", "additionalInfo": {"Country": "MAR"}},
    ]
    _patch(monkeypatch, FakeClient(germplasm=records, callsets_forbidden=True))
    out = _fn(metadata.get_germplasm_metadata)(DISTINCT_VS, output_dir=str(tmp_path))

    assert "2 accession(s), 1 attribute(s)" in out
    assert "fell back" not in out
    df = _read(tmp_path, "germplasm_metadata.csv")
    assert df["germplasm_name"].tolist() == ["acc1", "acc2"]
    assert df["Country"].tolist() == ["SYR", "MAR"]


# -- get_germplasm_metadata: callset fallback -------------------------------

@pytest.mark.parametrize(
    "germplasm",
    [
        pytest.param([], id="no-germplasm-records"),
        pytest.param(
            [{"germplasmName": "acc1", "germplasmDbId": "G1"}], id="germplasm-without-attributes"
        ),
    ],
)
def test_get_germplasm_metadata_falls_back_to_callsets(monkeypatch, tmp_path, germplasm):
    client = _patch(monkeypatch, FakeClient(germplasm=germplasm, callsets=ICARDA_CALLSETS))
    out = _fn(metadata.get_germplasm_metadata)(ICARDA_VS, output_dir=str(tmp_path))

    assert "fell back to callset (sample) metadata" in out
    assert "2 sample(s), 3 attribute(s)" in out
    assert client.callset_calls == 1
    df = _read(tmp_path, "germplasm_metadata.csv")
    assert list(df.columns) == ["germplasm_name", "germplasm_db_id", "Country", "ICARDA_IG", "SeedID"]


def test_fallback_keys_join_to_the_analysis_sample_names(monkeypatch, tmp_path):
    """germplasm_name == GenotypeMatrix.sample_names, germplasm_db_id == sample_ids.

    Both are what tools/diversity.py:_sample_group_map matches on (name first, then id),
    so the written file groups samples without any manual renaming.
    """
    _patch(monkeypatch, FakeClient(germplasm=[], callsets=DISTINCT_CALLSETS))
    _fn(metadata.get_germplasm_metadata)(DISTINCT_VS, output_dir=str(tmp_path))

    df = _read(tmp_path, "germplasm_metadata.csv")
    names = _name_map(DISTINCT_CALLSETS, DISTINCT_VS)
    assert df["germplasm_name"].tolist() == ["ACC0001", "ACC0002"] == list(names.values())
    # sample_ids are callSetDbIds -- explicitly not the sampleDbIds, which differ here.
    assert df["germplasm_db_id"].tolist() == ["S1", "S2"] == list(names)
    assert df["germplasm_db_id"].tolist() != [c["sampleDbId"] for c in DISTINCT_CALLSETS]


def test_get_germplasm_metadata_reports_when_neither_level_has_metadata(monkeypatch, tmp_path):
    bare = [{"callSetDbId": "S1", "callSetName": "acc1", "sampleDbId": "VS§acc1-1-run"}]
    _patch(monkeypatch, FakeClient(germplasm=[], callsets=bare))
    out = _fn(metadata.get_germplasm_metadata)(DISTINCT_VS, output_dir=str(tmp_path))

    assert "No server-stored metadata available" in out
    assert "import_metadata" in out
    assert not (tmp_path / "germplasm_metadata.csv").exists()
