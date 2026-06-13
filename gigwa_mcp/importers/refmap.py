"""Guess genomic coordinates for DArTseq markers by aligning their tag sequences.

DArTseq SNP markers ship with a ~69 bp ``AlleleSequence`` tag and a ``SnpPosition``
(0-based offset of the SNP within the tag). Aligning the tag to a reference genome
with minimap2 (via the ``mappy`` binding) recovers each marker's chromosome, position
and strand, so the imported data can be genome-anchored instead of placed on an
``Unmapped`` contig. On a minus-strand hit the REF/ALT alleles are complemented.

A reference FASTA for the species is required (provided by the caller).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .dartseq import _SNP_RE, _read_report

_COMPLEMENT = str.maketrans("ACGTNacgtn", "TGCANtgcan")
_CIGAR_OP = {"M": 0, "I": 1, "D": 2, "N": 3, "S": 4, "H": 5, "P": 6, "=": 7, "X": 8}


@dataclass
class TagMarker:
    allele_id: str
    tag: str
    snp_pos: int  # 0-based offset of the SNP within the tag
    ref: str
    alt: str


@dataclass
class MappedPosition:
    allele_id: str
    chrom: str | None
    pos: int | None  # 1-based (VCF) genomic position of the SNP
    strand: int
    mapq: int
    ref: str
    alt: str
    status: str  # "unique" | "multi" | "unmapped"


def extract_snp_tags(xlsx_path: str | Path, *, sheet: int | str | None = None) -> list[TagMarker]:
    """Extract one (reference-allele) tag per SNP marker from a DArTseq SNP report."""
    data = _read_report(Path(xlsx_path), sheet)
    if "AlleleSequence" not in data.columns or "SnpPosition" not in data.columns:
        raise ValueError(
            "Report lacks AlleleSequence / SnpPosition columns — cannot map tags to a reference."
        )
    snp = data["SNP"]
    is_ref = snp.isna().to_numpy()
    aid = data["AlleleID"].astype(str).to_numpy()
    seq = data["AlleleSequence"].astype(str).to_numpy()
    snp_str = snp.astype(str).to_numpy()
    snp_pos = pd.to_numeric(data["SnpPosition"], errors="coerce").to_numpy()

    markers: list[TagMarker] = []
    i, n = 0, len(data)
    while i < n - 1:
        if is_ref[i] and not is_ref[i + 1]:
            m = _SNP_RE.search(snp_str[i + 1]) or _SNP_RE.search(aid[i + 1])
            tag = seq[i].strip().upper()
            sp = snp_pos[i]
            if m and tag and tag not in ("", "NAN") and np.isfinite(sp) and 0 <= int(sp) < len(tag):
                markers.append(TagMarker(aid[i], tag, int(sp), m.group(2).upper(), m.group(3).upper()))
            i += 2
        else:
            i += 1
    return markers


def _parse_cigar(cg: str) -> list[tuple[int, int]]:
    ops, num = [], ""
    for ch in cg:
        if ch.isdigit():
            num += ch
        else:
            ops.append((int(num or 0), _CIGAR_OP.get(ch, 0)))
            num = ""
    return ops


def _ref_pos_from_aln(strand, r_st, q_st, q_en, cigar, q_off, qlen) -> int | None:
    """0-based reference coordinate of query offset ``q_off``.

    Walks the CIGAR (reference-forward orientation) so soft-clips and indels are
    handled. For minus-strand hits the query is reverse-complemented before
    alignment, so the offset is taken in that space.
    """
    if strand == 1:
        target = q_off
        qpos = q_st
    else:
        target = qlen - 1 - q_off
        qpos = qlen - q_en
    rpos = r_st
    for length, op in cigar:  # op: 0=M 1=I 2=D 3=N 4=S 7== 8=X
        if op in (0, 7, 8):
            if qpos <= target < qpos + length:
                return rpos + (target - qpos)
            qpos += length
            rpos += length
        elif op in (1, 4):
            if qpos <= target < qpos + length:
                return None  # SNP falls in an insertion / clip
            qpos += length
        elif op in (2, 3):
            rpos += length
    return None


def _make_position(m: TagMarker, ctg, ref0, strand, mapq, min_mapq) -> MappedPosition:
    if ctg is None or ref0 is None:
        return MappedPosition(m.allele_id, None, None, strand, mapq, m.ref, m.alt,
                              "multi" if ctg is not None else "unmapped")
    status = "unique" if mapq >= min_mapq else "multi"
    if strand == 1:
        ref_b, alt_b = m.ref, m.alt
    else:
        ref_b, alt_b = m.ref.translate(_COMPLEMENT), m.alt.translate(_COMPLEMENT)
    return MappedPosition(m.allele_id, ctg, ref0 + 1, strand, mapq, ref_b, alt_b, status)


def _finalize(markers, results) -> tuple[list[MappedPosition], dict]:
    counts = {"unique": 0, "multi": 0, "unmapped": 0}
    for r in results:
        counts[r.status] += 1
    mapped = [r for r in results if r.status == "unique"]
    chroms = pd.Series([r.chrom for r in mapped]).value_counts().to_dict() if mapped else {}
    return results, {"total": len(markers), **counts, "chromosomes": chroms}


def _map_via_mappy(markers, reference, *, min_mapq, preset):
    import mappy  # lazy

    aligner = mappy.Aligner(str(reference), preset=preset)
    if not aligner:
        raise ValueError(f"Could not load/index reference: {reference}")
    results = []
    for m in markers:
        primary = [h for h in aligner.map(m.tag) if h.is_primary]
        if not primary:
            results.append(_make_position(m, None, None, 0, 0, min_mapq))
            continue
        best = max(primary, key=lambda h: h.mapq)
        ref0 = _ref_pos_from_aln(best.strand, best.r_st, best.q_st, best.q_en,
                                 best.cigar, m.snp_pos, len(m.tag))
        results.append(_make_position(m, best.ctg, ref0, best.strand, best.mapq, min_mapq))
    return _finalize(markers, results)


def _map_via_cli(markers, reference, *, min_mapq, preset, threads, minimap2_bin):
    """Align tags with the minimap2 CLI (streams over multi-part indexes → bounded RAM)."""
    fa = tempfile.NamedTemporaryFile("w", suffix=".fa", delete=False)
    try:
        for i, m in enumerate(markers):
            fa.write(f">q{i}\n{m.tag}\n")
        fa.close()
        cmd = [minimap2_bin, "-c", "-x", preset, "--secondary=no",
               "-t", str(threads), str(reference), fa.name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ValueError(f"minimap2 failed (exit {proc.returncode}): {proc.stderr[-500:]}")
    finally:
        os.unlink(fa.name)

    best: dict[int, list[str]] = {}
    for line in proc.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 12:
            continue
        qi = int(f[0][1:])
        if qi not in best or int(f[11]) > int(best[qi][11]):
            best[qi] = f

    results = []
    for i, m in enumerate(markers):
        f = best.get(i)
        if f is None:
            results.append(_make_position(m, None, None, 0, 0, min_mapq))
            continue
        qlen, q_st, q_en = int(f[1]), int(f[2]), int(f[3])
        strand = 1 if f[4] == "+" else -1
        ctg, r_st, mapq = f[5], int(f[7]), int(f[11])
        cg = next((x[5:] for x in f[12:] if x.startswith("cg:Z:")), None)
        cigar = _parse_cigar(cg) if cg else [(qlen, 0)]
        ref0 = _ref_pos_from_aln(strand, r_st, q_st, q_en, cigar, m.snp_pos, qlen)
        results.append(_make_position(m, ctg, ref0, strand, mapq, min_mapq))
    return _finalize(markers, results)


def map_tags_to_reference(
    markers: list[TagMarker],
    reference_fasta: str | Path,
    *,
    min_mapq: int = 20,
    preset: str = "sr",
    backend: str = "auto",
    threads: int | None = None,
    minimap2_bin: str = "minimap2",
) -> tuple[list[MappedPosition], dict]:
    """Align tags to *reference_fasta* (FASTA or prebuilt ``.mmi``) → positions + stats.

    ``backend``: ``"cli"`` uses the minimap2 command line (streams over multi-part
    indexes, bounded RAM — best for large genomes); ``"mappy"`` uses the in-process
    minimap2 binding (loads the whole index in RAM); ``"auto"`` prefers the CLI when
    ``minimap2`` is on PATH, else falls back to mappy.
    """
    threads = threads or os.cpu_count() or 4
    use_cli = backend == "cli" or (backend == "auto" and shutil.which(minimap2_bin) is not None)
    if backend == "cli" and shutil.which(minimap2_bin) is None:
        raise ValueError(f"minimap2 CLI '{minimap2_bin}' not found on PATH.")
    if use_cli:
        return _map_via_cli(markers, reference_fasta, min_mapq=min_mapq, preset=preset,
                            threads=threads, minimap2_bin=minimap2_bin)
    return _map_via_mappy(markers, reference_fasta, min_mapq=min_mapq, preset=preset)


def positions_to_dataframe(results: list[MappedPosition]) -> pd.DataFrame:
    return pd.DataFrame([vars(r) for r in results],
                        columns=["allele_id", "chrom", "pos", "strand", "mapq", "ref", "alt", "status"])
