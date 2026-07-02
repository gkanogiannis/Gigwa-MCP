"""Unit tests for the server-side search / sequences / export / process-control client
methods, using a mocked httpx transport (mirrors tests/test_client.py)."""

from __future__ import annotations

import httpx

from gigwa_mcp.client import SEARCH_MODE_COUNT, SEARCH_MODE_FETCH, GigwaClient
from gigwa_mcp.config import GigwaConfig


def make_client(handler) -> GigwaClient:
    cfg = GigwaConfig(base_url="http://test/gigwa", username="u", password="p")
    client = GigwaClient(cfg)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _token_or(handler):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/generateToken"):
            return httpx.Response(201, json={"token": "t"})
        return handler(request)

    return wrapped


def test_count_variants_sends_count_mode_and_filters():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"count": 42})

    client = make_client(_token_or(handler))
    n = client.count_variants(
        "MOD§1§run1", reference_name="chr2", start=1, end=5000, min_maf=0.1, max_missing_data=0.2
    )
    assert n == 42
    import json

    body = json.loads(captured["body"])
    # variantSetId is normalised to GA4GH's module§project form (the §run is dropped).
    assert body["variantSetId"] == "MOD§1"
    assert body["callSetIds"] == []  # empty = all individuals; must be present, not omitted
    assert body["searchMode"] == SEARCH_MODE_COUNT
    assert body["referenceName"] == "chr2"
    assert body["start"] == 1 and body["end"] == 5000
    # Genotype-stat filters are single-group percentage arrays (0-1 fractions x100), with
    # the companion arrays Gigwa requires and discriminate=[None] (no self-discrimination).
    assert body["minMaf"] == [10.0]
    assert body["maxMissingData"] == [20.0]
    assert body["maxMaf"] == [50.0]  # default upper bound when only min_maf is given
    assert body["discriminate"] == [None]
    assert body["getGT"] is False


def test_search_variants_pages_until_exhausted():
    # count primes the match set, then FETCH pages return variants + nextPageToken.
    pages = [
        {"variants": [{"variantDbId": "v1", "referenceName": "chr1", "start": 10,
                       "referenceBases": "A", "alternateBases": ["T"]}],
         "nextPageToken": "1"},
        {"variants": [{"variantDbId": "v2", "referenceName": "chr1", "start": 20,
                       "referenceBases": "C", "alternateBases": ["G"]}],
         "nextPageToken": ""},
    ]
    modes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.read().decode())
        modes.append(body["searchMode"])
        if body["searchMode"] == SEARCH_MODE_COUNT:
            return httpx.Response(200, json={"count": 2})
        return httpx.Response(200, json=pages.pop(0))

    client = make_client(_token_or(handler))
    variants = client.search_variants("MOD§1§run1", page_size=1)
    assert [v["variantDbId"] for v in variants] == ["v1", "v2"]
    assert modes[0] == SEARCH_MODE_COUNT
    assert modes[1:] == [SEARCH_MODE_FETCH, SEARCH_MODE_FETCH]


def test_search_variants_empty_when_count_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0})

    client = make_client(_token_or(handler))
    assert client.search_variants("MOD§1§run1") == []


def test_list_sequences_parses_references():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/ga4gh/references/search")
        return httpx.Response(200, json={"references": [
            {"name": "chr1", "length": 1000},
            {"name": "chr2", "length": 2000},
        ]})

    client = make_client(_token_or(handler))
    refs = client.list_sequences("MOD§1§run1")
    assert [r["name"] for r in refs] == ["chr1", "chr2"]


def test_export_data_nonvcf_streams_after_202(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/export/plink" in request.url.path.lower()
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(202, text="preparing")
        return httpx.Response(200, content=b"X" * 128)

    client = make_client(_token_or(handler))
    dest = tmp_path / "out.zip"
    written = client.export_data("MOD§1§run1", dest, fmt="PLINK", poll_interval=0)
    assert written.read_bytes() == b"X" * 128


def test_abort_calls_abort_process():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    client = make_client(_token_or(handler))
    assert client.abort("import::u::xyz") is True
    assert seen["method"] == "DELETE"  # GET/POST return HTTP 500 on the live server
    assert seen["path"].endswith("/gigwa/abortProcess")
    assert seen["params"]["processID"] == "import::u::xyz"


def test_get_germplasm_falls_back_to_get_listing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/search/germplasm"):
            return httpx.Response(404, text="not supported")
        if request.method == "GET" and request.url.path.endswith("/brapi/v2/germplasm"):
            return httpx.Response(200, json={"result": {"data": [
                {"germplasmName": "acc1", "additionalInfo": {"country": "PE"}},
            ]}})
        return httpx.Response(200, json={})

    client = make_client(_token_or(handler))
    recs = client.get_germplasm("MOD§1§run1")
    assert recs and recs[0]["germplasmName"] == "acc1"
