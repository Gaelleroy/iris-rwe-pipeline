#!/usr/bin/env bash
# Deploy the automation layer.
#
# Creates: SNS topic, Lambda preflight, two Glue Python Shell jobs, the Step
# Functions state machine, and the EventBridge rule that makes the whole thing
# event-driven. Idempotent - safe to rerun.
#
#   export BUCKET=your-bucket
#   export ALERT_EMAIL=you@example.com
#   bash infra/deploy.sh
set -euo pipefail

: "${BUCKET:?set BUCKET first}"
: "${ALERT_EMAIL:?set ALERT_EMAIL first}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
DB="${ATHENA_DATABASE:-iris_rwe}"
PREFIX="iris"

say() { printf '\n== %s\n' "$1"; }

# ---------------------------------------------------------------------------
say "SNS topic"
TOPIC_ARN=$(aws sns create-topic --name ${PREFIX}-pipeline-alerts --query TopicArn --output text)
if ! aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
      --query 'Subscriptions[].Endpoint' --output text | grep -q "$ALERT_EMAIL"; then
  aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint "$ALERT_EMAIL" >/dev/null
  echo "Subscription sent to $ALERT_EMAIL - confirm it or you get no alerts."
fi
echo "$TOPIC_ARN"

# ---------------------------------------------------------------------------
say "Package code for Glue"
rm -rf build && mkdir -p build/pkg
# Ship sql/ alongside src/. render_sql() resolves the query relative to its own
# module (parents[2]/sql/cohort.sql), so the deployed layout has to mirror the
# repo layout - shipping src/ alone leaves the SQL missing at runtime.
cp -r src build/pkg/src
cp -r sql build/pkg/sql
python -c "import shutil; shutil.make_archive('build/src','zip','build/pkg')"
aws s3 cp build/src.zip "s3://$BUCKET/code/src.zip" >/dev/null
aws s3 cp infra/glue_jobs/validate_transform.py "s3://$BUCKET/code/validate_transform.py" >/dev/null
aws s3 cp infra/glue_jobs/cohort_analyze.py "s3://$BUCKET/code/cohort_analyze.py" >/dev/null
aws s3 cp config/study.yaml "s3://$BUCKET/code/study.yaml" >/dev/null

# ---------------------------------------------------------------------------
say "IAM role: Lambda"
LAMBDA_ROLE=${PREFIX}-preflight-role
aws iam create-role --role-name $LAMBDA_ROLE \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  >/dev/null 2>&1 || true
aws iam attach-role-policy --role-name $LAMBDA_ROLE \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name $LAMBDA_ROLE --policy-name S3Read \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"]}]}"
LAMBDA_ROLE_ARN=$(aws iam get-role --role-name $LAMBDA_ROLE --query Role.Arn --output text)

say "Lambda: preflight"
python -c "import zipfile; zipfile.ZipFile('build/preflight.zip','w',zipfile.ZIP_DEFLATED).write('infra/lambda_preflight/handler.py','handler.py')"
if aws lambda get-function --function-name ${PREFIX}-preflight >/dev/null 2>&1; then
  aws lambda update-function-code --function-name ${PREFIX}-preflight \
    --zip-file fileb://build/preflight.zip >/dev/null
else
  for i in 1 2 3 4 5; do
    aws lambda create-function --function-name ${PREFIX}-preflight \
      --runtime python3.12 --handler handler.handler --role "$LAMBDA_ROLE_ARN" \
      --zip-file fileb://build/preflight.zip --timeout 60 --memory-size 256 \
      >/dev/null 2>&1 && break
    echo "  waiting for IAM propagation ($i/5)"; sleep 10
  done
fi
PREFLIGHT_ARN=$(aws lambda get-function --function-name ${PREFIX}-preflight \
  --query Configuration.FunctionArn --output text)
echo "$PREFLIGHT_ARN"

# ---------------------------------------------------------------------------
say "IAM role: Glue"
GLUE_ROLE=AWSGlueServiceRole-iris
aws iam put-role-policy --role-name $GLUE_ROLE --policy-name IrisS3Write \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:DeleteObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"]}]}"
aws iam put-role-policy --role-name $GLUE_ROLE --policy-name IrisAthena \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["athena:StartQueryExecution","athena:GetQueryExecution","athena:GetQueryResults","glue:GetTable","glue:GetTables","glue:GetDatabase","glue:GetPartitions"],"Resource":"*"}]}'
GLUE_ROLE_ARN=$(aws iam get-role --role-name $GLUE_ROLE --query Role.Arn --output text)

say "Glue job: validate + transform"
GLUE_JOB=${PREFIX}-validate-transform
aws glue delete-job --job-name $GLUE_JOB >/dev/null 2>&1 || true
aws glue create-job --name $GLUE_JOB --role "$GLUE_ROLE_ARN" \
  --command "{\"Name\":\"pythonshell\",\"PythonVersion\":\"3.9\",\"ScriptLocation\":\"s3://$BUCKET/code/validate_transform.py\"}" \
  --default-arguments "{\"--additional-python-modules\":\"pyyaml,pyarrow\",\"--config_s3_uri\":\"s3://$BUCKET/code/study.yaml\",\"--bucket\":\"$BUCKET\",\"--run_id\":\"manual\"}" \
  --max-capacity 1.0 --glue-version 3.0 --timeout 60 >/dev/null
echo "$GLUE_JOB"

