"""Genotype loader: parse a small VCF fixture and map callset ids -> accession names."""

from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np

from gigwa_mcp.analysis.genotypes import clear_cache, load_genotypes

VCF = "\n".join([
    "##fileformat=VCFv4.1",
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
    "\t".join(["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", "S1", "S2"]),
    "\t".join(["1", "100", "m1", "A", "G", ".", ".", ".", "GT", "0/0", "0/1"]),
    "\t".join(["1", "200", "m2", "A", "G", ".", ".", ".", "GT", "1/1", "./."]),
]) + "\n"


class FakeClient:
    def __init__(self):
        # (endpoint, variant_set_db_id, kwargs) per fetch call, in order -- lets tests
        # assert *which* export path was actually used, not just the parsed result.
        self.calls: list[tuple[str, str, dict]] = []

    def export_variantset_vcf(self, vs, dest, **kw):
        self.calls.append(("brapi", vs, kw))
        Path(dest).write_text(VCF)
        return Path(dest)

    def export_selection(self, vs, dest, **kw):
        # Real Gigwa zips /gigwa/exportData's VCF output (a HOW_TO_CITE.txt alongside
        # the actual .vcf member) -- unlike BrAPI's plain body. Mirror that here so this
        # fixture can't drift back to plain text and hide the same bug again.
        self.calls.append(("selection", vs, kw))
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("HOW_TO_CITE.txt", "cite Gigwa")
            zf.writestr("export.vcf", VCF)
        return Path(dest)

    def search_callsets(self, vs):
        return [
            {"callSetDbId": "S1", "callSetName": "acc1"},
            {"callSetDbId": "S2", "callSetName": "acc2"},
        ]


def test_load_genotypes_parses_and_maps_names(tmp_path):
    clear_cache()
    client = FakeClient()
    gm = load_genotypes(client, "VS§1§run", cache_dir=tmp_path)
    assert gm.n_variants == 2
    assert gm.n_samples == 2
    assert gm.sample_ids == ["S1", "S2"]
    assert gm.sample_names == ["acc1", "acc2"]
    assert gm.gt.shape == (2, 2, 2)
    # m2/S2 is missing
    assert bool(gm.gt.is_missing()[1, 1])
    # No region/individuals filter -> whole-run BrAPI export, callset-raw, unchanged.
    assert client.calls == [("brapi", "VS§1§run", {})]


class SampleDbIdClient(FakeClient):
    """Callsets carry a numeric callSetName but the real accession is in sampleDbId."""

    def search_callsets(self, vs):
        return [
            {"callSetDbId": "S1", "callSetName": "1", "sampleDbId": "VS§ACC0001-1-run"},
            {"callSetDbId": "S2", "callSetName": "2", "sampleDbId": "VS§ACC0002-1-run"},
        ]


def test_accession_name_extraction():
    from gigwa_mcp.analysis.genotypes import _accession_name as a

    assert a("VS§ACC0050-1-clean", "VS§1§clean") == "ACC0050"
    assert a("MOD§foo-bar-2-run", "MOD§2§run") == "foo-bar"  # hyphenated accession kept
    assert a(None, "VS§1§run") is None
    assert a("VS§justname", "VS§1§run") == "justname"  # no -proj-run suffix


def test_load_genotypes_prefers_accession_from_sampledbid(tmp_path):
    clear_cache()
    gm = load_genotypes(SampleDbIdClient(), "VS§1§run", cache_dir=tmp_path)
    assert gm.sample_names == ["ACC0001", "ACC0002"]  # not the numeric callSetName


def test_load_genotypes_unknown_set_raises_clear_error(tmp_path):
    from gigwa_mcp.errors import GigwaAPIError, GigwaError

    class BadClient(FakeClient):
        def search_callsets(self, vs):
            raise GigwaAPIError("search/callsets failed", status_code=500, body="")

    clear_cache()
    try:
        load_genotypes(BadClient(), "NOPE§1§x", cache_dir=tmp_path)
    except GigwaError as exc:
        assert "may not exist" in str(exc)
    else:
        raise AssertionError("expected a clear GigwaError for an unknown variant set")


def test_subsample_markers(tmp_path):
    clear_cache()
    gm = load_genotypes(FakeClient(), "VS§1§run2", cache_dir=tmp_path)
    sub = gm.subsample_markers(1)
    assert sub.n_variants == 1
    assert sub.n_samples == 2
    assert list(sub.variant_ids) == ["m1"]


def test_parse_region():
    from gigwa_mcp.analysis.genotypes import parse_region

    assert parse_region("chr1") == ("chr1", None, None)
    assert parse_region("chr1:100-200") == ("chr1", 100, 200)
    assert parse_region("chr1:1,000-2,000") == ("chr1", 1000, 2000)
    assert parse_region("chr1:500-") == ("chr1", 500, None)
    assert parse_region("chr1:-900") == ("chr1", None, 900)


