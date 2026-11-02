"""Write and read ERP payment files: CSV rows plus the control trailer (src/common/schema.py).

Why reading lives here too:
    Writing and reading the same format side by side keeps them in agreement, and the tests use
    `read_file()` to prove that every file the generator writes passes its own trailer check.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.common.schema import (
    AMOUNT_DECIMALS,
    COLUMNS,
    FILE_NAME_FORMAT,
    TRAILER_COUNT_COLUMN,
    TRAILER_MARKER,
    TRAILER_TOTAL_COLUMN,
)


def trailer_row(rows: list[dict]) -> dict:
    """The control trailer for a set of rows: record count and total paid amount.

    Why Decimal: summing many two-decimal amounts as floats can drift by a cent, and a one-cent
    difference would make a correct file fail its own check.
    """
    total = sum((Decimal(row["paid_amount"]) for row in rows), Decimal("0"))
    trailer = dict.fromkeys(COLUMNS, "")
    trailer["payment_id"] = TRAILER_MARKER
    trailer[TRAILER_COUNT_COLUMN] = str(len(rows))
    trailer[TRAILER_TOTAL_COLUMN] = f"{total:.{AMOUNT_DECIMALS}f}"
    return trailer


def file_name(entity: str, day: date) -> str:
    """The ERP's file name for an entity and day, e.g. "MX01_ap_20261030.csv"."""
    return FILE_NAME_FORMAT.format(entity=entity, day=day)


def render_file(rows: list[dict]) -> str:
    """The full file content: header, rows in column order, then the trailer."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    writer.writerow(trailer_row(rows))
    return buffer.getvalue()


def write_file(folder: Path, entity: str, day: date, rows: list[dict]) -> Path:
    """Write one entity's file for one day (an empty day still gets a file with a 0 trailer).

    Why empty files: the ERP exports every business day. An empty file with "0 records" proves
    the export ran; a missing file means something broke upstream.

    Args:
        folder: Destination folder (e.g. data/generated/landing/ap/entity=MX01).
        entity: Entity id.
        day: The payment date the file covers.
        rows: The payments of that day, as file rows.

    Returns:
        The path written.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file_name(entity, day)
    path.write_text(render_file(rows), encoding="utf-8")
    return path


def read_file(text: str) -> tuple[list[dict], dict]:
    """Split a file into its data rows and its trailer (no validation: that is the Lambda's job).

    Args:
        text: The file content.

    Returns:
        (data rows, trailer row).

    Raises:
        ValueError: If the last row is not a trailer.
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or rows[-1]["payment_id"] != TRAILER_MARKER:
        raise ValueError("The file has no control trailer")
    return rows[:-1], rows[-1]
