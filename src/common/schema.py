"""The AP payment file contract: columns, payment types, the control trailer and formats.

Why it lives in src/common/:
    Every component speaks this contract: the ERP export (src/generators/ in the demo), the
    Lambda that validates each file as it lands, and the Glue job that detects duplicates and
    builds the KPIs. Defining it once, in a module none of them owns, keeps them in agreement.

These are protocol constants, not settings: changing them breaks every reader, so they stay in
code and not in config/settings.yaml.

File layout (one file per entity per day; the rows are the payments made that day):

    payment_id,entity,supplier_id,supplier_invoice_number,invoice_date,due_date,invoice_amount,...
    PAY-MX01-000457,MX01,SUP-PRECISA,FAC-0457,2026-09-02,2026-10-02,18450.00,MXN,2/10 NET 30,...
    ...
    TRAILER,2,,,,,,,,,36900.00,

The last line is the control trailer: the record count and the total paid amount. A file whose
trailer doesn't match its rows was cut or altered in transit and must never be loaded.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Columns, in file order
# ---------------------------------------------------------------------------
COLUMNS: tuple[str, ...] = (
    "payment_id",  # unique per payment, assigned by the ERP
    "entity",  # legal entity that pays: MX01 or US01
    "supplier_id",  # who is paid
    "supplier_invoice_number",  # the number printed on the supplier's invoice (typed by a person)
    "invoice_date",  # date on the invoice
    "due_date",  # invoice date + net days of the payment terms
    "invoice_amount",  # full invoice amount, in the invoice currency
    "currency",  # MXN or USD
    "payment_terms",  # e.g. "2/10 NET 30"
    "payment_date",  # the day the money left (the file's date)
    "paid_amount",  # amount of this payment
    "payment_type",  # FULL, ADVANCE or FINAL (below)
)

# ---------------------------------------------------------------------------
# Payment types: how an invoice is settled
# ---------------------------------------------------------------------------
FULL = "FULL"  # the whole invoice in one payment
ADVANCE = "ADVANCE"  # a part paid early (e.g. 30% when a service starts)
FINAL = "FINAL"  # the balance after an advance: ADVANCE + FINAL = invoice amount
PAYMENT_TYPES: tuple[str, ...] = (FULL, ADVANCE, FINAL)

# ---------------------------------------------------------------------------
# The control trailer (last line of every file)
# ---------------------------------------------------------------------------
TRAILER_MARKER = "TRAILER"  # value of the first column on the trailer line
TRAILER_COUNT_COLUMN = "entity"  # the trailer puts the record count in the 2nd column...
TRAILER_TOTAL_COLUMN = "paid_amount"  # ...and the total paid amount in the paid_amount column

# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------
DATE_FORMAT = "%Y-%m-%d"  # every date column
AMOUNT_DECIMALS = 2  # money is written with exactly two decimals
FILE_NAME_FORMAT = "{entity}_ap_{day:%Y%m%d}.csv"  # e.g. MX01_ap_20261030.csv
