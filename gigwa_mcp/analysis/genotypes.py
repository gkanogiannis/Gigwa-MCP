"""Genotype data-access layer: pull genotypes out of Gigwa into a GenotypeArray.

Primary path is VCF export (`GigwaClient.export_variantset_vcf`) parsed with
scikit-allel. The exported VCF is cached on disk and the parsed matrix is cached
in-process, so running several analysis tools over one variant set downloads and
parses it only once. VCF sample IDs are Gigwa callset DbIds (``MODULE§N``); we map
them to accession names via ``GigwaClient.search_callsets``.
"""

from __future__ import annotations

import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

import allel
import numpy as np

from ..client import GigwaClient
from ..errors import GigwaAPIError, GigwaError

# In-process cache of the full (un-subsampled) matrix, keyed by variantSetDbId.
_SESSION_CACHE: dict[str, "GenotypeMatrix"] = {}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "gigwa"


def module_of(variant_set_db_id: str) -> str:
    """Database/module name is the first ``§``-separated segment of the id."""
    return variant_set_db_id.split("§", 1)[0]


def _accession_name(sample_db_id: str | None, variant_set_db_id: str) -> str | None:
    """Recover the accession/individual name from a Gigwa ``sampleDbId``.

    Gigwa labels callsets with a meaningless numeric index (``callSetName`` = "1", "2",
    …), but encodes the real individual name in ``sampleDbId`` as
    ``<module>§<individual>-<project>-<run>``. Stripping the module prefix and the
    trailing ``-<project>-<run>`` recovers the name the user knows the accession by —
    essential for matching a metadata TSV in the grouping tools.
    """
    if not sample_db_id:
        return None
    s = str(sample_db_id)
    body = s.split("§", 1)[1] if "§" in s else s
    parts = str(variant_set_db_id).split("§")
    if len(parts) == 3:
        suffix = f"-{parts[1]}-{parts[2]}"
        if body.endswith(suffix) and len(body) > len(suffix):
            body = body[: -len(suffix)]
    return body or None


def _name_map(callsets: list[dict], variant_set_db_id: str) -> dict[str, str]:
    """Map callSetDbId -> best human name (accession from sampleDbId, else callSetName)."""
    out: dict[str, str] = {}
    for cs in callsets:
        cid = cs.get("callSetDbId")
        if cid is None:
            continue
        out[cid] = (
            _accession_name(cs.get("sampleDbId"), variant_set_db_id)
            or cs.get("callSetName")
            or cid
        )
    return out


def _callsets(client: GigwaClient, variant_set_db_id: str) -> list[dict]:
    """Fetch callsets, turning a raw server error (e.g. unknown id) into a clear message."""
    try:
        return client.search_callsets(variant_set_db_id)
    except GigwaAPIError as exc:
        raise GigwaError(
            f"Could not load callsets for '{variant_set_db_id}' — the variant set may "
            f"not exist on this server ({exc})."
        ) from exc


@dataclass
class GenotypeMatrix:
    gt: allel.GenotypeArray  # (n_variants, n_samples, ploidy)
    variant_ids: np.ndarray
    chrom: np.ndarray
    pos: np.ndarray
    sample_ids: list[str]  # callSetDbId, in VCF column order
    sample_names: list[str]  # callSetName aligned to sample_ids (falls back to id)
    variant_set_db_id: str
    # Depth-field summary (allelematrix path with with_depth=True only; None otherwise):
    # depth_present = any non-missing AD/DP token seen; depth_all_zero = depth present
    # but every value is zero (the fabricated-placeholder fingerprint).
    depth_present: bool | None = None
    depth_all_zero: bool | None = None

    @property
    def n_variants(self) -> int:
        return self.gt.shape[0]

    @property
    def n_samples(self) -> int:
        return self.gt.shape[1]

    def subsample_markers(self, max_markers: int) -> "GenotypeMatrix":
        if self.n_variants <= max_markers:
            return self
        # Evenly spaced indices -> reproducible, spread across the genome/report.
        idx = np.linspace(0, self.n_variants - 1, max_markers).astype(int)
        idx = np.unique(idx)
        return GenotypeMatrix(
            gt=allel.GenotypeArray(self.gt[idx]),
            variant_ids=self.variant_ids[idx],
            chrom=self.chrom[idx],
            pos=self.pos[idx],
            sample_ids=self.sample_ids,
            sample_names=self.sample_names,
            variant_set_db_id=self.variant_set_db_id,
        )


