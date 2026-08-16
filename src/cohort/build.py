"""Cohort construction.

Two things worth noting for anyone reading this in review:

1. The query engine is abstracted the same way storage is. DuckDB reads the
   curated parquet locally; Athena reads the same parquet on S3 through the
   Glue Catalog. Same SQL text, same parameters, same result.

2. Attrition is computed as a CONSORT-style ladder, not just a final N. A
   cohort count with no attrition table is unreviewable - you cannot tell
   whether a criterion removed 3% or 60% of the population, and "we lost 60%
   at the baseline-VA requirement" is a finding, not a footnote.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

SQL_PATH = Path(__file__).resolve().parents[2] / "sql" / "cohort.sql"


def _sql_list(values) -> str:
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def render_sql(cfg: dict, sql_path: Path | None = None) -> str:
    c = cfg["cohort"]
    a = cfg["analysis"]
    text = Path(sql_path or SQL_PATH).read_text()
    return text.format(
        dme_codes=_sql_list(c["dme_icd10_prefixes"]),
        meds=_sql_list(c["exposure_medications"]),
        min_age=c["min_age_at_index"],
        max_age=c["max_age_at_index"],
        washout_days=c["washout_days"],
        followup_days=c["followup_days"],
        fu_tol=c["followup_window_tolerance_days"],
        baseline_window=c["baseline_va_window_days"],
        improve_thresh=a["outcome_logmar_improvement_threshold"],
    )


class DuckDBEngine:
    """Local engine. Registers curated dataframes as tables."""

    def __init__(self, tables: dict[str, pd.DataFrame]):
        import duckdb

        self.con = duckdb.connect()
        for name, df in tables.items():
            self.con.register(name, df)

    def query(self, sql: str) -> pd.DataFrame:
        return self.con.execute(sql).fetch_df()


class AthenaEngine:  # pragma: no cover - requires AWS
    """Athena engine. Same SQL, tables resolved through the Glue Catalog."""

    def __init__(self, cfg: dict):
        import boto3

        acfg = cfg["query"]["athena"]
        self.database = acfg["database"]
        self.workgroup = acfg["workgroup"]
        self.output = acfg["output_location"]
        self.client = boto3.client("athena", region_name=cfg["storage"]["s3"]["region"])

    def query(self, sql: str) -> pd.DataFrame:
        import time

        qid = self.client.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": self.database},
            WorkGroup=self.workgroup,
            ResultConfiguration={"OutputLocation": self.output},
        )["QueryExecutionId"]
        while True:
            state = self.client.get_query_execution(QueryExecutionId=qid)[
                "QueryExecution"]["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(1.5)
        if state != "SUCCEEDED":
            raise RuntimeError(f"Athena query {qid} ended in state {state}")
        return pd.read_csv(f"{self.output.rstrip('/')}/{qid}.csv")


def get_engine(cfg: dict, tables: dict[str, pd.DataFrame] | None = None):
    engine = cfg["query"]["engine"]
    if engine == "duckdb":
        return DuckDBEngine(tables or {})
    if engine == "athena":
        return AthenaEngine(cfg)
    raise ValueError(f"unknown query engine: {engine}")


def attrition(tables: dict[str, pd.DataFrame], cohort: pd.DataFrame, cfg: dict) -> list[dict]:
    """CONSORT-style ladder from source population to analytic cohort."""
    c = cfg["cohort"]
    dx, inj, pat = tables["diagnoses"], tables["injections"], tables["patients"]

    n_source = pat["patient_id"].nunique()
    dme_pts = dx.loc[dx["diagnosis_code"].isin(c["dme_icd10_prefixes"]), "patient_id"].unique()
    n_dme = len(dme_pts)
    treated = inj.loc[
        inj["medication"].isin(c["exposure_medications"])
        & inj["patient_id"].isin(dme_pts), "patient_id"
    ].unique()
    n_treated = len(treated)

    steps = [
        ("Source population (all patients in extract)", n_source),
        ("With a qualifying DME diagnosis", n_dme),
        (f"Initiating {' or '.join(c['exposure_medications'])} on/after diagnosis", n_treated),
        ("Meeting age, sex, washout, and VA-availability criteria", len(cohort)),
    ]
    out, prev = [], None
    for label, n in steps:
        row = {"step": label, "n": int(n)}
        if prev is not None:
            row["excluded"] = int(prev - n)
            row["pct_retained"] = round(100 * n / prev, 1) if prev else None
        out.append(row)
        prev = n
    return out


def build(tables: dict[str, pd.DataFrame], cfg: dict, storage, manifest) -> pd.DataFrame:
    sql = render_sql(cfg)
    engine = get_engine(cfg, tables)
    cohort = engine.query(sql)

    # a couple of derived covariates that are cleaner in pandas than SQL
    if len(cohort):
        vol = cohort.groupby("site_id")["patient_id"].transform("count")
        cohort["site_volume_tertile"] = pd.qcut(
            vol.rank(method="first"), 3, labels=["low", "mid", "high"]
        ).astype(str)
        cohort["age_group"] = pd.cut(
            cohort["age_at_index"], [0, 55, 65, 75, 200],
            labels=["<55", "55-64", "65-74", "75+"], right=False,
        ).astype(str)

    lad = attrition(tables, cohort, cfg)
    study = cfg["study"]
    key = f"analytics/{study['id']}/{study['version']}/cohort"
    storage.write_parquet(cohort, key)
    storage.write_bytes(
        f"metadata/{manifest.run_id}/attrition.json", json.dumps(lad, indent=2).encode()
    )
    # a CSV copy for the R stage, which should not need parquet tooling
    storage.write_csv(cohort, f"analytics/{study['id']}/{study['version']}/cohort.csv")

    manifest.record(
        "cohort",
        n=len(cohort),
        attrition=lad,
        sql_sha256=__import__("hashlib").sha256(sql.encode()).hexdigest()[:16],
        output_uri=storage.uri(key),
    )
    return cohort
