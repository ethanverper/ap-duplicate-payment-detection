"""Glue Data Quality: evaluate the DQDL ruleset (dq/curated_payments.dqdl) on the curated payments.

Where it sits in the pipeline:
    daily_ap_kpis.run() calls `evaluate()` on the clean table right before writing. On AWS Glue the
    outcome of every rule is published to the job's "Data quality" tab and logged. On a laptop the
    Glue-only libraries don't exist, so the evaluation is skipped and says so: the ruleset itself is
    still tested locally (tests/glue_jobs/test_data_quality.py checks it agrees with the contract).

Why a failed rule doesn't stop the job (docs/decisions.md, ADR-008):
    Bad payments were already quarantined row by row. A failed DQ rule means the data drifted in a
    way nobody planned for (an export change, new master data); it is surfaced loudly, in the
    console and the logs, for a person to judge, rather than blocking the day's duplicate alerts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pyspark.sql import DataFrame, SparkSession

from src.common.logging_utils import get_logger

logger = get_logger(__name__)

# The name results are published under in the Glue console
EVALUATION_CONTEXT = "curated_payments"


@dataclass(frozen=True)
class RuleOutcome:
    """The outcome of one DQDL rule."""

    rule: str
    outcome: str  # "Passed" or "Failed"
    failure_reason: str | None = None


def strip_comments(text: str) -> str:
    """Remove '#' comment lines: DQDL itself has no comments, the repo file does for people."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")).strip()


def rules_of(ruleset: str) -> list[str]:
    """The individual rules of a ruleset (one per line inside `Rules = [ ... ]`), for tests and logs."""
    body = re.search(r"Rules\s*=\s*\[(?P<body>.*)\]", ruleset, re.DOTALL)
    if not body:
        raise ValueError("Not a DQDL ruleset: expected Rules = [ ... ]")
    return [line.strip().rstrip(",") for line in body["body"].splitlines() if line.strip()]


def read_ruleset(spark: SparkSession, path: str) -> str:
    """Read the ruleset file (S3 on Glue, a local path on a laptop) and drop its comments."""
    return strip_comments("\n".join(row.value for row in spark.read.text(path).collect()))


def evaluate(spark: SparkSession, frame: DataFrame, ruleset: str) -> list[RuleOutcome] | None:
    """Evaluate the ruleset with Glue Data Quality and publish the results.

    Args:
        spark: The Spark session (Glue's, on AWS).
        frame: The curated payments.
        ruleset: DQDL text, comments removed.

    Returns:
        One outcome per rule, or None when not running on Glue (local runs and tests).
    """
    # The Glue-only libraries exist only inside a Glue job
    try:
        from awsglue.context import GlueContext
        from awsglue.dynamicframe import DynamicFrame
        from awsgluedq.transforms import EvaluateDataQuality
    except ImportError:
        logger.info(
            "Glue Data Quality skipped: not running on AWS Glue", extra={"fields": {"rules": len(rules_of(ruleset))}}
        )
        return None

    # Glue Data Quality works on DynamicFrames: wrap the DataFrame, evaluate, publish to the console
    glue_context = GlueContext(spark.sparkContext)
    dynamic = DynamicFrame.fromDF(frame, glue_context, EVALUATION_CONTEXT)
    results = EvaluateDataQuality.apply(
        frame=dynamic,
        ruleset=ruleset,
        publishing_options={
            "dataQualityEvaluationContext": EVALUATION_CONTEXT,
            "enableDataQualityResultsPublishing": True,
            "enableDataQualityCloudWatchMetrics": False,  # no custom metrics: they cost money
        },
    )

    # One outcome per rule, logged so a failure is visible next to the job's other output
    outcomes = [RuleOutcome(row["Rule"], row["Outcome"], row["FailureReason"]) for row in results.toDF().collect()]
    failed = [o for o in outcomes if o.outcome != "Passed"]
    logger.log(
        30 if failed else 20,  # WARNING when any rule failed, INFO otherwise
        "Glue Data Quality evaluated",
        extra={"fields": {"rules": len(outcomes), "failed": [f"{o.rule}: {o.failure_reason}" for o in failed]}},
    )
    return outcomes
