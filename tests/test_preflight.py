"""Tests for the Lambda preflight handler.

boto3 is stubbed and the S3 read is monkeypatched, so these run in CI without
AWS credentials. What is under test is the decision logic: which inputs halt
the pipeline, which merely warn, and whether the completion marker fans out to
all five tables rather than being validated as if it were data.
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


def _good(table: str, n: int = 20) -> str:
    cols = sorted(handler.EXPECTED_COLUMNS[table])
    return ",".join(cols) + "\n" + "\n".join(",".join("x" for _ in cols) for _ in range(n))


@pytest.fixture
def files(monkeypatch):
    f = {f"raw/{t}/{t}.csv": _good(t) for t in handler.EXPECTED_COLUMNS}
    monkeypatch.setattr(handler, "_read_head", lambda b, k, max_bytes=1_000_000: f[k])
    return f


def _run(key="raw/_COMPLETE"):
    return handler.handler({"bucket": "b", "key": key}, None)


def test_marker_fans_out_to_every_table(files):
    """The marker means 'all extracts uploaded', not 'validate this object'.

    Validating the marker itself was the original bug: a zero-byte file with
    no table name and no .csv extension fails every check and halts the run.
    """
    r = _run()
    assert r["triggered_by_marker"] is True
    assert len(r["tables"]) == 5
    assert r["status"] == "PASS"


def test_one_bad_table_fails_the_whole_gate(files):
    files["raw/injections/injections.csv"] = "patient_id,medication,site_id\nP1,Drug A,SITE_01"
    r = _run()
    assert r["status"] == "FAIL"
    bad = [t for t in r["tables"] if t["status"] == "FAIL"]
    assert [t["table"] for t in bad] == ["injections"]


def test_schema_drift_warns_but_does_not_halt(files):
    """An added column is a site changing its export, not corruption."""
    cols = sorted(handler.EXPECTED_COLUMNS["injections"]) + ["lot_number"]
    files["raw/injections/injections.csv"] = ",".join(cols) + "\n" + "\n".join(
        ",".join("x" for _ in cols) for _ in range(20))
    r = _run()
    assert r["status"] == "PASS"
    assert "no_unexpected_columns" in r["warnings"]


def test_empty_file_halts(files):
    files["raw/patients/patients.csv"] = ""
    assert _run()["status"] == "FAIL"


def test_truncated_upload_halts(files):
    cols = sorted(handler.EXPECTED_COLUMNS["patients"])
    files["raw/patients/patients.csv"] = ",".join(cols) + "\n" + ",".join("x" for _ in cols)
    assert _run()["status"] == "FAIL"


def test_single_file_trigger_checks_only_that_file(files):
    r = _run("raw/patients/patients.csv")
    assert r["triggered_by_marker"] is False
    assert len(r["tables"]) == 1


def test_unrecognised_path_halts(files):
    files["raw/unknown/x.csv"] = _good("patients")
    r = _run("raw/unknown/x.csv")
    assert r["status"] == "FAIL"
    assert r["tables"][0]["table"] is None


def test_fingerprint_is_order_independent(files):
    a = _run()["tables"][0]["schema_fingerprint"]
    t = _run()["tables"][0]["table"]
    cols = list(reversed(sorted(handler.EXPECTED_COLUMNS[t])))
    files[f"raw/{t}/{t}.csv"] = ",".join(cols) + "\n" + "\n".join(
        ",".join("x" for _ in cols) for _ in range(20))
    assert _run()["tables"][0]["schema_fingerprint"] == a
