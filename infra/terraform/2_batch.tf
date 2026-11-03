# =============================================================================
# 2_batch.tf — the daily Glue job (PySpark): duplicates, KPIs, Data Quality, duplicate emails.
#
#   validated/ + reference/ ──► src/glue_jobs/daily_ap_kpis.py ──► curated/ · quarantine/ · gold/<table>/
#                                         └─► Glue Data Quality (dq/curated_payments.dqdl) · email AP (SNS)
#
# The schedule exists but is disabled by default; the demo runs the job on demand with `make kpis`.
# =============================================================================

# ---------------------------------------------------------------------------
# 1. The code and the Data Quality ruleset, uploaded to scripts/
# ---------------------------------------------------------------------------

# Glue runs this file as the job's main script
resource "aws_s3_object" "glue_script" {
  bucket = aws_s3_bucket.lake.id
  key    = "scripts/daily_ap_kpis.py"
  source = "${local.repo_root}/src/glue_jobs/daily_ap_kpis.py"
  etag   = filemd5("${local.repo_root}/src/glue_jobs/daily_ap_kpis.py") # re-upload when the code changes
}

# The modules the script imports, with their package __init__ files, so the imports work on Glue
# exactly as they do locally
data "archive_file" "glue_libs" {
  type        = "zip"
  output_path = "${path.module}/build/glue_libs.zip"

  dynamic "source" {
    for_each = toset([
      "src/__init__.py",
      "src/common/__init__.py",
      "src/common/schema.py",
      "src/common/logging_utils.py",
      "src/glue_jobs/__init__.py",
      "src/glue_jobs/transforms.py",
      "src/glue_jobs/data_quality.py",
      "src/glue_jobs/alerts.py",
    ])

    content {
      content  = file("${local.repo_root}/${source.value}")
      filename = source.value
    }
  }
}

# Upload the zip; Glue adds it to the Python path through --extra-py-files
resource "aws_s3_object" "glue_libs" {
  bucket = aws_s3_bucket.lake.id
  key    = "scripts/glue_libs.zip"
  source = data.archive_file.glue_libs.output_path
  etag   = data.archive_file.glue_libs.output_md5
}

# The Data Quality ruleset, versioned in the repo, read by the job at run time
resource "aws_s3_object" "dq_ruleset" {
  bucket = aws_s3_bucket.lake.id
  key    = "scripts/dq/curated_payments.dqdl"
  source = "${local.repo_root}/dq/curated_payments.dqdl"
  etag   = filemd5("${local.repo_root}/dq/curated_payments.dqdl")
}

# ---------------------------------------------------------------------------
# 2. IAM: read validated/ reference/ scripts/, rewrite curated/ quarantine/ gold/, email AP
# ---------------------------------------------------------------------------

# The role the Glue job runs as
resource "aws_iam_role" "glue" {
  name = "${local.project}-glue"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# AWS's base policy for Glue jobs: write logs and metrics to CloudWatch
resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

# Least privilege on the lake, plus the two extra actions this job needs
resource "aws_iam_role_policy" "glue_lake" {
  name = "lake"
  role = aws_iam_role.glue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # List folders (Spark discovers files by listing)
        Sid      = "ListLake"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.lake.arn
      },
      {
        # Read the validated files, master data and the job's own code and ruleset
        Sid    = "ReadInputs"
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = [
          "${aws_s3_bucket.lake.arn}/${local.files.validated_prefix}/*",
          "${aws_s3_bucket.lake.arn}/reference/*",
          "${aws_s3_bucket.lake.arn}/scripts/*",
        ]
      },
      {
        # Write the outputs; delete is needed to overwrite a date partition on a re-run. The
        # "<zone>_$folder$" objects are the empty folder markers Glue's S3 file system writes next to
        # each top-level output folder: the first real run of manufacturing-scrap-and-stockout-alerts
        # failed without them (its lesson 6), so they are allowed from the start here.
        Sid    = "WriteOutputs"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "${aws_s3_bucket.lake.arn}/curated/*",
          "${aws_s3_bucket.lake.arn}/quarantine/*",
          "${aws_s3_bucket.lake.arn}/gold/*",
          "${aws_s3_bucket.lake.arn}/curated_$folder$",
          "${aws_s3_bucket.lake.arn}/quarantine_$folder$",
          "${aws_s3_bucket.lake.arn}/gold_$folder$",
        ]
      },
      {
        # Email AP the day's high-confidence duplicates
        Sid      = "EmailAP"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      },
      {
        # Publish Glue Data Quality results to the job's "Data quality" tab
        Sid      = "PublishDataQuality"
        Effect   = "Allow"
        Action   = "glue:PublishDataQuality"
        Resource = "*"
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# 3. The job
# ---------------------------------------------------------------------------

resource "aws_glue_job" "daily_ap_kpis" {
  name     = "${local.project}-daily-kpis"
  role_arn = aws_iam_role.glue.arn

  # Spark job on the smallest setup: 2 G.1X workers (config: aws.glue), stopped after 10 minutes
  glue_version      = local.aws.glue.glue_version
  worker_type       = local.aws.glue.worker_type
  number_of_workers = local.aws.glue.number_of_workers
  timeout           = local.aws.glue.timeout_minutes
  max_retries       = 0 # a failed run is investigated, not silently repeated (and paid twice)

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.lake.bucket}/${aws_s3_object.glue_script.key}"
    python_version  = "3"
  }

  # One run at a time: two runs overwriting the same partitions would race
  execution_property {
    max_concurrent_runs = 1
  }

  # The job's arguments: where the lake is, and the business settings from config/settings.yaml
  default_arguments = {
    "--extra-py-files"           = "s3://${aws_s3_bucket.lake.bucket}/${aws_s3_object.glue_libs.key}"
    "--input_root"               = "s3://${aws_s3_bucket.lake.bucket}"
    "--files_prefix"             = local.files.validated_prefix
    "--output_root"              = "s3://${aws_s3_bucket.lake.bucket}"
    "--overpayment_tolerance"    = tostring(local.settings.detection.overpayment_tolerance)
    "--resubmission_window_days" = tostring(local.settings.detection.resubmission_window_days)
    "--dq_ruleset_path"          = "s3://${aws_s3_bucket.lake.bucket}/${aws_s3_object.dq_ruleset.key}"
    "--alert_topic_arn"          = aws_sns_topic.alerts.arn
    "--job-bookmark-option"      = "job-bookmark-disable" # full refresh every run
  }

  # The policies must exist before the first run can start
  depends_on = [aws_iam_role_policy_attachment.glue_service, aws_iam_role_policy.glue_lake]
}

# ---------------------------------------------------------------------------
# 4. The daily schedule (created DISABLED: a forgotten schedule keeps billing)
# ---------------------------------------------------------------------------

resource "aws_glue_trigger" "daily" {
  name     = "${local.project}-daily"
  type     = "SCHEDULED"
  schedule = local.aws.glue.schedule_cron    # after the ERP exports land
  enabled  = local.aws.glue.schedule_enabled # false unless changed in config/settings.yaml

  actions {
    job_name = aws_glue_job.daily_ap_kpis.name
  }
}
