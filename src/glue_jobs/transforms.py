"""Pure PySpark transformations of the daily AP job: DataFrames in, DataFrames out.

Where it sits in the pipeline:
    daily_ap_kpis.py reads the validated files and the reference CSVs, calls these functions in
    order, and writes what they return. Nothing here touches S3 or AWS, so every rule is tested
    locally with a small Spark session (tests/glue_jobs/).

    validated files → drop trailers → add_columns → validate_rows → deduplicate_rows → curated payments
                                                      └→ quarantine (with a reason)

    curated payments → detect_duplicates          (HIGH and MEDIUM suspects)
                     → build_payment_performance  (on time, DPO)
                     → build_discounts_lost

Two different kinds of "duplicate" (do not confuse them):
    - a duplicate ROW: the same payment_id twice in the data (a file loaded twice). It is the same
      payment, so one copy is dropped (deduplicate_rows);
    - a duplicate PAYMENT: two different payments (two payment_ids) for the same supplier invoice.
      Real money left twice: that is what detect_duplicates looks for.
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType

from src.common.schema import ADVANCE, COLUMNS, FINAL, FULL, PAYMENT_TYPES, TRAILER_MARKER

# ---------------------------------------------------------------------------
# Input schemas. Every file column is read as text and parsed explicitly, so a bad value is a
# quarantined row with a reason, never a silent null.
# ---------------------------------------------------------------------------
FILE_SCHEMA = StructType([StructField(name, StringType()) for name in COLUMNS])
SUPPLIERS_SCHEMA = (
    "entity STRING, supplier_id STRING, supplier_name STRING, category STRING, payment_terms STRING, "
    "net_days INT, discount_pct DOUBLE, discount_days INT"
)
ENTITIES_SCHEMA = "entity STRING, entity_name STRING, currency STRING, usd_rate DOUBLE"

# Detection rules and confidence levels (they appear in the gold table and in the alerts)
RULE_SAME_INVOICE_PAID_TWICE = "SAME_INVOICE_PAID_TWICE"  # same normalized number, paid twice in full
RULE_RETYPED_NUMBER = "SAME_INVOICE_RETYPED_NUMBER"  # ...and the number was typed differently
RULE_ADVANCE_THEN_FULL = "ADVANCE_THEN_FULL_AMOUNT"  # an advance, then the whole amount again
RULE_RESUBMITTED = "RESUBMITTED_UNDER_NEW_NUMBER"  # same amount, new number, dated days apart
HIGH, MEDIUM = "HIGH", "MEDIUM"

# Payments that settle an invoice (an ADVANCE is early by design: it is not judged on timeliness)
SETTLING_TYPES = (FULL, FINAL)


# ---------------------------------------------------------------------------
# 1. Columns
# ---------------------------------------------------------------------------


def drop_trailers(raw: DataFrame) -> DataFrame:
    """Remove the control-trailer line of every file (the Lambda already checked it)."""
    return raw.filter(F.col("payment_id") != TRAILER_MARKER)


def normalized_invoice_number(column: Column) -> Column:
    """An invoice number reduced to what identifies it: letters + digits without leading zeros.

    "FAC-0457", "FAC0457", "FAC-457" and "fac 457" all become "FAC457": the same invoice typed
    four ways (spec problem 1). Case, separators and leading zeros carry no meaning.
    """
    letters = F.regexp_replace(F.upper(column), "[^A-Z]", "")
    digits = F.regexp_replace(F.regexp_replace(column, "[^0-9]", ""), "^0+", "")
    return F.concat(letters, digits)


def add_columns(payments: DataFrame, entities: DataFrame, suppliers: DataFrame) -> DataFrame:
    """Parse text columns and add what every rule and KPI needs.

    Args:
        payments: File rows, all text (FILE_SCHEMA), trailers dropped.
        entities: Reference entities (currency and USD rate).
        suppliers: Reference suppliers per entity (category, discount terms).

    Returns:
        Typed payments plus: dt (payment date, the partition), USD amounts, the normalized invoice
        number, the supplier's category and discount terms, and whether master data knows the
        supplier for that entity (_supplier_known, used by validate_rows).
    """
    # Typed values (unreadable ones become null and are quarantined by validate_rows)
    typed = (
        payments.withColumn("invoice_date_d", F.to_date("invoice_date", "yyyy-MM-dd"))
        .withColumn("due_date_d", F.to_date("due_date", "yyyy-MM-dd"))
        .withColumn("payment_date_d", F.to_date("payment_date", "yyyy-MM-dd"))
        .withColumn("invoice_amount_n", F.col("invoice_amount").cast("decimal(18,2)"))
        .withColumn("paid_amount_n", F.col("paid_amount").cast("decimal(18,2)"))
        .withColumn("dt", F.col("payment_date"))
        .withColumn("invoice_key", normalized_invoice_number(F.col("supplier_invoice_number")))
    )

    # The entity's currency and rate: every KPI is reported in USD
    rates = entities.select("entity", F.col("currency").alias("_entity_currency"), "usd_rate")
    typed = typed.join(rates, "entity", "left")
    typed = typed.withColumn("invoice_amount_usd", F.round(F.col("invoice_amount_n") * F.col("usd_rate"), 2))
    typed = typed.withColumn("paid_amount_usd", F.round(F.col("paid_amount_n") * F.col("usd_rate"), 2))

    # The supplier's category and discount terms, as master data says for that entity
    master = suppliers.select("entity", "supplier_id", "category", "discount_pct", "discount_days")
    master = master.withColumn("_supplier_known", F.lit(True))
    return typed.join(master, ["entity", "supplier_id"], "left")


# ---------------------------------------------------------------------------
# 2. Row rules: quarantine one payment, keep the rest (docs/decisions.md, ADR-002)
# ---------------------------------------------------------------------------


def validate_rows(payments: DataFrame) -> tuple[DataFrame, DataFrame]:
    """Split payments into valid ones and quarantined ones, with the first rule each broke.

    The Lambda already proved each FILE is complete and well-formed. These are business rules on
    single payments: the payment is set aside for an analyst, the rest of the day still loads.

    Returns:
        (valid payments, quarantined payments with a `quarantine_reason` column).
    """
    # Rules in reporting order: the first one that fails names the problem
    checks: list[tuple[Column, str]] = [
        (F.col("_entity_currency").isNull(), "unknown_entity"),
        (F.col("_supplier_known").isNull(), "unknown_supplier_for_entity"),
        (F.col("currency") != F.col("_entity_currency"), "currency_differs_from_entity"),
        (~F.col("payment_type").isin(*PAYMENT_TYPES), "unknown_payment_type"),
        (F.col("invoice_amount_n").isNull() | (F.col("invoice_amount_n") <= 0), "invoice_amount_not_positive"),
        (F.col("paid_amount_n").isNull() | (F.col("paid_amount_n") <= 0), "paid_amount_not_positive"),
        (F.col("due_date_d") < F.col("invoice_date_d"), "due_date_before_invoice_date"),
        (F.col("payment_date_d") < F.col("invoice_date_d"), "paid_before_invoice_date"),
        (F.length("invoice_key") == 0, "invoice_number_empty"),
    ]
    reason = F.coalesce(*[F.when(condition, F.lit(name)) for condition, name in checks])
    tagged = payments.withColumn("quarantine_reason", reason)

    # Valid rows lose the empty reason column
    valid = tagged.filter(F.col("quarantine_reason").isNull()).drop("quarantine_reason")
    quarantine = tagged.filter(F.col("quarantine_reason").isNotNull())
    return valid, quarantine


def deduplicate_rows(valid: DataFrame) -> DataFrame:
    """Keep one row per payment_id (the same payment loaded twice is still one payment)."""
    return valid.dropDuplicates(["payment_id"])


def curated(payments: DataFrame) -> DataFrame:
    """The clean payments table: one row per real payment, typed, in USD, with its supplier's category."""
    return payments.select(
        "payment_id",
        "entity",
        "category",
        "supplier_id",
        "supplier_invoice_number",
        "invoice_key",
        F.col("invoice_date_d").alias("invoice_date"),
        F.col("due_date_d").alias("due_date"),
        F.col("invoice_amount_n").cast("double").alias("invoice_amount"),
        "currency",
        "payment_terms",
        "discount_pct",
        "discount_days",
        F.col("payment_date_d").alias("payment_date"),
        F.col("paid_amount_n").cast("double").alias("paid_amount"),
        "payment_type",
        "usd_rate",
        "invoice_amount_usd",
        "paid_amount_usd",
        "dt",
    )


