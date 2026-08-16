# Cohort definition

Executable form: [`sql/cohort.sql`](../sql/cohort.sql). Parameters:
[`config/study.yaml`](../config/study.yaml). **If this document and the SQL
disagree, that is a bug** — the SQL is generated from the same config values
listed here, and any change to a criterion should update both in one pull
request.

## Design

Retrospective **new-user, active-comparator** cohort study.

The active-comparator design (Drug A vs Drug B, rather than Drug A vs
untreated) is deliberate. Comparing treated with untreated patients in EHR
data confounds the treatment effect with whatever made a clinician decide to
treat at all — usually disease severity and access to care. Comparing two
drugs used for the same indication puts both arms on the same side of that
decision.

The new-user requirement (no study drug in the 365 days before index) matters
for the same reason: prevalent users have already survived early treatment
failure and early discontinuation, which biases the comparison in favour of
whatever they are still on.

## Index date

**First administration of a study drug on or after the first qualifying DME
diagnosis.**

Anchoring on the injection rather than the diagnosis makes follow-up start
when exposure starts. Anchoring on the diagnosis instead would introduce
immortal time: the interval between diagnosis and treatment initiation is a
period in which a patient must survive and remain in care to be classified as
treated, and attributing it to the treated arm inflates the apparent benefit.

## Exposure

| Group | Definition |
|---|---|
| Drug A | Drug A administered on the index date |
| Drug B (reference) | Drug B administered on the index date |

Exposure is assigned by the drug given on the index date and does not change
with later switching (an intention-to-treat-like analogue). Switching is a
limitation, quantified in the SAP.

## Inclusion criteria

1. At least one qualifying DME diagnosis code (`E11.311`, `E11.321`,
   `E11.331`, `E11.341`, `E11.351`, `H35.81`)
2. Initiated Drug A or Drug B on or after that diagnosis
3. Age >= 18 and < 95 at index
4. Recorded sex (M or F)
5. Baseline visual acuity within 30 days before or on index
6. A 12-month visual acuity measurement within +/- 90 days of the target date

## Exclusion criteria

1. Any study drug in the 365 days before index (prevalent user)
2. Missing or unparseable birth date
3. Missing baseline or 12-month VA

## Derived variables

| Variable | Definition |
|---|---|
| `age_at_index` | (index_date − birth_date) / 365.25 |
| `baseline_logmar` | VA closest to index within the baseline window, converted from Snellen |
| `followup_logmar` | VA closest to index + 365 days within tolerance |
| `logmar_change` | baseline − follow-up (positive = improvement) |
| `improved_12mo` | 1 if logmar_change >= 0.10 (~5 ETDRS letters) |
| `injection_count_yr1` | Study-drug administrations in [index, index + 365] |
| `time_to_event_days` | Days to first measurement meeting the improvement threshold; otherwise days to last observation |
| `event_observed` | 1 if improvement observed, 0 if censored |

### On the outcome threshold

0.10 logMAR is roughly 5 ETDRS letters, the conventional threshold for a
clinically meaningful change in ophthalmology trials. Dichotomizing a
continuous outcome loses power, so the continuous `logmar_change` is analysed
as a secondary endpoint.

### On Snellen conversion

Snellen acuity is an **ordinal string**, not a number. `20/40` and `20/80`
cannot be averaged, and the visual difference between 20/20 and 20/40 is not
the same as between 20/200 and 20/400. Every downstream calculation uses
logMAR = log10(denominator / numerator). Unparseable or non-physiologic values
become null rather than being coerced to a number, so they are counted by the
QC layer instead of silently entering the analysis.

## Known limitations

1. **Selection on a collider.** Requiring a 12-month acuity measurement
   conditions on retention, which both treatment and outcome influence.
   Retention falls from 95.1% in the best baseline-severity quartile to 88.8%
   in the worst, and differs by arm (92.2% Drug A vs 93.2% Drug B). This opens
   a collider path that adjustment for measured confounders does not close.
   See the DAG in docs/sap.md.
2. **No laterality handling in v1.** Patients may be treated bilaterally; the
   current cohort takes one eye per patient. Eye-level analysis with a
   patient-level random effect is the correct extension.
3. **Unmeasured confounding.** Diabetes duration, HbA1c, OCT central subfield
   thickness, and prior treatment history are all prognostic and unavailable
   here. No adjustment method fixes a variable that is not in the data.
4. **Site heterogeneity.** Sites differ in coding practice and measurement
   frequency. Site volume tertile is adjusted for; a random effect by site is
   the better treatment.
