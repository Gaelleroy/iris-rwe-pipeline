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

# lifelines is optional. It is the nicer interactive API, but it pulls
# autograd, formulaic, and matplotlib, which is a heavy resolution inside a
# Glue Python Shell job and was breaking the pyarrow install there. statsmodels
# PHReg fits the same Cox partial likelihood - verified identical to 6dp on
# this cohort - so the automated path falls back to it.
try:
    from lifelines import CoxPHFitter, KaplanMeierFitter
    HAS_LIFELINES = True
except ImportError:  # pragma: no cover - exercised only in the Glue runtime
    HAS_LIFELINES = False


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

    # -- multivariable logistic: TOTAL effect ------------------------------
    # Confounders only. Post-treatment variables are excluded here by design:
    # the primary estimand is the total effect of treatment, which includes
    # whatever the treatment achieves by changing how often the eye is
    # injected. See the DAG in docs/sap.md.
    def _term(c):
        return f"C({c})" if c in ("sex", "race", "site_volume_tertile") else c

    formula = "improved_12mo ~ exposed + " + " + ".join(
        _term(c) for c in covars if c in df.columns
    )
    adj = smf.glm(formula, data=df, family=sm.families.Binomial()).fit()
    results["adjusted_logistic"] = {
        "formula": formula,
        "estimand": "total effect",
        "or": float(np.exp(adj.params["exposed"])),
        "ci_low": float(np.exp(adj.conf_int().loc["exposed", 0])),
        "ci_high": float(np.exp(adj.conf_int().loc["exposed", 1])),
        "p": float(adj.pvalues["exposed"]),
    }

    # -- g-computation: marginal effects from the same model ----------------
    # The regression coefficient is a CONDITIONAL odds ratio. The odds ratio is
    # non-collapsible: conditioning on a strong prognostic covariate inflates
    # it relative to the marginal OR even with no confounding whatsoever. With
    # baseline logMAR carrying an SMD of ~0.62 that gap is large, and it is why
    # the adjusted OR (~2.17) sits well above the IPTW OR (~1.75) without the
    # two estimators actually disagreeing.
    #
    # Standardising the fitted model over the observed covariate distribution
    # recovers a marginal contrast comparable to IPTW. Risk difference and risk
    # ratio are collapsible and are usually the more communicable summaries.
    d1, d0 = df.copy(), df.copy()
    d1["exposed"], d0["exposed"] = 1, 0
    r1, r0 = float(adj.predict(d1).mean()), float(adj.predict(d0).mean())
    results["g_computation"] = {
        "estimand": "marginal total effect (standardised over observed covariates)",
        "risk_treated": r1,
        "risk_untreated": r0,
        "risk_difference": r1 - r0,
        "risk_ratio": r1 / r0,
        "marginal_or": (r1 / (1 - r1)) / (r0 / (1 - r0)),
        "note": "point estimates only; bootstrap required for valid intervals",
    }

    # -- secondary: controlled direct effect --------------------------------
    # Adding the mediator answers a different question - the effect of
    # treatment NOT operating through injection frequency. Reported separately
    # and labelled, because the two are routinely conflated: adjusting for a
    # mediator and calling the result "the adjusted effect" understates the
    # treatment's total benefit.
    mediators = [m for m in a.get("mediators", []) if m in df.columns]
    if mediators:
        f_direct = formula + " + " + " + ".join(_term(m) for m in mediators)
        direct = smf.glm(f_direct, data=df, family=sm.families.Binomial()).fit()
        results["direct_effect_logistic"] = {
            "formula": f_direct,
            "estimand": "controlled direct effect (mediator held fixed)",
            "mediators_adjusted": mediators,
            "or": float(np.exp(direct.params["exposed"])),
            "ci_low": float(np.exp(direct.conf_int().loc["exposed", 0])),
            "ci_high": float(np.exp(direct.conf_int().loc["exposed", 1])),
            "p": float(direct.pvalues["exposed"]),
        }

    # -- IPTW --------------------------------------------------------------
    # covars is already confounders-only, so the PS model needs no special case
    X = _design(df, covars)
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
              "age_at_index", "baseline_logmar"]],
        pd.get_dummies(surv["sex"], prefix="sex", drop_first=True, dtype=float),
    ], axis=1)
    if HAS_LIFELINES:
        cph = CoxPHFitter().fit(cph_data, "time_to_event_days", "event_observed")
        row = cph.summary.loc["exposed"]
        cox = {
            "hr": float(row["exp(coef)"]),
            "ci_low": float(row["exp(coef) lower 95%"]),
            "ci_high": float(row["exp(coef) upper 95%"]),
            "p": float(row["p"]),
            "fitter": "lifelines.CoxPHFitter",
        }
    else:
        design = cph_data.drop(columns=["time_to_event_days", "event_observed"])
        ph = sm.PHReg(
            surv["time_to_event_days"].values,
            design.values.astype(float),
            status=surv["event_observed"].values,
            ties="efron",
        ).fit()
        i = list(design.columns).index("exposed")
        lo, hi = np.exp(ph.conf_int()[i])
        cox = {
            "hr": float(np.exp(ph.params[i])),
            "ci_low": float(lo),
            "ci_high": float(hi),
            "p": float(ph.pvalues[i]),
            "fitter": "statsmodels.PHReg",
        }
    cox["n_events"] = int(surv["event_observed"].sum())
    cox["n_at_risk"] = int(len(surv))
    results["cox"] = cox

    # -- KM curve points for plotting --------------------------------------
    km_out = []
    for arm, g in surv.groupby("treatment"):
        if HAS_LIFELINES:
            kmf = KaplanMeierFitter().fit(g["time_to_event_days"], g["event_observed"], label=arm)
            sf = kmf.survival_function_.reset_index()
            sf.columns = ["days", "survival"]
        else:
            # Kaplan-Meier by hand: S(t) = prod(1 - d_i / n_i) over event times
            ev = g.sort_values("time_to_event_days")
            times, surv_p, n_at_risk, s_hat = [], [], len(ev), 1.0
            for t, grp in ev.groupby("time_to_event_days"):
                d = int(grp["event_observed"].sum())
                if d:
                    s_hat *= 1 - d / n_at_risk
                times.append(t)
                surv_p.append(s_hat)
                n_at_risk -= len(grp)
            sf = pd.DataFrame({"days": times, "survival": surv_p})
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
                    primary_estimand="total effect",
                    confounders_adjusted=[c for c in covars if c in df.columns],
                    mediators_excluded=a.get("mediators", []),
                    crude_or=round(results["crude"]["or"], 3),
                    adjusted_or=round(results["adjusted_logistic"]["or"], 3),
                    iptw_or=round(results["iptw"]["or"], 3),
                    cox_hr=round(results["cox"]["hr"], 3),
                    cox_fitter=results["cox"]["fitter"])
    return results