# ---------------------------------------------------------------------------
# 3. Duplicate payments
# ---------------------------------------------------------------------------


def _suspect_columns(frame: DataFrame) -> DataFrame:
    """The columns every suspect row has, in one order (both rules produce the same shape)."""
    return frame.select(
        "dt",
        "entity",
        "category",
        "supplier_id",
        "rule",
        "confidence",
        "suspect_payment_id",
        "original_payment_id",
        "suspect_invoice_number",
        "original_invoice_number",
        "invoice_amount",
        "currency",
        "amount_at_risk",
        "amount_at_risk_usd",
    )


def detect_overpaid_invoices(payments: DataFrame, tolerance: float) -> DataFrame:
    """HIGH confidence: one supplier invoice (same normalized number) paid more than its amount.

    This single rule catches two planted problems with one piece of arithmetic:
      - the same invoice keyed twice with its number typed differently, paid twice (spec problem 1);
      - an advance, then the FULL amount instead of the balance (spec problem 3).
    A legitimate advance + final adds up to exactly the invoice amount, so it is never flagged.

    Args:
        payments: Curated payments.
        tolerance: Excess below this (in the invoice currency) is rounding, not an overpayment.

    Returns:
        One row per overpaid invoice: the latest payment is the suspect, the first is the original.
    """
    invoice = ["entity", "supplier_id", "invoice_key"]

    # Per invoice: how much was paid against how much it was for, how the number was typed, and
    # its first and last payment (min/max of (date, id) gives a stable order)
    when_paid = F.struct("payment_date", "payment_id")
    totals = payments.groupBy(*invoice).agg(
        F.max("invoice_amount").alias("invoice_amount"),
        F.max("usd_rate").alias("usd_rate"),
        F.sum("paid_amount").alias("total_paid"),
        F.countDistinct("supplier_invoice_number").alias("spellings"),
        F.max(F.when(F.col("payment_type") == ADVANCE, 1).otherwise(0)).alias("had_advance"),
        F.min(when_paid).getField("payment_id").alias("original_payment_id"),
        F.max(when_paid).getField("payment_id").alias("suspect_payment_id"),
    )
    overpaid = totals.withColumn("amount_at_risk", F.round(F.col("total_paid") - F.col("invoice_amount"), 2))
    overpaid = overpaid.filter(F.col("amount_at_risk") > tolerance)

    # Name the rule: retyped number, advance then full, or plainly paid twice
    rule = (
        F.when(F.col("spellings") > 1, RULE_RETYPED_NUMBER)
        .when(F.col("had_advance") == 1, RULE_ADVANCE_THEN_FULL)
        .otherwise(RULE_SAME_INVOICE_PAID_TWICE)
    )
    overpaid = overpaid.withColumn("rule", rule).withColumn("confidence", F.lit(HIGH))

    # Attach the suspect payment's own details (its date is the partition)
    suspect = payments.select(
        F.col("payment_id").alias("suspect_payment_id"),
        "dt",
        "category",
        "currency",
        F.col("supplier_invoice_number").alias("suspect_invoice_number"),
    )
    original = payments.select(
        F.col("payment_id").alias("original_payment_id"),
        F.col("supplier_invoice_number").alias("original_invoice_number"),
    )
    result = overpaid.join(suspect, "suspect_payment_id").join(original, "original_payment_id")
    result = result.withColumn("amount_at_risk_usd", F.round(F.col("amount_at_risk") * F.col("usd_rate"), 2))
    return _suspect_columns(result)


