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


def test_get_export_formats_parses_registry():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/gigwa/exportFormats")
        return httpx.Response(200, json={
            "VCF": {"desc": "Variant Call Format", "supportedPloidyLevels": "",
                     "supportedVariantTypes": "", "dataFileExtensions": "vcf"},
            "EIGENSTRAT": {"desc": "SNP-only <a href='x'>alignment</a> format",
                            "supportedPloidyLevels": "2", "supportedVariantTypes": "SNP",
                            "dataFileExtensions": "geno;snp;ind"},
        })

    client = make_client(_token_or(handler))
    formats = client.get_export_formats()
    assert set(formats) == {"VCF", "EIGENSTRAT"}
    assert formats["EIGENSTRAT"]["supportedPloidyLevels"] == "2"
    assert formats["EIGENSTRAT"]["supportedVariantTypes"] == "SNP"


def test_export_selection_posts_filters_polls_and_downloads(tmp_path):
    calls = {"progress": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/gigwa/exportData"):
            import json

            body = json.loads(request.read().decode())
            assert body["variantSetId"] == "MOD§1"
            assert body["referenceName"] == "chr1"
            assert body["start"] == 100 and body["end"] == 200
            assert body["selectedVariantTypes"] == "SNP"
            assert body["exportFormat"] == "VCF.gz"
            assert body["keepExportOnServer"] is False
            assert body["exportedIndividuals"] == ["acc1", "acc2"]
            assert body["metadataFields"] == ["Country"]
            assert body["minMaf"] == [5.0]  # 0.05 fraction -> percentage
            return httpx.Response(200, text="/gigwaV2/tmpOutput/u/abc/out.vcf.gz")
        if path.endswith("/gigwa/progress"):
            assert "progressToken" not in request.url.params
            assert request.headers["authorization"] == "Bearer export_t"
            calls["progress"] += 1
            if calls["progress"] < 2:
                return httpx.Response(200, json={"complete": False, "progressDescription": "working"})
            return httpx.Response(200, json={"complete": True})
        if path.endswith("/tmpOutput/u/abc/out.vcf.gz"):
            assert request.headers["authorization"] == "Bearer t"
            return httpx.Response(200, content=b"VCFGZDATA")
        raise AssertionError(f"unexpected path: {path}")

    client = make_client(_token_or(handler))
    dest = tmp_path / "out.vcf.gz"
    written = client.export_selection(
        "MOD§1§run1", dest,
        fmt="VCF.gz",
        reference_name="chr1", start=100, end=200,
        selected_variant_types="SNP",
        min_maf=0.05,
        exported_individuals=["acc1", "acc2"],
        metadata_fields=["Country"],
        poll_interval=0,
    )
    assert written == dest
    assert dest.read_bytes() == b"VCFGZDATA"
    assert calls["progress"] == 2


def test_export_selection_raises_on_server_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gigwa/exportData"):
            return httpx.Response(200, text="/gigwaV2/tmpOutput/u/abc/out.vcf")
        if request.url.path.endswith("/gigwa/progress"):
            return httpx.Response(200, json={"complete": False, "error": "boom"})
        raise AssertionError("download should not be reached")

    from gigwa_mcp.errors import GigwaExportError

    client = make_client(_token_or(handler))
    try:
        client.export_selection("MOD§1§run1", "/tmp/whatever.vcf", poll_interval=0)
        raise AssertionError("expected GigwaExportError")
    except GigwaExportError as exc:
        assert "boom" in str(exc)


def test_start_export_returns_url_without_polling():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gigwa/exportData"):
            return httpx.Response(200, text="/gigwaV2/ddl_tmpOutput/u/abc/out.vcf")
        raise AssertionError(f"no polling/download expected, got {request.url.path}")

    client = make_client(_token_or(handler))
    url = client.start_export("MOD§1§run1", fmt="VCF")
    assert url == "/gigwaV2/ddl_tmpOutput/u/abc/out.vcf"


def test_export_progress_and_download_export_split():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/gigwa/progress"):
            assert "progressToken" not in request.url.params
            assert request.headers["authorization"] == "Bearer export_t"
            return httpx.Response(204)  # no export running yet
        if path.endswith("/out.vcf"):
            return httpx.Response(200, content=b"DATA")
        raise AssertionError(f"unexpected path: {path}")

    client = make_client(_token_or(handler))
    assert client.export_progress() is None  # 204 -> nothing running

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "out.vcf"
        written = client.download_export("/gigwaV2/ddl_tmpOutput/u/abc/out.vcf", dest)
        assert written.read_bytes() == b"DATA"


def test_download_export_refuses_off_origin_urls_and_never_leaks_the_token():
    """The download URL is caller-supplied and the request carries the bearer token, so an
    absolute URL naming any other host must be rejected before a request is made."""
    import tempfile
    from pathlib import Path

    import pytest

    from gigwa_mcp.errors import GigwaExportError

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"DATA")

    client = make_client(_token_or(handler))
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "out.vcf"

        for hostile in (
            "http://evil.example/steal",
            "https://test/gigwa/x",          # right host, wrong scheme
            "http://test.evil.example/x",    # prefix of the real host
        ):
            with pytest.raises(GigwaExportError, match="not the configured Gigwa server"):
                client.download_export(hostile, dest)

        # Nothing was ever sent anywhere, so the Authorization header did not leak.
        assert [r.url.host for r in seen if r.url.host != "test"] == []

        # A same-origin absolute URL and a relative path both still work.
        assert client.download_export("http://test/gigwaV2/out.vcf", dest).read_bytes() == b"DATA"
        assert client.download_export("/gigwaV2/out.vcf", dest).read_bytes() == b"DATA"