def test_load_genotypes_region_filter(tmp_path):
    clear_cache()
    # VCF fixture has chrom "1" at pos 100 and 200.
    client = FakeClient()
    gm = load_genotypes(client, "VS§1§reg", cache_dir=tmp_path, region="1:150-250")
    assert gm.n_variants == 1
    assert int(gm.pos[0]) == 200
    # BrAPI has no region parameter at all -- a region filter can only be served
    # server-side via the selection-aware export, not the whole-run BrAPI path.
    assert client.calls[-1] == (
        "selection", "VS§1§reg",
        {"reference_name": "1", "start": 150, "end": 250, "exported_individuals": None},
    )

    clear_cache()
    client2 = FakeClient()
    none = load_genotypes(client2, "VS§1§reg2", cache_dir=tmp_path, region="2")
    assert none.n_variants == 0
    assert client2.calls[-1][0] == "selection"


def test_load_genotypes_individuals_filter_routes_to_selection_export(tmp_path):
    """BrAPI can't filter by individual either -- same rule, the other trigger."""
    clear_cache()
    client = FakeClient()
    gm = load_genotypes(client, "VS§1§ind", cache_dir=tmp_path, individuals=["acc1"])
    assert gm.n_variants == 2  # fixture is unfiltered; only the routing is under test
    assert client.calls[-1] == (
        "selection", "VS§1§ind",
        {"reference_name": None, "start": None, "end": None, "exported_individuals": ["acc1"]},
    )


def test_load_genotypes_filtered_results_are_cached_separately(tmp_path):
    """Different (region, individuals) combinations -- and the unfiltered fetch -- must
    never share a cache entry or silently substitute for one another."""
    clear_cache()
    client = FakeClient()

    load_genotypes(client, "VS§1§sep", cache_dir=tmp_path)  # unfiltered: BrAPI
    load_genotypes(client, "VS§1§sep", cache_dir=tmp_path, individuals=["acc1"])
    load_genotypes(client, "VS§1§sep", cache_dir=tmp_path, individuals=["acc2"])
    load_genotypes(client, "VS§1§sep", cache_dir=tmp_path, region="1")
    assert [c[0] for c in client.calls] == ["brapi", "selection", "selection", "selection"]
    # four calls, four genuinely distinct filter signatures -- none reused another's fetch
    signatures = [repr(c[2]) for c in client.calls]
    assert len(set(signatures)) == len(signatures) == 4

    # Repeating an already-fetched filter combination hits the in-process cache (no
    # new fetch) -- same guarantee the unfiltered/allelematrix paths already had.
    calls_before = len(client.calls)
    load_genotypes(client, "VS§1§sep", cache_dir=tmp_path, individuals=["acc1"])
    assert len(client.calls) == calls_before


def test_ensure_plain_vcf_passes_through_non_zip(tmp_path):
    from gigwa_mcp.analysis.genotypes import _ensure_plain_vcf

    plain = tmp_path / "plain.vcf"
    plain.write_text(VCF)
    assert _ensure_plain_vcf(plain) == plain  # BrAPI's output: already plain, no-op


def test_ensure_plain_vcf_unwraps_zipped_plain_member(tmp_path):
    """/gigwa/exportData with fmt=VCF: HOW_TO_CITE.txt + a plain .vcf member."""
    from gigwa_mcp.analysis.genotypes import _ensure_plain_vcf

    zipped = tmp_path / "export.vcf"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("HOW_TO_CITE.txt", "cite Gigwa")
        zf.writestr("export.vcf", VCF)

    result = _ensure_plain_vcf(zipped)
    assert result != zipped
    assert result.read_text() == VCF


def test_ensure_plain_vcf_unwraps_zipped_gzipped_member(tmp_path):
    """/gigwa/exportData with fmt=VCF.gz: every export is zipped, even this one -- the
    zip's inner member is itself bgzipped, so it needs a second decompression step."""
    import gzip as _gzip

    from gigwa_mcp.analysis.genotypes import _ensure_plain_vcf

    zipped = tmp_path / "export.vcf.gz"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("HOW_TO_CITE.txt", "cite Gigwa")
        zf.writestr("export.vcf.gz", _gzip.compress(VCF.encode()))

    result = _ensure_plain_vcf(zipped)
    assert result != zipped
    assert result.read_text() == VCF  # decompressed, not just unzipped


# --- allelematrix extraction path -------------------------------------------

