#!/usr/bin/env bash
# Trigger the pipeline the way it is meant to run: upload data, then write the
# completion marker. EventBridge sees the marker and starts the state machine.
# No human invokes Step Functions.
#
#   export BUCKET=your-bucket
#   bash infra/run.sh
set -euo pipefail

: "${BUCKET:?set BUCKET first}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
SM_ARN="arn:aws:states:$REGION:$ACCOUNT:stateMachine:iris-rwe-pipeline"

echo "Uploading raw data..."
aws s3 sync data/raw/ "s3://$BUCKET/raw/" --exclude "_COMPLETE"

echo "Writing completion marker (this is what triggers the pipeline)..."
date -u +%Y-%m-%dT%H:%M:%SZ | aws s3 cp - "s3://$BUCKET/raw/_COMPLETE"

echo "Waiting for EventBridge to start an execution..."
EXEC_ARN=""
for i in $(seq 1 20); do
  sleep 5
  EXEC_ARN=$(aws stepfunctions list-executions --state-machine-arn "$SM_ARN" \
    --max-results 1 --query 'executions[0].executionArn' --output text 2>/dev/null || echo "")
  START=$(aws stepfunctions describe-execution --execution-arn "$EXEC_ARN" \
    --query startDate --output text 2>/dev/null || echo "0")
  if [ -n "$EXEC_ARN" ] && [ "$EXEC_ARN" != "None" ]; then
    AGE=$(python -c "import sys,datetime;d=sys.argv[1];print(int((datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(d)).total_seconds()))" "$START" 2>/dev/null || echo 9999)
    [ "$AGE" -lt 120 ] && break
  fi
  echo "  ...($i/20)"
done

if [ -z "$EXEC_ARN" ] || [ "$EXEC_ARN" = "None" ]; then
  echo "No execution started. Check the EventBridge rule:"
  echo "  aws events describe-rule --name iris-raw-complete"
  exit 1
fi

echo "execution: $EXEC_ARN"
echo
while true; do
  STATUS=$(aws stepfunctions describe-execution --execution-arn "$EXEC_ARN" --query status --output text)
  echo "  $STATUS"
  [ "$STATUS" = "RUNNING" ] || break
  sleep 20
done

echo
aws stepfunctions get-execution-history --execution-arn "$EXEC_ARN" \
  --query 'events[?type==`TaskStateEntered` || type==`ExecutionFailed` || type==`ExecutionSucceeded`].[type,stateEnteredEventDetails.name]' \
  --output text
