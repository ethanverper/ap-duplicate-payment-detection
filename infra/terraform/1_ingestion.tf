# =============================================================================
# 1_ingestion.tf — every ERP file is checked the moment it lands: S3 event → file_validator Lambda.
#
#   ERP → s3://<bucket>/landing/ap/entity=<E>/<file>.csv ─(event)→ Lambda ─┬→ validated/ (Glue reads here)
#                                                                          └→ rejected/ + email to AP
# Why an event and not a schedule (docs/decisions.md, ADR-001): a broken file reaches AP's inbox in
# seconds, and nothing bills by the hour.
# =============================================================================

# ---------------------------------------------------------------------------
# 1. The code: src/common/ (minus the YAML reader) and src/lambdas/file_validator/
# ---------------------------------------------------------------------------

# Package only what the Lambda runs, with the package __init__ files. boto3 is in the Lambda runtime.
data "archive_file" "file_validator" {
  type        = "zip"
  output_path = "${path.module}/build/file_validator.zip"

  dynamic "source" {
    for_each = toset(concat(
      ["src/__init__.py", "src/lambdas/__init__.py"],
      [for file in fileset(local.repo_root, "src/common/*.py") : file if file != "src/common/settings.py"],
      tolist(fileset(local.repo_root, "src/lambdas/file_validator/*.py")),
    ))

    content {
      content  = file("${local.repo_root}/${source.value}")
      filename = source.value
    }
  }
}

# Logs kept 1 day (config: aws.lambda.log_retention_days); created here so destroy removes them too
resource "aws_cloudwatch_log_group" "file_validator" {
  name              = "/aws/lambda/${local.project}-file-validator"
  retention_in_days = local.aws.lambda.log_retention_days
}

# ---------------------------------------------------------------------------
# 2. IAM: read landing/, write validated/ and rejected/, email AP, write its own logs
# ---------------------------------------------------------------------------

# The role the Lambda runs as
resource "aws_iam_role" "file_validator" {
  name = "${local.project}-file-validator"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Least privilege, statement by statement
resource "aws_iam_role_policy" "file_validator" {
  name = "file-validator"
  role = aws_iam_role.file_validator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Without ListBucket, asking for a missing object returns 403 instead of 404, and the
        # "was this day already validated?" check could not tell "no" from "not allowed"
        Sid      = "ListLake"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.lake.arn
      },
      {
        # Read the files as they land (also the source side of the copies)
        Sid      = "ReadLanding"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.lake.arn}/${local.files.landing_prefix}/*"
      },
      {
        # Copy files into the two output zones, and read back a validated copy's fingerprint
        Sid    = "WriteZones"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.lake.arn}/${local.files.validated_prefix}/*",
          "${aws_s3_bucket.lake.arn}/${local.files.rejected_prefix}/*",
        ]
      },
      {
        # Email AP about a rejected file
        Sid      = "EmailAP"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      },
      {
        # Its own log group only
        Sid      = "WriteLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.file_validator.arn}:*"
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# 3. The Lambda
# ---------------------------------------------------------------------------

# Every value arrives as an environment variable, taken from the config files (docs/configuration.md)
resource "aws_lambda_function" "file_validator" {
  function_name    = "${local.project}-file-validator"
  role             = aws_iam_role.file_validator.arn
  runtime          = "python3.12"
  handler          = "src.lambdas.file_validator.handler.handler"
  filename         = data.archive_file.file_validator.output_path
  source_code_hash = data.archive_file.file_validator.output_base64sha256
  timeout          = local.aws.lambda.timeout_seconds
  memory_size      = local.aws.lambda.memory_mb

  environment {
    variables = {
      KNOWN_ENTITIES   = join(",", local.entities)
      VALIDATED_PREFIX = local.files.validated_prefix
      REJECTED_PREFIX  = local.files.rejected_prefix
      MAX_FILE_MB      = tostring(local.files.max_file_mb)
      ALERT_TOPIC_ARN  = aws_sns_topic.alerts.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.file_validator, aws_iam_role_policy.file_validator]
}

# ---------------------------------------------------------------------------
# 4. The trigger: S3 calls the Lambda for every new .csv under landing/ap/
# ---------------------------------------------------------------------------

# S3 may invoke the function, but only for this bucket in this account
resource "aws_lambda_permission" "s3_invokes_validator" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.file_validator.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.lake.arn
  source_account = data.aws_caller_identity.current.account_id
}

# The event: object created under landing/ap/, ending in .csv. Copies into validated/ and rejected/
# don't match the prefix, so the Lambda never triggers itself.
resource "aws_s3_bucket_notification" "landing" {
  bucket = aws_s3_bucket.lake.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.file_validator.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "${local.files.landing_prefix}/"
    filter_suffix       = ".csv"
  }

  depends_on = [aws_lambda_permission.s3_invokes_validator]
}
