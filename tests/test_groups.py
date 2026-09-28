"""Group / subset / server-side statistics tools against a simulated Gigwa.

``FakeGigwa`` reproduces the server behaviour these tools orchestrate: tokens from
``generateToken``, the project's individuals/samples from GA4GH ``callsets/search``,
and -- the key point -- one temporary variant collection **per token**, filled by a
searchMode 3 search and read back by searchMode 2 browsing, exports and the
per-interval statistics endpoints.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest

import gigwa_mcp.server as server
from gigwa_mcp import subsets
from gigwa_mcp.client import GigwaClient
from gigwa_mcp.config import GigwaConfig
from gigwa_mcp.tools import groups as group_tools
from gigwa_mcp.tools import search as search_tools

VS = "MOD§1§run1"
INDIVIDUALS = ["ind1", "ind2", "ind3", "ind4"]
SAMPLES = ["11", "12", "13"]


class FakeGigwa:
    def __init__(self, n_variants: int = 5):
        self.requests: list[tuple[str, dict, dict]] = []  # (path, headers, body)
        self.temp: dict[str, list[dict]] = {}  # token -> temporary collection
        self.minted = 0
        self.reject_tokens: set[str] = set()
        self.chart_error: str | None = None
        self.n_variants = n_variants
        # Search progress simulation: while `gate` is set-able, searches block on it and
        # /progress reports `search_status`; `release_after` progress polls open the gate.
        self.gate: threading.Event | None = None
        self.release_after: int | None = None
        self.search_status = {"progressDescription": "Finding matching variants...",
                              "currentStepProgress": 40, "complete": False}
        self.search_polls: list[tuple[str | None, str]] = []  # (progressToken, auth token)
        self.fail_search = False
        self.chart_progress_ids: list[str | None] = []

    def variants(self, n: int) -> list[dict]:
        return [
            {"variantDbId": f"MOD§1§v{i}", "referenceName": "chr1", "start": 100 * i,
             "referenceBases": "A", "alternateBases": ["T"]}
            for i in range(n)
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/gigwa/rest", "")
        raw = request.read()
        body = json.loads(raw) if raw else {}
        headers = dict(request.headers)
        self.requests.append((path, headers, body))
        token = (headers.get("authorization") or "").removeprefix("Bearer ")
        if token in self.reject_tokens:
            return httpx.Response(401)

        if path == "/gigwa/generateToken":
            self.minted += 1
            return httpx.Response(201, json={"token": f"tok{self.minted}"})
        if path == "/ga4gh/callsets/search":
            names = SAMPLES if headers.get("workwithsamples") == "true" else INDIVIDUALS
            return httpx.Response(200, json={"callSets": [{"id": f"MOD§{n}", "name": n} for n in names]})
        if path == "/gigwa/filterIndividualsFromMetadata/MOD":
            return httpx.Response(200, json=[{"id": "ind2"}, {"id": "ind3"}, {"id": "other"}])
        if path == "/gigwa/filterSamplesFromMetadata/MOD":
            return httpx.Response(200, json=[{"id": "11"}, {"id": "13"}])
        if path == "/ga4gh/variants/search":
            if self.gate is not None and body["searchMode"] in (0, 3):
                assert self.gate.wait(5), "search never released"
            if self.fail_search:
                return httpx.Response(500, text="boom")
            mode = body["searchMode"]
            if mode == 3:
                self.temp[token] = self.variants(self.n_variants)
                return httpx.Response(200, json={"count": self.n_variants, "variants": self.temp[token][:1]})
            if mode == 2:
                rows = self.temp.get(token, [])
                size, page = body["pageSize"], int(body["pageToken"])
                return httpx.Response(200, json={"count": self.n_variants,
                                                 "variants": rows[page * size:(page + 1) * size]})
            return httpx.Response(200, json={"count": self.n_variants})
        if path in ("/gigwa/mafData", "/gigwa/fstData"):
            self.chart_progress_ids.append(request.url.params.get("progressToken"))
            if self.gate is not None:
                assert self.gate.wait(5), "chart never released"
            if self.chart_error or not self.temp.get(token):
                self.chart_error = self.chart_error or "Unable to find temporary records: please SEARCH again!"
                return httpx.Response(200, content=b"")
            # Jackson writes Float.NaN as a bare NaN token for windows without variants.
            return httpx.Response(200, content=b'{"100":12.5,"200":NaN,"300":30.0}',
                                  headers={"content-type": "application/json"})
        if path == "/gigwa/tajimaDData":
            return httpx.Response(200, json=[{"100": -1.2, "200": 0.4}, {"100": 3, "200": 5}])
        if path == "/gigwa/progress":
            if self.gate is not None and not self.gate.is_set():
                self.search_polls.append((request.url.params.get("progressToken"), token))
                if self.release_after is not None and len(self.search_polls) >= self.release_after:
                    self.gate.set()
                return httpx.Response(200, json=self.search_status)
            if self.chart_error:
                msg, self.chart_error = self.chart_error, None
                return httpx.Response(200, json={"error": msg})
            return httpx.Response(204)
        if path == "/gigwa/exportData":
            return httpx.Response(200, content=b"PK\x03\x04zipbytes",
                                  headers={"content-type": "application/zip"})
        return httpx.Response(404, text=f"unexpected {path}")

    def calls(self, path: str) -> list[tuple[dict, dict]]:
        return [(h, b) for p, h, b in self.requests if p == path]


@pytest.fixture()
def gigwa(monkeypatch):
    fake = FakeGigwa()
    client = GigwaClient(GigwaConfig(base_url="http://test/gigwa", username="u", password="p"))
    client._http = httpx.Client(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(server, "_client", client)
    subsets.clear()
    yield fake
    subsets.clear()


def test_define_group_validates_members_against_the_project(gigwa):
    out = group_tools.define_group(VS, "G1", members=["ind1", "ind2", "ghost"])
    assert "2 individuals" in out and "ghost" in out
    group = subsets.get_group(server.get_client(), "G1")
    assert group.members == ("ind1", "ind2")
    (headers, body), = gigwa.calls("/ga4gh/callsets/search")
    assert body["variantSetId"] == "MOD§1"
    assert "workwithsamples" not in headers


def test_define_group_rejects_when_nothing_matches(gigwa):
    with pytest.raises(ValueError, match="None of the 1"):
        group_tools.define_group(VS, "G1", members=["ghost"])


def test_define_group_needs_exactly_one_source(gigwa):
    with pytest.raises(ValueError):
        group_tools.define_group(VS, "G1")
    with pytest.raises(ValueError):
        group_tools.define_group(VS, "G1", members=["ind1"], metadata_filters_json={"a": ["b"]})


def test_define_group_from_metadata_keeps_only_project_members(gigwa):
    out = group_tools.define_group(VS, "cA", metadata_filters_json='{"GroupK4": ["cA"]}')
    assert subsets.get_group(server.get_client(), "cA").members == ("ind2", "ind3")
    assert "other" in out  # reported as not in this project
    (_, body), = gigwa.calls("/gigwa/filterIndividualsFromMetadata/MOD")
    assert body == {"GroupK4": ["cA"]}


def test_sample_groups_use_sample_endpoints_and_header(gigwa):
    group_tools.define_group(
        VS, "S", metadata_filters_json={"ind.Country": ["FR"], "tissue": ["leaf"]}, entity="samples"
    )
    (_, body), = gigwa.calls("/gigwa/filterSamplesFromMetadata/MOD")
    assert body == {"individual": {"Country": ["FR"]}, "sample": {"tissue": ["leaf"]}}
    (headers, _), = gigwa.calls("/ga4gh/callsets/search")
    assert headers["workwithsamples"] == "true"

    group_tools.define_variant_subset.fn(VS, name="S1", group_filters_json=[{"group": "S", "min_maf": 0.05}])
    headers, body = gigwa.calls("/ga4gh/variants/search")[-1]
    assert headers["workwithsamples"] == "true"
    assert body["callSetIds"] == ["MOD§11", "MOD§13"]


def test_subset_runs_search_under_its_own_token(gigwa):
    group_tools.define_group(VS, "G1", members=["ind1", "ind2"])
    group_tools.define_group(VS, "G2", members=["ind3"])
    out = group_tools.define_variant_subset.fn(
        VS, name="S1", region="chr1:1-1000",
        group_filters_json=json.dumps([
            {"group": "G1", "min_maf": 0.05, "max_missing": 0.1, "max_het": 0.2},
            {"group": "G2", "max_missing": 0.5},
        ]),
    )
    assert "5 variant(s)" in out
    headers, body = gigwa.calls("/ga4gh/variants/search")[-1]
    assert body["searchMode"] == 3
    assert body["callSetIds"] == ["MOD§ind1", "MOD§ind2"]
    assert body["additionalCallSetIds"] == [["MOD§ind3"]]
    assert body["minMaf"] == [5.0, 0.0] and body["maxMissingData"] == [10.0, 50.0]
    assert body["maxHeZ"] == [20.0, 100.0]
    assert body["referenceName"] == "chr1" and body["start"] == 1 and body["end"] == 1000
    subset = subsets.get_subset(server.get_client(), "S1")
    assert headers["authorization"] == f"Bearer {subset.token}"


def test_two_subsets_keep_separate_temporary_collections(gigwa):
    group_tools.define_variant_subset.fn(VS, name="A", min_maf=0.05)
    group_tools.define_variant_subset.fn(VS, name="B", max_missing_data=0.1)
    client = server.get_client()
    a, b = subsets.get_subset(client, "A"), subsets.get_subset(client, "B")
    assert a.token != b.token
    assert a.token in gigwa.temp and b.token in gigwa.temp


def test_search_variants_pages_the_subset_with_browse_mode(gigwa, tmp_path):
    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    token = subsets.get_subset(server.get_client(), "S1").token
    n_before = len(gigwa.calls("/ga4gh/variants/search"))

    out = search_tools.search_variants.fn(VS, subset="S1", max_variants=2, output_dir=str(tmp_path))
    assert "Found 2 variant(s)" in out and "page=1" in out
    browse = gigwa.calls("/ga4gh/variants/search")[n_before:]
    assert [b["searchMode"] for _, b in browse] == [2]  # no new filtering
    assert browse[0][0]["authorization"] == f"Bearer {token}"

    out = search_tools.search_variants.fn(VS, subset="S1", page=2, max_variants=2, output_dir=str(tmp_path))
    assert "Found 1 variant(s)" in out and "More variants remain" not in out


def test_expired_subset_token_is_rebuilt_transparently(gigwa, tmp_path):
    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    old = subsets.get_subset(server.get_client(), "S1").token
    gigwa.reject_tokens.add(old)

    out = search_tools.search_variants.fn(VS, subset="S1", output_dir=str(tmp_path))
    assert "Found 5 variant(s)" in out
    new = subsets.get_subset(server.get_client(), "S1").token
    assert new != old and new in gigwa.temp


def test_group_filters_without_subset_register_one(gigwa, tmp_path):
    group_tools.define_group(VS, "G1", members=["ind1"])
    out = search_tools.search_variants.fn(
        VS, group_filters_json=[{"group": "G1", "min_maf": 0.1}], output_dir=str(tmp_path)
    )
    assert "subset 'S1'" in out
    assert subsets.get_subset(server.get_client(), "S1").count == 5


def test_count_variants_with_group_filters_uses_count_mode(gigwa):
    group_tools.define_group(VS, "G1", members=["ind1"])
    out = search_tools.count_variants.fn(VS, group_filters_json=[{"group": "G1", "max_het": 0.1}])
    assert out.startswith("5 variant(s)")
    _, body = gigwa.calls("/ga4gh/variants/search")[-1]
    assert body["searchMode"] == 0 and body["maxHeZ"] == [10.0]


def test_unknown_group_or_subset_is_a_clear_error(gigwa):
    from gigwa_mcp.errors import GigwaError

    with pytest.raises(GigwaError, match="Unknown group 'nope'"):
        search_tools.count_variants.fn(VS, group_filters_json=[{"group": "nope"}])
    with pytest.raises(GigwaError, match="Unknown subset 'nope'"):
        search_tools.count_variants.fn(VS, subset="nope")


def test_selection_profile_calls_gigwa_with_the_subset_token(gigwa, tmp_path):
    group_tools.define_group(VS, "G1", members=["ind1", "ind2"])
    group_tools.define_variant_subset.fn(VS, name="S1", group_filters_json=[{"group": "G1", "min_maf": 0.05}])
    token = subsets.get_subset(server.get_client(), "S1").token

    out = group_tools.selection_profile.fn("S1", "maf", "chr1", intervals=10, output_dir=str(tmp_path))
    headers, body = gigwa.calls("/gigwa/mafData")[-1]
    assert headers["authorization"] == f"Bearer {token}"
    assert body["displayedSequence"] == "chr1"
    assert body["displayedRangeIntervalCount"] == 50  # clamped to Gigwa's 50-5000
    assert body["callSetIds"] == ["MOD§ind1", "MOD§ind2"]
    assert "2/3 windows" in out and "max=30" in out
    assert (tmp_path / "profile_maf_chr1.csv").exists()


def test_selection_profile_rebuilds_when_temporary_records_are_gone(gigwa, tmp_path):
    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    old = subsets.get_subset(server.get_client(), "S1").token
    gigwa.temp.pop(old)  # purged server-side while the token stays valid

    out = group_tools.selection_profile.fn("S1", "maf", "chr1", output_dir=str(tmp_path))
    assert "windows with a value" in out
    assert subsets.get_subset(server.get_client(), "S1").token != old


def test_fst_needs_two_groups_and_tajima_returns_two_series(gigwa, tmp_path):
    group_tools.define_group(VS, "G1", members=["ind1"])
    group_tools.define_group(VS, "G2", members=["ind2"])
    group_tools.define_variant_subset.fn(VS, name="one", group_filters_json=[{"group": "G1"}])
    with pytest.raises(ValueError, match="at least two groups"):
        group_tools.selection_profile.fn("one", "fst", "chr1")

    group_tools.define_variant_subset.fn(
        VS, name="two", group_filters_json=[{"group": "G1"}, {"group": "G2"}]
    )
    group_tools.selection_profile.fn("two", "fst", "chr1", output_dir=str(tmp_path))
    _, body = gigwa.calls("/gigwa/fstData")[-1]
    assert body["callSetIds"] == ["MOD§ind1"] and body["additionalCallSetIds"] == [["MOD§ind2"]]

    group_tools.selection_profile.fn("two", "tajima_d", "chr1", output_dir=str(tmp_path))
    csv = (tmp_path / "profile_tajima_d_chr1.csv").read_text()
    assert "tajima_d" in csv and "segregating_sites" in csv


def test_export_of_a_subset_reuses_its_token_and_exports_group_members(gigwa, tmp_path):
    group_tools.define_group(VS, "G1", members=["ind1", "ind2"])
    group_tools.define_variant_subset.fn(VS, name="S1", group_filters_json=[{"group": "G1", "min_maf": 0.05}])
    token = subsets.get_subset(server.get_client(), "S1").token
    minted_before = gigwa.minted

    out = search_tools.export_genotypes.fn(VS, str(tmp_path / "s1.zip"), subset="S1")
    assert "Exported" in out
    headers, body = gigwa.calls("/gigwa/exportData")[-1]
    assert headers["authorization"] == f"Bearer {token}"
    assert gigwa.minted == minted_before  # no dedicated export token minted
    assert body["exportedIndividuals"] == ["ind1", "ind2"]
    assert body["groupName"] == ["G1"] and body["minMaf"] == [5.0]


def test_export_subset_refuses_extra_filters(gigwa, tmp_path):
    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    with pytest.raises(ValueError, match="already carries its filters"):
        search_tools.export_genotypes.fn(VS, str(tmp_path / "x.vcf"), subset="S1", region="chr1")


def test_anonymous_subset_token_is_requested_without_a_body(monkeypatch):
    fake = FakeGigwa()
    client = GigwaClient(GigwaConfig(base_url="http://test/gigwa"))
    client._http = httpx.Client(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(server, "_client", client)
    subsets.clear()
    try:
        group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
        (headers, body), = fake.calls("/gigwa/generateToken")
        assert body == {}  # no credentials body: Gigwa returns an anonymous-user token
        assert subsets.get_subset(client, "S1").token == "tok1"
    finally:
        subsets.clear()


def test_subsets_are_refused_after_switching_server(gigwa, monkeypatch):
    from gigwa_mcp.errors import GigwaError

    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    other = GigwaClient(GigwaConfig(base_url="http://elsewhere/gigwa", username="u", password="p"))
    monkeypatch.setattr(server, "_client", other)
    with pytest.raises(GigwaError, match="not on the currently connected server"):
        search_tools.count_variants.fn(VS, subset="S1")


def test_analysis_tools_load_a_subset_through_gigwa_export(gigwa, tmp_path, monkeypatch):
    from gigwa_mcp.analysis import genotypes

    genotypes.clear_cache()
    parsed = []
    monkeypatch.setattr(genotypes, "_ensure_plain_vcf", lambda path: path)
    monkeypatch.setattr(genotypes, "_parse_vcf", lambda client, vs, path: parsed.append(path) or "GM")

    group_tools.define_group(VS, "G1", members=["ind1", "ind3"])
    group_tools.define_variant_subset.fn(VS, name="S1", group_filters_json=[{"group": "G1", "max_missing": 0.1}])
    token = subsets.get_subset(server.get_client(), "S1").token

    gm = genotypes.load_genotypes(server.get_client(), VS, subset="S1", cache_dir=tmp_path)
    assert gm == "GM"
    headers, body = gigwa.calls("/gigwa/exportData")[-1]
    assert headers["authorization"] == f"Bearer {token}"
    assert body["exportFormat"] == "VCF"
    assert body["exportedIndividuals"] == ["ind1", "ind3"]
    assert body["maxMissingData"] == [10.0]

    # Same subset again: served from the session cache, no second export.
    genotypes.load_genotypes(server.get_client(), VS, subset="S1", cache_dir=tmp_path)
    assert len(gigwa.calls("/gigwa/exportData")) == 1

    with pytest.raises(ValueError, match="allelematrix"):
        genotypes.load_genotypes(server.get_client(), VS, subset="S1", method="allelematrix")
    with pytest.raises(ValueError, match="region"):
        genotypes.load_genotypes(server.get_client(), VS, subset="S1", region="chr1")
    genotypes.clear_cache()


def test_qc_and_diversity_tools_accept_subset():
    import inspect

    from gigwa_mcp.tools import diversity, qc

    for tool in (qc.qc_call_rate, qc.qc_heterozygosity, qc.qc_duplicate_accessions, qc.qc_maf_filter,
                 diversity.diversity_summary, diversity.diversity_pca, diversity.diversity_kinship,
                 diversity.diversity_fst, diversity.diversity_by_group, diversity.diversity_core_collection,
                 diversity.diversity_structure, diversity.diversity_admixture, diversity.diversity_tree):
        assert "subset" in inspect.signature(tool.fn).parameters, tool.fn.__name__


def test_unstable_page_order_is_reread_then_reported(gigwa, tmp_path, monkeypatch):
    monkeypatch.setattr(subsets.time, "sleep", lambda s: None)
    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    client = server.get_client()
    real = client.browse_selection
    calls = {"n": 0}

    def flaky(selection, *, token, page, page_size):
        calls["n"] += 1
        variants, nxt, count = real(selection, token=token, page=page, page_size=page_size)
        if calls["n"] == 2:  # second page of the first walk repeats the first page
            variants, nxt, count = real(selection, token=token, page=0, page_size=page_size)
        return variants, nxt, count

    monkeypatch.setattr(client, "browse_selection", flaky)
    out = search_tools.search_variants.fn(VS, subset="S1", max_variants=2, page=0, output_dir=str(tmp_path))
    # max_variants=2 -> page size 2: first walk sees duplicates, second walk is clean.
    assert "Found 2 variant(s)" in out and "⚠" not in out

    # Persistently unstable (every page repeats page 0): duplicates dropped, then reported.
    monkeypatch.setattr(client, "browse_selection",
                        lambda selection, *, token, page, page_size: real(selection, token=token, page=0, page_size=page_size))
    sub = subsets.get_subset(client, "S1")
    variants, next_page, warning = subsets.browse_subset(client, sub, page_size=2, max_variants=10)
    assert [v["variantDbId"] for v in variants] == ["MOD§1§v0", "MOD§1§v1"]
    assert warning and "more than once" in warning


@pytest.fixture()
def notes(monkeypatch):
    """Capture the MCP progress notifications the client emits."""
    import gigwa_mcp.client as client_module

    seen: list[tuple[str, float | None, float | None]] = []
    monkeypatch.setattr(client_module, "notify", lambda msg, p=None, t=None: seen.append((msg, p, t)))
    server.get_client().search_poll_interval = 0.01
    return seen


def test_subset_search_relays_gigwa_progress_while_filtering(gigwa, notes):
    gigwa.gate, gigwa.release_after = threading.Event(), 3
    out = group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    assert "5 variant(s)" in out
    token = subsets.get_subset(server.get_client(), "S1").token
    # Polled like an export's progress: GET /gigwa/progress?progressToken=<search token>.
    assert gigwa.search_polls and all(p == (token, token) for p in gigwa.search_polls)
    assert ("Finding matching variants... | 40%", 40, 100) in notes


def test_genotype_filtered_count_relays_progress(gigwa, notes):
    gigwa.gate, gigwa.release_after = threading.Event(), 2
    gigwa.search_status = {"progressDescription": "Counting matching variants...",
                           "currentStepProgress": 75}
    assert search_tools.count_variants.fn(VS, min_het=0.1).startswith("5 variant(s)")
    assert ("Counting matching variants... | 75%", 75, 100) in notes


def test_plain_region_count_is_not_tracked(gigwa, notes):
    gigwa.gate = threading.Event()
    gigwa.gate.set()  # no genotype filter: nothing to follow
    search_tools.count_variants.fn(VS, reference_name="chr1")
    assert not gigwa.search_polls and not notes


def test_subset_without_waiting_then_progress_then_use(gigwa, notes, tmp_path):
    gigwa.gate = threading.Event()  # search stays blocked until released
    out = group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05, wait=False)
    assert "not waiting" in out and 'get_subset_progress(subset="S1")' in out

    status = group_tools.get_subset_progress("S1")
    assert "still being computed" in status and "Finding matching variants... | 40%" in status
    assert "still being computed" in group_tools.list_groups()

    gigwa.release_after = len(gigwa.search_polls) + 2  # let the next use's polls release it
    out = search_tools.count_variants.fn(VS, subset="S1")  # waits, relaying progress
    assert out.startswith("5 variant(s) in subset 'S1'")
    assert any(msg.startswith("Finding matching variants") for msg, _, _ in notes)
    assert group_tools.get_subset_progress("S1") == "Subset 'S1' is ready: 5 variant(s)."


def test_background_subset_failure_is_reported(gigwa):
    from gigwa_mcp.errors import GigwaError

    gigwa.fail_search = True
    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05, wait=False)
    subsets.get_subset(server.get_client(), "S1", wait=False).job.join(5)
    assert "failed" in group_tools.get_subset_progress("S1")
    with pytest.raises(GigwaError, match="could not be built"):
        search_tools.count_variants.fn(VS, subset="S1")


def test_discrimination_between_two_groups_reaches_gigwa(gigwa):
    group_tools.define_group(VS, "XI", members=["ind1", "ind2"])
    group_tools.define_group(VS, "GJ", members=["ind3", "ind4"])
    group_tools.define_variant_subset.fn(VS, name="D", region="chr10", group_filters_json=[
        {"group": "XI", "max_missing": 0.05, "discriminate_with": "GJ"},
        {"group": "GJ", "max_missing": 0.05, "discriminate_with": "XI"},
    ])
    _, body = gigwa.calls("/ga4gh/variants/search")[-1]
    assert body["discriminate"] == [2, 1] and body["maxMissingData"] == [5.0, 5.0]
    assert body["referenceName"] == "chr10"
    with pytest.raises(ValueError, match="not one of this query's groups"):
        search_tools.count_variants.fn(VS, group_filters_json=[{"group": "XI", "discriminate_with": "zz"}])


def test_selection_profile_relays_gigwa_progress_under_its_own_id(gigwa, notes, tmp_path):
    group_tools.define_variant_subset.fn(VS, name="S1", min_maf=0.05)
    token = subsets.get_subset(server.get_client(), "S1").token
    gigwa.gate, gigwa.release_after = threading.Event(), 2
    gigwa.search_status = {"progressDescription": "Calculating MAF on sequence chr1",
                           "currentStepProgress": 60}

    group_tools.selection_profile.fn("S1", "maf", "chr1", output_dir=str(tmp_path))
    progress_id = gigwa.chart_progress_ids[-1]
    assert progress_id and progress_id.startswith("chart_")
    # Polled under the statistic's own id, authenticated with the subset token (which
    # owns the temporary collection the statistic reads).
    assert gigwa.search_polls and all(p == (progress_id, token) for p in gigwa.search_polls)
    assert ("Calculating MAF on sequence chr1 | 60%", 60, 100) in notes
