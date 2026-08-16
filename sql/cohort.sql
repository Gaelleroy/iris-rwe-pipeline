-- ---------------------------------------------------------------------------
-- Cohort: adults with newly diagnosed DME initiating anti-VEGF therapy
--
-- This SQL is the executable form of docs/cohort_definition.md. If the two
-- disagree, that is a bug. Parameters are injected from config/study.yaml -
-- nothing here is hardcoded, so changing the follow-up window or the code list
-- is a config edit reviewed in a pull request, not a hand-edit to a query.
--
-- Placeholders: {dme_codes} {meds} {min_age} {max_age} {washout_days}
--               {followup_days} {fu_tol} {baseline_window} {improve_thresh}
--
-- Runs unchanged on DuckDB (local parquet) and Athena (S3 + Glue Catalog).
-- ---------------------------------------------------------------------------

WITH
-- Step 1: earliest qualifying DME diagnosis per patient
dme_dx AS (
    SELECT patient_id,
           MIN(diagnosis_date) AS first_dme_date
    FROM diagnoses
    WHERE diagnosis_code IN ({dme_codes})
    GROUP BY patient_id
),

-- Step 2: all anti-VEGF injections of the study drugs
study_inj AS (
    SELECT patient_id, medication, injection_date, eye
    FROM injections
    WHERE medication IN ({meds})
),

-- Step 3: index = first study-drug injection on/after the DME diagnosis.
-- Anchoring on the injection (not the diagnosis) makes this a new-user design:
-- follow-up starts when exposure starts, which avoids immortal time between
-- diagnosis and treatment initiation.
index_event AS (
    SELECT i.patient_id,
           MIN(i.injection_date) AS index_date
    FROM study_inj i
    JOIN dme_dx d ON i.patient_id = d.patient_id
    WHERE i.injection_date >= d.first_dme_date
    GROUP BY i.patient_id
),

-- Step 4: the drug given ON the index date defines the exposure group
index_treatment AS (
    SELECT e.patient_id,
           e.index_date,
           MIN(i.medication) AS treatment
    FROM index_event e
    JOIN study_inj i
      ON i.patient_id = e.patient_id
     AND i.injection_date = e.index_date
    GROUP BY e.patient_id, e.index_date
),

-- Step 5: washout - any study drug in the year before index disqualifies the
-- patient as a new user (prevalent users have already survived early failure,
-- which biases the comparison in favour of whatever they are on)
prior_use AS (
    SELECT DISTINCT i.patient_id
    FROM study_inj i
    JOIN index_event e ON i.patient_id = e.patient_id
    WHERE i.injection_date < e.index_date
      AND i.injection_date >= e.index_date - INTERVAL '{washout_days}' DAY
),

base AS (
    SELECT t.patient_id,
           t.index_date,
           t.treatment,
           p.sex,
           p.race,
           p.site_id,
           p.birth_date,
           DATE_DIFF('day', p.birth_date, t.index_date) / 365.25 AS age_at_index,
           CASE WHEN pu.patient_id IS NULL THEN 0 ELSE 1 END AS has_prior_use
    FROM index_treatment t
    JOIN patients p ON p.patient_id = t.patient_id
    LEFT JOIN prior_use pu ON pu.patient_id = t.patient_id
),

-- Step 6: baseline VA - closest measurement within the window before index
baseline_va AS (
    SELECT patient_id, logmar AS baseline_logmar, measurement_date AS baseline_date
    FROM (
        SELECT v.patient_id,
               v.logmar,
               v.measurement_date,
               ROW_NUMBER() OVER (
                   PARTITION BY v.patient_id
                   ORDER BY ABS(DATE_DIFF('day', v.measurement_date, b.index_date))
               ) AS rn
        FROM visual_acuity v
        JOIN base b ON v.patient_id = b.patient_id
        WHERE v.logmar IS NOT NULL
          AND v.measurement_date <= b.index_date
          AND v.measurement_date >= b.index_date - INTERVAL '{baseline_window}' DAY
    ) t
    WHERE rn = 1
),

