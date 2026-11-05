#!/bin/bash
# =============================================================================
# scripts/evidence.sh — save proof of a real deploy into docs/evidence/<phase>/, safe to publish.
#
# Each phase captures read-only AWS CLI output (nothing is created or changed):
#   deployed   after `make deploy`: every resource exists and is configured as designed
#   loaded     after `make upload-history`: every history file was validated by the Lambda
#   demo       after scripts/demo.sh: the rejected file and its reasons, the Lambda's decisions
#   batch      after `make kpis`: the Glue run, its summary line, Data Quality results, gold files
#   destroyed  after `make destroy`: Terraform state is empty and nothing billable is left
#
# Redaction: the repo is public, so the AWS account id and every email address are replaced with
# <account-id> and <email> before anything is written.
#
# Usage: scripts/evidence.sh <deployed|loaded|demo|batch|destroyed>
# =============================================================================
set -uo pipefail

PHASE="${1:?usage: evidence.sh <deployed|loaded|demo|batch|destroyed>}"
TF="terraform -chdir=infra/terraform"
REGION="${AWS_REGION:-us-east-1}"
OUT="docs/evidence/$PHASE"
mkdir -p "$OUT"

# The account id, read once, so it can be blanked out of every file
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Replace the account id and any email address with placeholders
redact() {
  sed -E -e "s/$ACCOUNT_ID/<account-id>/g" -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<email>/g'
}

# capture <file> <command...>: run a read-only command, redact its output, save it, show a one-line status
capture() {
  local file="$1"; shift
  {
    echo "\$ $*" | redact
    echo "# captured $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    "$@" 2>&1
  } | redact > "$OUT/$file"
  echo "  saved $OUT/$file"
}

# Names of the deployed resources (empty after destroy)
out() { $TF output -raw "$1" 2>/dev/null; }
PROJECT=$(grep -m1 'name:' config/settings.yaml | awk '{print $2}')
FUNCTION="$PROJECT-file-validator"

echo "== Evidence: $PHASE"
case "$PHASE" in
  deployed)
    # Everything Terraform created, and the same list as AWS sees it through the Project tag
    capture terraform-state.txt $TF state list
    capture tagged-resources.txt aws resourcegroupstaggingapi get-resources --region "$REGION" \
      --tag-filters "Key=Project,Values=$PROJECT" --query 'ResourceTagMappingList[].ResourceARN' --output text
    # The trigger: which S3 events invoke the Lambda
    capture s3-event-trigger.json aws s3api get-bucket-notification-configuration --bucket "$(out bucket)" \
      --query 'LambdaFunctionConfigurations[].{events:Events,filter:Filter.Key.FilterRules}'
    capture lambda-config.json aws lambda get-function-configuration --function-name "$FUNCTION" \
      --query '{runtime:Runtime,memory_mb:MemorySize,timeout_s:Timeout,state:State,environment:Environment.Variables}'
    capture sns-subscriptions.json aws sns list-subscriptions-by-topic --topic-arn "$(out alerts_topic_arn)" \
      --query 'Subscriptions[].{endpoint:Endpoint,protocol:Protocol,status:SubscriptionArn}'
    capture sns-filter-policies.json $TF output -json alert_subscriptions
    capture glue-job.json aws glue get-job --job-name "$(out glue_job_name)" \
      --query 'Job.{glue_version:GlueVersion,worker_type:WorkerType,workers:NumberOfWorkers,timeout_min:Timeout,max_retries:MaxRetries}'
    capture glue-trigger.json aws glue get-trigger --name "$PROJECT-daily" --query 'Trigger.{schedule:Schedule,state:State}'
    capture athena-tables.txt aws glue get-tables --database-name "$(out athena_database)" --query 'TableList[].Name' --output text
    capture athena-workgroup.json aws athena get-work-group --work-group "$(out athena_workgroup)" \
      --query 'WorkGroup.Configuration.{scan_limit_bytes:BytesScannedCutoffPerQuery,enforced:EnforceWorkGroupConfiguration}'
    capture s3-security.txt bash -c "aws s3api get-public-access-block --bucket $(out bucket) --output text; aws s3api get-bucket-encryption --bucket $(out bucket) --query 'ServerSideEncryptionConfiguration.Rules[].ApplyServerSideEncryptionByDefault' --output text"
    ;;

  loaded)
    # Every history file reached the validated zone; nothing was rejected
    capture s3-validated-files.txt bash -c "aws s3 ls s3://$(out bucket)/validated/ap/ --recursive | wc -l | sed 's/^ */validated files: /'; aws s3 ls s3://$(out bucket)/rejected/ap/ --recursive | wc -l | sed 's/^ */rejected objects: /'"
    # The Lambda's work over the last hour, from CloudWatch metrics
    capture lambda-metrics.txt bash -c "for m in Invocations Errors Throttles; do echo -n \"\$m: \"; aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name \$m --dimensions Name=FunctionName,Value=$FUNCTION --start-time \$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ) --end-time \$(date -u +%Y-%m-%dT%H:%M:%SZ) --period 3600 --statistics Sum --query 'Datapoints[0].Sum' --output text; done"
    ;;

  demo)
    # The rejected file's reasons, and every decision the Lambda logged
    capture rejected-files.txt aws s3 ls "s3://$(out bucket)/rejected/ap/" --recursive
    capture rejected-reasons.json bash -c "for key in \$(aws s3 ls s3://$(out bucket)/rejected/ap/ --recursive | awk '{print \$4}' | grep reason.json); do aws s3 cp s3://$(out bucket)/\$key -; done"
    capture lambda-decisions.txt aws logs filter-log-events --log-group-name "/aws/lambda/$FUNCTION" \
      --filter-pattern '?"File rejected" ?"Duplicate file ignored"' --query 'events[].message' --output text
    ;;

  batch)
    # The most recent Glue run: state, duration and capacity
    capture glue-job-run.json aws glue get-job-runs --job-name "$(out glue_job_name)" --max-items 1 \
      --query 'JobRuns[0].{state:JobRunState,started:StartedOn,seconds:ExecutionTime,workers:NumberOfWorkers,worker_type:WorkerType,error:ErrorMessage}'
    # The job's own summary line (reconciliation, suspects, Data Quality, emails sent). Python logging
    # writes to stderr, which Glue sends to the "error" group; both are searched.
    capture glue-summary-log.txt bash -c "for group in /aws-glue/jobs/output /aws-glue/jobs/error; do aws logs filter-log-events --log-group-name \$group --filter-pattern '\"AP KPIs written\"' --query 'events[-1].message' --output text 2>/dev/null; done | grep -v '^None$'"
    # Glue Data Quality: the latest result, rule by rule
    capture data-quality-results.json bash -c "id=\$(aws glue list-data-quality-results --filter JobName=$(out glue_job_name) --query 'Results[0].ResultId' --output text); aws glue get-data-quality-result --result-id \$id --query '{score:Score,rules:RuleResults[].{rule:Name,result:Result,evaluated:EvaluatedMetrics}}'"
    capture s3-gold-files.txt aws s3 ls "s3://$(out bucket)/gold/" --recursive --summarize
    ;;

  destroyed)
    capture terraform-state.txt bash -c "$TF state list; echo \"resources in state: \$($TF state list 2>/dev/null | wc -l | tr -d ' ')\""
    capture leftovers.txt scripts/leftovers.sh "$PROJECT"
    ;;

  *)
    echo "unknown phase: $PHASE (deployed|loaded|demo|batch|destroyed)"; exit 2 ;;
esac
