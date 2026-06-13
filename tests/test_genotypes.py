"""Genotype loader: parse a small VCF fixture and map callset ids -> accession names."""

from __future__ import annotations

from pathlib import Path

from gigwa_mcp.analysis.genotypes import clear_cache, load_genotypes

VCF = "\n".join([
    "##fileformat=VCFv4.1",
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
    "\t".join(["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", "S1", "S2"]),
    "\t".join(["1", "100", "m1", "A", "G", ".", ".", ".", "GT", "0/0", "0/1"]),
    "\t".join(["1", "200", "m2", "A", "G", ".", ".", ".", "GT", "1/1", "./."]),
]) + "\n"


class FakeClient:
    def export_variantset_vcf(self, vs, dest, **kw):
        Path(dest).write_text(VCF)
        return Path(dest)

    def search_callsets(self, vs):
        return [
            {"callSetDbId": "S1", "callSetName": "acc1"},
            {"callSetDbId": "S2", "callSetName": "acc2"},
        ]


def test_load_genotypes_parses_and_maps_names(tmp_path):
    clear_cache()
    gm = load_genotypes(FakeClient(), "VS§1§run", cache_dir=tmp_path)
    assert gm.n_variants == 2
    assert gm.n_samples == 2
    assert gm.sample_ids == ["S1", "S2"]
    assert gm.sample_names == ["acc1", "acc2"]
    assert gm.gt.shape == (2, 2, 2)
    # m2/S2 is missing
    assert bool(gm.gt.is_missing()[1, 1])


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

    def search_allelematrix(self, vs, *, variant_page=0, variant_page_size=5000,
                            callset_page=0, callset_page_size=100000,
                            data_matrix_abbreviations=("GT",)):
        vids, matrix = self._PAGES[variant_page]
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
