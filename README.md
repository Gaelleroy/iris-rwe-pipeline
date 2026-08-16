# IRIS RWE Pipeline

A reproducible real-world evidence pipeline that takes messy, multi-site
ophthalmology EHR data from raw extract to a defensible treatment-comparison
estimate, with a data-quality gate that stops the pipeline when the data stop
making clinical sense.

Runs locally on synthetic data with no cloud account. The same code runs on AWS
(S3, Glue, Athena, Step Functions) by changing two lines of config, and can be
triggered automatically when new data arrives.

## The question

> Among adults with newly diagnosed diabetic macular edema initiating anti-VEGF
> therapy, what patient and treatment factors are associated with visual acuity
> improvement at 12 months, and how does time-to-improvement differ between
> Drug A and Drug B?

A retrospective, new-user, active-comparator cohort study on observational
data. Full specification in [`docs/cohort_definition.md`](docs/cohort_definition.md)
and [`docs/sap.md`](docs/sap.md).

## Quickstart

```bash
pip install -r requirements.txt
make pipeline      # generate, ingest, validate, transform, cohort, analyze
make test
```

No credentials and no external data download. The synthetic data is generated
from a fixed seed, so the run reproduces exactly.

## What the pipeline does

```
config/study.yaml ... every parameter, threshold, and criterion
        |
  generate    synthetic multi-site EHR with deliberately injected defects
        v
  ingest      schema fingerprint and row counts; wrong-shaped files stop here
        v
  validate    STRUCTURAL: nulls, duplicates, orphans, unparseable dates
              CLINICAL:   treatment-before-diagnosis, diagnosis-before-birth,
                          VA out of physiologic range, activity past cutoff
        |           |
      PASS/WARN   FAIL -> pipeline halts, curated layer never written
        v
  transform   normalize dates and codes, Snellen to logMAR, dedupe,
              quarantine orphans, write partitioned Parquet
        v
  cohort      sql/cohort.sql via DuckDB (local) or Athena (AWS),
              plus a CONSORT attrition ladder
        v
  analyze     Table 1, crude and adjusted logistic, g-computation, IPTW with
              balance diagnostics, Cox for time-to-improvement
        v
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

Raw is the source of truth. If a transformation decision turns out to be wrong,
curated is rebuilt from raw and the git history explains why the rule changed.

## The data quality gate

Validation is split into two tiers, and the split is the point.

**Structural** rules cover nulls, duplicates, referential integrity, and date
parseability. Any ETL framework supplies these.

**Clinical** rules encode what patient data can and cannot look like:

| Rule | Why it matters |
|---|---|
| `treatment_before_diagnosis` | An injection before the DME diagnosis is impossible. A low rate means stray records; a high rate usually means date parsing broke or the extract joined on the wrong encounter. |
| `diagnosis_before_birth` | Catches two-digit-year parsing (`65` becoming 2065). |
| `va_out_of_range` | Snellen values better than 20/10 or worse than NLP are data errors, not measurements. |
| `followup_beyond_data_cutoff` | Activity after the declared cutoff means the extract window is not what was documented. |
| `implausible_age` | Age outside [0, 120] at cutoff. |

A pipeline that runs only the structural tier will happily produce a clean,
well-typed, internally consistent dataset that is clinically nonsense.

Thresholds live in `config/study.yaml`. `WARN` is logged and carried into the
manifest. `FAIL` halts the run before the curated layer is written, so a partial
or silently wrong cohort is never available for someone to analyze in good faith.

## Estimands

The adjustment set is read off a DAG in [`docs/sap.md`](docs/sap.md), not
assembled from available columns.

| Estimate | Value | What it is |
|---|---|---|
| Crude | OR 3.50 | Unadjusted, badly confounded by indication |
| Adjusted | OR 2.17 | Conditional OR, total effect, confounders only |
| g-computation | OR 1.67 | Same model standardised, marginal |
| IPTW | OR 1.75 | Marginal, propensity weighted |
| Risk difference | +6.5 pp | Marginal and collapsible, the number to lead with |
| Direct effect | OR 1.94 | Secondary, mediator held fixed |

Three points the analysis is built around:

1. Treatment is confounded by indication. Eyes with worse baseline acuity are
   preferentially assigned Drug A, and worse eyes have more room to improve.
   Crude and adjusted estimates are always reported together.
2. Year-1 injection count is a mediator, not a confounder. It is excluded from
   the primary model, which targets the total effect, and included only in a
   separately labelled direct-effect analysis.
3. The odds ratio is non-collapsible. The gap between the conditional 2.17 and
   the marginal 1.75 is not estimator disagreement; standardising the same
   model gives 1.67.

## Reproducibility

Every results directory contains a `run_manifest.json` pinning four things:

| Pin | Source |
|---|---|
| Code | git SHA, with a `-dirty` marker for uncommitted changes |
| Config | SHA-256 of `study.yaml`, plus resolved values |
| Data | row counts at every stage, schema fingerprints, QC findings |
| Environment | Python version, platform, package versions, Cox fitter used |

Local (DuckDB) and AWS (Athena) runs agree to six decimal places on every
estimate. Getting there required removing two sources of row-order dependence:
`ROW_NUMBER` windows without secondary sort keys, and a site-volume tertile
computed from row-level ranks. Both were silent, producing identical cohort
counts and no errors.

## Running on AWS

```yaml
storage:
  backend: s3
  s3: {bucket: your-bucket, region: us-east-1}
query:
  engine: athena
  athena: {database: iris_rwe, output_location: s3://your-bucket/athena-results/}
```

That is the entire change. Storage sits behind `src/storage/backends.py` and the
query engine behind `src/cohort/build.py`. No other module knows which backend it
is using, and `sql/cohort.sql` runs unmodified on both DuckDB and Athena.

Curated Parquet is partitioned by `site_id` because Athena bills by bytes
scanned, so a single-site query reads one prefix instead of the whole dataset.

## Automation

```bash
export BUCKET=your-bucket
export ALERT_EMAIL=you@example.com
bash infra/deploy.sh
bash infra/run.sh
```

Writing `raw/_COMPLETE` raises an EventBridge event that starts a Step Functions
execution: a Lambda preflight check, a Glue job for validation and transformation,
a second Glue job for cohort construction and analysis, then an SNS notification.
Both Glue jobs import the same `src/` package the local pipeline runs.

The marker convention exists because syncing five files would otherwise start
five concurrent executions racing on the same curated layer. Upload the data,
write the marker last.

Failure paths are separate on purpose. A schema mismatch alerts engineering. A
clinical plausibility breach alerts a scientist. Collapsing them into one
"pipeline failed" message sends every failure to the wrong person.

See [`infra/README.md`](infra/README.md) for details and for what production
would add.

## Layout

```
config/study.yaml   all parameters and thresholds
src/storage/        local and S3 backends behind one interface
src/generate/       synthetic EHR and defect injection
src/ingest/         schema fingerprinting and pre-flight
src/validate/       structural and clinical rules
src/transform/      normalization primitives and curation job
src/cohort/         SQL rendering, query engines, attrition
src/analyze/        analysis (statsmodels, lifelines optional)
sql/cohort.sql      the cohort definition, executable
r/analysis.R        reference analysis implementation
infra/              Lambda, Glue jobs, state machine, deploy scripts
tests/              unit tests
docs/               data dictionary, cohort definition, SAP with DAG
```

The data is synthetic and generated with a known confounding structure. Results
characterize that generating process, so no clinical conclusion about any real
therapy follows from them.
