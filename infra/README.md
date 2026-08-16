# Automation layer

Turns the pipeline from "I run these stages" into "the pipeline runs when data
arrives." Everything here is optional, the pipeline works without it.

## Flow

```
raw/*.csv lands in S3
        ↓
   EventBridge
        ↓
 Step Functions
        ↓
  Lambda preflight        schema fingerprint, row count
        ↓
   [Choice gate]  ──FAIL──► SNS: structural failure ──► Fail
        ↓ PASS
 Glue: validate + transform    structural + clinical rules
        ↓
   [QC gate]      ──FAIL──► SNS: quality failure ──► Fail
        ↓ PASS                (curated layer NOT written)
 Athena: build cohort
        ↓
   SNS: success
```

## Why the failure paths are separate

The three alert paths carry different messages on purpose, because they need
different people:

| Path | Meaning | Who fixes it |
|---|---|---|
| Structural failure | File doesn't match the expected schema | Data engineering, did the site change its export? |
| Quality failure | Clinical plausibility check breached | A scientist, 12% treatment-before-diagnosis is a definition question, not a bug |
| Query failure | Cohort SQL itself failed | Whoever owns the study definition |

A single "pipeline failed" alert would collapse all three and send everything
to the wrong person.

## Why the QC gate does not retry

The `ValidateAndTransform` retry block covers `Glue.ConcurrentRunsExceeded`
and `Glue.InternalServiceException`, transient service problems. It
deliberately excludes `Glue.JobRunFailed`, which is what a QC gate failure
surfaces as. Rerunning the same bad data produces the same bad result; the
retry would just delay the alert.

## Why Python Shell, not Spark

At ~22,000 patients a Glue Python Shell job (1 DPU) is the right size, cheaper and faster to start than Spark. At registry scale this becomes a Glue
Spark job or a Databricks job. The job module is structured so that swap
changes the runtime, not the logic: the validation rules and transformations
are imported from `src/`, not reimplemented.

## Deploy

```bash
export BUCKET=your-bucket
export ALERT_EMAIL=you@example.com
bash infra/deploy.sh
```

Then confirm the SNS subscription email, without it you get no alerts.

## Run

```bash
bash infra/trigger.sh                              # default file
bash infra/trigger.sh raw/patients/patients.csv    # specific file
```

## Enable the automatic trigger

`deploy.sh` does not wire up EventBridge, deliberately, an always-on trigger
on a bucket you're actively syncing will fire repeatedly. Enable it only when
you want it:

```bash
aws s3api put-bucket-notification-configuration --bucket $BUCKET \
  --notification-configuration '{"EventBridgeConfiguration":{}}'

aws events put-rule --name iris-raw-arrival \
  --event-pattern "{\"source\":[\"aws.s3\"],\"detail-type\":[\"Object Created\"],\"detail\":{\"bucket\":{\"name\":[\"$BUCKET\"]},\"object\":{\"key\":[{\"prefix\":\"raw/\"}]}}}"
```

Note the EventBridge path passes only bucket and key, it does not carry
`cohort_sql` or `config_s3_uri`. Wiring it up properly needs either a small
input transformer or for the state machine to render the SQL itself. That is
left undone rather than half-done.

## What is not here

Production would also want: per-layer IAM roles rather than one broad role,
CloudWatch alarms on execution failures, a dead-letter queue, VPC endpoints so
traffic never leaves the AWS network, KMS rather than SSE-S3, and a
de-identification step before the analytics layer. None of that is implemented.
