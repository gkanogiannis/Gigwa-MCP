"""ADMIXTURE integration: PLINK 1 BED writer + binary fetch/run wrapper.

Runs the real ADMIXTURE tool (Alexander, Novembre & Lange 2009) rather than
reimplementing its block-relaxation EM in Python. The binary is statically linked and
has no installer, so it is fetched into a per-user cache dir on first use instead of
requiring a system package (there is no apt/conda package for it, and no Windows build
at all -- see :func:`ensure_admixture_binary`).

Allele identity is not carried through: Gigwa's VCF export (see ``genotypes.py``) does
not request REF/ALT, and ADMIXTURE's likelihood depends only on allele *frequency* per
marker, not which base is which, so the BED's A1/A2 columns are arbitrary placeholders.
"""

from __future__ import annotations

import io
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from ..progress import notify

_ADMIXTURE_VERSION = "1.3.0"
# Official static binaries (David Alexander's own site) -- Linux and macOS only.
_ADMIXTURE_URLS = {
    "linux": f"https://dalexander.github.io/admixture/binaries/admixture_linux-{_ADMIXTURE_VERSION}.tar.gz",
    "darwin": f"https://dalexander.github.io/admixture/binaries/admixture_macosx-{_ADMIXTURE_VERSION}.tar.gz",
}

# PLINK 1 BED 2-bit genotype codes, indexed by (n_alt + 1): missing=-1, hom-ref=0,
# het=1, hom-alt=2 -> PLINK 0b01 (missing), 0b11 (hom A2), 0b10 (het), 0b00 (hom A1).
_BED_CODE_LUT = np.array([0b01, 0b11, 0b10, 0b00], dtype=np.uint8)
_BED_MAGIC = bytes([0x6C, 0x1B, 0x01])  # SNP-major mode


def _cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    d = base / "gigwa-mcp" / "admixture"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_admixture_binary() -> Path:
    """Return a path to a working ``admixture`` executable, fetching it on first use.

    Checks PATH first, then the per-user cache. ADMIXTURE ships only Linux and macOS
    binaries (no native Windows build) -- on Windows this raises an actionable error,
    the same pattern ``importers/refmap.py`` uses for ``mappy``'s missing Windows wheels.
    """
    found = shutil.which("admixture")
    if found:
        return Path(found)

    dest = _cache_dir() / "admixture"
    if dest.exists() and os.access(dest, os.X_OK):
        return dest

    system = platform.system().lower()  # 'linux', 'darwin', 'windows'
    if system not in _ADMIXTURE_URLS:
        raise ValueError(
            "ADMIXTURE has no native Windows build -- the author publishes Linux and "
            "macOS binaries only. Run diversity_admixture from WSL, Linux, or Docker, "
            "or build ADMIXTURE from source and put it on PATH."
        )

    url = _ADMIXTURE_URLS[system]
    notify(f"Fetching ADMIXTURE {_ADMIXTURE_VERSION} binary…")
    try:
        with httpx.Client(follow_redirects=True, timeout=120) as c:
            resp = c.get(url)
            resp.raise_for_status()
            data = resp.content
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not download ADMIXTURE from {url}: {exc}") from exc

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        member = next(
            (m for m in tf.getmembers() if m.isfile() and Path(m.name).name == "admixture"),
            None,
        )
        if member is None:
            raise ValueError(f"Downloaded archive from {url} has no 'admixture' executable.")
        extracted = tf.extractfile(member)
        if extracted is None:
            raise ValueError(f"Could not extract 'admixture' from {url}.")
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(extracted.read())
        tmp.replace(dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _safe_id(name: str, seen: dict[str, int]) -> str:
    """PLINK IDs are whitespace-delimited fields -- sanitise and de-duplicate."""
    base = re.sub(r"\s+", "_", str(name).strip()) or "sample"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}_{n}"


def _plink_chrom(name: object) -> str:
    """ADMIXTURE accepts only integer chromosome codes ("Invalid chromosome code!  Use
    integers." on e.g. ``chr10``): strip a chr/Chr/CHR prefix and keep the number, else
    ``0`` (unplaced). ADMIXTURE doesn't model linkage, so ``0`` loses nothing."""
    m = re.fullmatch(r"(?:chr)?0*(\d+)", str(name or "").strip(), flags=re.IGNORECASE)
    return m.group(1) if m else "0"


@dataclass
class PlinkDataset:
    bed_path: Path
    n_variants: int
    n_samples: int