def detect_resubmissions(payments: DataFrame, window_days: int) -> DataFrame:
    """MEDIUM confidence: the same amount invoiced again under ANOTHER number, a few days later.

    A supplier that resends an invoice with a new number (spec problem 2) can't be caught by the
    number. What gives it away: same supplier, same entity, exactly the same amount, and invoice
    dates within a few days. A monthly fee with the same amount is ~30 days apart, so it is not a
    match. It stays MEDIUM: an analyst should look at the invoices before claiming the money back.

    Args:
        payments: Curated payments.
        window_days: Maximum days between the two invoice dates.

    Returns:
        One row per resubmitted invoice: the later invoice's payment is the suspect.
    """
    # One row per invoice: its amount, date, and the payment that settled it (first by date)
    first_payment = Window.partitionBy("entity", "supplier_id", "invoice_key").orderBy("payment_date", "payment_id")
    invoices = (
        payments.withColumn("_n", F.row_number().over(first_payment))
        .filter(F.col("_n") == 1)
        .select("entity", "supplier_id", "invoice_key", "invoice_date", "invoice_amount", "payment_id",
                "supplier_invoice_number", "dt", "category", "currency", "paid_amount", "paid_amount_usd")
    )  # fmt: skip

    # Pair each invoice with an EARLIER invoice of the same supplier and entity, same exact amount
    a, b = invoices.alias("a"), invoices.alias("b")
    gap = F.datediff(F.col("b.invoice_date"), F.col("a.invoice_date"))
    pairs = a.join(
        b,
        (F.col("a.entity") == F.col("b.entity"))
        & (F.col("a.supplier_id") == F.col("b.supplier_id"))
        & (F.col("a.invoice_amount") == F.col("b.invoice_amount"))
        & (F.col("a.invoice_key") != F.col("b.invoice_key"))
        & (gap >= 0)
        & (gap <= window_days)
        # Same date: keep one direction only
        & ((gap > 0) | (F.col("a.invoice_key") < F.col("b.invoice_key"))),
    )

    # The later invoice is the suspect; the money at risk is what was paid on it
    return _suspect_columns(
        pairs.select(
            F.col("b.dt").alias("dt"),
            F.col("b.entity").alias("entity"),
            F.col("b.category").alias("category"),
            F.col("b.supplier_id").alias("supplier_id"),
            F.lit(RULE_RESUBMITTED).alias("rule"),
            F.lit(MEDIUM).alias("confidence"),
            F.col("b.payment_id").alias("suspect_payment_id"),
            F.col("a.payment_id").alias("original_payment_id"),
            F.col("b.supplier_invoice_number").alias("suspect_invoice_number"),
            F.col("a.supplier_invoice_number").alias("original_invoice_number"),
            F.col("b.invoice_amount").alias("invoice_amount"),
            F.col("b.currency").alias("currency"),
            F.col("b.paid_amount").alias("amount_at_risk"),
            F.col("b.paid_amount_usd").alias("amount_at_risk_usd"),
        )
    )


