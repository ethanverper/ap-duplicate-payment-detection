# =============================================================================
# 4_analytics.tf — Athena: the lake's tables in the Glue Data Catalog, and a capped workgroup.
#
# Athena stores no data: the catalog tells it, per table, which S3 folder to read, the columns and
# the format. Every table is partitioned by dt (the payment date) with PARTITION PROJECTION: Athena
# computes the dt=... folders from a date range, so new days are queryable as soon as Glue writes.
# =============================================================================

locals {
  # Athena database names can't contain hyphens
  database_name = replace(local.project, "-", "_")

  # The lake's tables. Naming: table = folder with "/" replaced by "_". Column types match what
  # src/glue_jobs/transforms.py writes (Spark double -> double, long -> bigint, date -> date).
  lake_tables = {
    curated_payments = {
      description = "One row per real payment: validated, deduplicated, typed, in USD, with supplier category."
      folder      = "curated/payments"
      columns = {
        payment_id              = "string", entity = "string", category = "string", supplier_id = "string",
        supplier_invoice_number = "string", invoice_key = "string", invoice_date = "date", due_date = "date",
        invoice_amount          = "double", currency = "string", payment_terms = "string", discount_pct = "double",
        discount_days           = "int", payment_date = "date", paid_amount = "double", payment_type = "string",
        usd_rate                = "double", invoice_amount_usd = "double", paid_amount_usd = "double"
      }
    }
    quarantine_payments = {
      description = "Payments that broke a business rule, as received, with the first rule they broke."
      folder      = "quarantine/payments"
      columns = {
        payment_id        = "string", entity = "string", supplier_id = "string", supplier_invoice_number = "string",
        invoice_date      = "string", due_date = "string", invoice_amount = "string", currency = "string",
        payment_terms     = "string", payment_date = "string", paid_amount = "string", payment_type = "string",
        quarantine_reason = "string"
      }
    }
    gold_duplicate_suspects = {
      description = "KPI 1. One row per suspect payment: rule, confidence (HIGH/MEDIUM), amount at risk."
      folder      = "gold/duplicate_suspects"
      columns = {
        entity                  = "string", category = "string", supplier_id = "string", rule = "string", confidence = "string",
        suspect_payment_id      = "string", original_payment_id = "string", suspect_invoice_number = "string",
        original_invoice_number = "string", invoice_amount = "double", currency = "string",
        amount_at_risk          = "double", amount_at_risk_usd = "double"
      }
    }
    gold_payment_performance = {
      description = "KPIs 2-3. Grain dt, entity, category, supplier: on-time counts and $, DPO inputs."
      folder      = "gold/payment_performance"
      columns = {
        entity           = "string", category = "string", supplier_id = "string", settling_payments = "bigint",
        on_time_payments = "bigint", paid_usd = "double", on_time_paid_usd = "double", days_x_usd = "double"
      }
    }
    gold_discounts_lost = {
      description = "KPI 4. One row per invoice paid after its early-payment discount window."
      folder      = "gold/discounts_lost"
      columns = {
        entity        = "string", category = "string", supplier_id = "string", supplier_invoice_number = "string",
        payment_id    = "string", invoice_date = "date", discount_deadline = "date", payment_date = "date",
        payment_terms = "string", invoice_amount_usd = "double", discount_lost_usd = "double"
      }
    }
    gold_data_quality = {
      description = "Row reconciliation per day: file rows = curated + duplicate rows + quarantined."
      folder      = "gold/data_quality"
      columns = {
        file_rows   = "bigint", curated_rows = "bigint", quarantined_rows = "bigint", duplicate_rows = "bigint",
        is_balanced = "boolean"
      }
    }
  }
}

# The database: a folder of table definitions in the Glue Data Catalog (free)
resource "aws_glue_catalog_database" "lake" {
  name        = local.database_name
  description = "AP duplicate payments and payment performance: curated payments, quarantine and gold KPI tables."
}

# One table per lake folder, all Parquet, all with partition projection on dt
resource "aws_glue_catalog_table" "lake" {
  for_each = local.lake_tables

  name          = each.key
  database_name = aws_glue_catalog_database.lake.name
  description   = each.value.description
  table_type    = "EXTERNAL_TABLE" # dropping the table never deletes data

  # dt is a folder (dt=2026-10-30), not a column inside the files
  partition_keys {
    name = "dt"
    type = "string"
  }

  parameters = {
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.range"         = "NOW-${local.aws.athena.projection_days_back}DAYS,NOW+1DAYS"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"
    "storage.location.template"   = "s3://${aws_s3_bucket.lake.bucket}/${each.value.folder}/dt=$${dt}/"
    "classification"              = "parquet"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.lake.bucket}/${each.value.folder}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    # The columns, in the order declared above
    dynamic "columns" {
      for_each = each.value.columns

      content {
        name = columns.key
        type = columns.value
      }
    }
  }
}

# The workgroup: where results go, and a hard limit on what one query may scan (and cost)
resource "aws_athena_workgroup" "analytics" {
  name          = local.project
  force_destroy = true # destroy also removes the saved query history

  configuration {
    # Clients can't override these settings: the scan limit always applies
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = false
    bytes_scanned_cutoff_per_query     = local.aws.athena.bytes_scanned_cutoff_per_query

    # Results land in athena-results/, which a lifecycle rule (0_main.tf) deletes after 7 days
    result_configuration {
      output_location = "s3://${aws_s3_bucket.lake.bucket}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}