def write_plink_bed(gm, out_prefix: Path) -> PlinkDataset:
    """Write ``<out_prefix>.{bed,bim,fam}`` from a :class:`GenotypeMatrix`.

    Drops fully-missing and monomorphic markers (matching ``diversity_pca``'s
    ``drop_invariant``) -- ADMIXTURE gains nothing from them and they cost EM time.
    Missing genotypes are preserved (not imputed): ADMIXTURE handles missingness itself.
    """
    n_alt = np.asarray(gm.gt.to_n_alt(fill=-1), dtype=np.int8)  # (variants, samples): -1/0/1/2
    with np.errstate(invalid="ignore"):
        masked = np.where(n_alt >= 0, n_alt, np.nan)
        row_min = np.nanmin(masked, axis=1)
        row_max = np.nanmax(masked, axis=1)
    keep = np.isfinite(row_min) & (row_min != row_max)
    n_alt = n_alt[keep]
    n_variants, n_samples = n_alt.shape
    if n_variants == 0:
        raise ValueError("No polymorphic markers left to write for ADMIXTURE.")

    codes = _BED_CODE_LUT[n_alt + 1]
    n_bytes = (n_samples + 3) // 4
    packed = np.zeros((n_variants, n_bytes), dtype=np.uint8)
    for byte_i in range(n_bytes):
        for slot in range(4):
            col = byte_i * 4 + slot
            if col >= n_samples:
                break
            packed[:, byte_i] |= (codes[:, col] << (2 * slot)).astype(np.uint8)

    bed_path = out_prefix.with_suffix(".bed")
    with open(bed_path, "wb") as f:
        f.write(_BED_MAGIC)
        f.write(packed.tobytes())

    chrom = gm.chrom[keep] if gm.chrom is not None else None
    pos = gm.pos[keep] if gm.pos is not None else None
    vids = gm.variant_ids[keep] if gm.variant_ids is not None else None
    with open(out_prefix.with_suffix(".bim"), "w") as f:
        for i in range(n_variants):
            c = _plink_chrom(chrom[i]) if chrom is not None else "0"
            p = int(pos[i]) if pos is not None and pos[i] else i + 1
            vid = str(vids[i]) if vids is not None and vids[i] not in (None, "") else f"snp{i}"
            f.write(f"{c}\t{vid}\t0\t{p}\tA\tC\n")

    seen: dict[str, int] = {}
    with open(out_prefix.with_suffix(".fam"), "w") as f:
        for name in gm.sample_names:
            sid = _safe_id(name, seen)
            f.write(f"{sid}\t{sid}\t0\t0\t0\t-9\n")

    return PlinkDataset(bed_path=bed_path, n_variants=n_variants, n_samples=n_samples)


def write_pop_file(dataset: PlinkDataset, labels: list[str | None]) -> list[str]:
    """Write ``<prefix>.pop`` for ``--supervised`` and return the Q column order.

    One line per ``.fam`` row: the sample's reference-population label, or ``-`` for a
    sample whose ancestry is to be estimated (``None`` in *labels*). ADMIXTURE orders the
    supervised Q columns by each label's *first appearance* in this file (verified
    empirically against 1.3.0 -- not alphabetical), which is what the returned list gives.
    """
    if len(labels) != dataset.n_samples:
        raise ValueError(f"{len(labels)} population labels for {dataset.n_samples} samples.")
    clean = [re.sub(r"\s+", "_", str(lab).strip()) if lab is not None else "-" for lab in labels]
    if any(lab in ("", "-") for lab, raw in zip(clean, labels) if raw is not None):
        raise ValueError("Reference population labels must be non-empty and not '-'.")
    with open(dataset.bed_path.with_suffix(".pop"), "w") as f:
        f.write("\n".join(clean) + "\n")
    return list(dict.fromkeys(lab for lab in clean if lab != "-"))


@dataclass
class AdmixtureRun:
    k: int
    q: np.ndarray  # (n_samples, k) ancestry fractions
    cv_error: float | None
    log_likelihood: float | None


_CV_RE = re.compile(r"CV error \(K=\d+\):\s*([0-9.]+)")
_LL_RE = re.compile(r"^Loglikelihood:\s*(-?[0-9.]+)\s*$", re.MULTILINE)


def run_admixture(
    dataset: PlinkDataset,
    k: int,
    *,
    cv: bool = True,
    supervised: bool = False,
    seed: int = 1,
    threads: int | None = None,
    binary: Path | None = None,
) -> AdmixtureRun:
    """Run ADMIXTURE for one K, in the BED file's own directory (where it writes output).

    ``supervised`` requires a ``.pop`` file next to the BED (see :func:`write_pop_file`)
    and *k* equal to its number of distinct reference populations.
    """
    exe = binary or ensure_admixture_binary()
    args = [str(exe)]
    if cv:
        args.append("--cv")
    if supervised:
        args.append("--supervised")
    args.append(f"--seed={seed}")
    if threads:
        args.append(f"-j{int(threads)}")
    args += [dataset.bed_path.name, str(k)]

    notify(f"Running ADMIXTURE K={k}…")
    proc = subprocess.run(
        args, cwd=dataset.bed_path.parent, capture_output=True, text=True, timeout=3600
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-800:]
        raise ValueError(f"ADMIXTURE K={k} failed (exit {proc.returncode}): {tail}")

    cv_match = _CV_RE.search(proc.stdout)
    ll_match = _LL_RE.search(proc.stdout)
    q_path = dataset.bed_path.with_suffix(f".{k}.Q")
    if not q_path.exists():
        raise ValueError(f"ADMIXTURE K={k} reported success but wrote no {q_path.name}.")
    q = np.loadtxt(q_path)
    if q.ndim == 1:  # k == 1 collapses to a single column
        q = q.reshape(-1, 1)
    return AdmixtureRun(
        k=k,
        q=q,
        cv_error=float(cv_match.group(1)) if cv_match else None,
        log_likelihood=float(ll_match.group(1)) if ll_match else None,
    )


def q_dataframe(gm, run: AdmixtureRun, populations: list[str] | None = None) -> pd.DataFrame:
    """Q matrix as a table; *populations* (supervised runs) names the columns instead of
    ``Q1..Qk``, and ``dominant_cluster`` then holds the population name."""
    names = populations if populations else [f"Q{j + 1}" for j in range(run.k)]
    cols = {"sample_id": gm.sample_ids, "sample_name": gm.sample_names}
    for j, name in enumerate(names):
        cols[name] = run.q[:, j]
    dominant = np.argmax(run.q, axis=1)
    cols["dominant_cluster"] = [names[i] for i in dominant] if populations else 1 + dominant
    return pd.DataFrame(cols)
