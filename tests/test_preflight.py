"""Tests for the Lambda preflight handler.

boto3 is stubbed: the handler's S3 read is monkeypatched, so these run in CI
without AWS credentials. What is being tested is the decision logic — which
inputs halt the pipeline and which merely warn.
"""
from __future__ import annotations

import sys
import types

import pytest

if "boto3" not in sys.modules:
    _fake = types.ModuleType("boto3")
    _fake.client = lambda *a, **k: None
    sys.modules["boto3"] = _fake

sys.path.insert(0, "infra/lambda_preflight")
import handler  # noqa: E402

HEADER = "patient_id,medication,injection_date,eye,site_id"
ROWS = "\n".join(f"P{i},Drug A,2024-01-01,OD,SITE_01" for i in range(20))


@pytest.fixture(autouse=True)
def _stub_s3(monkeypatch):
    monkeypatch.setattr(handler, "_read_head", lambda b, k, max_bytes=1_000_000: FILES[k])


FILES = {
    "raw/injections/ok.csv": f"{HEADER}\n{ROWS}",
    "raw/injections/missing.csv": "patient_id,medication,site_id\nP1,Drug A,SITE_01",
    "raw/injections/drift.csv": f"{HEADER},lot_number\n" + "\n".join(
        f"P{i},Drug A,2024-01-01,OD,SITE_01,LOT-1{i}" for i in range(20)),
    "raw/injections/empty.csv": "",
    "raw/injections/thin.csv": f"{HEADER}\nP1,Drug A,2024-01-01,OD,SITE_01",
    "raw/unknown/x.csv": f"{HEADER}\n{ROWS}",
}


def _run(key):
    return handler.handler({"bucket": "b", "key": key}, None)


def test_well_formed_file_passes():
    assert _run("raw/injections/ok.csv")["status"] == "PASS"


def test_missing_required_column_halts():
    r = _run("raw/injections/missing.csv")
    assert r["status"] == "FAIL"
    assert any(c["check"] == "required_columns" and c["status"] == "FAIL" for c in r["checks"])


def test_schema_drift_warns_but_does_not_halt():
    """An added column is the site changing its export, not corruption.

    Blocking on it would halt the pipeline every time a site adds a field.
    """
    r = _run("raw/injections/drift.csv")
    assert r["status"] == "PASS"
    assert "no_unexpected_columns" in r["warnings"]


def test_empty_file_halts():
    assert _run("raw/injections/empty.csv")["status"] == "FAIL"


def test_truncated_upload_halts():
    assert _run("raw/injections/thin.csv")["status"] == "FAIL"


def test_unrecognised_path_halts():
    r = _run("raw/unknown/x.csv")
    assert r["status"] == "FAIL"
    assert r["table"] is None


def test_fingerprint_is_order_independent():
    a = _run("raw/injections/ok.csv")["schema_fingerprint"]
    FILES["raw/injections/reordered.csv"] = (
        "site_id,eye,injection_date,medication,patient_id\n"
        + "\n".join("SITE_01,OD,2024-01-01,Drug A,P1" for _ in range(20))
    )
    b = _run("raw/injections/reordered.csv")["schema_fingerprint"]
    assert a == b
