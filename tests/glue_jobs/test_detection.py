"""The duplicate detector, graded against the answer key, and the traps it must not fall into."""

from __future__ import annotations

from pyspark.sql import functions as F

from src.evaluation.grading import grade
from src.generators.reference import MXN_PER_USD
from src.glue_jobs import daily_ap_kpis, transforms
from tests.glue_jobs.conftest import job_args

RATES = {"USD": 1.0, "MXN": 1 / MXN_PER_USD}


def _suspects(tables) -> list[dict]:
    return [row.asDict() for row in tables["gold/duplicate_suspects"].collect()]


def test_invoice_numbers_typed_four_ways_normalize_to_one(spark) -> None:
    """Case, separators and leading zeros carry no meaning."""
    numbers = spark.createDataFrame([("FAC-0457",), ("FAC0457",), ("FAC-457",), ("fac 457",), ("MTC/00055",)], ["n"])
    keys = [row["k"] for row in numbers.select(transforms.normalized_invoice_number(F.col("n")).alias("k")).collect()]
    assert keys == ["FAC457"] * 4 + ["MTC55"]


def test_every_planted_problem_is_found_with_the_right_label(tables, truth) -> None:
    """Recall 100% for each kind, with the rule and confidence that kind should get, to the cent."""
    result = grade(_suspects(tables), truth, RATES)

    for score in result.by_kind.values():
        assert score.found == score.planted > 0, score
        assert abs(score.found_at_risk - score.planted_at_risk) < 1.0, score
    assert result.recall_pct == 100.0


def test_no_false_alarms_on_legitimate_patterns(tables, truth) -> None:
    """Monthly subscriptions (same amount every month) and advance + final payments are not flagged."""
    result = grade(_suspects(tables), truth, RATES)
    assert result.false_alarms == []
    assert result.precision_pct == 100.0


def test_the_seven_day_window_keeps_out_monthly_fees_and_chance_matches(extracted, generated, truth) -> None:
    """Proof the traps are real: with a 31-day window the same rule raises false alarms.

    Two kinds appear (docs/lessons-learned.md, lesson 1):
      - monthly subscriptions: the same amount every month, ~30 days apart;
      - pure chance: two different freight invoices of exactly the same amount, weeks apart.
    That is why "same amount, new number" is MEDIUM confidence and the window is short.
    """
    rows, reference = extracted
    wide = daily_ap_kpis.transform(rows, reference, job_args(generated, window_days=31))
    result = grade([r.asDict() for r in wide["gold/duplicate_suspects"].collect()], truth, RATES)
    flagged_suppliers = {row["supplier_id"] for row in result.false_alarms}

    assert flagged_suppliers & {"SUP-CLOUDOPS", "SUP-TELENET"}, "monthly fees should be mistaken for resubmissions"
    assert all(row["confidence"] == "MEDIUM" for row in result.false_alarms)
    assert result.recall_pct == 100.0  # a wider window finds the same real problems, plus noise


def test_a_file_loaded_twice_changes_no_suspect(extracted, generated, tables) -> None:
    """Duplicate ROWS (same payment_id) are not duplicate PAYMENTS: replaying every file changes nothing."""
    rows, reference = extracted
    replayed = daily_ap_kpis.transform(rows.unionByName(rows), reference, job_args(generated))

    def ids(frame):
        return sorted(row["suspect_payment_id"] for row in frame.collect())

    assert ids(replayed["gold/duplicate_suspects"]) == ids(tables["gold/duplicate_suspects"])
    quality = replayed["gold/data_quality"].agg(F.sum("duplicate_rows"), F.sum("curated_rows")).first()
    assert quality[0] == quality[1] > 6000
