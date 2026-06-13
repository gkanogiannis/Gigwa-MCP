"""End-to-end test + benchmark of every MCP tool against a live Gigwa instance.

Drives the whole tool surface — import (DArTseq SNP/Silico, VCF, genome-anchoring),
metadata (validate/import), the anomaly auditor, all QC tools, all diversity tools —
using the seeded synthetic data produced by ``gen_synthetic_data.py``. Every call is
timed; broken inputs are fed deliberately to confirm the detectors fire; the two
data-access backends (vcf vs allelematrix) are cross-checked for agreement; and a
larger panel exercises paging and the O(n^2) tools.

Connection settings come from the environment / a local ``.env`` (GIGWA_URL/USER/PASS).
Data is imported into dedicated ``BENCH_*`` modules with clear_project_data=True, so
reruns are idempotent and the harness never touches other databases.

    python scripts/gen_synthetic_data.py        # once, to create data/synthetic/
    python scripts/benchmark_local.py           # full run (imports + scale tier)
    python scripts/benchmark_local.py --no-scale --no-import   # reuse existing imports
"""
from __future__ import annotations

import argparse
import json
import re
import time
import traceback
from pathlib import Path

from gigwa_mcp.analysis.genotypes import clear_cache
from gigwa_mcp.errors import GigwaError
from gigwa_mcp.server import get_client
from gigwa_mcp.tools import audit, connection, diversity, genotype, metadata, qc

DATA = Path("data/synthetic")
REPORT = Path("gigwa_results/benchmark_report.md")


def fn(tool):
    """Unwrap a FastMCP tool to its plain function."""
    return getattr(tool, "fn", tool)


# --------------------------------------------------------------------------- #
# recording + timing
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, phase, tool, dataset, backend, elapsed, status, note=""):
        self.rows.append({
            "phase": phase, "tool": tool, "dataset": dataset, "backend": backend,
            "elapsed": elapsed, "status": status, "note": note,
        })
        flag = {"OK": "✓", "PASS": "✓", "FAIL": "✗", "XFAIL": "✓", "ERROR": "✗"}.get(status, "?")
        print(f"  [{flag} {status:5s}] {tool:28s} {backend:12s} {elapsed:6.1f}s  {note}", flush=True)

    def to_markdown(self) -> str:
        out = ["| Phase | Tool | Dataset | Backend | Time (s) | Status | Notes |",
               "|---|---|---|---|---:|---|---|"]
        for r in self.rows:
            note = r["note"].replace("|", "\\|").replace("\n", " ")[:140]
            out.append(f"| {r['phase']} | `{r['tool']}` | {r['dataset']} | {r['backend']} "
                       f"| {r['elapsed']:.1f} | {r['status']} | {note} |")
        return "\n".join(out)


REC = Recorder()


def timed(func, **kw):
    t0 = time.time()
    try:
        return func(**kw), time.time() - t0, None
    except Exception as exc:  # noqa: BLE001 — we want the full bleed, not an abort
        return None, time.time() - t0, exc


def call(phase, tool_name, func, dataset, backend, *, head=1, **kw):
    """Invoke a tool, record timing + a short output head, return (output, error)."""
    out, dt, err = timed(func, **kw)
    if err is not None:
        REC.add(phase, tool_name, dataset, backend, dt, "ERROR",
                f"{type(err).__name__}: {str(err)[:90]}")
        return None, err
    note = " / ".join(str(out).splitlines()[:head]) if out else ""
    REC.add(phase, tool_name, dataset, backend, dt, "OK", note)
    return out, None


def expect_graceful(phase, tool_name, func, dataset, must_contain, **kw):
    """An edge case that must return a clear message (or raise a clean ValueError),
    not crash with an opaque traceback. Records XFAIL when handled as designed."""
    out, dt, err = timed(func, **kw)
    if err is not None:
        # A clean domain error (ValueError / GigwaError) is graceful; a raw HTTP error is not.
        ok = isinstance(err, (ValueError, GigwaError)) and (not must_contain or must_contain.lower() in str(err).lower())
        REC.add(phase, tool_name, dataset, "edge", dt, "XFAIL" if ok else "FAIL",
                f"{type(err).__name__}: {str(err)[:80]}")
        return
    ok = (not must_contain) or (must_contain.lower() in str(out).lower())
    REC.add(phase, tool_name, dataset, "edge", dt, "XFAIL" if ok else "FAIL",
            " / ".join(str(out).splitlines()[:1])[:90])


