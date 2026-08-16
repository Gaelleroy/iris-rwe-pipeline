"""Synthetic ophthalmology EHR generator.

Produces five relational tables across simulated clinic sites, then injects
specific, named defects. Every defect maps to a rule in src/validate/rules.py -
nothing here is decorative. The point of generating our own data rather than
using a clean synthetic dataset is that clean data cannot demonstrate a
validation layer.

Defects injected
----------------
1. Heterogeneous date formats by site (real: every EHR export differs)
2. ICD-10 casing/punctuation variance
3. Duplicate injection records
4. Treatment recorded before diagnosis (extraction artifact)
5. Missing follow-up VA, informative (sicker patients miss visits)
6. Orphan encounters with no matching patient
7. Schema drift: one site adds a column mid-year
8. Implausible VA values
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Snellen values ordered best -> worst, with their logMAR equivalents.
SNELLEN_LADDER = [
    ("20/20", 0.0), ("20/25", 0.097), ("20/30", 0.176), ("20/40", 0.301),
    ("20/50", 0.398), ("20/60", 0.477), ("20/70", 0.544), ("20/80", 0.602),
    ("20/100", 0.699), ("20/125", 0.796), ("20/150", 0.875), ("20/200", 1.0),
    ("20/250", 1.097), ("20/400", 1.301),
]
SNELLEN_BY_LOGMAR = np.array([v for _, v in SNELLEN_LADDER])
SNELLEN_LABELS = [s for s, _ in SNELLEN_LADDER]

DME_CODES = ["E11.311", "E11.321", "E11.331", "E11.341", "E11.351", "H35.81"]
COMORBID_CODES = ["E11.9", "I10", "N18.3", "E78.5", "H25.11", "H40.11X1"]
RACE = ["White", "Black or African American", "Asian", "Hispanic or Latino", "Other", "Unknown"]


def _snellen_from_logmar(logmar: float) -> str:
    idx = int(np.argmin(np.abs(SNELLEN_BY_LOGMAR - logmar)))
    return SNELLEN_LABELS[idx]


def _fmt_date(ts: pd.Timestamp, fmt: str) -> str:
    if pd.isna(ts):
        return ""
    return ts.strftime(fmt)


def _mangle_code(code: str, rng: np.random.Generator) -> str:
    """Reproduce the casing/punctuation variance seen across source systems."""
    roll = rng.random()
    if roll < 0.08:
        return code.lower()
    if roll < 0.13:
        return code.replace(".", "-")
    if roll < 0.16:
        return code.replace(".", "")
    if roll < 0.18:
        return f" {code} "
    return code


def generate(cfg: dict) -> dict[str, pd.DataFrame]:
    g = cfg["generate"]
    d = g["defects"]
    rng = np.random.default_rng(g["seed"])

    n = int(g["n_patients"])
    sites = [f"SITE_{i:02d}" for i in range(1, int(g["n_sites"]) + 1)]
    accrual_start = pd.Timestamp(g["accrual_start"])
    accrual_end = pd.Timestamp(g["accrual_end"])
    cutoff = pd.Timestamp(g["data_cutoff"])
    accrual_days = (accrual_end - accrual_start).days

    # ---- patients --------------------------------------------------------
    # Site volume is deliberately skewed (a few large centres, several small),
    # which is what real registry data looks like and what makes site a
    # meaningful covariate.
    site_weights = rng.dirichlet(np.ones(len(sites)) * 0.7)
    patient_id = np.array([f"P{100000 + i}" for i in range(n)])
    site_id = rng.choice(sites, size=n, p=site_weights)

    age_at_index = np.clip(rng.normal(64, 11, n), 21, 94)
    sex = rng.choice(["M", "F"], size=n, p=[0.52, 0.48])
    race = rng.choice(RACE, size=n, p=[0.55, 0.16, 0.08, 0.14, 0.04, 0.03])

    index_offset = rng.integers(0, accrual_days, n)
    index_date = accrual_start + pd.to_timedelta(index_offset, unit="D")
    birth_date = index_date - pd.to_timedelta((age_at_index * 365.25).astype(int), unit="D")

    # DME diagnosis lands 5-120 days before the first injection
    dx_lag = rng.integers(5, 120, n)
    diagnosis_date = index_date - pd.to_timedelta(dx_lag, unit="D")

    patients = pd.DataFrame({
        "patient_id": patient_id,
        "birth_date": birth_date,
        "sex": sex,
        "race": race,
        "site_id": site_id,
    })

    # ---- treatment assignment (confounded on purpose) --------------------
    # Baseline severity drives treatment choice: worse eyes are more likely to
    # get Drug A. This is exactly the confounding-by-indication that makes the
    # naive comparison wrong and the adjusted analysis necessary.
    baseline_logmar = np.clip(rng.gamma(shape=3.0, scale=0.19, size=n), 0.0, 1.301)
    p_drug_a = 1 / (1 + np.exp(-(-1.1 + 2.4 * baseline_logmar + 0.012 * (age_at_index - 64))))
    treatment = np.where(rng.random(n) < p_drug_a, "Drug A", "Drug B")

    # Injections in year 1: Drug A dosed slightly more frequently
    inj_mean = np.where(treatment == "Drug A", 7.4, 6.1)
    injection_count = np.clip(rng.poisson(inj_mean), 1, 14)

    # ---- true outcome model ---------------------------------------------
    # More baseline impairment -> more room to improve (ceiling effect).
    # Drug A has a modest true benefit once severity is accounted for.
    true_gain = (
        0.62 * baseline_logmar
        + 0.055 * (treatment == "Drug A")
        + 0.011 * (injection_count - 6)
        - 0.0028 * (age_at_index - 64)
        + rng.normal(0, 0.14, n)
    )
    followup_logmar = np.clip(baseline_logmar - true_gain, 0.0, 1.301)

    # ---- encounters ------------------------------------------------------
    enc_rows = []
    for i in range(n):
        n_enc = int(np.clip(rng.poisson(9), 3, 22))
        offsets = np.sort(rng.integers(-int(dx_lag[i]), 400, n_enc))
        for j, off in enumerate(offsets):
            dt = index_date[i] + pd.Timedelta(days=int(off))
            if dt > cutoff:
                continue
            enc_rows.append((
                f"{patient_id[i]}-E{j:03d}", patient_id[i], dt,
                "inpatient" if rng.random() < 0.05 else "outpatient",
                site_id[i],
            ))
    encounters = pd.DataFrame(
        enc_rows, columns=["encounter_id", "patient_id", "encounter_date", "encounter_type", "site_id"]
    )

    # ---- diagnoses -------------------------------------------------------
    dx_rows = []
    for i in range(n):
        dx_rows.append((patient_id[i], rng.choice(DME_CODES), diagnosis_date[i], site_id[i]))
        for _ in range(int(rng.integers(0, 4))):
            off = int(rng.integers(-700, 300))
            dx_rows.append((
                patient_id[i], rng.choice(COMORBID_CODES),
                index_date[i] + pd.Timedelta(days=off), site_id[i],
            ))
    diagnoses = pd.DataFrame(
        dx_rows, columns=["patient_id", "diagnosis_code", "diagnosis_date", "site_id"]
    )

    # ---- injections ------------------------------------------------------
    inj_rows = []
    for i in range(n):
        eye = rng.choice(["OD", "OS"])
        # roughly monthly loading then extended dosing
        gaps = np.cumsum(np.concatenate([[0], rng.integers(28, 70, injection_count[i] - 1)]))
        for gap in gaps:
            dt = index_date[i] + pd.Timedelta(days=int(gap))
            if dt > cutoff:
                continue
            inj_rows.append((patient_id[i], treatment[i], dt, eye, site_id[i]))
    injections = pd.DataFrame(
        inj_rows, columns=["patient_id", "medication", "injection_date", "eye", "site_id"]
    )

    # ---- visual acuity ---------------------------------------------------
    va_rows = []
    # informative missingness: worse baseline -> more likely to miss follow-up
    p_missing = np.clip(
        d["missing_followup_va_rate"] * (0.4 + 1.6 * baseline_logmar), 0, 0.45
    )
    miss_followup = rng.random(n) < p_missing
    for i in range(n):
        eye = rng.choice(["OD", "OS"])
        base_off = int(rng.integers(-25, 1))
        va_rows.append((
            patient_id[i], _snellen_from_logmar(baseline_logmar[i]),
            index_date[i] + pd.Timedelta(days=base_off), eye, site_id[i],
        ))
        # interim measurements
        for off in rng.integers(30, 330, int(rng.integers(1, 4))):
            dt = index_date[i] + pd.Timedelta(days=int(off))
            if dt <= cutoff:
                interim = np.clip(
                    baseline_logmar[i] - true_gain[i] * (off / 365) + rng.normal(0, 0.08),
                    0, 1.301,
                )
                va_rows.append((patient_id[i], _snellen_from_logmar(interim), dt, eye, site_id[i]))
        if not miss_followup[i]:
            off = int(365 + rng.integers(-60, 61))
            dt = index_date[i] + pd.Timedelta(days=off)
            if dt <= cutoff:
                va_rows.append((
                    patient_id[i], _snellen_from_logmar(followup_logmar[i]), dt, eye, site_id[i]
                ))
    visual_acuity = pd.DataFrame(
        va_rows, columns=["patient_id", "va_snellen", "measurement_date", "eye", "site_id"]
    )

    tables = {
        "patients": patients,
        "encounters": encounters,
        "diagnoses": diagnoses,
        "injections": injections,
        "visual_acuity": visual_acuity,
    }
    tables = _add_ineligible(tables, cfg, rng, sites, site_weights,
                            accrual_start, accrual_days, cutoff)
    tables = _inject_defects(tables, cfg, rng)
    return tables


def _add_ineligible(tables, cfg, rng, sites, site_weights, accrual_start, accrual_days, cutoff):
    """Add patients who appear in the feed but do not qualify for the study.

    Three groups, each excluded by a different criterion, so the attrition
    ladder exercises the cohort SQL rather than confirming that everyone we
    generated was eligible:
      - no DME diagnosis at all
      - DME diagnosed but never treated
      - DME treated with a drug outside the study exposure definition
    """
    g = cfg["generate"]
    groups = [
        ("N", int(g["n_no_dme_patients"]), False, None),
        ("U", int(g["n_untreated_dme_patients"]), True, None),
        ("O", int(g["n_other_drug_patients"]), True, "Drug C"),
    ]
    pat_rows, dx_rows, enc_rows, inj_rows, va_rows = [], [], [], [], []
    counter = 500000

    for tag, count, has_dme, drug in groups:
        for _ in range(count):
            pid = f"P{counter}"
            counter += 1
            site = rng.choice(sites, p=site_weights)
            age = float(np.clip(rng.normal(63, 12), 21, 94))
            anchor = accrual_start + pd.Timedelta(days=int(rng.integers(0, accrual_days)))
            birth = anchor - pd.Timedelta(days=int(age * 365.25))
            pat_rows.append((pid, birth, rng.choice(["M", "F"]), rng.choice(RACE), site))

            for _ in range(int(rng.integers(1, 4))):
                dx_rows.append((pid, rng.choice(COMORBID_CODES),
                                anchor + pd.Timedelta(days=int(rng.integers(-500, 200))), site))
            if has_dme:
                dx_rows.append((pid, rng.choice(DME_CODES), anchor, site))

            for j in range(int(np.clip(rng.poisson(5), 1, 14))):
                dt = anchor + pd.Timedelta(days=int(rng.integers(-200, 400)))
                if dt <= cutoff:
                    enc_rows.append((f"{pid}-E{j:03d}", pid, dt, "outpatient", site))

            if drug:
                eye = rng.choice(["OD", "OS"])
                for gap in np.cumsum(np.concatenate([[0], rng.integers(28, 70, int(rng.integers(2, 9)))])):
                    dt = anchor + pd.Timedelta(days=int(gap))
                    if dt <= cutoff:
                        inj_rows.append((pid, drug, dt, eye, site))

            eye = rng.choice(["OD", "OS"])
            for off in rng.integers(-20, 380, int(rng.integers(1, 4))):
                dt = anchor + pd.Timedelta(days=int(off))
                if dt <= cutoff:
                    lm = float(np.clip(rng.gamma(2.4, 0.16), 0, 1.301))
                    va_rows.append((pid, _snellen_from_logmar(lm), dt, eye, site))

    tables["patients"] = pd.concat([tables["patients"], pd.DataFrame(
        pat_rows, columns=["patient_id", "birth_date", "sex", "race", "site_id"])],
        ignore_index=True)
    tables["diagnoses"] = pd.concat([tables["diagnoses"], pd.DataFrame(
        dx_rows, columns=["patient_id", "diagnosis_code", "diagnosis_date", "site_id"])],
        ignore_index=True)
    tables["encounters"] = pd.concat([tables["encounters"], pd.DataFrame(
        enc_rows, columns=["encounter_id", "patient_id", "encounter_date", "encounter_type", "site_id"])],
        ignore_index=True)
    if inj_rows:
        tables["injections"] = pd.concat([tables["injections"], pd.DataFrame(
            inj_rows, columns=["patient_id", "medication", "injection_date", "eye", "site_id"])],
            ignore_index=True)
    tables["visual_acuity"] = pd.concat([tables["visual_acuity"], pd.DataFrame(
        va_rows, columns=["patient_id", "va_snellen", "measurement_date", "eye", "site_id"])],
        ignore_index=True)
    return tables


def _inject_defects(tables: dict, cfg: dict, rng) -> dict:
    d = cfg["generate"]["defects"]
    fmt_by_site = d["date_format_by_site"]

    # 1. duplicate injections
    inj = tables["injections"]
    k = int(len(inj) * d["duplicate_injection_rate"])
    if k:
        dupes = inj.sample(k, random_state=int(rng.integers(1e6)))
        tables["injections"] = pd.concat([inj, dupes], ignore_index=True)

    # 2. treatment before diagnosis (extraction artifact)
    inj = tables["injections"]
    k = int(len(inj) * d["treatment_before_diagnosis_rate"])
    if k:
        idx = rng.choice(inj.index, size=k, replace=False)
        inj.loc[idx, "injection_date"] = inj.loc[idx, "injection_date"] - pd.Timedelta(days=400)
        tables["injections"] = inj

    # 3. orphan encounters
    enc = tables["encounters"]
    k = int(len(enc) * d["orphan_encounter_rate"])
    if k:
        orphans = enc.sample(k, random_state=int(rng.integers(1e6))).copy()
        orphans["patient_id"] = ["P" + str(999000 + i) for i in range(k)]
        orphans["encounter_id"] = [f"ORPH-{i:05d}" for i in range(k)]
        tables["encounters"] = pd.concat([enc, orphans], ignore_index=True)

    # 4. missing sex
    pat = tables["patients"]
    k = int(len(pat) * d["missing_sex_rate"])
    if k:
        idx = rng.choice(pat.index, size=k, replace=False)
        pat.loc[idx, "sex"] = rng.choice(["", "U", "Unknown"], size=k)
        tables["patients"] = pat

    # 5. implausible VA
    va = tables["visual_acuity"]
    k = int(len(va) * d["implausible_va_rate"])
    if k:
        idx = rng.choice(va.index, size=k, replace=False)
        va.loc[idx, "va_snellen"] = rng.choice(["20/0", "0/20", "-1", "20/2000"], size=k)
        tables["visual_acuity"] = va

    # 6. ICD-10 casing/punctuation variance
    dx = tables["diagnoses"]
    dx["diagnosis_code"] = [_mangle_code(c, rng) for c in dx["diagnosis_code"]]
    tables["diagnoses"] = dx

    # 7. per-site date formats -> everything becomes a string
    date_cols = {
        "patients": ["birth_date"],
        "encounters": ["encounter_date"],
        "diagnoses": ["diagnosis_date"],
        "injections": ["injection_date"],
        "visual_acuity": ["measurement_date"],
    }
    for name, cols in date_cols.items():
        df = tables[name]
        for col in cols:
            df[col] = [
                _fmt_date(ts, fmt_by_site.get(site, "%Y-%m-%d"))
                for ts, site in zip(pd.to_datetime(df[col], errors="coerce"), df["site_id"])
            ]
        tables[name] = df

    # 8. schema drift: one site starts emitting an extra column mid-year
    drift_site = d["schema_drift_site"]
    drift_from = pd.Timestamp(d["schema_drift_date"])
    inj = tables["injections"]
    parsed = pd.to_datetime(inj["injection_date"], errors="coerce", format="mixed")
    mask = (inj["site_id"] == drift_site) & (parsed >= drift_from)
    lots = "LOT-" + pd.Series(rng.integers(1000, 9999, len(inj))).astype(str)
    inj["lot_number"] = np.where(mask, lots, None)
    tables["injections"] = inj

    return tables
