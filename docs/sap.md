# Statistical analysis plan

Implemented in [`r/analysis.R`](../r/analysis.R) (reference) and
[`src/analyze/run.py`](../src/analyze/run.py) (executable in CI). Both produce
the same estimands.

## Estimands

**Primary.** Difference between Drug A and Drug B in the probability of a
clinically meaningful VA improvement (>= 0.10 logMAR) at 12 months, among
patients who would be eligible under the cohort definition (ATE).

**Secondary.** (1) Continuous change in logMAR at 12 months. (2) Time to first
clinically meaningful improvement within the follow-up window.

## Analysis sequence

1. **Table 1** — baseline characteristics by arm, with standardized mean
   differences rather than p-values. A p-value comparing baseline
   characteristics in an observational cohort tests a hypothesis nobody holds;
   the SMD reports the magnitude of imbalance, which is what actually matters
   for confounding.

2. **Crude estimate** — unadjusted logistic regression. **Reported alongside
   the adjusted estimate, always.** In this cohort the crude and adjusted
   estimates differ by roughly a factor of two; presenting only the adjusted
   one conceals how much of the result is coming from the model rather than
   from the data.

3. **Multivariable logistic regression** — adjusted for age, sex, race,
   baseline logMAR, year-1 injection count, and site volume tertile.

4. **IPTW** — stabilized weights from a propensity model, with weights trimmed
   at the 99th percentile. Balance assessed by SMD before and after weighting,
   with 0.1 as the conventional adequacy threshold. Robust or bootstrap
   standard errors are required for valid inference; the naive CI is not
   correct and is labelled as such in the output.

   `injection_count_yr1` is **excluded** from the propensity model. It is
   measured after index and lies on the causal path from treatment to outcome;
   conditioning on it would adjust away part of the effect being estimated.
   It is retained in the outcome regression only as a descriptive covariate,
   and that inconsistency is itself a limitation worth stating.

5. **Time to event** — Kaplan-Meier by arm and adjusted Cox proportional
   hazards. The PH assumption is tested (`cox.zph`) and the result reported
   whether or not it holds; a violated PH assumption means the hazard ratio is
   a weighted average over follow-up rather than a constant effect.

## What would strengthen this

- Negative control outcome to detect residual confounding
- E-value for the minimum unmeasured confounding that would explain the result
- Sensitivity analysis under different missingness assumptions for the
  patients excluded for lacking a 12-month measurement
- Eye-level analysis with a patient-level random effect

## Interpretation

The data are synthetic and generated with a known confounding structure.
Results characterize the generating process and the estimators' behaviour
under it. **No clinical conclusion about any real therapy follows from them.**