def _cache_dir(cache_dir: str | Path | None) -> Path:
    base = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "gigwa_mcp_vcf"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _download_and_parse(
    client: GigwaClient, variant_set_db_id: str, cache_dir: str | Path | None
) -> GenotypeMatrix:
    vcf_path = _cache_dir(cache_dir) / f"{_safe_name(variant_set_db_id)}.vcf"
    if not (vcf_path.exists() and vcf_path.stat().st_size > 64):
        client.export_variantset_vcf(variant_set_db_id, vcf_path)

    with warnings.catch_warnings():
        # Gigwa's export omits the ##FORMAT=<ID=GT> header line; GT still parses fine.
        warnings.filterwarnings("ignore", message=".*FORMAT header not found.*")
        callset = allel.read_vcf(
            str(vcf_path),
            fields=["samples", "calldata/GT", "variants/CHROM", "variants/POS", "variants/ID"],
        )
    if callset is None or "calldata/GT" not in callset:
        raise GigwaError(
            f"No genotype data parsed from the VCF export of {variant_set_db_id}."
        )

    gt = allel.GenotypeArray(callset["calldata/GT"])
    sample_ids = [str(s) for s in callset["samples"]]
    name_map = _name_map(_callsets(client, variant_set_db_id), variant_set_db_id)
    sample_names = [name_map.get(sid) or sid for sid in sample_ids]

    return GenotypeMatrix(
        gt=gt,
        variant_ids=np.asarray(callset.get("variants/ID")),
        chrom=np.asarray(callset.get("variants/CHROM")),
        pos=np.asarray(callset.get("variants/POS")),
        sample_ids=sample_ids,
        sample_names=sample_names,
        variant_set_db_id=variant_set_db_id,
    )


def _decode_gt_token(
    tok: str, ploidy: int, sep_unphased: str = "/", sep_phased: str = "|", missing: str = "."
) -> list[int]:
    """Decode one Gigwa allelematrix GT token into a list of allele indices.

    Gigwa collapses homozygous diploid genotypes to a single allele code
    (``0`` -> ``0/0``, ``1`` -> ``1/1``), keeps heterozygotes as ``0/1`` and uses
    ``unknownString`` (``.``) for missing. A missing allele within a token is -1.
    """
    if not tok or tok == missing:
        return [-1] * ploidy
    if sep_phased in tok or sep_unphased in tok:
        parts = tok.replace(sep_phased, sep_unphased).split(sep_unphased)
        alleles = [(-1 if p in ("", missing) else int(p)) for p in parts]
    else:
        alleles = [int(tok)] * ploidy  # collapsed homozygote
    if len(alleles) < ploidy:
        alleles += [-1] * (ploidy - len(alleles))
    return alleles[:ploidy]


_DEPTH_DIGITS = frozenset("123456789")


def _matrix_by_abbrev(data_matrices: list, abbrev: str) -> list:
    """Return the 2-D token list for a dataMatrix abbreviation.

    allelematrix returns one matrix per requested abbreviation in an arbitrary order
    (e.g. ``AD, DP, GT``), so select by label; fall back to the sole matrix when a
    GT-only request omits the abbreviation field.
    """
    for dm in data_matrices:
        if str(dm.get("dataMatrixAbbreviation", "")).upper() == abbrev.upper():
            return dm.get("dataMatrix", [])
    return data_matrices[0].get("dataMatrix", []) if len(data_matrices) == 1 else []


