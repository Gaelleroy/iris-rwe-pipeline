"""Analysis stage.

Deliberately reports the crude estimate alongside the adjusted one. In this
cohort treatment is confounded by indication - eyes with worse baseline acuity
are preferentially given Drug A, and worse eyes have more room to improve
(a ceiling effect). The crude contrast is therefore badly biased *away from the
null*, and showing both is the point: an RWE result presented without the
unadjusted comparison hides how much of the estimate is coming from adjustment.

Three estimators, so the reader can see whether the conclusion is robust to how
confounding is handled rather than to one arbitrary choice:
  1. multivariable logistic regression
  2. IPTW (stabilized weights) with balance diagnostics
  3. Cox proportional hazards for time to first meaningful improvement

The R implementation in r/analysis.R produces the same estimands; this module
exists so the pipeline is executable end to end without an R toolchain.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from lifelines import CoxPHFitter, KaplanMeierFitter


def _design(df: pd.DataFrame, covars: list[str]) -> pd.DataFrame:
    keep = [c for c in covars if c in df.columns]
    X = pd.get_dummies(df[keep], drop_first=True, dtype=float)
    return X


def table_one(df: pd.DataFrame, by: str = "treatment") -> pd.DataFrame:
    rows = []
    for arm, g in df.groupby(by):
        rows.append({
            "arm": arm,
            "n": len(g),
            "age_mean": round(g["age_at_index"].mean(), 1),
            "age_sd": round(g["age_at_index"].std(), 1),
            "female_pct": round(100 * (g["sex"] == "F").mean(), 1),
            "baseline_logmar_mean": round(g["baseline_logmar"].mean(), 3),
            "baseline_logmar_sd": round(g["baseline_logmar"].std(), 3),
            "injections_yr1_mean": round(g["injection_count_yr1"].mean(), 1),
            "improved_12mo_pct": round(100 * g["improved_12mo"].mean(), 1),
        })
    return pd.DataFrame(rows)


def smd(x: pd.Series, group: pd.Series, weights: pd.Series | None = None) -> float:
    """Standardized mean difference - the balance diagnostic for weighting."""
    w = weights if weights is not None else pd.Series(1.0, index=x.index)
    a, b = group == group.unique()[0], group == group.unique()[-1]
    ma = np.average(x[a], weights=w[a])
    mb = np.average(x[b], weights=w[b])
    va = np.average((x[a] - ma) ** 2, weights=w[a])
    vb = np.average((x[b] - mb) ** 2, weights=w[b])
    pooled = np.sqrt((va + vb) / 2)
    return float(abs(ma - mb) / pooled) if pooled > 0 else 0.0


def analyze(cohort: pd.DataFrame, cfg: dict, storage, manifest) -> dict:
    a = cfg["analysis"]
    ref = a["reference_treatment"]
    covars = a["covariates"]
    df = cohort.dropna(subset=["improved_12mo", "baseline_logmar", "treatment"]).copy()
    df["exposed"] = (df["treatment"] != ref).astype(int)

    results: dict = {"n_analyzed": int(len(df)), "reference_treatment": ref}

    # -- Table 1 ----------------------------------------------------------
    t1 = table_one(df)
    results["table_one"] = t1.to_dict(orient="records")

    # -- crude ------------------------------------------------------------
    crude = smf.glm("improved_12mo ~ exposed", data=df,
                    family=sm.families.Binomial()).fit()
    results["crude"] = {
        "or": float(np.exp(crude.params["exposed"])),
        "ci_low": float(np.exp(crude.conf_int().loc["exposed", 0])),
        "ci_high": float(np.exp(crude.conf_int().loc["exposed", 1])),
        "p": float(crude.pvalues["exposed"]),
    }

    # -- multivariable logistic -------------------------------------------
    formula = "improved_12mo ~ exposed + " + " + ".join(
        c if c not in ("sex", "race", "site_volume_tertile") else f"C({c})"
        for c in covars if c in df.columns
    )
    adj = smf.glm(formula, data=df, family=sm.families.Binomial()).fit()
    results["adjusted_logistic"] = {
        "formula": formula,
        "or": float(np.exp(adj.params["exposed"])),
        "ci_low": float(np.exp(adj.conf_int().loc["exposed", 0])),
        "ci_high": float(np.exp(adj.conf_int().loc["exposed", 1])),
        "p": float(adj.pvalues["exposed"]),
    }

    # -- IPTW --------------------------------------------------------------
    X = _design(df, [c for c in covars if c != "injection_count_yr1"])
    ps_model = sm.Logit(df["exposed"], sm.add_constant(X)).fit(disp=0)
    ps = ps_model.predict(sm.add_constant(X)).clip(0.02, 0.98)
    p_treat = df["exposed"].mean()
    w = np.where(df["exposed"] == 1, p_treat / ps, (1 - p_treat) / (1 - ps))
    df["iptw"] = np.clip(w, None, np.quantile(w, 0.99))  # trim extreme weights

    balance = []
    for col in ("age_at_index", "baseline_logmar"):
        balance.append({
            "covariate": col,
            "smd_unweighted": round(smd(df[col], df["treatment"]), 3),
            "smd_weighted": round(smd(df[col], df["treatment"], df["iptw"]), 3),
        })
    results["ps_balance"] = balance

    iptw_fit = smf.glm("improved_12mo ~ exposed", data=df, freq_weights=df["iptw"],
                       family=sm.families.Binomial()).fit()
    results["iptw"] = {
        "or": float(np.exp(iptw_fit.params["exposed"])),
        "ci_low": float(np.exp(iptw_fit.conf_int().loc["exposed", 0])),
        "ci_high": float(np.exp(iptw_fit.conf_int().loc["exposed", 1])),
        "note": "robust/bootstrap SEs required for valid inference; naive CI shown",
    }

    # -- Cox: time to first meaningful improvement --------------------------
    surv = df.dropna(subset=["time_to_event_days"]).copy()
    surv = surv[surv["time_to_event_days"] > 0]
    cph_data = pd.concat([
        surv[["time_to_event_days", "event_observed", "exposed",
              "age_at_index", "baseline_logmar", "injection_count_yr1"]],
        pd.get_dummies(surv["sex"], prefix="sex", drop_first=True, dtype=float),
    ], axis=1)
    cph = CoxPHFitter().fit(cph_data, "time_to_event_days", "event_observed")
    row = cph.summary.loc["exposed"]
    results["cox"] = {
        "hr": float(row["exp(coef)"]),
        "ci_low": float(row["exp(coef) lower 95%"]),
        "ci_high": float(row["exp(coef) upper 95%"]),
        "p": float(row["p"]),
        "n_events": int(surv["event_observed"].sum()),
        "n_at_risk": int(len(surv)),
    }

    # -- KM curve points for plotting --------------------------------------
    km_out = []
    for arm, g in surv.groupby("treatment"):
        kmf = KaplanMeierFitter().fit(g["time_to_event_days"], g["event_observed"], label=arm)
        sf = kmf.survival_function_.reset_index()
        sf.columns = ["days", "survival"]
        sf["treatment"] = arm
        km_out.append(sf)
    km = pd.concat(km_out, ignore_index=True)

    # -- persist ------------------------------------------------------------
    base = f"results/{manifest.run_id}"
    storage.write_csv(t1, f"{base}/table_one.csv")
    storage.write_csv(km, f"{base}/km_curve_points.csv")
    storage.write_bytes(f"{base}/estimates.json",
                        json.dumps(results, indent=2, default=str).encode())
    manifest.record("analyze",
                    n=results["n_analyzed"],
                    crude_or=round(results["crude"]["or"], 3),
                    adjusted_or=round(results["adjusted_logistic"]["or"], 3),
                    iptw_or=round(results["iptw"]["or"], 3),
                    cox_hr=round(results["cox"]["hr"], 3))
    return results