def test_download_export_treats_httpish_relative_paths_as_relative():
    """A relative path merely *starting* with 'http' must not be mistaken for absolute."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"DATA")

    import tempfile
    from pathlib import Path

    client = make_client(_token_or(handler))
    with tempfile.TemporaryDirectory() as d:
        client.download_export("/httpexport/out.vcf", Path(d) / "out.vcf")
    assert seen["url"] == "http://test/httpexport/out.vcf"


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
        if request.url.path.endswith("/gigwa/filterIndividualsFromMetadata/MOD"):
            return httpx.Response(200, json=[])  # native endpoint unavailable/empty
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


def test_get_germplasm_prefers_native_metadata_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gigwa/filterIndividualsFromMetadata/MOD"):
            import json

            assert json.loads(request.read().decode()) == {}
            return httpx.Response(200, json=[{"id": "7", "additionalInfo": {"GroupK4": "cA"}}])
        raise AssertionError(f"BrAPI fallback should not be reached: {request.url.path}")

    client = make_client(_token_or(handler))
    recs = client.get_germplasm("MOD§1§run1")
    assert recs == [{"germplasmName": "7", "germplasmDbId": "MOD§7", "additionalInfo": {"GroupK4": "cA"}}]


def test_distinct_individual_metadata_parses_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/gigwa/distinctIndividualMetadata/MOD")
        return httpx.Response(200, json={"GroupK4": ["cA", "XI", "GJ"], "Country": ["PE", "China"]})

    client = make_client(_token_or(handler))
    fields = client.distinct_individual_metadata("MOD")
    assert fields["GroupK4"] == ["cA", "XI", "GJ"]
    assert fields["Country"] == ["PE", "China"]


def test_filter_individuals_by_metadata_sends_filters():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.path.endswith("/gigwa/filterIndividualsFromMetadata/MOD")
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json=[
            {"id": "21", "additionalInfo": {"GroupK4": "cA"}},
            {"id": "34", "additionalInfo": {"GroupK4": "cA"}},
        ])

    client = make_client(_token_or(handler))
    recs = client.filter_individuals_by_metadata("MOD", {"GroupK4": ["cA"]})
    assert [r["id"] for r in recs] == ["21", "34"]
    assert captured["body"] == {"GroupK4": ["cA"]}
