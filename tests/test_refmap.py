"""DArT tag -> reference mapping: recover known positions/strands from a synthetic genome."""

from __future__ import annotations

import random
import shutil
import sys
import subprocess

import pytest

from gigwa_mcp.importers.refmap import TagMarker, map_tags_to_reference

_COMP = str.maketrans("ACGT", "TGCA")


def _rnd(n, rng):
    return "".join(rng.choice("ACGT") for _ in range(n))


def _rc(s):
    return s.translate(_COMP)[::-1]


def _build_reference(tmp_path, placements):
    """placements: list of (tag, pos0, strand). Returns FASTA path; one contig 'chr1'."""
    rng = random.Random(7)
    size = max(p + len(t) for t, p, _ in placements) + 500
    seq = list(_rnd(size, rng))
    for tag, pos0, strand in placements:
        ins = tag if strand == 1 else _rc(tag)
        seq[pos0 : pos0 + len(ins)] = list(ins)
    fa = tmp_path / "ref.fa"
    fa.write_text(">chr1\n" + "".join(seq) + "\n")
    return fa


@pytest.mark.parametrize("backend", ["cli", "mappy"])
def test_maps_forward_and_reverse(tmp_path, backend):
    rng = random.Random(42)
    # Distinct random 69 bp tags so each maps uniquely with high mapq.
    tags = [_rnd(69, rng) for _ in range(4)]
    snp_pos = 54
    # (tag, genomic placement pos0, strand)
    placements = [
        (tags[0], 1000, 1),
        (tags[1], 3000, -1),
        (tags[2], 6000, 1),
        (tags[3], 9000, -1),
    ]
    fa = _build_reference(tmp_path, placements)

    markers = []
    for t in tags:
        ref_b = t[snp_pos]
        alt_b = "A" if ref_b != "A" else "C"
        markers.append(TagMarker(allele_id=f"m_{t[:6]}", tag=t, snp_pos=snp_pos, ref=ref_b, alt=alt_b))

    if backend == "cli" and shutil.which("minimap2") is None:
        pytest.skip("minimap2 CLI not installed")
    if backend == "mappy":
        pytest.importorskip("mappy")
    results, stats = map_tags_to_reference(markers, fa, min_mapq=20, backend=backend)
    by_id = {r.allele_id: r for r in results}
    assert stats["unique"] == 4 and stats["unmapped"] == 0

    for (tag, pos0, strand), m in zip(placements, markers):
        r = by_id[m.allele_id]
        assert r.status == "unique"
        assert r.chrom == "chr1"
        assert r.strand == strand
        if strand == 1:
            assert r.pos == pos0 + snp_pos + 1  # 1-based SNP genomic position
            assert (r.ref, r.alt) == (m.ref, m.alt)
        else:
            assert r.pos == pos0 + (len(tag) - 1 - snp_pos) + 1
            assert (r.ref, r.alt) == (m.ref.translate(_COMP), m.alt.translate(_COMP))


def test_unmapped_tag(tmp_path):
    if shutil.which("minimap2") is None:
        pytest.importorskip("mappy")
    rng = random.Random(99)
    placed = _rnd(69, rng)
    fa = _build_reference(tmp_path, [(placed, 1000, 1)])
    absent = _rnd(69, random.Random(123))  # not in the reference
    markers = [TagMarker("absent", absent, 54, absent[54], "A")]
    results, stats = map_tags_to_reference(markers, fa, min_mapq=20)
    assert results[0].status == "unmapped"
    assert results[0].pos is None


@pytest.mark.parametrize("backend", ["auto", "mappy"])
def test_missing_mappy_has_actionable_error(monkeypatch, backend):
    monkeypatch.setitem(sys.modules, "mappy", None)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="minimap2 CLI.*Linux/Docker"):
        map_tags_to_reference([], "unused.fa", backend=backend)


def test_cli_selected_without_mappy(monkeypatch):
    from gigwa_mcp.importers import refmap

    monkeypatch.setitem(sys.modules, "mappy", None)
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/minimap2")
    expected = ([], {"total": 0})
    monkeypatch.setattr(refmap, "_map_via_cli", lambda *args, **kwargs: expected)
    assert map_tags_to_reference([], "unused.fa") == expected


def test_unrelated_import_error_preserved(monkeypatch):
    import builtins

    original = builtins.__import__

    def import_with_broken_dependency(name, *args, **kwargs):
        if name == "mappy":
            raise ModuleNotFoundError("missing internal dependency", name="internal_dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_broken_dependency)
    with pytest.raises(ModuleNotFoundError, match="internal dependency"):
        map_tags_to_reference([], "unused.fa", backend="mappy")


def test_server_import_without_mappy():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['mappy'] = None; import gigwa_mcp.server"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