-- Step 7: 12-month VA - closest measurement to the target date within tolerance
followup_va AS (
    SELECT patient_id, logmar AS followup_logmar, measurement_date AS followup_date
    FROM (
        SELECT v.patient_id,
               v.logmar,
               v.measurement_date,
               ROW_NUMBER() OVER (
                   PARTITION BY v.patient_id
                   ORDER BY ABS(DATE_DIFF('day', v.measurement_date,
                                          b.index_date + INTERVAL '{followup_days}' DAY))
               ) AS rn
        FROM visual_acuity v
        JOIN base b ON v.patient_id = b.patient_id
        WHERE v.logmar IS NOT NULL
          AND v.measurement_date >= b.index_date + INTERVAL '{followup_days}' DAY
                                   - INTERVAL '{fu_tol}' DAY
          AND v.measurement_date <= b.index_date + INTERVAL '{followup_days}' DAY
                                   + INTERVAL '{fu_tol}' DAY
    ) t
    WHERE rn = 1
),

-- Step 8: time to first clinically meaningful improvement, for the survival
-- analysis. Censored at last observed measurement if never reached.
improvement AS (
    SELECT b.patient_id,
           MIN(CASE WHEN bv.baseline_logmar - v.logmar >= {improve_thresh}
                    THEN DATE_DIFF('day', b.index_date, v.measurement_date) END) AS days_to_improve,
           MAX(DATE_DIFF('day', b.index_date, v.measurement_date)) AS days_last_obs
    FROM base b
    JOIN baseline_va bv ON bv.patient_id = b.patient_id
    JOIN visual_acuity v ON v.patient_id = b.patient_id
    WHERE v.logmar IS NOT NULL
      AND v.measurement_date > b.index_date
      AND v.measurement_date <= b.index_date + INTERVAL '{followup_days}' DAY
                                + INTERVAL '{fu_tol}' DAY
    GROUP BY b.patient_id
),

inj_counts AS (
    SELECT b.patient_id, COUNT(*) AS injection_count_yr1
    FROM base b
    JOIN study_inj i
      ON i.patient_id = b.patient_id
     AND i.injection_date >= b.index_date
     AND i.injection_date <= b.index_date + INTERVAL '{followup_days}' DAY
    GROUP BY b.patient_id
)

SELECT
    b.patient_id,
    b.site_id,
    b.index_date,
    b.treatment,
    b.age_at_index,
    b.sex,
    b.race,
    b.has_prior_use,
    bv.baseline_logmar,
    fv.followup_logmar,
    bv.baseline_logmar - fv.followup_logmar               AS logmar_change,
    CASE WHEN bv.baseline_logmar - fv.followup_logmar >= {improve_thresh}
         THEN 1 ELSE 0 END                                 AS improved_12mo,
    COALESCE(ic.injection_count_yr1, 0)                    AS injection_count_yr1,
    imp.days_to_improve,
    COALESCE(imp.days_to_improve, imp.days_last_obs)       AS time_to_event_days,
    CASE WHEN imp.days_to_improve IS NULL THEN 0 ELSE 1 END AS event_observed
FROM base b
LEFT JOIN baseline_va bv ON bv.patient_id = b.patient_id
LEFT JOIN followup_va fv ON fv.patient_id = b.patient_id
LEFT JOIN inj_counts  ic ON ic.patient_id = b.patient_id
LEFT JOIN improvement imp ON imp.patient_id = b.patient_id
WHERE b.age_at_index >= {min_age}
  AND b.age_at_index <  {max_age}
  AND b.sex IS NOT NULL
  AND b.has_prior_use = 0
  AND bv.baseline_logmar IS NOT NULL
  AND fv.followup_logmar IS NOT NULL
