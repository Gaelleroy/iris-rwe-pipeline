"""Transform stage: raw -> curated.

Normalizes types and vocabularies, resolves duplicates, and writes partitioned
Parquet. Raw is never modified; if a decision here turns out to be wrong we can
always rebuild curated from raw and the git history explains why the rule
changed.
"""
from __future__ import annotations

import json

import pandas as pd

from src.transform.normalize import (
    normalize_icd10,
    normalize_sex,
    parse_dates_mixed,
    snellen_to_logmar,
)


def curate(tables: dict[str, pd.DataFrame], cfg: dict, storage, manifest) -> dict:
    stats = {}
    out = {}

    # -- patients ----------------------------------------------------------
    pat = tables["patients"].copy()
    pat["birth_date"] = parse_dates_mixed(pat["birth_date"])
    pat["sex"] = normalize_sex(pat["sex"])
    before = len(pat)
    pat = pat.drop_duplicates(subset=["patient_id"], keep="first")
    stats["patients"] = {"in": before, "out": len(pat), "dropped_duplicates": before - len(pat)}
    out["patients"] = pat

    known_ids = set(pat["patient_id"].astype(str))

    # -- diagnoses ---------------------------------------------------------
    dx = tables["diagnoses"].copy()
    dx["diagnosis_code"] = normalize_icd10(dx["diagnosis_code"])
    dx["diagnosis_date"] = parse_dates_mixed(dx["diagnosis_date"])
    before = len(dx)
    dx = dx[dx["patient_id"].astype(str).isin(known_ids)]
    dx = dx.dropna(subset=["diagnosis_date", "diagnosis_code"])
    dx = dx.drop_duplicates(subset=["patient_id", "diagnosis_code", "diagnosis_date"])
    stats["diagnoses"] = {"in": before, "out": len(dx), "dropped": before - len(dx)}
    out["diagnoses"] = dx

    # -- encounters --------------------------------------------------------
    enc = tables["encounters"].copy()
    enc["encounter_date"] = parse_dates_mixed(enc["encounter_date"])
    before = len(enc)
    # orphans are quarantined, not silently dropped: they are written out so
    # someone can go ask the site what happened
    orphans = enc[~enc["patient_id"].astype(str).isin(known_ids)]
    enc = enc[enc["patient_id"].astype(str).isin(known_ids)]
    enc = enc.dropna(subset=["encounter_date"]).drop_duplicates(subset=["encounter_id"])
    stats["encounters"] = {
        "in": before, "out": len(enc), "quarantined_orphans": int(len(orphans))
    }
    if len(orphans):
        storage.write_csv(orphans, f"metadata/{manifest.run_id}/quarantine_orphan_encounters.csv")
    out["encounters"] = enc

    # -- injections --------------------------------------------------------
    inj = tables["injections"].copy()
    inj["injection_date"] = parse_dates_mixed(inj["injection_date"])
    inj["medication"] = inj["medication"].astype(str).str.strip()
    before = len(inj)
    inj = inj[inj["patient_id"].astype(str).isin(known_ids)]
    inj = inj.dropna(subset=["injection_date"])
    # same drug, same patient, same eye, same day = one administration
    inj = inj.drop_duplicates(subset=["patient_id", "medication", "injection_date", "eye"])
    stats["injections"] = {"in": before, "out": len(inj), "dropped": before - len(inj)}
    out["injections"] = inj

    # -- visual acuity -----------------------------------------------------
    va = tables["visual_acuity"].copy()
    va["measurement_date"] = parse_dates_mixed(va["measurement_date"])
    va["logmar"] = snellen_to_logmar(va["va_snellen"])
    before = len(va)
    va = va[va["patient_id"].astype(str).isin(known_ids)]
    # implausible values are set to null rather than dropped: the visit still
    # happened, we just cannot use that measurement
    implausible = va["logmar"].notna() & ((va["logmar"] < -0.3) | (va["logmar"] > 3.0))
    va.loc[implausible, "logmar"] = None
    va = va.dropna(subset=["measurement_date"])
    va = va.drop_duplicates(subset=["patient_id", "measurement_date", "eye"])
    stats["visual_acuity"] = {
        "in": before, "out": len(va),
        "nulled_implausible_va": int(implausible.sum()),
    }
    out["visual_acuity"] = va

    # -- write curated parquet, partitioned by site ------------------------
    uris = {}
    for name, df in out.items():
        uris[name] = storage.write_parquet(df, f"curated/{name}", partition_by=["site_id"])

    storage.write_bytes(
        f"metadata/{manifest.run_id}/transform_stats.json",
        json.dumps(stats, indent=2).encode(),
    )
    manifest.record("transform", row_counts=stats, curated_uris=uris)
    return out
