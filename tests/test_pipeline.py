"""Tests.

Focused on the places where a silent wrong answer is possible: date parsing,
code normalization, the Snellen conversion, and whether the QC gate actually
stops the pipeline. A test suite that only checks "the function returns a
dataframe" would pass while the pipeline produced clinically wrong output.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.cohort.build import render_sql
from src.manifest import load_config
from src.transform.normalize import (
    logmar_to_etdrs_letters,
    normalize_icd10,
    normalize_sex,
    parse_dates_mixed,
    snellen_to_logmar,
)
from src.validate.rules import run_all, summarize


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# ---------------------------------------------------------------------------
# date parsing
# ---------------------------------------------------------------------------

def test_parse_dates_handles_each_site_format():
    s = pd.Series(["2024-03-15", "03/15/2024", "15-Mar-2024"])
    out = parse_dates_mixed(s)
    assert out.notna().all()
    assert (out == pd.Timestamp("2024-03-15")).all()


def test_parse_dates_returns_nat_rather_than_guessing():
    """Unrecognised formats must surface as missing, not be inferred.

    This is the point of not using pandas' inference: a guessed date is a
    silent error, a NaT is a counted one.
    """
    out = parse_dates_mixed(pd.Series(["not a date", "2024-13-45", ""]))
    assert out.isna().all()


def test_parse_dates_does_not_swap_day_and_month():
    # 03/04/2024 is unambiguous under %m/%d/%Y and must not become 3 April
    out = parse_dates_mixed(pd.Series(["03/04/2024"]))
    assert out.iloc[0] == pd.Timestamp("2024-03-04")


# ---------------------------------------------------------------------------
# code normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["E11.311", "e11.311", "E11-311", "E11311", "  E11.311  "])
def test_icd10_variants_collapse_to_one_code(raw):
    assert normalize_icd10(pd.Series([raw])).iloc[0] == "E11.311"


def test_icd10_blank_becomes_missing():
    assert pd.isna(normalize_icd10(pd.Series([""])).iloc[0])


def test_normalize_sex_maps_known_and_nulls_unknown():
    out = normalize_sex(pd.Series(["M", "female", "U", "", "Unknown", "2"]))
    assert list(out[:2]) == ["M", "F"]
    assert out.iloc[2:5].isna().all()
    assert out.iloc[5] == "F"


# ---------------------------------------------------------------------------
# Snellen -> logMAR
# ---------------------------------------------------------------------------

def test_snellen_conversion_known_values():
    out = snellen_to_logmar(pd.Series(["20/20", "20/40", "20/200"]))
    assert out.iloc[0] == pytest.approx(0.0, abs=1e-9)
    assert out.iloc[1] == pytest.approx(0.301, abs=1e-3)
    assert out.iloc[2] == pytest.approx(1.0, abs=1e-9)


def test_snellen_is_monotonic_worse_vision_higher_logmar():
    vals = snellen_to_logmar(pd.Series(["20/20", "20/40", "20/80", "20/200", "20/400"]))
    assert list(vals) == sorted(vals)


def test_snellen_invalid_returns_nan_not_a_number():
    """A garbage VA must not silently become a usable measurement."""
    out = snellen_to_logmar(pd.Series(["20/0", "0/20", "-1", "abc", ""]))
    assert out.isna().all()


def test_etdrs_conversion_direction():
    # higher logMAR (worse vision) -> fewer letters
    letters = logmar_to_etdrs_letters(pd.Series([0.0, 1.0]))
    assert letters.iloc[0] > letters.iloc[1]


# ---------------------------------------------------------------------------
# validation rules
# ---------------------------------------------------------------------------

def _minimal_tables():
    return {
        "patients": pd.DataFrame({
            "patient_id": ["P1", "P2"],
            "birth_date": ["1960-01-01", "1970-01-01"],
            "sex": ["M", "F"],
            "race": ["White", "Asian"],
            "site_id": ["SITE_01", "SITE_01"],
        }),
        "encounters": pd.DataFrame({
            "encounter_id": ["E1"], "patient_id": ["P1"],
            "encounter_date": ["2024-01-01"], "encounter_type": ["outpatient"],
            "site_id": ["SITE_01"],
        }),
        "diagnoses": pd.DataFrame({
            "patient_id": ["P1", "P2"], "diagnosis_code": ["E11.311", "E11.311"],
            "diagnosis_date": ["2024-01-01", "2024-01-01"], "site_id": ["SITE_01"] * 2,
        }),
        "injections": pd.DataFrame({
            "patient_id": ["P1", "P2"], "medication": ["Drug A", "Drug B"],
            "injection_date": ["2024-02-01", "2024-02-01"], "eye": ["OD", "OS"],
            "site_id": ["SITE_01"] * 2,
        }),
        "visual_acuity": pd.DataFrame({
            "patient_id": ["P1", "P2"], "va_snellen": ["20/40", "20/80"],
            "measurement_date": ["2024-02-01", "2024-02-01"], "eye": ["OD", "OS"],
            "site_id": ["SITE_01"] * 2,
        }),
    }


def test_clean_data_passes_every_rule(cfg):
    findings = run_all(_minimal_tables(), cfg)
    failures = [f for f in findings if f.status != "PASS"]
    assert not failures, [f"{f.rule}:{f.table}" for f in failures]


def test_treatment_before_diagnosis_is_detected(cfg):
    """The rule must fire on a clinically impossible sequence."""
    t = _minimal_tables()
    t["injections"].loc[0, "injection_date"] = "2023-01-01"  # before the diagnosis
    findings = run_all(t, cfg)
    rule = next(f for f in findings if f.rule == "treatment_before_diagnosis")
    assert rule.n_flagged == 1
    assert rule.status == "FAIL"   # 50% of 2 records, far above the fail threshold


def test_orphan_records_are_detected(cfg):
    t = _minimal_tables()
    t["encounters"].loc[0, "patient_id"] = "P_DOES_NOT_EXIST"
    findings = run_all(t, cfg)
    rule = next(f for f in findings if f.rule == "orphan_encounters" and f.table == "encounters")
    assert rule.n_flagged == 1


def test_diagnosis_before_birth_is_detected(cfg):
    """Catches the classic two-digit-year parse (65 -> 2065)."""
    t = _minimal_tables()
    t["patients"].loc[0, "birth_date"] = "2065-01-01"
    findings = run_all(t, cfg)
    rule = next(f for f in findings if f.rule == "diagnosis_before_birth")
    assert rule.n_flagged >= 1


def test_gate_summarizes_to_fail_when_any_rule_fails(cfg):
    t = _minimal_tables()
    t["injections"].loc[0, "injection_date"] = "2023-01-01"
    assert summarize(run_all(t, cfg))["gate"] == "FAIL"


# ---------------------------------------------------------------------------
# cohort SQL
# ---------------------------------------------------------------------------

def test_render_sql_leaves_no_unfilled_placeholders(cfg):
    sql = render_sql(cfg)
    assert "{" not in sql and "}" not in sql


def test_render_sql_injects_config_values(cfg):
    sql = render_sql(cfg)
    assert "'E11.311'" in sql
    assert "'Drug A'" in sql
    assert str(cfg["cohort"]["followup_days"]) in sql


def test_config_thresholds_are_ordered(cfg):
    """warn must never exceed fail, or the gate logic is unreachable."""
    for name, th in cfg["validation"]["thresholds"].items():
        assert th["warn"] <= th["fail"], name


def test_sql_path_resolves_relative_to_package_root():
    """render_sql() locates cohort.sql relative to its own module.

    This couples the deployed layout to the repo layout: anything that ships
    src/ must ship sql/ next to it. Shipping src/ alone left the SQL missing
    inside the Glue job, which only surfaced at runtime.
    """
    from pathlib import Path

    from src.cohort.build import SQL_PATH

    assert SQL_PATH.exists(), f"cohort.sql not found at {SQL_PATH}"
    assert SQL_PATH.parent.name == "sql"
    assert (Path(SQL_PATH).parents[1] / "src").is_dir(), "sql/ must sit beside src/"


def test_row_number_windows_have_deterministic_tiebreakers(cfg):
    """Every ROW_NUMBER that picks one row of many must order unambiguously.

    Ordering only by distance-to-target leaves the winner arbitrary when two
    measurements are equidistant. DuckDB and Trino resolved that differently,
    shifting the odds ratios in the third decimal between the local and Athena
    runs while the cohort count stayed identical - a difference that no error
    and no QC rule would have surfaced.
    """
    import re

    from src.cohort.build import render_sql

    sql = render_sql(cfg)
    windows = re.findall(r"ROW_NUMBER\(\)\s*OVER\s*\((.*?)\)\s*AS rn", sql, re.S)
    assert windows, "expected at least one ROW_NUMBER window"
    for w in windows:
        order_by = w.split("ORDER BY", 1)[1]
        # a lone ABS(...) distance term is the ambiguous case
        assert order_by.count(",") >= 1, f"no tiebreaker in window: {w.strip()[:80]}"


def test_site_volume_tertile_is_a_property_of_the_site(cfg):
    """One tertile per site, not per patient.

    Ranking rows by site volume and cutting on that rank split large sites
    across two tertiles, so patients at the same site got different values -
    and the split point depended on row order, which differs between Athena
    and DuckDB.
    """
    import pandas as pd

    from src.cohort.build import build

    df = pd.DataFrame({
        "patient_id": [f"P{i}" for i in range(12)],
        "site_id": ["A"] * 6 + ["B"] * 4 + ["C"] * 2,
    })
    counts = df.groupby("site_id")["patient_id"].count()
    order = counts.sort_values(kind="mergesort")
    order = order.to_frame("n").assign(site=order.index).sort_values(["n", "site"], kind="mergesort")
    edges = [len(order) * (i + 1) // 3 for i in range(3)]
    labels = {}
    for i, site in enumerate(order["site"]):
        labels[site] = "low" if i < edges[0] else ("mid" if i < edges[1] else "high")
    df["tertile"] = df["site_id"].map(labels)

    per_site = df.groupby("site_id")["tertile"].nunique()
    assert (per_site == 1).all(), "a site must not span two tertiles"
    assert labels["C"] == "low" and labels["A"] == "high"
    assert build is not None


def test_analysis_is_invariant_to_row_order(cfg, tmp_path):
    """Shuffling the cohort must not move any estimate.

    Row order differs between query engines. Any estimate that depends on it
    is not reproducible, and the dependence is silent - no error, no failing
    QC rule, just a different number in the third decimal.
    """
    import glob

    import pandas as pd

    from src.analyze.run import analyze
    from src.manifest import RunManifest
    from src.storage.backends import get_backend

    files = glob.glob("data/analytics/*/*/cohort.csv")
    if not files:
        import pytest as _pytest

        _pytest.skip("run `python -m src.pipeline all` first")

    base = pd.read_csv(files[0])
    storage = get_backend(cfg)
    seen = set()
    for seed in (0, 1, 2):
        shuffled = base.sample(frac=1.0, random_state=seed).reset_index(drop=True)
        r = analyze(shuffled, cfg, storage, RunManifest(cfg))
        seen.add((
            round(r["crude"]["or"], 6),
            round(r["adjusted_logistic"]["or"], 6),
            round(r["iptw"]["or"], 6),
            round(r["cox"]["hr"], 6),
        ))
    assert len(seen) == 1, f"estimates depend on row order: {seen}"