def test_decode_gt_token():
    from gigwa_mcp.analysis.genotypes import _decode_gt_token as d

    assert d("0", 2) == [0, 0]      # collapsed homozygous ref
    assert d("1", 2) == [1, 1]      # collapsed homozygous alt
    assert d("0/1", 2) == [0, 1]    # heterozygote
    assert d("1|0", 2) == [1, 0]    # phased
    assert d(".", 2) == [-1, -1]    # missing
    assert d("", 2) == [-1, -1]
    assert d("./1", 2) == [-1, 1]   # half-missing


class FakeAMClient:
    """Serves a 3-variant × 3-callset matrix across two variant pages."""

    _PAGES = {
        0: (["m§chr1§100", "m§chr1§200"], [["0", "0/1", "."], ["1", ".", "0"]]),
        1: (["m§chr2§50"], [["0/1", "1", "."]]),
    }

    def __init__(self):
        self.calls = 0

    def search_allelematrix(self, vs, *, variant_page=0, variant_page_size=5000,
                            callset_page=0, callset_page_size=100000,
                            data_matrix_abbreviations=("GT",), variant_db_ids=None):
        self.calls += 1
        vids, matrix = self._PAGES[variant_page]
        if variant_db_ids:
            wanted = {value.split("§", 1)[-1] for value in variant_db_ids}
            pairs = [
                (vid, row) for vid, row in zip(vids, matrix)
                if vid.split("§", 1)[-1] in wanted
            ]
            vids = [pair[0] for pair in pairs]
            matrix = [pair[1] for pair in pairs]
        return {
            "callSetDbIds": ["S1", "S2", "S3"],
            "variantDbIds": vids,
            "dataMatrices": [{"dataMatrix": matrix, "dataMatrixAbbreviation": "GT"}],
            "sepUnphased": "/", "sepPhased": "|", "unknownString": ".",
            "pagination": [
                {"dimension": "VARIANTS", "page": variant_page, "pageSize": 2,
                 "totalCount": 3, "totalPages": 2},
                {"dimension": "CALLSETS", "page": 0, "pageSize": 3,
                 "totalCount": 3, "totalPages": 1},
            ],
        }

    def search_callsets(self, vs):
        return [{"callSetDbId": f"S{i}", "callSetName": f"acc{i}"} for i in (1, 2, 3)]

    def search_variants(self, vs, **kwargs):
        ids = [variant for variants, _ in self._PAGES.values() for variant in variants]
        return [
            {"id": f"VS§1§{variant.split('§', 1)[-1]}"}
            for variant in ids[: kwargs["max_variants"]]
        ]


def test_load_via_allelematrix():
    gm = load_genotypes(FakeAMClient(), "VS§1§run", method="allelematrix")
    assert gm.n_variants == 3 and gm.n_samples == 3
    assert gm.sample_names == ["acc1", "acc2", "acc3"]
    assert gm.gt.shape == (3, 3, 2)
    assert list(gm.gt[0, 0]) == [0, 0]        # "0"
    assert list(gm.gt[0, 1]) == [0, 1]        # "0/1"
    assert bool(gm.gt.is_missing()[0, 2])      # "."
    assert list(gm.gt[1, 0]) == [1, 1]        # "1"
    # chrom/pos parsed from the §-delimited variant DbIds
    assert gm.chrom[2] == "chr2" and int(gm.pos[2]) == 50


def test_allelematrix_max_markers_caps_pages():
    gm = load_genotypes(FakeAMClient(), "VS§1§run", method="allelematrix", max_markers=2)
    assert gm.n_variants == 2  # only the first variant page pulled
    assert list(gm.variant_ids) == ["chr1§100", "chr1§200"]


def test_allelematrix_is_session_cached():
    clear_cache()
    client = FakeAMClient()

    first = load_genotypes(client, "VS§1§run", method="allelematrix")
    calls_after_first = client.calls
    assert calls_after_first > 0

    # Identical params -> cache hit: same matrix, no further client calls.
    second = load_genotypes(client, "VS§1§run", method="allelematrix")
    assert client.calls == calls_after_first
    assert second is first
    np.testing.assert_array_equal(np.asarray(second.gt), np.asarray(first.gt))

    # Different cap -> distinct key -> re-fetch.
    load_genotypes(client, "VS§1§run", method="allelematrix", max_markers=2)
    assert client.calls > calls_after_first

    # clear_cache() forces a re-fetch on identical params.
    calls_before_clear = client.calls
    clear_cache()
    load_genotypes(client, "VS§1§run", method="allelematrix")
    assert client.calls > calls_before_clear


def test_allelematrix_use_cache_false_bypasses_cache():
    clear_cache()
    client = FakeAMClient()
    load_genotypes(client, "VS§1§run", method="allelematrix", use_cache=False)
    calls_after_first = client.calls
    # No store, so a second uncached call re-fetches.
    load_genotypes(client, "VS§1§run", method="allelematrix", use_cache=False)
    assert client.calls > calls_after_first
