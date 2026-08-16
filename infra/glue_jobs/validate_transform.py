"""Glue Python Shell job: validate + transform.

Runs the *same* validation rules and transformation code as the local
pipeline. That is the point - there is no second implementation to drift out
of sync. `src` is shipped as a zip via --extra-py-files and imported here.

A Python Shell job (not a Spark job) is the right size for this data. Spark
would cost more and start slower for a few million rows; the honest answer at
registry scale is a Glue Spark job or Databricks, and this module is
structured so that swap is a change of runtime rather than of logic.

Exits non-zero when the QC gate fails, which is how Step Functions learns to
route to the alert branch rather than continuing to the cohort build.
"""
from __future__ import annotations

import json
import sys

from awsglue.utils import getResolvedOptions  # provided by the Glue runtime

sys.path.insert(0, "/tmp")

from src.ingest.land import land  # noqa: E402
from src.manifest import RunManifest, load_config  # noqa: E402
from src.storage.backends import get_backend  # noqa: E402
from src.transform.curate import curate  # noqa: E402
from src.validate.rules import run_all, summarize  # noqa: E402

RAW_TABLES = ["patients", "encounters", "diagnoses", "injections", "visual_acuity"]


def main():
    args = getResolvedOptions(sys.argv, ["config_s3_uri", "bucket", "run_id"])

    # Pull the config from S3 so the job runs the same parameters the repo
    # declares - the job does not carry its own copy.
    import boto3

    s3 = boto3.client("s3")
    cfg_bucket, _, cfg_key = args["config_s3_uri"].replace("s3://", "").partition("/")
    s3.download_file(cfg_bucket, cfg_key, "/tmp/study.yaml")

    cfg = load_config("/tmp/study.yaml")
    cfg["storage"]["backend"] = "s3"
    cfg["storage"]["s3"]["bucket"] = args["bucket"]

    storage = get_backend(cfg)
    manifest = RunManifest(cfg, args["run_id"])

    tables = {
        name: storage.read_csv(f"raw/{name}/{name}.csv", dtype=str, keep_default_na=False)
        for name in RAW_TABLES
    }

    ingest_report = land(tables, storage, manifest)
    if ingest_report["status"] == "FAIL":
        manifest.write(storage)
        raise SystemExit("ingest FAILED: required columns missing")

    findings = run_all(tables, cfg)
    summary = summarize(findings)
    payload = {"summary": summary, "findings": [f.to_dict() for f in findings]}
    storage.write_bytes(
        f"metadata/{manifest.run_id}/validation_report.json",
        json.dumps(payload, indent=2).encode(),
    )
    manifest.record(
        "validate", **summary,
        failures=[f.rule for f in findings if f.status == "FAIL"],
    )

    for f in findings:
        if f.status != "PASS":
            print(f"{f.status} {f.rule} {f.table} {f.n_flagged}/{f.n_total} ({f.rate:.3%})")

    if summary["gate"] == "FAIL":
        manifest.write(storage)
        failing = sorted({f.rule for f in findings if f.status == "FAIL"})
        # Non-zero exit is the signal Step Functions branches on. The curated
        # layer is deliberately not written: a partial cohort that someone
        # then analyses in good faith is the failure mode this prevents.
        raise SystemExit(f"QC GATE FAILED - curated layer not written: {failing}")

    curate(tables, cfg, storage, manifest)
    manifest.write(storage)
    print(json.dumps({"run_id": manifest.run_id, "gate": summary["gate"]}))


if __name__ == "__main__":
    main()
