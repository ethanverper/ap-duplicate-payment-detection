"""Glue job (PySpark): validated AP files → clean payments, duplicate suspects and payment KPIs.

Where it sits in the pipeline:
    The file_validator Lambda copies every good ERP file to s3://<bucket>/validated/ap/. This job
    runs once a day (trigger disabled by default) or on demand (`make kpis`):

        extract()   validated files + reference CSVs
        transform   transforms.py: rows → validate → dedupe → curated → suspects, performance, discounts
        reconcile   every file row is curated, a duplicate row, or quarantined, on every day
        load()      curated/, quarantine/, gold/<table>/  (Parquet, partitioned by dt)

The same file runs on AWS Glue 5.0 and on a laptop (`make kpis-local`, local Spark, no AWS): it
uses plain Spark DataFrames, never Glue-only APIs, so every rule is tested before a deploy
(same approach as manufacturing-scrap-and-stockout-alerts, its ADR-012).

Why a full refresh on every run: 90 days are ~6,300 payments, and a resubmission can pair an
invoice with one from days earlier, so the detector must see every day anyway. Dynamic partition
overwrite replaces only the dates present: re-running gives the same tables (idempotent).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from pyspark.sql import DataFrame, SparkSession

from src.common.logging_utils import get_logger
from src.glue_jobs import transforms

logger = get_logger(__name__)

# Lake layout (protocol, not settings: every reader depends on it)
REFERENCE = "reference"
CURATED = "curated/payments"
QUARANTINE = "quarantine/payments"
GOLD = "gold"

# Reference table name -> its Spark schema (one folder per table under reference/)
REFERENCE_TABLES = {"suppliers": transforms.SUPPLIERS_SCHEMA, "entities": transforms.ENTITIES_SCHEMA}


@dataclass(frozen=True)
class JobArgs:
    """Everything the job needs. On Glue, Terraform passes each value as a job argument.

    Attributes:
        input_root: Where the files and reference/ are: "s3://<bucket>" or a local folder.
        files_prefix: Folder of the AP files under input_root ("validated/ap" on AWS; locally the
            generator's "landing/ap", since there is no Lambda in between).
        output_root: Where curated/, quarantine/ and gold/ go.
        overpayment_tolerance: config: detection.overpayment_tolerance.
        resubmission_window_days: config: detection.resubmission_window_days.
    """

    input_root: str
    files_prefix: str
    output_root: str
    overpayment_tolerance: float
    resubmission_window_days: int


def parse_args(argv: list[str] | None = None) -> JobArgs:
    """Read the job arguments; Glue's own extra arguments (--JOB_ID, --TempDir...) are ignored."""
    parser = argparse.ArgumentParser(description="Build AP duplicate suspects and payment KPIs.")
    parser.add_argument("--input_root", required=True)
    parser.add_argument("--files_prefix", required=True)
    parser.add_argument("--output_root", required=True)
    parser.add_argument("--overpayment_tolerance", type=float, required=True)
    parser.add_argument("--resubmission_window_days", type=int, required=True)
    known, _glue_extras = parser.parse_known_args(argv)
    return JobArgs(
        input_root=known.input_root.rstrip("/"),
        files_prefix=known.files_prefix.strip("/"),
        output_root=known.output_root.rstrip("/"),
        overpayment_tolerance=known.overpayment_tolerance,
        resubmission_window_days=known.resubmission_window_days,
    )


def build_spark(app_name: str = "daily-ap-kpis") -> SparkSession:
    """Spark session with UTC time and dynamic partition overwrite (re-runs replace, never append)."""
    return (
        SparkSession.builder.appName(app_name)
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.sources.partitionOverwriteMode", "dynamic")
        .getOrCreate()
    )


def extract(spark: SparkSession, args: JobArgs) -> tuple[DataFrame, dict[str, DataFrame]]:
    """Read every AP file row (trailers included) and the reference tables.

    recursiveFileLookup reads every CSV under the prefix and ignores the entity=/dt= folder names:
    the entity and the payment date come from the rows themselves (the Lambda checked they agree).
    """
    rows = (
        spark.read.option("header", "true")
        .option("recursiveFileLookup", "true")
        .schema(transforms.FILE_SCHEMA)
        .csv(f"{args.input_root}/{args.files_prefix}/")
    )
    reference = {
        name: spark.read.option("header", "true").schema(schema).csv(f"{args.input_root}/{REFERENCE}/{name}/")
        for name, schema in REFERENCE_TABLES.items()
    }
    return rows, reference


def transform(rows: DataFrame, reference: dict[str, DataFrame], args: JobArgs) -> dict[str, DataFrame]:
    """Run every step of transforms.py in order. Returns output tables by their path."""
    # Rows of every file, without the trailers, typed and joined to master data
    file_rows = transforms.drop_trailers(rows)
    enriched = transforms.add_columns(file_rows, reference["entities"], reference["suppliers"])

    # Quarantine bad payments, drop rows loaded twice, keep the clean table (cached: 4 tables use it)
    valid, quarantine = transforms.validate_rows(enriched)
    clean = transforms.curated(transforms.deduplicate_rows(valid)).cache()

    return {
        CURATED: clean,
        QUARANTINE: quarantine.select(*transforms.FILE_SCHEMA.fieldNames(), "dt", "quarantine_reason"),
        f"{GOLD}/duplicate_suspects": transforms.detect_duplicates(
            clean, args.overpayment_tolerance, args.resubmission_window_days
        ),
        f"{GOLD}/payment_performance": transforms.build_payment_performance(clean),
        f"{GOLD}/discounts_lost": transforms.build_discounts_lost(clean),
        f"{GOLD}/data_quality": transforms.build_data_quality(enriched, valid, clean, quarantine),
    }


def summarize(tables: dict[str, DataFrame]) -> dict[str, object]:
    """Run summary for the logs: reconciliation totals, suspects found, rows per table."""
    quality = tables[f"{GOLD}/data_quality"].collect()
    suspects = tables[f"{GOLD}/duplicate_suspects"].groupBy("confidence").count().collect()
    return {
        "file_rows": sum(row["file_rows"] for row in quality),
        "curated_rows": sum(row["curated_rows"] for row in quality),
        "duplicate_rows": sum(row["duplicate_rows"] for row in quality),
        "quarantined_rows": sum(row["quarantined_rows"] for row in quality),
        "all_days_balanced": all(row["is_balanced"] for row in quality),
        "suspects_by_confidence": {row["confidence"]: row["count"] for row in suspects},
        "rows_per_table": {path: frame.count() for path, frame in tables.items()},
    }


def load(tables: dict[str, DataFrame], output_root: str) -> None:
    """Write every table as Parquet, partitioned by dt, replacing only the dates present."""
    for path, frame in tables.items():
        frame.write.mode("overwrite").partitionBy("dt").parquet(f"{output_root}/{path}/")


def run(spark: SparkSession, args: JobArgs) -> dict[str, object]:
    """Extract, transform, reconcile, then load. Nothing is written if any row went missing.

    Raises:
        RuntimeError: If the row counts don't reconcile on some day.
    """
    rows, reference = extract(spark, args)
    tables = transform(rows, reference, args)

    # Check BEFORE writing: unbalanced data must never reach gold/
    summary = summarize(tables)
    if not summary["all_days_balanced"]:
        raise RuntimeError(f"Row counts don't reconcile: {summary}")

    load(tables, args.output_root)
    logger.info("AP KPIs written", extra={"fields": {"output_root": args.output_root, **summary}})
    return summary


def main(argv: list[str] | None = None) -> None:
    """Entry point, the same on Glue and on a laptop."""
    run(build_spark(), parse_args(argv))


if __name__ == "__main__":
    main()
