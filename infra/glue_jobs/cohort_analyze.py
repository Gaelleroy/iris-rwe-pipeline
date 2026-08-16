"""Glue Python Shell job: cohort construction + statistical analysis.

Renders the cohort SQL itself from the config rather than receiving it as an
argument. That is what makes event-driven triggering possible: an EventBridge
event carries only a bucket and a key, so anything the pipeline needs beyond
that has to be derived inside the pipeline, not passed in by whoever started
it. The previous design required the caller to supply pre-rendered SQL, which
only worked when a human ran trigger.sh.

Runs the same src/ code as the local pipeline. The analysis here is Python
(statsmodels + lifelines) because R has no serverless runtime on AWS; the R
implementation in r/analysis.R produces the same estimands and stays as the
interactive reference.
"""
from __future__ import annotations

import json
import sys
import zipfile

import boto3
from awsglue.utils import getResolvedOptions

_args = getResolvedOptions(sys.argv, ["config_s3_uri", "bucket", "run_id"])
_s3 = boto3.client("s3")
_s3.download_file(_args["bucket"], "code/src.zip", "/tmp/src.zip")
with zipfile.ZipFile("/tmp/src.zip") as zf:
    zf.extractall("/tmp")
sys.path.insert(0, "/tmp")

import pandas as pd  # noqa: E402

from src.analyze.run import analyze  # noqa: E402
from src.cohort.build import build as build_cohort  # noqa: E402
from src.manifest import RunManifest, load_config  # noqa: E402
from src.storage.backends import get_backend  # noqa: E402

RAW_TABLES = ["patients", "encounters", "diagnoses", "injections", "visual_acuity"]


def main():
    cfg_bucket, _, cfg_key = _args["config_s3_uri"].replace("s3://", "").partition("/")
    _s3.download_file(cfg_bucket, cfg_key, "/tmp/study.yaml")

    cfg = load_config("/tmp/study.yaml")
    cfg["storage"]["backend"] = "s3"
    cfg["storage"]["s3"]["bucket"] = _args["bucket"]
    cfg["query"]["engine"] = "athena"
    cfg["query"]["athena"]["output_location"] = f"s3://{_args['bucket']}/athena-results/"

    storage = get_backend(cfg)
    manifest = RunManifest(cfg, _args["run_id"])

    # Curated tables are needed for the attrition ladder, which is computed in
    # pandas rather than SQL - a CONSORT count at each criterion is clearer as
    # explicit steps than as a stack of nested subqueries.
    curated = {}
    for name in RAW_TABLES:
        df = storage.read_parquet(f"curated/{name}")
        for col in df.columns:
            if col.endswith("_date"):
                df[col] = pd.to_datetime(df[col], errors="coerce")
        curated[name] = df

    cohort = build_cohort(curated, cfg, storage, manifest)
    print(f"cohort n={len(cohort)}")
    for row in manifest.doc["stages"][-1]["attrition"]:
        print(f"  {row['n']:>8,}  {row['step']}")

    if len(cohort) == 0:
        manifest.write(storage)
        raise SystemExit("cohort is empty - nothing to analyse")

    results = analyze(cohort, cfg, storage, manifest)
    print(json.dumps({
        "n": results["n_analyzed"],
        "crude_or": round(results["crude"]["or"], 3),
        "adjusted_or": round(results["adjusted_logistic"]["or"], 3),
        "iptw_or": round(results["iptw"]["or"], 3),
        "cox_hr": round(results["cox"]["hr"], 3),
    }))

    manifest.write(storage)


if __name__ == "__main__":
    main()
