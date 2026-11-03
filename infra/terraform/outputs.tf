# =============================================================================
# outputs.tf — names that the Makefile and scripts need after `terraform apply`.
# Read one with: terraform -chdir=infra/terraform output -raw <name>
# =============================================================================

# Where the ERP files land (and everything else of the lake)
output "bucket" {
  value = aws_s3_bucket.lake.bucket
}

# The Lambda that checks every landed file
output "file_validator_function" {
  value = aws_lambda_function.file_validator.function_name
}

# The daily job (`make kpis` starts it)
output "glue_job_name" {
  value = aws_glue_job.daily_ap_kpis.name
}

# The AP emails topic, and who is subscribed to what (email -> filter policy)
output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "alert_subscriptions" {
  value = { for key, subscription in aws_sns_topic_subscription.email : subscription.endpoint => subscription.filter_policy }
}

# Where to run the KPI queries
output "athena_workgroup" {
  value = aws_athena_workgroup.analytics.name
}

output "athena_database" {
  value = aws_glue_catalog_database.lake.name
}
