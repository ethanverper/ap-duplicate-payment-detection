"""Row rules, reconciliation and the payment-performance KPIs on the generated history."""

from __future__ import annotations

from pathlib import Path

from pyspark.sql import functions as F

from src.glue_jobs import daily_ap_kpis, transforms
from tests.glue_jobs.conftest import job_args

GOOD = {
    "payment_id": "PAY-MX01-999001", "entity": "MX01", "supplier_id": "SUP-PRECISA",
    "supplier_invoice_number": "FAC-9001", "invoice_date": "2026-10-01", "due_date": "2026-10-31",
    "invoice_amount": "1000.00", "currency": "MXN", "payment_terms": "2/10 NET 30",
    "payment_date": "2026-10-09", "paid_amount": "1000.00", "payment_type": "FULL",
}  # fmt: skip


def test_each_bad_payment_is_quarantined_with_its_reason(spark, extracted) -> None:
    """Business rules on single rows: the bad payment is set aside, the good one loads."""
    _rows, reference = extracted
    cases = {
        "ok": {},
        "unknown_supplier_for_entity": {"supplier_id": "SUP-GHOST"},
        "currency_differs_from_entity": {"currency": "USD"},
        "unknown_payment_type": {"payment_type": "REFUND"},
        "paid_amount_not_positive": {"paid_amount": "-10.00"},
        "due_date_before_invoice_date": {"due_date": "2026-09-01"},
        "paid_before_invoice_date": {"payment_date": "2026-09-15"},
    }
    rows = spark.createDataFrame([{**GOOD, **change, "payment_id": name} for name, change in cases.items()],
                                 schema=transforms.FILE_SCHEMA)  # fmt: skip
    enriched = transforms.add_columns(rows, reference["entities"], reference["suppliers"])
    valid, quarantine = transforms.validate_rows(enriched)

    assert [row["payment_id"] for row in valid.collect()] == ["ok"]
    assert {r["payment_id"]: r["quarantine_reason"] for r in quarantine.collect()} == {
        name: name for name in cases if name != "ok"
    }


def test_every_file_row_is_accounted_for(tables) -> None:
    """file rows = curated + duplicate rows + quarantined, every day."""
    rows = tables["gold/data_quality"].collect()
    assert all(row["is_balanced"] for row in rows)
    assert sum(row["file_rows"] for row in rows) == sum(row["curated_rows"] for row in rows) > 6000


def _rollup(tables, table: str, *keys: str):
    """Sum every measure of a gold table up to the given keys."""
    frame = tables[table]
    measures = [c for c in frame.columns if c not in ("dt", "entity", "category", "supplier_id")]
    return frame.groupBy(*keys).agg(*[F.sum(c).alias(c) for c in measures]).collect()


def test_the_late_supplier_stands_out_in_on_time_pct(tables) -> None:
    """Andina in MX01 is never paid on time; the rest of MX01 almost always is."""
    rows = {r["supplier_id"]: r for r in _rollup(tables, "gold/payment_performance", "entity", "supplier_id")
            if r["entity"] == "MX01"}  # fmt: skip
    assert rows["SUP-ANDINA"]["on_time_payments"] == 0
    others = [r for s, r in rows.items() if s != "SUP-ANDINA"]
    assert sum(r["on_time_payments"] for r in others) / sum(r["settling_payments"] for r in others) > 0.9


def test_friday_only_payments_lose_far_more_discounts(tables) -> None:
    """US01 (Fridays, scheduled to due dates) loses many times more discount dollars than MX01."""
    lost = {
        r["entity"]: r["discount_lost_usd"]
        for r in tables["gold/discounts_lost"]
        .groupBy("entity")
        .agg(F.sum("discount_lost_usd").alias("discount_lost_usd"))
        .collect()
    }  # noqa: E501
    assert lost["US01"] > 5 * lost["MX01"] > 0


def test_full_run_on_disk_is_idempotent(spark, generated, tmp_path: Path) -> None:
    """Run the job twice into the same folder: same row counts, nothing appended."""
    args = job_args(generated, tmp_path / "lake")
    first = daily_ap_kpis.run(spark, args)
    second = daily_ap_kpis.run(spark, args)

    assert first["all_days_balanced"]
    assert first["rows_per_table"] == second["rows_per_table"]
    written = spark.read.parquet(str(tmp_path / "lake" / "gold" / "duplicate_suspects")).count()
    assert written == first["rows_per_table"]["gold/duplicate_suspects"] == 26
