# Data dictionary

## Raw layer (`raw/`), as received, CSV, unmodified

Dates arrive as **strings in site-specific formats** (`%Y-%m-%d`, `%m/%d/%Y`,
`%d-%b-%Y`). This is not an artifact of the synthetic generator; it is what
multi-source EHR extracts actually look like, and it is why parsing is
explicit rather than inferred.

### `patients`
| Column | Type | Notes |
|---|---|---|
| patient_id | string | Join key for every other table |
| birth_date | string | Site-specific format |
| sex | string | `M`/`F`, but also `""`, `U`, `Unknown` in practice |
| race | string | Includes `Unknown` |
| site_id | string | Contributing site; the partition key downstream |

### `encounters`
| Column | Type | Notes |
|---|---|---|
| encounter_id | string | |
| patient_id | string | May be orphaned, no matching patient |
| encounter_date | string | Site-specific format |
| encounter_type | string | `inpatient` / `outpatient` |
| site_id | string | |

### `diagnoses`
| Column | Type | Notes |
|---|---|---|
| patient_id | string | |
| diagnosis_code | string | ICD-10 with casing and punctuation variance |
| diagnosis_date | string | Site-specific format |
| site_id | string | |

### `injections`
| Column | Type | Notes |
|---|---|---|
| patient_id | string | |
| medication | string | `Drug A`, `Drug B`, `Drug C` (non-study) |
| injection_date | string | Site-specific format |
| eye | string | `OD` / `OS` |
| site_id | string | |
| lot_number | string | **Schema drift**, one site added this mid-year |

### `visual_acuity`
| Column | Type | Notes |
|---|---|---|
| patient_id | string | |
| va_snellen | string | Snellen fraction; includes invalid values |
| measurement_date | string | Site-specific format |
| eye | string | |
| site_id | string | |

## Curated layer (`curated/`), normalized Parquet, partitioned by `site_id`

All `*_date` columns are proper dates. `diagnosis_code` is uppercase dotted
ICD-10. `sex` is `M`/`F` or null, never imputed. `visual_acuity` gains a
`logmar` column; implausible values are **nulled, not dropped**, because the
visit happened even though the measurement is unusable. Orphan encounters are
written to `metadata/<run_id>/quarantine_orphan_encounters.csv` rather than
discarded silently.

## Analytics layer (`analytics/<study_id>/<version>/cohort`)

One row per patient. Columns defined in
[`cohort_definition.md`](cohort_definition.md).

## Injected defects and the rules that catch them

| Defect | Rate | Rule |
|---|---|---|
| Heterogeneous date formats | 3 formats across 8 sites | `unparseable_dates` |
| ICD-10 casing/punctuation | ~18% of codes | (normalized in transform) |
| Duplicate injections | 1.5% | `duplicate_records` |
| Treatment before diagnosis | 2.0% | `treatment_before_diagnosis` |
| Missing follow-up VA | 8%, informative | (a study limitation, not a QC failure) |
| Orphan encounters | 0.4% | `orphan_encounters` |
| Missing sex | 0.6% | `missing_sex` |
| Implausible VA | 0.3% | `va_out_of_range` |
| Schema drift (`lot_number`) | 1 site, mid-year | `schema_drift` |

The missing-follow-up-VA defect is deliberately *not* a QC failure. It is
informative missingness, worse eyes miss more visits, which is a threat to
validity that a data-quality gate cannot detect and an analyst has to reason
about. Keeping it out of the gate is the point.