say "Glue job: cohort + analysis"
ANALYZE_JOB=${PREFIX}-cohort-analyze
aws glue delete-job --job-name $ANALYZE_JOB >/dev/null 2>&1 || true
aws glue create-job --name $ANALYZE_JOB --role "$GLUE_ROLE_ARN" \
  --command "{\"Name\":\"pythonshell\",\"PythonVersion\":\"3.9\",\"ScriptLocation\":\"s3://$BUCKET/code/cohort_analyze.py\"}" \
  --default-arguments "{\"--additional-python-modules\":\"pyyaml,pyarrow,statsmodels\",\"--config_s3_uri\":\"s3://$BUCKET/code/study.yaml\",\"--bucket\":\"$BUCKET\",\"--run_id\":\"manual\"}" \
  --max-capacity 1.0 --glue-version 3.0 --timeout 60 >/dev/null
echo "$ANALYZE_JOB"

# ---------------------------------------------------------------------------
say "IAM role: Step Functions"
SFN_ROLE=${PREFIX}-statemachine-role
aws iam create-role --role-name $SFN_ROLE \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"states.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  >/dev/null 2>&1 || true
aws iam put-role-policy --role-name $SFN_ROLE --policy-name Orchestrate \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"lambda:InvokeFunction\"],\"Resource\":\"$PREFLIGHT_ARN\"},{\"Effect\":\"Allow\",\"Action\":[\"glue:StartJobRun\",\"glue:GetJobRun\",\"glue:GetJobRuns\",\"glue:BatchStopJobRun\"],\"Resource\":\"*\"},{\"Effect\":\"Allow\",\"Action\":[\"sns:Publish\"],\"Resource\":\"$TOPIC_ARN\"},{\"Effect\":\"Allow\",\"Action\":[\"events:PutTargets\",\"events:PutRule\",\"events:DescribeRule\"],\"Resource\":\"*\"}]}"
SFN_ROLE_ARN=$(aws iam get-role --role-name $SFN_ROLE --query Role.Arn --output text)

say "State machine"
SM_ARN="arn:aws:states:$REGION:$ACCOUNT:stateMachine:${PREFIX}-rwe-pipeline"
sed -e "s|\${PREFLIGHT_FUNCTION_ARN}|$PREFLIGHT_ARN|g" \
    -e "s|\${GLUE_VALIDATE_JOB}|$GLUE_JOB|g" \
    -e "s|\${GLUE_ANALYZE_JOB}|$ANALYZE_JOB|g" \
    -e "s|\${SNS_TOPIC_ARN}|$TOPIC_ARN|g" \
    infra/statemachine.asl.json > build/statemachine.json

if aws stepfunctions describe-state-machine --state-machine-arn "$SM_ARN" >/dev/null 2>&1; then
  aws stepfunctions update-state-machine --state-machine-arn "$SM_ARN" \
    --definition file://build/statemachine.json --role-arn "$SFN_ROLE_ARN" >/dev/null
else
  for i in 1 2 3 4 5; do
    aws stepfunctions create-state-machine --name ${PREFIX}-rwe-pipeline \
      --definition file://build/statemachine.json --role-arn "$SFN_ROLE_ARN" \
      >/dev/null 2>&1 && break
    echo "  waiting for IAM propagation ($i/5)"; sleep 10
  done
fi
echo "$SM_ARN"

# ---------------------------------------------------------------------------
say "EventBridge trigger"
# Fire on a completion marker, not on every data file. Syncing five CSVs would
# otherwise start five concurrent executions racing on the same curated layer.
# Convention: upload the data, write raw/_COMPLETE last.
aws s3api put-bucket-notification-configuration --bucket "$BUCKET" \
  --notification-configuration '{"EventBridgeConfiguration":{}}'

aws events put-rule --name ${PREFIX}-raw-complete \
  --event-pattern "{\"source\":[\"aws.s3\"],\"detail-type\":[\"Object Created\"],\"detail\":{\"bucket\":{\"name\":[\"$BUCKET\"]},\"object\":{\"key\":[\"raw/_COMPLETE\"]}}}" >/dev/null

EB_ROLE=${PREFIX}-eventbridge-role
aws iam create-role --role-name $EB_ROLE \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"events.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  >/dev/null 2>&1 || true
aws iam put-role-policy --role-name $EB_ROLE --policy-name StartExecution \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"states:StartExecution\",\"Resource\":\"$SM_ARN\"}]}"
EB_ROLE_ARN=$(aws iam get-role --role-name $EB_ROLE --query Role.Arn --output text)

# The event carries bucket and key only. Everything else is derived inside the
# Glue jobs from the config in S3 - which is why the cohort SQL is rendered
# in-job rather than passed in by the caller.
python - "$SM_ARN" "$EB_ROLE_ARN" > build/targets.json <<'PYEOF'
import json, sys
sm, role = sys.argv[1], sys.argv[2]
print(json.dumps([{
    "Id": "statemachine", "Arn": sm, "RoleArn": role,
    "InputTransformer": {
        "InputPathsMap": {"bucket": "$.detail.bucket.name", "key": "$.detail.object.key"},
        "InputTemplate": '{"bucket": <bucket>, "key": <key>}',
    }}]))
PYEOF
aws events put-targets --rule ${PREFIX}-raw-complete --targets file://build/targets.json >/dev/null
echo "rule ${PREFIX}-raw-complete -> state machine"

printf '\n== Deployed\n'
printf 'State machine : %s\n' "$SM_ARN"
printf 'Glue jobs     : %s, %s\n' "$GLUE_JOB" "$ANALYZE_JOB"
printf 'EventBridge   : %s-raw-complete (fires on raw/_COMPLETE)\n' "$PREFIX"
printf '\nConfirm the SNS subscription email, then trigger with:\n'
printf '  bash infra/run.sh\n'
