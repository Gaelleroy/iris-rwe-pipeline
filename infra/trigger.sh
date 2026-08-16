#!/usr/bin/env bash
# Start one pipeline execution manually.
#
# Renders the cohort SQL from config (same function the local pipeline uses),
# builds the input payload, and starts the state machine.
#
#   export BUCKET=your-bucket
#   bash infra/trigger.sh [raw/injections/injections.csv]
set -euo pipefail

: "${BUCKET:?set BUCKET first}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
KEY="${1:-raw/injections/injections.csv}"
SM_ARN="arn:aws:states:$REGION:$ACCOUNT:stateMachine:iris-rwe-pipeline"

mkdir -p build
python - "$BUCKET" "$KEY" <<'PY'
import json, sys
sys.path.insert(0, ".")
from src.cohort.build import render_sql
from src.manifest import load_config

bucket, key = sys.argv[1], sys.argv[2]
cfg = load_config()
sql = "SELECT COUNT(*) AS n FROM (\n" + render_sql(cfg) + "\n)"
payload = {
    "bucket": bucket,
    "key": key,
    "config_s3_uri": f"s3://{bucket}/code/study.yaml",
    "cohort_sql": sql,
}
json.dump(payload, open("build/execution_input.json", "w"))
print(f"input ready: s3://{bucket}/{key}")
PY

EXEC_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "$SM_ARN" \
  --input file://build/execution_input.json \
  --query executionArn --output text)

echo "execution: $EXEC_ARN"
echo
echo "Polling..."
while true; do
  STATUS=$(aws stepfunctions describe-execution --execution-arn "$EXEC_ARN" \
    --query status --output text)
  echo "  $STATUS"
  [ "$STATUS" = "RUNNING" ] || break
  sleep 15
done

echo
aws stepfunctions get-execution-history --execution-arn "$EXEC_ARN" \
  --query 'events[?type==`TaskStateEntered` || type==`ExecutionFailed` || type==`ExecutionSucceeded`].[type,stateEnteredEventDetails.name]' \
  --output text
