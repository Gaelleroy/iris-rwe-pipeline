"""Data quality rules.

Two tiers, and the distinction is the whole point of this layer:

STRUCTURAL rules are what any data engineer would write - nulls, duplicates,
referential integrity, parseability. Necessary, but table stakes.

CLINICAL rules encode domain knowledge about what real patient data can and
cannot look like. Treatment cannot precede diagnosis. Diagnosis cannot precede
birth. Visual acuity cannot be better than 20/10 or worse than no light
perception. Follow-up cannot extend past the data cutoff.

A pipeline that only runs the structural tier will happily produce a clean,
well-typed, internally consistent dataset that is clinically nonsense. The
clinical tier is where an epidemiologist adds value that an ETL framework
cannot supply on its own.

Each rule returns a Finding. Thresholds come from config, never hardcoded.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import pandas as pd

from src.transform.normalize import normalize_icd10, parse_dates_mixed, snellen_to_logmar

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Finding:
    rule: str
    tier: str                 # structural | clinical
    table: str
    status: str               # PASS | WARN | FAIL
    n_flagged: int
    n_total: int
    rate: float
    threshold_warn: float | None = None
    threshold_fail: float | None = None
    detail: str = ""
    examples: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _grade(rate: float, thresholds: dict | None) -> tuple[str, float | None, float | None]:
    if not thresholds:
        return PASS, None, None
    warn, fail = thresholds.get("warn"), thresholds.get("fail")
    if fail is not None and rate > fail:
        return FAIL, warn, fail
    if warn is not None and rate > warn:
        return WARN, warn, fail
    return PASS, warn, fail


def _finding(rule, tier, table, mask, thresholds, detail="", example_col=None, df=None):
    n_total = int(len(mask))
    n_flagged = int(mask.sum()) if n_total else 0
    rate = n_flagged / n_total if n_total else 0.0
    status, w, f = _grade(rate, thresholds)
    examples = []
    if n_flagged and df is not None and example_col is not None:
        examples = df.loc[mask, example_col].head(5).astype(str).tolist()
    return Finding(rule, tier, table, status, n_flagged, n_total, round(rate, 6), w, f, detail, examples)


# ---------------------------------------------------------------------------
# STRUCTURAL
# ---------------------------------------------------------------------------

def rule_missing_patient_id(tables, cfg):
    out = []
    for name, df in tables.items():
        if "patient_id" not in df.columns:
            continue
        mask = df["patient_id"].isna() | (df["patient_id"].astype(str).str.strip() == "")
        out.append(_finding(
            "missing_patient_id", "structural", name, mask,
            cfg["validation"]["thresholds"].get("missing_patient_id"),
            "patient_id is the join key for every table; a null here silently drops the record",
        ))
    return out


def rule_missing_birth_date(tables, cfg):
    df = tables["patients"]
    parsed = parse_dates_mixed(df["birth_date"])
    mask = parsed.isna()
    return [_finding(
        "missing_birth_date", "structural", "patients", mask,
        cfg["validation"]["thresholds"].get("missing_birth_date"),
        "birth_date drives age at index, an inclusion criterion and a covariate",
        example_col="patient_id", df=df,
    )]


def rule_missing_sex(tables, cfg):
    df = tables["patients"]
    s = df["sex"].astype(str).str.strip().str.upper()
    mask = ~s.isin(["M", "F"])
    return [_finding(
        "missing_sex", "structural", "patients", mask,
        cfg["validation"]["thresholds"].get("missing_sex"),
        "values outside {M,F} include blank, 'U', 'Unknown' - treated as missing, not imputed",
        example_col="sex", df=df,
    )]


def rule_duplicate_records(tables, cfg):
    keys = {
        "patients": ["patient_id"],
        "injections": ["patient_id", "medication", "injection_date", "eye"],
        "visual_acuity": ["patient_id", "measurement_date", "eye"],
        "encounters": ["encounter_id"],
    }
    out = []
    for name, key in keys.items():
        if name not in tables:
            continue
        df = tables[name]
        if not set(key).issubset(df.columns):
            continue
        mask = df.duplicated(subset=key, keep="first")
        out.append(_finding(
            "duplicate_records", "structural", name, mask,
            cfg["validation"]["thresholds"].get("duplicate_records"),
            f"exact duplicates on {key}; inflates injection counts if not resolved",
            example_col=key[0], df=df,
        ))
    return out


def rule_orphan_encounters(tables, cfg):
    known = set(tables["patients"]["patient_id"].astype(str))
    out = []
    for name in ("encounters", "diagnoses", "injections", "visual_acuity"):
        df = tables[name]
        mask = ~df["patient_id"].astype(str).isin(known)
        out.append(_finding(
            "orphan_encounters", "structural", name, mask,
            cfg["validation"]["thresholds"].get("orphan_encounters"),
            "child records with no matching patient - usually a partial extract or a join on the wrong key",
            example_col="patient_id", df=df,
        ))
    return out


def rule_unparseable_dates(tables, cfg):
    date_cols = {
        "patients": ["birth_date"],
        "encounters": ["encounter_date"],
        "diagnoses": ["diagnosis_date"],
        "injections": ["injection_date"],
        "visual_acuity": ["measurement_date"],
    }
    out = []
    for name, cols in date_cols.items():
        df = tables[name]
        for col in cols:
            raw = df[col].astype(str).str.strip()
            nonblank = raw != ""
            parsed = parse_dates_mixed(df[col])
            mask = nonblank & parsed.isna()
            out.append(_finding(
                "unparseable_dates", "structural", f"{name}.{col}", mask,
                cfg["validation"]["thresholds"].get("unparseable_dates"),
                "non-blank value that no accepted format parses - sites emit different date formats",
                example_col=col, df=df,
            ))
    return out


def rule_schema_drift(tables, cfg):
    """Detect unexpected columns against the declared contract."""
    expected = {
        "patients": {"patient_id", "birth_date", "sex", "race", "site_id"},
        "encounters": {"encounter_id", "patient_id", "encounter_date", "encounter_type", "site_id"},
        "diagnoses": {"patient_id", "diagnosis_code", "diagnosis_date", "site_id"},
        "injections": {"patient_id", "medication", "injection_date", "eye", "site_id"},
        "visual_acuity": {"patient_id", "va_snellen", "measurement_date", "eye", "site_id"},
    }
    out = []
    for name, cols in expected.items():
        actual = set(tables[name].columns)
        extra, missing = actual - cols, cols - actual
        status = FAIL if missing else (WARN if extra else PASS)
        out.append(Finding(
            "schema_drift", "structural", name, status,
            len(extra) + len(missing), len(cols), 0.0,
            detail=f"unexpected={sorted(extra)} missing={sorted(missing)}",
        ))
    return out


# ---------------------------------------------------------------------------
# CLINICAL
# ---------------------------------------------------------------------------

def rule_treatment_before_diagnosis(tables, cfg):
    """An injection recorded before the DME diagnosis is not possible clinically.

    In practice this is the single most informative check in the pipeline. A
    small rate suggests stray records; a large rate usually means date parsing
    went wrong, the wrong diagnosis was used as the anchor, or the extract
    joined on the wrong encounter.
    """
    dx = tables["diagnoses"].copy()
    dx["_d"] = parse_dates_mixed(dx["diagnosis_date"])
    # Anchor on the DISEASE diagnosis, not the earliest diagnosis of any kind.
    # Anchoring on any diagnosis makes the rule nearly inert: most patients
    # have some comorbidity coded years earlier, so almost no injection can
    # precede it and genuine extraction artifacts slip through.
    dx["_code"] = normalize_icd10(dx["diagnosis_code"])
    dme = dx[dx["_code"].isin(cfg["cohort"]["dme_icd10_prefixes"])]
    first_dx = dme.groupby("patient_id")["_d"].min()

    inj = tables["injections"].copy()
    inj["_i"] = parse_dates_mixed(inj["injection_date"])
    inj["_first_dx"] = inj["patient_id"].map(first_dx)
    mask = inj["_i"].notna() & inj["_first_dx"].notna() & (inj["_i"] < inj["_first_dx"])
    return [_finding(
        "treatment_before_diagnosis", "clinical", "injections", mask,
        cfg["validation"]["thresholds"].get("treatment_before_diagnosis"),
        "injection date precedes the earliest recorded diagnosis for that patient",
        example_col="patient_id", df=inj,
    )]


def rule_diagnosis_before_birth(tables, cfg):
    pat = tables["patients"].copy()
    pat["_b"] = parse_dates_mixed(pat["birth_date"])
    birth = pat.set_index("patient_id")["_b"]
    dx = tables["diagnoses"].copy()
    dx["_d"] = parse_dates_mixed(dx["diagnosis_date"])
    dx["_b"] = dx["patient_id"].map(birth)
    mask = dx["_d"].notna() & dx["_b"].notna() & (dx["_d"] < dx["_b"])
    return [_finding(
        "diagnosis_before_birth", "clinical", "diagnoses", mask,
        cfg["validation"]["thresholds"].get("diagnosis_before_birth"),
        "temporal impossibility; typically a two-digit-year parse (65 -> 2065)",
        example_col="patient_id", df=dx,
    )]


def rule_va_out_of_range(tables, cfg):
    """Snellen values outside the physiologic range for a clinic measurement."""
    va = tables["visual_acuity"].copy()
    lm = snellen_to_logmar(va["va_snellen"])
    mask = lm.isna() | (lm < -0.3) | (lm > 3.0)
    return [_finding(
        "va_out_of_range", "clinical", "visual_acuity", mask,
        cfg["validation"]["thresholds"].get("va_out_of_range"),
        "unparseable Snellen or logMAR outside [-0.3, 3.0] (better than 20/10 or worse than NLP)",
        example_col="va_snellen", df=va,
    )]


def rule_followup_beyond_cutoff(tables, cfg):
    cutoff = pd.Timestamp(cfg["generate"]["data_cutoff"])
    out = []
    for name, col in (("visual_acuity", "measurement_date"), ("injections", "injection_date"),
                      ("encounters", "encounter_date")):
        df = tables[name]
        parsed = parse_dates_mixed(df[col])
        mask = parsed.notna() & (parsed > cutoff)
        out.append(_finding(
            "followup_beyond_data_cutoff", "clinical", f"{name}.{col}", mask,
            cfg["validation"]["thresholds"].get("followup_beyond_data_cutoff"),
            f"activity recorded after the declared data cutoff {cutoff.date()}",
            example_col=col, df=df,
        ))
    return out


def rule_implausible_age(tables, cfg):
    pat = tables["patients"].copy()
    pat["_b"] = parse_dates_mixed(pat["birth_date"])
    ref = pd.Timestamp(cfg["generate"]["data_cutoff"])
    age = (ref - pat["_b"]).dt.days / 365.25
    mask = pat["_b"].notna() & ((age < 0) | (age > 120))
    return [_finding(
        "implausible_age", "clinical", "patients", mask,
        {"warn": 0.0, "fail": 0.001},
        "age at data cutoff outside [0, 120] years",
        example_col="patient_id", df=pat,
    )]


ALL_RULES: list[Callable] = [
    rule_missing_patient_id,
    rule_missing_birth_date,
    rule_missing_sex,
    rule_duplicate_records,
    rule_orphan_encounters,
    rule_unparseable_dates,
    rule_schema_drift,
    rule_treatment_before_diagnosis,
    rule_diagnosis_before_birth,
    rule_va_out_of_range,
    rule_followup_beyond_cutoff,
    rule_implausible_age,
]


def run_all(tables: dict[str, pd.DataFrame], cfg: dict) -> list[Finding]:
    findings: list[Finding] = []
    for rule in ALL_RULES:
        findings.extend(rule(tables, cfg))
    return findings


def summarize(findings: list[Finding]) -> dict:
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    return {
        "n_rules_evaluated": len(findings),
        "pass": counts[PASS],
        "warn": counts[WARN],
        "fail": counts[FAIL],
        "gate": FAIL if counts[FAIL] else (WARN if counts[WARN] else PASS),
    }