def detect_duplicates(payments: DataFrame, tolerance: float, window_days: int) -> DataFrame:
    """Both rules together: the review list for AP, one row per suspect payment."""
    return detect_overpaid_invoices(payments, tolerance).unionByName(detect_resubmissions(payments, window_days))


# ---------------------------------------------------------------------------
# 4. Payment performance KPIs (counts and sums only: rates are computed after rolling up)
# ---------------------------------------------------------------------------


def _settling(payments: DataFrame) -> DataFrame:
    """Payments that settle an invoice (FULL or FINAL), with days to pay and the discount deadline."""
    return (
        payments.filter(F.col("payment_type").isin(*SETTLING_TYPES))
        .withColumn("days_to_pay", F.datediff("payment_date", "invoice_date"))
        .withColumn("is_on_time", F.col("payment_date") <= F.col("due_date"))
        .withColumn(
            "discount_deadline",
            F.when(F.col("discount_pct") > 0, F.date_add("invoice_date", F.col("discount_days"))),
        )
    )


def build_payment_performance(payments: DataFrame) -> DataFrame:
    """On-time % and DPO inputs, at the grain dt · entity · category · supplier.

    On-time % = on_time_payments / settling_payments (or the $ version).
    DPO       = days_x_usd / paid_usd  (amount-weighted average days from invoice to payment).
    """
    settled = _settling(payments)
    return settled.groupBy("dt", "entity", "category", "supplier_id").agg(
        F.count(F.lit(1)).alias("settling_payments"),
        F.sum(F.col("is_on_time").cast("int")).alias("on_time_payments"),
        F.round(F.sum("paid_amount_usd"), 2).alias("paid_usd"),
        F.round(F.sum(F.when(F.col("is_on_time"), F.col("paid_amount_usd")).otherwise(0.0)), 2).alias(
            "on_time_paid_usd"
        ),
        F.round(F.sum(F.col("days_to_pay") * F.col("paid_amount_usd")), 2).alias("days_x_usd"),
    )


def build_discounts_lost(payments: DataFrame) -> DataFrame:
    """Early-payment discounts lost, one row per invoice paid after its discount window.

    Lost = discount % x invoice amount (USD), when the settling payment left after the deadline.
    """
    settled = _settling(payments).filter(F.col("discount_deadline").isNotNull())
    lost = settled.filter(F.col("payment_date") > F.col("discount_deadline"))
    return lost.select(
        "dt",
        "entity",
        "category",
        "supplier_id",
        "supplier_invoice_number",
        "payment_id",
        "invoice_date",
        "discount_deadline",
        "payment_date",
        "payment_terms",
        "invoice_amount_usd",
        F.round(F.col("invoice_amount_usd") * F.col("discount_pct") / 100, 2).alias("discount_lost_usd"),
    )


# ---------------------------------------------------------------------------
# 5. Reconciliation: does every row end up somewhere?
# ---------------------------------------------------------------------------


def build_data_quality(rows: DataFrame, valid: DataFrame, clean: DataFrame, quarantine: DataFrame) -> DataFrame:
    """Per day: file rows = curated + duplicate rows + quarantined (is_balanced = True when it adds up)."""

    def per_day(frame: DataFrame, name: str) -> DataFrame:
        return frame.groupBy(F.coalesce("dt", F.lit("unknown")).alias("dt")).agg(F.count(F.lit(1)).alias(name))

    counts = (
        per_day(rows, "file_rows")
        .join(per_day(valid, "valid_rows"), "dt", "left")
        .join(per_day(clean, "curated_rows"), "dt", "left")
        .join(per_day(quarantine, "quarantined_rows"), "dt", "left")
        .fillna(0)
        .withColumn("duplicate_rows", F.col("valid_rows") - F.col("curated_rows"))
    )
    balanced = F.col("file_rows") == F.col("curated_rows") + F.col("duplicate_rows") + F.col("quarantined_rows")
    return counts.withColumn("is_balanced", balanced).drop("valid_rows")