_FLOAT = r"([-+]?\d*\.?\d+)"


def grab(text, pattern):
    m = re.search(pattern, text or "")
    return float(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# variant-set id resolution
# --------------------------------------------------------------------------- #
def resolve_vsid(client, module, run) -> str:
    """Find the variantSetDbId for a freshly imported (module, run)."""
    for vs in client.list_variantsets():
        vsid = str(vs.get("variantSetDbId", ""))
        if vsid.startswith(module + "§") and vsid.endswith("§" + run):
            return vsid
    return f"{module}§1§{run}"  # conventional fallback


# --------------------------------------------------------------------------- #
# import phase
# --------------------------------------------------------------------------- #
def phase_import(client) -> dict:
    print("\n== Phase B: import ==", flush=True)
    ids = {}

    # DArTseq SNP, genome-anchored: first map tags, then import using the mapping CSV.
    map_out, _ = call("B-import", "map_dartseq_to_reference", fn(genotype.map_dartseq_to_reference),
                      "dartseq_snp", "minimap2",
                      snp_xlsx=str(DATA / "dartseq_snp.xlsx"),
                      reference_fasta=str(DATA / "reference.fasta"))
    pos_csv = None
    if map_out:
        m = re.search(r"File:\s*(\S+)", map_out)
        pos_csv = m.group(1) if m else None

    call("B-import", "import_dartseq", fn(genotype.import_dartseq), "dartseq_snp (anchored)", "xlsx",
         module="BENCH_DART", project="synthetic", run="snp",
         snp_xlsx=str(DATA / "dartseq_snp.xlsx"),
         positions_csv=pos_csv, clear_project_data=True, wait=True)
    ids["dart"] = resolve_vsid(client, "BENCH_DART", "snp")

    call("B-import", "import_dartseq", fn(genotype.import_dartseq), "silico_dart", "xlsx",
         module="BENCH_SILICO", project="synthetic", run="silico",
         silico_xlsx=str(DATA / "silico_dart.xlsx"), clear_project_data=True, wait=True)
    ids["silico"] = resolve_vsid(client, "BENCH_SILICO", "silico")

    call("B-import", "import_vcf", fn(genotype.import_vcf), "clean", "vcf",
         module="BENCH_VCF", project="synthetic", run="clean",
         vcf_path=str(DATA / "clean.vcf"), clear_project_data=True, wait=True)
    ids["vcf"] = resolve_vsid(client, "BENCH_VCF", "clean")

    call("B-import", "import_vcf", fn(genotype.import_vcf), "tiny", "vcf",
         module="BENCH_TINY", project="synthetic", run="tiny",
         vcf_path=str(DATA / "tiny.vcf"), clear_project_data=True, wait=True)
    ids["tiny"] = resolve_vsid(client, "BENCH_TINY", "tiny")

    # Adversarial sets for the anomaly phase.
    for mod, run, vcf in [("BENCH_BROKEN_HET", "run", "broken_allhet.vcf"),
                          ("BENCH_MONO", "run", "suspect_monomorphic.vcf")]:
        call("B-import", "import_vcf", fn(genotype.import_vcf), vcf, "vcf",
             module=mod, project="synthetic", run=run,
             vcf_path=str(DATA / vcf), clear_project_data=True, wait=True)
        ids[mod] = resolve_vsid(client, mod, run)

    # Async path: import with wait=False, poll progress, then block until done.
    out, err = call("B-import", "import_vcf (async)", fn(genotype.import_vcf), "broken_losthomalt", "vcf",
                    module="BENCH_LOSTHOMALT", project="synthetic", run="run",
                    vcf_path=str(DATA / "broken_losthomalt.vcf"), clear_project_data=True, wait=False)
    if out:
        tok = re.search(r"token:\s*(\S+?)\)", out)
        token = tok.group(1) if tok else None
        if token:
            call("B-import", "get_import_progress", fn(genotype.get_import_progress),
                 "broken_losthomalt", "poll", progress_token=token)
            try:
                client.wait_for_completion(token, timeout=900)
            except Exception as exc:  # noqa: BLE001
                print(f"    (async wait note: {exc})")
    ids["BENCH_LOSTHOMALT"] = resolve_vsid(client, "BENCH_LOSTHOMALT", "run")

    return ids


# --------------------------------------------------------------------------- #
# metadata phase
# --------------------------------------------------------------------------- #
def phase_metadata():
    print("\n== Phase C: metadata ==", flush=True)
    call("C-metadata", "validate_metadata", fn(metadata.validate_metadata), "metadata.tsv", "good",
         tsv_path=str(DATA / "metadata.tsv"), module="BENCH_VCF")
    # Bad file: wrong id-column header -> should report a problem gracefully.
    expect_graceful("C-metadata", "validate_metadata", fn(metadata.validate_metadata),
                    "metadata_bad.tsv", must_contain="",
                    tsv_path=str(DATA / "metadata_bad.tsv"), module="BENCH_VCF",
                    metadata_type="individual")
    call("C-metadata", "import_metadata", fn(metadata.import_metadata), "metadata.tsv", "validate+import",
         tsv_path=str(DATA / "metadata.tsv"), module="BENCH_VCF", validate_first=True)


# --------------------------------------------------------------------------- #
# anomaly phase
# --------------------------------------------------------------------------- #
def phase_anomaly(client, ids, expected):
    print("\n== Phase D: anomaly / audit ==", flush=True)
    # Targeted audit on one broken set (shows the detailed reasons in the report).
    call("D-anomaly", "audit_import_quality", fn(audit.audit_import_quality),
         "BENCH_BROKEN_HET", "allelematrix", head=4,
         variant_set_db_id=ids["BENCH_BROKEN_HET"])

    # Whole-instance scan -> read the CSV and assert each BENCH_* lands in its class.
    out, err = call("D-anomaly", "audit_import_quality (instance)", fn(audit.audit_import_quality),
                    "<whole instance>", "allelematrix", head=1)
    csv = Path("gigwa_results/import_quality_scan.csv")
    status_by_id = {}
    if csv.exists():
        import pandas as pd
        df = pd.read_csv(csv)
        status_by_id = dict(zip(df["variant_set_db_id"].astype(str), df["status"].astype(str)))

    checks = {
        ids["vcf"]: "OK",
        ids["BENCH_BROKEN_HET"]: "BROKEN",
        ids["BENCH_LOSTHOMALT"]: "BROKEN",
        ids["BENCH_MONO"]: "SUSPECT",
    }
    for vsid, want in checks.items():
        got = status_by_id.get(vsid, "<missing>")
        REC.add("D-anomaly", "audit:classify", vsid.split("§")[0], "assert", 0.0,
                "PASS" if got == want else "FAIL", f"expected {want}, got {got}")


# --------------------------------------------------------------------------- #
# QC phase + cross-backend consistency
# --------------------------------------------------------------------------- #
def phase_qc(vsid):
    print("\n== Phase E: QC (both backends) ==", flush=True)
    results = {}
    for backend in ("vcf", "allelematrix"):
        clear_cache()
        out, _ = call("E-qc", "qc_call_rate", fn(qc.qc_call_rate), "clean", backend,
                      variant_set_db_id=vsid, method=backend)
        results[backend] = grab(out, rf"Overall call rate:\s*{_FLOAT}")
        call("E-qc", "qc_heterozygosity", fn(qc.qc_heterozygosity), "clean", backend,
             variant_set_db_id=vsid, method=backend)
        call("E-qc", "qc_maf_filter", fn(qc.qc_maf_filter), "clean", backend,
             variant_set_db_id=vsid, method=backend)
        call("E-qc", "qc_duplicate_accessions", fn(qc.qc_duplicate_accessions), "clean", backend,
             variant_set_db_id=vsid, method=backend, max_markers=1000)

    a, b = results.get("vcf"), results.get("allelematrix")
    if a is not None and b is not None:
        ok = abs(a - b) <= 0.02
        REC.add("E-qc", "xbackend:call_rate", "clean", "assert", 0.0,
                "PASS" if ok else "FAIL", f"vcf={a:.3f} allelematrix={b:.3f} (Δ{abs(a-b):.3f})")


# --------------------------------------------------------------------------- #
# diversity phase + known-truth checks
# --------------------------------------------------------------------------- #
def phase_diversity(vsid, meta_tsv, expected_k):
    print("\n== Phase F: diversity ==", flush=True)
    clear_cache()
    out, _ = call("F-diversity", "diversity_summary", fn(diversity.diversity_summary), "clean", "vcf",
                  variant_set_db_id=vsid)
    out, _ = call("F-diversity", "diversity_pca", fn(diversity.diversity_pca), "clean", "vcf", head=2,
                  variant_set_db_id=vsid, metadata_tsv=meta_tsv, group_column="population")
    call("F-diversity", "diversity_kinship", fn(diversity.diversity_kinship), "clean", "vcf",
         variant_set_db_id=vsid)

    out, _ = call("F-diversity", "diversity_fst", fn(diversity.diversity_fst), "clean", "vcf", head=4,
                  variant_set_db_id=vsid, metadata_tsv=meta_tsv, group_column="population")
    if out:
        fsts = [float(x) for x in re.findall(r"Fst=" + _FLOAT, out)]
        ok = bool(fsts) and all(f > 0 for f in fsts)
        REC.add("F-diversity", "truth:fst>0", "clean", "assert", 0.0,
                "PASS" if ok else "FAIL", f"pairwise Fst={fsts}")

    call("F-diversity", "diversity_by_group", fn(diversity.diversity_by_group), "clean", "vcf", head=4,
         variant_set_db_id=vsid, metadata_tsv=meta_tsv, group_column="population")
    call("F-diversity", "diversity_core_collection", fn(diversity.diversity_core_collection),
         "clean", "vcf", variant_set_db_id=vsid, fraction=0.1)

    out, _ = call("F-diversity", "diversity_structure", fn(diversity.diversity_structure), "clean", "vcf",
                  head=2, variant_set_db_id=vsid, k_min=2, k_max=6)
    if out:
        k = grab(out, r"Suggested K=" + _FLOAT)
        ok = k is not None and abs(k - expected_k) <= 1
        REC.add("F-diversity", "truth:structure_K", "clean", "assert", 0.0,
                "PASS" if ok else "FAIL", f"suggested K={k}, expected ~{expected_k}")

    out, _ = call("F-diversity", "diversity_tree", fn(diversity.diversity_tree), "clean", "vcf",
                  variant_set_db_id=vsid)


# --------------------------------------------------------------------------- #
# edge / error phase
# --------------------------------------------------------------------------- #
def phase_edge(ids):
    print("\n== Phase G: edge / error cases ==", flush=True)
    expect_graceful("G-edge", "import_dartseq", fn(genotype.import_dartseq), "no xlsx",
                    must_contain="at least one",
                    module="BENCH_X", project="p", run="r")
    expect_graceful("G-edge", "diversity_fst", fn(diversity.diversity_fst), "no groups",
                    must_contain="No groups", variant_set_db_id=ids["vcf"])
    expect_graceful("G-edge", "diversity_fst", fn(diversity.diversity_fst), "one group",
                    must_contain="at least two",
                    variant_set_db_id=ids["vcf"], groups_json='{"only": ["ACC0000","ACC0001"]}')
    expect_graceful("G-edge", "diversity_pca", fn(diversity.diversity_pca), "tiny (<2 poly)",
                    must_contain="Not enough", variant_set_db_id=ids["tiny"])
    expect_graceful("G-edge", "diversity_structure", fn(diversity.diversity_structure), "tiny (<4 samples)",
                    must_contain="Not enough", variant_set_db_id=ids["tiny"])
    expect_graceful("G-edge", "qc_call_rate", fn(qc.qc_call_rate), "bogus id",
                    must_contain="", variant_set_db_id="NO_SUCH_DB§1§nope", method="allelematrix")


# --------------------------------------------------------------------------- #
# scale phase
# --------------------------------------------------------------------------- #
def phase_scale(client):
    print("\n== Phase H: scale tier ==", flush=True)
    call("H-scale", "import_vcf", fn(genotype.import_vcf), "scale", "vcf",
         module="BENCH_SCALE", project="synthetic", run="scale",
         vcf_path=str(DATA / "scale.vcf"), clear_project_data=True, wait=True)
    vsid = resolve_vsid(client, "BENCH_SCALE", "scale")

    # Session-cache effect: first vcf call pays the export+parse, the second is cached.
    clear_cache()
    call("H-scale", "diversity_summary (cold)", fn(diversity.diversity_summary), "scale", "vcf",
         variant_set_db_id=vsid)
    call("H-scale", "diversity_summary (warm)", fn(diversity.diversity_summary), "scale", "vcf",
         variant_set_db_id=vsid)

    # allelematrix paging cost at high sample count.
    call("H-scale", "qc_call_rate", fn(qc.qc_call_rate), "scale", "allelematrix",
         variant_set_db_id=vsid, method="allelematrix", max_markers=2000)

    # O(n^2) tools on ~500 samples (cached vcf matrix).
    call("H-scale", "diversity_kinship", fn(diversity.diversity_kinship), "scale", "vcf",
         variant_set_db_id=vsid, max_markers=2000)
    call("H-scale", "qc_duplicate_accessions", fn(qc.qc_duplicate_accessions), "scale", "vcf",
         variant_set_db_id=vsid, max_markers=2000)
    call("H-scale", "diversity_tree", fn(diversity.diversity_tree), "scale", "vcf",
         variant_set_db_id=vsid, max_markers=2000)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-import", action="store_true", help="reuse already-imported BENCH_* sets")
    ap.add_argument("--no-scale", action="store_true", help="skip the scale tier")
    args = ap.parse_args()

    if not DATA.exists():
        raise SystemExit(f"{DATA} not found — run: python scripts/gen_synthetic_data.py")
    manifest = json.loads((DATA / "manifest.json").read_text())
    expected_k = manifest["main"]["expected_k"]

    client = get_client()
    t_start = time.time()

    print("== Phase A: connection / inventory ==", flush=True)
    call("A-conn", "gigwa_server_info", fn(connection.gigwa_server_info), "-", "rest", head=3)
    call("A-conn", "list_content", fn(connection.list_content), "-", "rest", head=1)

    if args.no_import:
        ids = {
            "vcf": resolve_vsid(client, "BENCH_VCF", "clean"),
            "tiny": resolve_vsid(client, "BENCH_TINY", "tiny"),
            "BENCH_BROKEN_HET": resolve_vsid(client, "BENCH_BROKEN_HET", "run"),
            "BENCH_MONO": resolve_vsid(client, "BENCH_MONO", "run"),
            "BENCH_LOSTHOMALT": resolve_vsid(client, "BENCH_LOSTHOMALT", "run"),
        }
    else:
        ids = phase_import(client)
        phase_metadata()

    phase_anomaly(client, ids, manifest["expected_audit_class"])
    phase_qc(ids["vcf"])
    phase_diversity(ids["vcf"], str(DATA / "metadata.tsv"), expected_k)
    phase_edge(ids)
    if not args.no_scale:
        phase_scale(client)

    write_report(time.time() - t_start)


def write_report(total_s):
    n_fail = sum(1 for r in REC.rows if r["status"] in ("FAIL", "ERROR"))
    n_pass = sum(1 for r in REC.rows if r["status"] in ("PASS", "XFAIL", "OK"))
    asserts = [r for r in REC.rows if r["status"] in ("PASS", "FAIL")]
    slow = sorted([r for r in REC.rows if r["elapsed"] > 0], key=lambda r: -r["elapsed"])[:10]

    md = [
        "# Gigwa MCP — local end-to-end benchmark",
        "",
        f"Total wall time: **{total_s:.1f}s** — {len(REC.rows)} tool calls, "
        f"{n_pass} ok, {n_fail} failed/errored.",
        "",
        "## Assertions (known-truth + cross-backend)",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for r in asserts:
        md.append(f"| `{r['tool']}` ({r['dataset']}) | {r['status']} | {r['note']} |")
    md += ["", "## Slowest calls", "", "| Tool | Dataset | Backend | Time (s) |",
           "|---|---|---|---:|"]
    for r in slow:
        md.append(f"| `{r['tool']}` | {r['dataset']} | {r['backend']} | {r['elapsed']:.1f} |")
    md += ["", "## Full call log", "", REC.to_markdown(), ""]

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(md))

    print("\n" + "=" * 72)
    print(f"DONE in {total_s:.1f}s — {len(REC.rows)} calls, {n_pass} ok, {n_fail} failed/errored.")
    if n_fail:
        print("FAILURES / ERRORS:")
        for r in REC.rows:
            if r["status"] in ("FAIL", "ERROR"):
                print(f"  [{r['status']}] {r['tool']} ({r['dataset']}/{r['backend']}): {r['note']}")
    print(f"Report: {REPORT}")


if __name__ == "__main__":
    main()