def _scan_depth(matrix: list, miss: str) -> tuple[bool, bool]:
    """Scan a depth (DP/AD) token matrix → (any_present, any_positive).

    A token of ``.``/``""`` (or the matrix's ``unknownString``) means the field is
    absent for that genotype (normal — many DArTseq/array VCFs carry no depth). A
    present token is "positive" if it contains any non-zero digit; depth present but
    everywhere zero (``DP=0``/``AD=0,0``) is the fabricated-placeholder fingerprint.
    """
    present = positive = False
    for row in matrix:
        for tok in row:
            if tok is None or tok in ("", ".", miss):
                continue
            present = True
            if any(ch in _DEPTH_DIGITS for ch in str(tok)):
                return True, True  # present + positive: nothing more to learn
    return present, positive


def _parse_variant_coords(variant_ids: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Best-effort chrom/pos from variant DbIds (``…§chrom§pos``); placeholders otherwise."""
    chrom, pos = [], []
    for vid in variant_ids:
        parts = str(vid).split("§")
        if len(parts) >= 2 and parts[-1].isdigit():
            chrom.append(parts[-2])
            pos.append(int(parts[-1]))
        else:
            chrom.append("NA")
            pos.append(-1)
    return np.asarray(chrom), np.asarray(pos, dtype=int)


def _load_via_allelematrix(
    client: GigwaClient,
    variant_set_db_id: str,
    *,
    max_markers: int | None = None,
    max_samples: int | None = None,
    with_depth: bool = False,
    cell_cap: int = 10000,
) -> GenotypeMatrix:
    """Load genotypes via paged BrAPI ``search/allelematrix`` (no VCF export).

    Gigwa caps each response at ~``cell_cap`` genotype values and derives the
    variant-page size from the *requested* callset page size, so we size both pages
    to the actual sample count (learned from ``search_callsets``) and then drive the
    loop from the page sizes the server *returns*. Pages over variants (and callsets,
    if the server still splits them), decoding the compact GT tokens into a
    scikit-allel ``GenotypeArray``. ``max_markers`` caps how many variant pages are
    pulled (each page is one HTTP round-trip). ``max_samples`` caps the callsets to a
    single leading page of that size — a bounded subsample for fast whole-instance
    triage on very large/wide variant sets, where a full matrix would be huge.
    ``with_depth`` additionally requests the AD/DP matrices and summarises whether the
    depth fields are present and whether they are all zero (a fabricated placeholder).
    """
    abbrevs = ("GT", "DP", "AD") if with_depth else ("GT",)
    callsets = _callsets(client, variant_set_db_id)
    name_map = _name_map(callsets, variant_set_db_id)
    n_samples = len(callsets) or 1
    if max_samples and max_samples < n_samples:
        cs_ps = max_samples
        cap_cs_pages = 1  # only the first callset page -> a sample of the columns
    else:
        cs_ps = n_samples
        cap_cs_pages = None
    var_ps = max(1, cell_cap // cs_ps)  # stay under the server's per-response cell cap

    first = client.search_allelematrix(
        variant_set_db_id, variant_page=0, variant_page_size=var_ps,
        callset_page=0, callset_page_size=cs_ps, data_matrix_abbreviations=abbrevs,
    )
    sep_u = first.get("sepUnphased", "/")
    sep_p = first.get("sepPhased", "|")
    miss = first.get("unknownString", ".")
    page_meta = {str(p.get("dimension", "")).upper(): p for p in first.get("pagination", [])}
    actual_var_ps = int(page_meta.get("VARIANTS", {}).get("pageSize", var_ps) or var_ps)
    var_pages = int(page_meta.get("VARIANTS", {}).get("totalPages", 1) or 1)
    cs_pages = int(page_meta.get("CALLSETS", {}).get("totalPages", 1) or 1)
    if cap_cs_pages:
        cs_pages = min(cs_pages, cap_cs_pages)
    if max_markers:
        var_pages = min(var_pages, max(1, -(-max_markers // max(actual_var_ps, 1))))  # ceil

    # Infer ploidy from the first heterozygous-style token, default diploid.
    ploidy = 2
    for row in _matrix_by_abbrev(first.get("dataMatrices") or [], "GT"):
        hit = next((t for t in row if sep_u in t or sep_p in t), None)
        if hit:
            ploidy = max(ploidy, hit.replace(sep_p, sep_u).count(sep_u) + 1)
            break

    depth_present = depth_positive = False
    var_blocks, var_id_blocks, sample_ids = [], [], None
    for vp in range(var_pages):
        cs_cols, page_var_ids, page_sids = [], None, []
        for cp in range(cs_pages):
            res = first if (vp == 0 and cp == 0) else client.search_allelematrix(
                variant_set_db_id, variant_page=vp, variant_page_size=var_ps,
                callset_page=cp, callset_page_size=cs_ps, data_matrix_abbreviations=abbrevs,
            )
            dm = res.get("dataMatrices") or []
            if not dm:
                continue
            tokens = _matrix_by_abbrev(dm, "GT")
            page_var_ids = res.get("variantDbIds") or []
            page_sids += [str(x) for x in (res.get("callSetDbIds") or [])]
            if with_depth:
                for abbr in ("DP", "AD"):
                    pres, pos = _scan_depth(_matrix_by_abbrev(dm, abbr), miss)
                    depth_present = depth_present or pres
                    depth_positive = depth_positive or pos
            block = np.array(
                [[_decode_gt_token(t, ploidy, sep_u, sep_p, miss) for t in row] for row in tokens],
                dtype="i1",
            )
            cs_cols.append(block)
        if not cs_cols:
            continue
        var_blocks.append(np.concatenate(cs_cols, axis=1) if len(cs_cols) > 1 else cs_cols[0])
        var_id_blocks.append(np.asarray(page_var_ids))
        if sample_ids is None:
            sample_ids = page_sids

    if not var_blocks:
        raise GigwaError(f"allelematrix returned no genotype data for {variant_set_db_id}.")

    gt = np.concatenate(var_blocks, axis=0)
    variant_ids = np.concatenate(var_id_blocks)
    if max_markers and gt.shape[0] > max_markers:
        gt, variant_ids = gt[:max_markers], variant_ids[:max_markers]

    # variantDbIds are "MODULE§<name>"; strip the module prefix to match VCF-path IDs.
    variant_ids = np.array(
        [str(v).split("§", 1)[1] if "§" in str(v) else str(v) for v in variant_ids]
    )
    sample_ids = sample_ids or []
    sample_names = [name_map.get(sid) or sid for sid in sample_ids]
    chrom, pos = _parse_variant_coords(list(variant_ids))
    return GenotypeMatrix(
        gt=allel.GenotypeArray(gt),
        variant_ids=variant_ids,
        chrom=chrom,
        pos=pos,
        sample_ids=sample_ids,
        sample_names=sample_names,
        variant_set_db_id=variant_set_db_id,
        depth_present=depth_present if with_depth else None,
        depth_all_zero=(depth_present and not depth_positive) if with_depth else None,
    )


def load_genotypes(
    client: GigwaClient,
    variant_set_db_id: str,
    *,
    max_markers: int | None = None,
    max_samples: int | None = None,
    with_depth: bool = False,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    method: str = "vcf",
) -> GenotypeMatrix:
    """Load a variant set's genotypes as a :class:`GenotypeMatrix`.

    ``method="vcf"`` (default) exports the whole variant set once, parses it with
    scikit-allel and caches it in-process; ``max_markers`` then returns an
    evenly-spaced subsample. ``method="allelematrix"`` pulls genotypes via paged
    BrAPI ``search/allelematrix`` instead — useful for subset/scale extraction
    (honours ``max_markers`` and ``max_samples`` server-side); it is not
    session-cached. ``max_samples`` only applies to the allelematrix path.
    """
    if method == "allelematrix":
        return _load_via_allelematrix(
            client, variant_set_db_id, max_markers=max_markers,
            max_samples=max_samples, with_depth=with_depth,
        )

    full = _SESSION_CACHE.get(variant_set_db_id) if use_cache else None
    if full is None:
        full = _download_and_parse(client, variant_set_db_id, cache_dir)
        if use_cache:
            _SESSION_CACHE[variant_set_db_id] = full
    if max_markers:
        return full.subsample_markers(max_markers)
    return full


def clear_cache() -> None:
    _SESSION_CACHE.clear()
