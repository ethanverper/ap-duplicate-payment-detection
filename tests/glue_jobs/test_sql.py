"""The sql/ queries, run with Spark SQL on the job's gold tables (the same text runs on Athena)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyspark.sql import functions as F

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


@pytest.fixture(scope="module")
def results(spark, tables) -> dict[str, list[dict]]:
    """Every query's rows, with each table registered under its Athena name ("/" -> "_")."""
    for folder, frame in tables.items():
        frame.createOrReplaceTempView(folder.replace("/", "_"))
    return {
        path.stem: [row.asDict() for row in spark.sql(path.read_text(encoding="utf-8").strip().rstrip(";")).collect()]
        for path in sorted(SQL_DIR.glob("*.sql"))
    }


def _level(rows: list[dict], level: str) -> list[dict]:
    return [row for row in rows if row["level"] == level]


def test_every_query_runs_and_returns_rows(results) -> None:
    assert sorted(results) == [
        "01_duplicates_at_risk", "02_duplicate_review_list", "03_on_time_and_dpo",
        "04_discounts_lost", "05_data_quality",
    ]  # fmt: skip
    assert all(results.values())


def test_rollup_total_equals_the_sum_of_entities_and_the_table(results, tables) -> None:
    rows = results["01_duplicates_at_risk"]
    total = _level(rows, "0 All entities")[0]["at_risk_usd"]
    entities = sum(row["at_risk_usd"] for row in _level(rows, "1 Entity"))
    table = tables["gold/duplicate_suspects"].agg(F.sum("amount_at_risk_usd")).first()[0]
    assert total == pytest.approx(entities, abs=0.05) == pytest.approx(table, abs=0.05)


def test_review_list_puts_proven_duplicates_first(results) -> None:
    confidences = [row["confidence"] for row in results["02_duplicate_review_list"]]
    assert confidences == sorted(confidences, key=lambda c: c != "HIGH")
    assert len(confidences) == 26


def test_the_late_supplier_is_at_the_top_of_the_on_time_ranking(results) -> None:
    suppliers = _level(results["03_on_time_and_dpo"], "3 Supplier")
    worst = min(suppliers, key=lambda row: row["on_time_pct"])
    assert (worst["supplier_id"], worst["on_time_pct"]) == ("SUP-ANDINA", 0.0)


def test_discounts_lost_concentrate_in_the_friday_only_entity(results) -> None:
    entities = {row["entity"]: row["discount_lost_usd"] for row in _level(results["04_discounts_lost"], "1 Entity")}
    assert entities["US01"] > 5 * entities["MX01"]
