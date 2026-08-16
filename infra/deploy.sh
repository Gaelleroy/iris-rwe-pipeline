#!/usr/bin/env bash
# Deploy the automation layer: Lambda preflight, Glue job, Step Functions,
# EventBridge trigger, SNS topic.
#
# Idempotent - safe to rerun. Requires $BUCKET and $ALERT_EMAIL.
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
  echo "Subscription sent to $ALERT_EMAIL - confirm it from your inbox or you get no alerts."
fi
echo "$TOPIC_ARN"

# ---------------------------------------------------------------------------
say "Package src/ and config for Glue"
rm -rf build && mkdir -p build
cp -r src build/src
# python zipfile, not the zip binary: Git Bash on Windows has no zip
python -c "import shutil; shutil.make_archive('build/src','zip','build','src')"
aws s3 cp build/src.zip "s3://$BUCKET/code/src.zip" >/dev/null
aws s3 cp infra/glue_jobs/validate_transform.py "s3://$BUCKET/code/validate_transform.py" >/dev/null
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
  # IAM propagation lags role creation; retry rather than fail on first run
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
say "IAM role: Glue job"
GLUE_ROLE=AWSGlueServiceRole-iris   # reuse the crawler role
aws iam put-role-policy --role-name $GLUE_ROLE --policy-name IrisS3Write \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:DeleteObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"]}]}"
GLUE_ROLE_ARN=$(aws iam get-role --role-name $GLUE_ROLE --query Role.Arn --output text)

say "Glue job: validate + transform"
GLUE_JOB=${PREFIX}-validate-transform
aws glue delete-job --job-name $GLUE_JOB >/dev/null 2>&1 || true
aws glue create-job --name $GLUE_JOB --role "$GLUE_ROLE_ARN" \
  --command "{\"Name\":\"pythonshell\",\"PythonVersion\":\"3.9\",\"ScriptLocation\":\"s3://$BUCKET/code/validate_transform.py\"}" \
  --default-arguments "{\"--extra-py-files\":\"s3://$BUCKET/code/src.zip\",\"--additional-python-modules\":\"pyyaml,pyarrow\",\"--config_s3_uri\":\"s3://$BUCKET/code/study.yaml\",\"--bucket\":\"$BUCKET\",\"--run_id\":\"manual\"}" \
  --max-capacity 1.0 --glue-version 3.0 >/dev/null
echo "$GLUE_JOB"

# ---------------------------------------------------------------------------
say "IAM role: Step Functions"
SFN_ROLE=${PREFIX}-statemachine-role
aws iam create-role --role-name $SFN_ROLE \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"states.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  >/dev/null 2>&1 || true
aws iam put-role-policy --role-name $SFN_ROLE --policy-name Orchestrate \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":[\"lambda:InvokeFunction\"],\"Resource\":\"$PREFLIGHT_ARN\"},
    {\"Effect\":\"Allow\",\"Action\":[\"glue:StartJobRun\",\"glue:GetJobRun\",\"glue:GetJobRuns\",\"glue:BatchStopJobRun\"],\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"athena:StartQueryExecution\",\"athena:GetQueryExecution\",\"athena:GetQueryResults\",\"athena:StopQueryExecution\"],\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"glue:GetTable\",\"glue:GetTables\",\"glue:GetDatabase\",\"glue:GetPartitions\"],\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\",\"s3:GetBucketLocation\"],\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"]},
    {\"Effect\":\"Allow\",\"Action\":[\"sns:Publish\"],\"Resource\":\"$TOPIC_ARN\"},
    {\"Effect\":\"Allow\",\"Action\":[\"events:PutTargets\",\"events:PutRule\",\"events:DescribeRule\"],\"Resource\":\"*\"}]}"
SFN_ROLE_ARN=$(aws iam get-role --role-name $SFN_ROLE --query Role.Arn --output text)

say "State machine"
sed -e "s|\${PREFLIGHT_FUNCTION_ARN}|$PREFLIGHT_ARN|g" \
    -e "s|\${GLUE_JOB_NAME}|$GLUE_JOB|g" \
    -e "s|\${ATHENA_DATABASE}|$DB|g" \
    -e "s|\${ATHENA_WORKGROUP}|primary|g" \
    -e "s|\${ATHENA_OUTPUT_LOCATION}|s3://$BUCKET/athena-results/|g" \
    -e "s|\${SNS_TOPIC_ARN}|$TOPIC_ARN|g" \
    infra/statemachine.asl.json > build/statemachine.json

SM_ARN="arn:aws:states:$REGION:$ACCOUNT:stateMachine:${PREFIX}-rwe-pipeline"
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

printf '\n== Deployed\n'
printf 'State machine : %s\n' "$SM_ARN"
printf 'Glue job      : %s\n' "$GLUE_JOB"
printf 'Lambda        : %s\n' "$PREFLIGHT_ARN"
printf 'SNS topic     : %s\n' "$TOPIC_ARN"
printf '\nConfirm the SNS subscription email, then run: bash infra/trigger.sh\n'
