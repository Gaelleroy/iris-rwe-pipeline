"""Pre-flight check, run as a Lambda before any Glue compute is spent.

Deliberately cheap and deliberately first. Reading a CSV header and counting
rows costs milliseconds; a Glue job costs money and minutes. A site that
silently changed its export format should fail here, not three stages later
after the ETL has already half-written a curated table.

This is the "structural" tier of validation pulled forward - it answers "is
this file even the shape we expect?" The clinical checks stay in the Glue job
because they need the whole dataset joined together.

Returns a dict consumed by the Step Functions state machine. The `status`
field drives the Choice state: PASS continues, FAIL routes to the alert path.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import urllib.parse

import boto3

s3 = boto3.client("s3")

EXPECTED_COLUMNS = {
    "patients": {"patient_id", "birth_date", "sex", "race", "site_id"},
    "encounters": {"encounter_id", "patient_id", "encounter_date", "encounter_type", "site_id"},
    "diagnoses": {"patient_id", "diagnosis_code", "diagnosis_date", "site_id"},
    "injections": {"patient_id", "medication", "injection_date", "eye", "site_id"},
    "visual_acuity": {"patient_id", "va_snellen", "measurement_date", "eye", "site_id"},
}

# A new extract that is a fraction of the previous one usually means a
# truncated upload, not a real drop in volume. Flag rather than fail: a small
# site legitimately can send few rows.
MIN_ROWS = int(os.environ.get("MIN_ROWS", "10"))


def _table_from_key(key: str) -> str | None:
    """raw/injections/injections.csv -> injections"""
    parts = key.strip("/").split("/")
    for part in parts:
        if part in EXPECTED_COLUMNS:
            return part
    return None


def _read_head(bucket: str, key: str, max_bytes: int = 1_000_000) -> str:
    """Range-GET the first chunk. We never need the whole file here."""
    obj = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")
    return obj["Body"].read().decode("utf-8", errors="replace")


def _check_one(bucket: str, key: str) -> dict:
    """Validate a single raw CSV. Returns a per-table result block."""
    result = {"key": key, "table": None, "status": "FAIL", "checks": []}

    table = _table_from_key(key)
    result["table"] = table
    if table is None:
        result["checks"].append({
            "check": "recognised_table", "status": "FAIL",
            "detail": f"key {key} does not map to a known table"})
        return result

    if not key.lower().endswith(".csv"):
        result["checks"].append({
            "check": "file_extension", "status": "FAIL",
            "detail": "raw layer expects CSV"})
        return result

    try:
        head = _read_head(bucket, key)
    except Exception as exc:
        result["checks"].append({
            "check": "object_readable", "status": "FAIL", "detail": str(exc)[:200]})
        return result

    reader = csv.reader(io.StringIO(head))
    try:
        header = next(reader)
    except StopIteration:
        result["checks"].append({
            "check": "readable_header", "status": "FAIL", "detail": "file is empty"})
        return result

    actual = {c.strip() for c in header}
    expected = EXPECTED_COLUMNS[table]
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)

    result["schema_fingerprint"] = hashlib.sha256(
        "|".join(sorted(actual)).encode()).hexdigest()[:12]
    result["checks"].append({
        "check": "required_columns",
        "status": "FAIL" if missing else "PASS",
        "detail": f"missing={missing}"})
    # Extra columns are schema drift, not corruption - a site added a field.
    # Warn so someone looks; do not block the pipeline on it.
    result["checks"].append({
        "check": "no_unexpected_columns",
        "status": "WARN" if unexpected else "PASS",
        "detail": f"unexpected={unexpected}"})

    rows = sum(1 for _ in reader)
    truncated = len(head.encode()) >= 1_000_000
    result["rows_sampled"] = rows
    result["checks"].append({
        "check": "minimum_rows",
        "status": "PASS" if (rows >= MIN_ROWS or truncated) else "FAIL",
        "detail": f"{rows} rows in sampled prefix (min {MIN_ROWS})"})

    statuses = [c["status"] for c in result["checks"]]
    result["status"] = "FAIL" if "FAIL" in statuses else "PASS"
    result["warnings"] = [c["check"] for c in result["checks"] if c["status"] == "WARN"]
    return result


def handler(event, context):
    # Accept both a raw S3 event and a Step Functions passthrough
    if "detail" in event:                       # EventBridge
        bucket = event["detail"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(event["detail"]["object"]["key"])
    elif "Records" in event:                    # direct S3 notification
        rec = event["Records"][0]["s3"]
        bucket = rec["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["object"]["key"])
    else:                                       # manual invoke / test
        bucket = event["bucket"]
        key = event["key"]

    # The trigger is a completion marker, not a data file. Its arrival means
    # "all five extracts are uploaded" - so validate all of them, not the
    # marker itself. Validating the marker was the original bug: a zero-byte
    # object with no table name and no .csv extension fails every check.
    if key.rsplit("/", 1)[-1] in ("_COMPLETE", "_SUCCESS"):
        keys = [f"raw/{t}/{t}.csv" for t in sorted(EXPECTED_COLUMNS)]
    else:
        keys = [key]

    tables = [_check_one(bucket, k) for k in keys]
    statuses = [t["status"] for t in tables]
    out = {
        "bucket": bucket,
        "key": key,
        "triggered_by_marker": len(keys) > 1,
        "status": "FAIL" if "FAIL" in statuses else "PASS",
        "tables": tables,
        "warnings": sorted({w for t in tables for w in t.get("warnings", [])}),
    }
    print(json.dumps(out))
    return out
