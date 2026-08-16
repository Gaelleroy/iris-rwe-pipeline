"""Pipeline entry point.

    python -m src.pipeline <stage> [--config path] [--run-id id]

Stages: generate | ingest | validate | transform | cohort | all

The QC gate is the one place the pipeline is allowed to stop itself. A FAIL
means nothing propagates to the curated layer - the failure mode this prevents
is a partial or silently wrong cohort that someone then analyses in good faith.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analyze.run import analyze  # noqa: E402
from src.cohort.build import build as build_cohort  # noqa: E402
from src.generate.synthesize import generate  # noqa: E402
from src.ingest.land import land  # noqa: E402
from src.manifest import RunManifest, load_config  # noqa: E402
from src.storage.backends import get_backend  # noqa: E402
from src.transform.curate import curate  # noqa: E402
from src.validate.rules import run_all, summarize  # noqa: E402

RAW_TABLES = ["patients", "encounters", "diagnoses", "injections", "visual_acuity"]


def _read_raw(storage) -> dict[str, pd.DataFrame]:
    return {
        name: storage.read_csv(f"raw/{name}/{name}.csv", dtype=str, keep_default_na=False)
        for name in RAW_TABLES
    }


def _read_curated(storage) -> dict[str, pd.DataFrame]:
    out = {}
    for name in RAW_TABLES:
        df = storage.read_parquet(f"curated/{name}")
        for col in df.columns:
            if col.endswith("_date"):
                df[col] = pd.to_datetime(df[col], errors="coerce")
        out[name] = df
    return out


def _log(msg: str) -> None:
    print(f"[pipeline] {msg}", flush=True)


def stage_generate(cfg, storage, manifest):
    _log("generating synthetic EHR with injected defects")
    tables = generate(cfg)
    for name, df in tables.items():
        storage.write_csv(df, f"raw/{name}/{name}.csv")
        _log(f"  {name:<15} {len(df):>8,} rows")
    manifest.record("generate", seed=cfg["generate"]["seed"],
                    rows={k: len(v) for k, v in tables.items()})
    return tables


def stage_ingest(cfg, storage, manifest):
    _log("ingest pre-flight")
    tables = _read_raw(storage)
    report = land(tables, storage, manifest)
    for t in report["tables"]:
        flag = "" if t["status"] == "PASS" else f"  <- {t['status']}"
        extra = f" unexpected={t['unexpected_columns']}" if t["unexpected_columns"] else ""
        _log(f"  {t['table']:<15} {t['rows']:>8,} rows  fp={t['schema_fingerprint']}{extra}{flag}")
    if report["status"] == "FAIL":
        raise SystemExit("ingest FAILED: required columns missing")
    return tables


def stage_validate(cfg, storage, manifest, tables=None):
    _log("validation: structural + clinical")
    tables = tables or _read_raw(storage)
    findings = run_all(tables, cfg)
    summary = summarize(findings)

    for tier in ("structural", "clinical"):
        _log(f"  -- {tier} --")
        for f in findings:
            if f.tier != tier or f.status == "PASS":
                continue
            _log(f"  {f.status:<4} {f.rule:<28} {f.table:<24} "
                 f"{f.n_flagged:>6,}/{f.n_total:<8,} ({f.rate:.3%})")
    _log(f"  gate={summary['gate']}  pass={summary['pass']} "
         f"warn={summary['warn']} fail={summary['fail']}")

    payload = {"summary": summary, "findings": [f.to_dict() for f in findings]}
    storage.write_bytes(
        f"metadata/{manifest.run_id}/validation_report.json",
        json.dumps(payload, indent=2).encode(),
    )
    storage.write_csv(
        pd.DataFrame([f.to_dict() for f in findings]),
        f"metadata/{manifest.run_id}/validation_report.csv",
    )
    manifest.record("validate", **summary,
                    failures=[f.rule for f in findings if f.status == "FAIL"],
                    warnings=[f.rule for f in findings if f.status == "WARN"])

    if summary["gate"] == "FAIL":
        raise SystemExit(
            "QC GATE FAILED - curated layer not written. "
            "Failing rules: "
            + ", ".join(sorted({f.rule for f in findings if f.status == 'FAIL'}))
        )
    return tables


def stage_transform(cfg, storage, manifest, tables=None):
    _log("transform: raw -> curated parquet")
    tables = tables or _read_raw(storage)
    curated = curate(tables, cfg, storage, manifest)
    for name, df in curated.items():
        _log(f"  {name:<15} {len(df):>8,} rows -> curated/")
    return curated


def stage_cohort(cfg, storage, manifest, curated=None):
    _log(f"cohort: {cfg['study']['id']} ({cfg['query']['engine']})")
    curated = curated or _read_curated(storage)
    cohort = build_cohort(curated, cfg, storage, manifest)
    for row in manifest.doc["stages"][-1]["attrition"]:
        extra = ""
        if "excluded" in row:
            extra = f"  (-{row['excluded']:,}, {row['pct_retained']}% retained)"
        _log(f"  {row['n']:>8,}  {row['step']}{extra}")
    return cohort


def stage_analyze(cfg, storage, manifest, cohort=None):
    _log("analysis: descriptive, logistic, IPTW, Cox")
    if cohort is None:
        study = cfg["study"]
        cohort = storage.read_csv(f"analytics/{study['id']}/{study['version']}/cohort.csv")
    res = analyze(cohort, cfg, storage, manifest)
    ref = res["reference_treatment"]
    _log(f"  n={res['n_analyzed']:,}  (reference = {ref})")
    rows = [("crude", "crude"), ("adjusted", "adjusted_logistic"), ("IPTW", "iptw")]
    if "direct_effect_logistic" in res:
        rows.append(("direct", "direct_effect_logistic"))
    for label, key in rows:
        v = res[key]
        note = f"  <- {v['estimand']}" if "estimand" in v else ""
        _log(f"  {label:<10} OR = {v['or']:.2f} [{v['ci_low']:.2f}, {v['ci_high']:.2f}]{note}")
    if "g_computation" in res:
        g = res["g_computation"]
        _log(f"  {'g-comp':<10} marginal OR = {g['marginal_or']:.2f}, "
             f"RD = {g['risk_difference']:+.3f}, RR = {g['risk_ratio']:.3f}")
        _log("             (conditional OR > marginal OR is non-collapsibility, not disagreement)")
    c = res["cox"]
    _log(f"  {'Cox':<10} HR = {c['hr']:.2f} [{c['ci_low']:.2f}, {c['ci_high']:.2f}] "
         f"({c['n_events']:,} events)")
    for b in res["ps_balance"]:
        _log(f"  balance {b['covariate']:<18} SMD {b['smd_unweighted']:.3f} -> {b['smd_weighted']:.3f}")
    _log("  NOTE: crude vs adjusted differ substantially - confounding by indication")
    return cohort


def main(argv=None):
    ap = argparse.ArgumentParser(description="IRIS RWE pipeline")
    ap.add_argument("stage", choices=["generate", "ingest", "validate",
                                      "transform", "cohort", "analyze", "all"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    storage = get_backend(cfg)
    manifest = RunManifest(cfg, args.run_id)
    _log(f"run_id={manifest.run_id} backend={cfg['storage']['backend']} "
         f"code={manifest.doc['pins']['code_git_sha']}")

    tables = curated = None
    if args.stage in ("generate", "all"):
        tables = stage_generate(cfg, storage, manifest)
    if args.stage in ("ingest", "all"):
        tables = stage_ingest(cfg, storage, manifest)
    if args.stage in ("validate", "all"):
        tables = stage_validate(cfg, storage, manifest, tables)
    if args.stage in ("transform", "all"):
        curated = stage_transform(cfg, storage, manifest, tables)
    cohort = None
    if args.stage in ("cohort", "all"):
        cohort = stage_cohort(cfg, storage, manifest, curated)
    if args.stage in ("analyze", "all"):
        cohort = stage_analyze(cfg, storage, manifest, cohort)

    uri = manifest.write(storage)
    _log(f"manifest -> {uri}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
