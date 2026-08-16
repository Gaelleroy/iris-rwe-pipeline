# IRIS RWE Pipeline

A reproducible real-world evidence pipeline that takes messy, multi-site
ophthalmology EHR data from raw extract to a defensible treatment-comparison
estimate — with a data-quality gate that stops the pipeline when the data stop
making clinical sense.

Runs locally on synthetic data with no cloud account. The same code runs on
AWS (S3 + Glue Catalog + Athena) by changing two lines of config.

---

## The question

> Among adults with newly diagnosed diabetic macular edema initiating
> anti-VEGF therapy, what patient and treatment factors are associated with
> visual acuity improvement at 12 months, and how does time-to-improvement
> differ between Drug A and Drug B?

This is a retrospective, new-user, active-comparator cohort study on
observational data. Full specification in [`docs/cohort_definition.md`](docs/cohort_definition.md)
and [`docs/sap.md`](docs/sap.md).

## Quickstart

```bash
pip install -r requirements.txt
make pipeline      # generate -> ingest -> validate -> transform -> cohort -> analyze
make test
```

No credentials, no cloud account, no external data download. The synthetic
data is generated from a fixed seed, so the run is byte-reproducible.

## What the pipeline does

```
config/study.yaml ─── every parameter, every threshold, every criterion
        │
  generate    synthetic multi-site EHR with deliberately injected defects
        ▼
  ingest      schema fingerprint + row counts; wrong-shaped files stop here
        ▼
  validate    STRUCTURAL: nulls, duplicates, orphans, unparseable dates
              CLINICAL:   treatment-before-diagnosis, diagnosis-before-birth,
                          VA out of physiologic range, activity past cutoff
        │           │
      PASS/WARN   FAIL ──► pipeline halts, curated layer never written
        ▼
  transform   normalize dates/codes, Snellen→logMAR, dedupe, quarantine
              orphans → partitioned Parquet in curated/
        ▼
  cohort      sql/cohort.sql via DuckDB (local) or Athena (AWS)
              → analysis-ready cohort + CONSORT attrition ladder
        ▼
  analyze     Table 1, crude + adjusted logistic, IPTW with balance
              diagnostics, Cox for time-to-improvement
        ▼
  results/<run_id>/  estimates, figures, and a run manifest
```

## Storage layers

| Layer | Contents | Mutability |
|---|---|---|
| `raw/` | Exactly what the site sent, CSV, unmodified | Never modified |
| `curated/` | Normalized, deduplicated, typed Parquet, partitioned by site | Rebuildable from raw |
| `analytics/` | Study-specific analysis-ready cohorts, versioned | Rebuildable from curated |
| `results/` | Estimates, figures, run manifest | Append-only per run |
| `metadata/` | QC reports, attrition, quarantined records | Append-only per run |

Raw is the source of truth. If a transformation decision turns out to be
wrong, curated is rebuilt from raw and the git history explains why the rule
changed.

## The data quality gate

The validation layer is split into two tiers, and the split is the point.

**Structural** rules — nulls, duplicates, referential integrity, date
parseability — are what any ETL framework supplies. Necessary, but table
stakes.

**Clinical** rules encode what patient data can and cannot look like:

| Rule | Why it matters |
|---|---|
| `treatment_before_diagnosis` | An injection before the DME diagnosis is impossible. A low rate means stray records; a high rate usually means date parsing broke or the extract joined on the wrong encounter. |
| `diagnosis_before_birth` | Catches two-digit-year parsing (`65` → 2065). |
| `va_out_of_range` | Snellen values better than 20/10 or worse than NLP are data errors, not measurements. |
| `followup_beyond_data_cutoff` | Activity recorded after the declared cutoff means the extract window is not what was documented. |
| `implausible_age` | Age outside [0, 120] at cutoff. |

A pipeline that only runs the structural tier will happily produce a clean,
well-typed, internally consistent dataset that is clinically nonsense.

Thresholds live in `config/study.yaml`. `WARN` is logged and carried into the
manifest; `FAIL` halts the run before the curated layer is written, so a
partial or silently wrong cohort is never available for someone to analyze in
good faith.

## Reproducibility

Every results directory contains a `run_manifest.json` pinning four things:

| Pin | Source |
|---|---|
| Code | git SHA, with a `-dirty` marker for uncommitted changes |
| Config | SHA-256 of `study.yaml`, plus the resolved values |
| Data | row counts at every stage, schema fingerprints, QC findings |
| Environment | Python version, platform, package versions (R: `sessionInfo()`) |

Three of the four are the usual answer. The fourth — pinning the *data* by
recording stage-level row counts and schema fingerprints — is what lets you
tell whether a result changed because the code changed or because the input
did.

## Running on AWS

```yaml
storage:
  backend: s3
  s3: {bucket: your-bucket, region: us-east-1}
query:
  engine: athena
  athena: {database: iris_rwe, output_location: s3://your-bucket/athena-results/}
```

That is the entire change. Storage is behind `src/storage/backends.py` and the
query engine behind `src/cohort/build.py`; no other module knows which backend
it is talking to. `sql/cohort.sql` runs unmodified on both DuckDB and Athena.

Curated Parquet is partitioned by `site_id` because Athena bills by bytes
scanned — a single-site query reads one prefix instead of the whole dataset.

**Production would add**, and this repo does not implement: Step Functions for
orchestration and retry, EventBridge triggers on object-created, Glue or
Databricks for transformations at real scale, CloudWatch alarms, per-layer IAM
roles, and a de-identification step before the analytics layer.

## Scope and honesty

This runs on ~22,000 synthetic patients. It demonstrates the design end to
end — layer separation, clinical QC gating, parameterized cohort logic,
confounding-aware analysis, provenance tracking — on data small enough to
reason about. It has not been run at registry scale, and the AWS path has been
exercised at small volume rather than in production.

The synthetic data contains a deliberately confounded treatment assignment:
eyes with worse baseline acuity are preferentially assigned Drug A, and worse
eyes have more room to improve. The crude estimate is therefore badly biased
away from the null, and the analysis reports crude, adjusted, and IPTW
estimates side by side so the reader can see how much of the result depends on
the model. Results from synthetic data describe the generating process, not
clinical reality; no clinical conclusion should be drawn from them.

## Layout

```
config/study.yaml   all parameters and thresholds
src/storage/        local + S3 backends behind one interface
src/generate/       synthetic EHR + defect injection
src/ingest/         schema fingerprinting and pre-flight
src/validate/       structural + clinical rules
src/transform/      normalization primitives + curation job
src/cohort/         SQL rendering, query engines, attrition
src/analyze/        Python analysis (statsmodels + lifelines)
sql/cohort.sql      the cohort definition, executable
r/analysis.R        reference analysis implementation
tests/              unit tests
docs/               data dictionary, cohort definition, SAP
```
