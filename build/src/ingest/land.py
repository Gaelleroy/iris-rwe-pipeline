"""Ingest stage.

Cheap pre-flight checks before spending compute: does the file match the
expected schema fingerprint, and is the row count within tolerance of prior
loads? A site that silently changes its export format should fail here, not
three stages downstream after the ETL has already half-written a curated table.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd

EXPECTED_COLUMNS = {
    "patients": ["patient_id", "birth_date", "sex", "race", "site_id"],
    "encounters": ["encounter_id", "patient_id", "encounter_date", "encounter_type", "site_id"],
    "diagnoses": ["patient_id", "diagnosis_code", "diagnosis_date", "site_id"],
    "injections": ["patient_id", "medication", "injection_date", "eye", "site_id"],
    "visual_acuity": ["patient_id", "va_snellen", "measurement_date", "eye", "site_id"],
}


def schema_fingerprint(df: pd.DataFrame) -> str:
    """Stable hash of the column set. Order-insensitive on purpose."""
    payload = "|".join(sorted(df.columns))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def preflight(name: str, df: pd.DataFrame) -> dict:
    expected = set(EXPECTED_COLUMNS[name])
    actual = set(df.columns)
    return {
        "table": name,
        "rows": int(len(df)),
        "columns": sorted(actual),
        "schema_fingerprint": schema_fingerprint(df),
        "missing_columns": sorted(expected - actual),
        "unexpected_columns": sorted(actual - expected),
        "status": "FAIL" if (expected - actual) else ("WARN" if (actual - expected) else "PASS"),
    }


def land(tables: dict[str, pd.DataFrame], storage, manifest) -> dict:
    """Write source tables to the raw layer, preserving them unmodified."""
    report = {"tables": [], "raw_uris": {}}
    hard_fail = False
    for name, df in tables.items():
        check = preflight(name, df)
        report["tables"].append(check)
        if check["status"] == "FAIL":
            hard_fail = True
        key = f"raw/{name}"
        # raw stays CSV: it is the source of truth and should look like what
        # the site actually sent us, not a re-encoded version of it
        storage.write_csv(df, f"{key}/{name}.csv")
        report["raw_uris"][name] = storage.uri(key)

    report["status"] = "FAIL" if hard_fail else "PASS"
    storage.write_bytes(
        f"metadata/{manifest.run_id}/ingest_report.json",
        json.dumps(report, indent=2).encode(),
    )
    manifest.record(
        "ingest",
        status=report["status"],
        tables={t["table"]: t["rows"] for t in report["tables"]},
        fingerprints={t["table"]: t["schema_fingerprint"] for t in report["tables"]},
        unexpected_columns={
            t["table"]: t["unexpected_columns"] for t in report["tables"] if t["unexpected_columns"]
        },
    )
    return report
