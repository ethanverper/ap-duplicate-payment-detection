#!/bin/bash
# =============================================================================
# scripts/demo.sh — upload the demo day's files one by one and show how the pipeline reacts.
#
# Run after `make deploy`, `make upload-reference` and `make upload-history`, with the files of
# `make generate-demo` (data/generated/demo/). Each step waits for the Lambda, then shows where the
# file went. The S3 event invokes the Lambda asynchronously, so a few seconds are allowed per step.
#
#   1. MX01 file with an amount edited by hand  → REJECTED (trailer total mismatch) + email to AP Mexico
#   2. the correct MX01 file                    → VALIDATED
#   3. the same correct MX01 file again         → DUPLICATE (a retry: ignored, no email)
#   4. the US01 file                            → VALIDATED
# Then `make kpis`: the duplicate planted in the MX01 file → HIGH → email to AP Mexico.
#
# Usage: scripts/demo.sh
# =============================================================================
set -euo pipefail

TF="terraform -chdir=infra/terraform"
BUCKET=$($TF output -raw bucket)
DEMO=data/generated/demo
WAIT_SECONDS=8

# The demo day, from the demo file names (e.g. MX01_ap_20261104.csv -> 20261104)
MX_FILE=$(basename "$(ls $DEMO/valid/MX01_ap_*.csv)")
US_FILE=$(basename "$(ls $DEMO/valid/US01_ap_*.csv)")
DAY=$(echo "$MX_FILE" | sed -E 's/.*_([0-9]{8})\.csv/\1/')
DT="${DAY:0:4}-${DAY:4:2}-${DAY:6:2}"

# upload <local file> <entity>: put a file in landing/, exactly as the ERP would
upload() {
  aws s3 cp "$1" "s3://$BUCKET/landing/ap/entity=$2/$(basename "$1")" --only-show-errors
}

# show <title>: wait for the Lambda, then list where this day's files are
show() {
  sleep "$WAIT_SECONDS"
  echo "   validated/: $(aws s3 ls "s3://$BUCKET/validated/ap/" --recursive | grep -c "dt=$DT" || true) file(s) for $DT"
  echo "   rejected/:  $(aws s3 ls "s3://$BUCKET/rejected/ap/" --recursive | grep -c "_$DAY" || true) object(s) for $DAY"
}

echo "== Demo day $DT, bucket $BUCKET"

echo "1) MX01 file with one amount edited by hand (expect: REJECTED + email)"
upload "$DEMO/corrupt/$MX_FILE" MX01
show
echo "   reason:"
aws s3 cp "s3://$BUCKET/rejected/ap/entity=MX01/$MX_FILE.reason.json" - 2>/dev/null | sed 's/^/     /' || echo "     (not there yet: wait a few seconds and list rejected/ again)"

echo "2) The correct MX01 file (expect: VALIDATED)"
upload "$DEMO/valid/$MX_FILE" MX01
show

echo "3) The same correct MX01 file again (expect: DUPLICATE, nothing changes, no email)"
upload "$DEMO/valid/$MX_FILE" MX01
show

echo "4) The US01 file (expect: VALIDATED)"
upload "$DEMO/valid/$US_FILE" US01
show

echo
echo "The Lambda's decisions for the demo day (from its JSON logs):"
aws logs filter-log-events --log-group-name "/aws/lambda/$($TF output -raw file_validator_function)" \
  --filter-pattern "\"$DAY\"" --query 'events[].message' --output json \
  | python3 -c '
import json, sys
for line in json.load(sys.stdin):
    record = json.loads(line[line.index("{"):]) if "{" in line else {}
    if record.get("message") in ("File validated", "File rejected", "Duplicate file ignored"):
        print("  ", record["message"], record.get("key", ""), record.get("issues", ""))
'
echo
echo "Next: make kpis  (the duplicate in today's MX01 file is emailed to AP Mexico when the job finishes)"
