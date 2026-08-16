# ---------------------------------------------------------------------------
# Primary statistical analysis
#
# Reads the analysis-ready cohort produced by the Python pipeline and produces
# the estimands specified in docs/sap.md. The Python module src/analyze/run.py
# implements the same analysis so the pipeline is executable without an R
# toolchain; this script is the reference implementation.
#
# Usage:  Rscript r/analysis.R [path/to/cohort.csv] [output_dir]
# ---------------------------------------------------------------------------

suppressPackageStartupMessages({
  library(dplyr); library(readr); library(yaml)
  library(survival); library(survminer); library(ggplot2)
  library(broom);   library(WeightIt); library(cobalt)
})

args    <- commandArgs(trailingOnly = TRUE)
cfg     <- yaml::read_yaml("config/study.yaml")
in_path <- if (length(args) >= 1) args[1] else
  file.path("data", "analytics", cfg$study$id, cfg$study$version, "cohort.csv")
out_dir <- if (length(args) >= 2) args[2] else file.path("data", "results", "r")
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

ref_trt   <- cfg$analysis$reference_treatment
covars    <- unlist(cfg$analysis$covariates)

cohort <- read_csv(in_path, show_col_types = FALSE) %>%
  mutate(
    treatment = relevel(factor(treatment), ref = ref_trt),
    sex       = factor(sex),
    race      = factor(race),
    site_volume_tertile = factor(site_volume_tertile)
  ) %>%
  filter(!is.na(improved_12mo), !is.na(baseline_logmar))

message(sprintf("Cohort: N = %d | reference arm = %s", nrow(cohort), ref_trt))

# ---------------------------------------------------------------------------
# 1. Table 1
# ---------------------------------------------------------------------------
table_one <- cohort %>%
  group_by(treatment) %>%
  summarise(
    n                     = n(),
    age_mean              = mean(age_at_index),
    age_sd                = sd(age_at_index),
    female_pct            = 100 * mean(sex == "F"),
    baseline_logmar_mean  = mean(baseline_logmar),
    baseline_logmar_sd    = sd(baseline_logmar),
    injections_yr1_mean   = mean(injection_count_yr1),
    improved_12mo_pct     = 100 * mean(improved_12mo),
    .groups = "drop"
  )
write_csv(table_one, file.path(out_dir, "table_one.csv"))
print(as.data.frame(table_one), digits = 3)

# ---------------------------------------------------------------------------
# 2. Crude vs adjusted logistic regression
#
# Both are reported. Treatment is confounded by indication here: eyes with
# worse baseline acuity are preferentially assigned the more aggressive agent,
# and worse eyes have more room to improve. Reporting only the adjusted
# estimate conceals how much of the result depends on the model.
# ---------------------------------------------------------------------------
fit_crude <- glm(improved_12mo ~ treatment, data = cohort, family = binomial())

form_adj  <- as.formula(paste("improved_12mo ~ treatment +",
                              paste(covars, collapse = " + ")))
fit_adj   <- glm(form_adj, data = cohort, family = binomial())

or_table <- bind_rows(
  tidy(fit_crude, exponentiate = TRUE, conf.int = TRUE) %>% mutate(model = "crude"),
  tidy(fit_adj,   exponentiate = TRUE, conf.int = TRUE) %>% mutate(model = "adjusted")
) %>% filter(grepl("^treatment", term))
write_csv(or_table, file.path(out_dir, "odds_ratios.csv"))
print(as.data.frame(or_table), digits = 3)

# ---------------------------------------------------------------------------
# 3. Propensity score weighting (IPTW) with balance diagnostics
#
# Note injection_count_yr1 is deliberately EXCLUDED from the PS model: it is
# measured after index and lies on the causal path between treatment and
# outcome, so conditioning on it would be adjusting for a mediator.
# ---------------------------------------------------------------------------
ps_covars <- setdiff(covars, "injection_count_yr1")
w <- weightit(
  as.formula(paste("treatment ~", paste(ps_covars, collapse = " + "))),
  data = cohort, method = "ps", estimand = "ATE", stabilize = TRUE
)

bal <- bal.tab(w, un = TRUE, thresholds = c(m = 0.1))
print(bal)
png(file.path(out_dir, "fig_love_plot.png"), width = 900, height = 650)
print(love.plot(w, thresholds = c(m = 0.1), abs = TRUE,
                title = "Covariate balance before and after IPTW"))
dev.off()

fit_iptw <- glm(improved_12mo ~ treatment, data = cohort,
                weights = w$weights, family = quasibinomial())
iptw_est <- tidy(fit_iptw, exponentiate = TRUE, conf.int = TRUE) %>%
  filter(grepl("^treatment", term))
write_csv(iptw_est, file.path(out_dir, "iptw_estimate.csv"))

# ---------------------------------------------------------------------------
# 4. Time to first clinically meaningful improvement
# ---------------------------------------------------------------------------
surv_df <- cohort %>% filter(time_to_event_days > 0)
so      <- Surv(surv_df$time_to_event_days, surv_df$event_observed)

km <- survfit(so ~ treatment, data = surv_df)
png(file.path(out_dir, "fig_km_improvement.png"), width = 1000, height = 700)
print(ggsurvplot(
  km, data = surv_df, fun = "event", risk.table = TRUE, conf.int = TRUE,
  xlab = "Days since treatment initiation",
  ylab = sprintf("Cumulative incidence of >= %.2f logMAR improvement",
                 cfg$analysis$outcome_logmar_improvement_threshold),
  legend.title = "Treatment"
))
dev.off()

cox_form <- as.formula(paste("so ~ treatment +", paste(covars, collapse = " + ")))
fit_cox  <- coxph(cox_form, data = surv_df)
cox_tab  <- tidy(fit_cox, exponentiate = TRUE, conf.int = TRUE)
write_csv(cox_tab, file.path(out_dir, "cox_hazard_ratios.csv"))
print(summary(fit_cox))

# Proportional hazards assumption - report it, do not assume it
ph <- cox.zph(fit_cox)
print(ph)
png(file.path(out_dir, "fig_ph_assumption.png"), width = 900, height = 700)
plot(ph); dev.off()

# ---------------------------------------------------------------------------
# 5. Forest plot of adjusted hazard ratios
# ---------------------------------------------------------------------------
png(file.path(out_dir, "fig_forest_cox.png"), width = 950, height = 700)
print(
  cox_tab %>%
    ggplot(aes(x = estimate, y = reorder(term, estimate))) +
    geom_vline(xintercept = 1, linetype = "dashed", colour = "grey50") +
    geom_pointrange(aes(xmin = conf.low, xmax = conf.high)) +
    scale_x_log10() +
    labs(x = "Adjusted hazard ratio (log scale)", y = NULL,
         title = "Time to clinically meaningful VA improvement") +
    theme_minimal(base_size = 13)
)
dev.off()

# ---------------------------------------------------------------------------
# 6. Session info - part of the reproducibility pin set
# ---------------------------------------------------------------------------
writeLines(capture.output(sessionInfo()), file.path(out_dir, "r_session_info.txt"))
message("Analysis complete -> ", out_dir)
