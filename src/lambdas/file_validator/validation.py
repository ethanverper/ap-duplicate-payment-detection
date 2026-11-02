"""Pure checks of one AP payment file: does it look exactly like a complete, untouched ERP export?

Where it sits in the pipeline:
    handler.py reads the new S3 object and calls `check_key()` and `check_content()`. If either
    returns issues, the file is rejected before anything is built on it. No AWS calls here, so
    every rule is unit-tested on plain text.

What is checked here, and what is NOT (docs/decisions.md, ADR-002):
    Here: the FILE's integrity. Name and folder agree, the header is the contract, every row has
    every column and readable dates and amounts, rows belong to this entity and day, and the
    control trailer matches the rows. A file failing any of these is broken as a whole.
    Not here: business rules on single rows (an unknown supplier, a negative amount, a due date
    before the invoice date). Those go to Glue Data Quality, which quarantines the row, not the file.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from src.common.schema import (
    AMOUNT_DECIMALS,
    COLUMNS,
    DATE_FORMAT,
    TRAILER_COUNT_COLUMN,
    TRAILER_MARKER,
    TRAILER_TOTAL_COLUMN,
)

# Issue codes (they appear in the rejection email and in the .reason.json file)
BAD_KEY = "BAD_KEY"  # the S3 key is not <prefix>/entity=<E>/<E>_ap_<YYYYMMDD>.csv
UNKNOWN_ENTITY = "UNKNOWN_ENTITY"  # the entity is not one the company has
TOO_LARGE = "TOO_LARGE"  # far bigger than any normal daily export
EMPTY_FILE = "EMPTY_FILE"  # no content at all (not even a header and a trailer)
NOT_UTF8 = "NOT_UTF8"  # unreadable bytes: not a text export
BAD_HEADER = "BAD_HEADER"  # columns missing, extra or out of order
MISSING_TRAILER = "MISSING_TRAILER"  # the last line is not the control trailer: likely cut in transit
MALFORMED_ROW = "MALFORMED_ROW"  # a row with too many or too few columns
UNPARSEABLE_VALUE = "UNPARSEABLE_VALUE"  # a date or an amount that can't be read
WRONG_ENTITY = "WRONG_ENTITY"  # a row of another entity inside this entity's file
WRONG_PAYMENT_DATE = "WRONG_PAYMENT_DATE"  # a row paid on another day than the file's date
TRAILER_COUNT_MISMATCH = "TRAILER_COUNT_MISMATCH"  # the trailer's record count differs from the rows
TRAILER_TOTAL_MISMATCH = "TRAILER_TOTAL_MISMATCH"  # the trailer's total differs from the sum of the rows

# At most this many examples per issue code are reported (a broken file can have thousands)
MAX_EXAMPLES = 5

# Columns that must parse as dates or as two-decimal amounts
DATE_COLUMNS = ("invoice_date", "due_date", "payment_date")
AMOUNT_COLUMNS = ("invoice_amount", "paid_amount")

# entity=<E>/<E>_ap_<YYYYMMDD>.csv at the end of the key
_KEY_PATTERN = re.compile(r"entity=(?P<folder_entity>[^/]+)/(?P<name_entity>[^/_]+)_ap_(?P<day>\d{8})\.csv$")


@dataclass(frozen=True)
class Issue:
    """One problem found in a file.

    Attributes:
        code: Machine-readable code (constants above).
        message: What is wrong, for a person.
        line: File line number (1 = header), or None for file-level problems.
    """

    code: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class FileIdentity:
    """What the S3 key says the file is: entity, day and file name."""

    entity: str
    file_date: date
    file_name: str


@dataclass
class ContentCheck:
    """The result of checking a file's content.

    Attributes:
        issues: Everything wrong (empty = the file is valid).
        record_count: Data rows found (the trailer excluded).
        total_paid: Sum of paid_amount over the data rows, as text with two decimals.
        sha256: Fingerprint of the exact bytes: two uploads of the same file have the same one.
    """

    issues: list[Issue] = field(default_factory=list)
    record_count: int = 0
    total_paid: str = "0.00"
    sha256: str = ""

    @property
    def is_valid(self) -> bool:
        """True when no issue was found."""
        return not self.issues


# ---------------------------------------------------------------------------
# The S3 key
# ---------------------------------------------------------------------------


def check_key(key: str, known_entities: set[str]) -> tuple[FileIdentity | None, list[Issue]]:
    """Read entity and day from the S3 key, and check they are consistent and known.

    Args:
        key: S3 object key, e.g. "landing/ap/entity=MX01/MX01_ap_20261030.csv".
        known_entities: Entities the company has (from configuration).

    Returns:
        (the identity, or None if the key can't be read; the issues found).
    """
    # The key must end in entity=<E>/<E>_ap_<YYYYMMDD>.csv
    match = _KEY_PATTERN.search(key)
    if not match:
        return None, [Issue(BAD_KEY, f"Key {key!r} is not <prefix>/entity=<E>/<E>_ap_<YYYYMMDD>.csv")]

    # The folder and the file name must name the same entity, and the date must be real
    issues = []
    if match["folder_entity"] != match["name_entity"]:
        issues.append(
            Issue(
                BAD_KEY, f"Folder entity {match['folder_entity']} differs from file name entity {match['name_entity']}"
            )
        )
    try:
        file_date = datetime.strptime(match["day"], "%Y%m%d").date()
    except ValueError:
        return None, [Issue(BAD_KEY, f"{match['day']} in the file name is not a date")]

    # The entity must exist
    entity = match["name_entity"]
    if entity not in known_entities:
        issues.append(Issue(UNKNOWN_ENTITY, f"Entity {entity} is not one of {sorted(known_entities)}"))
    return FileIdentity(entity, file_date, key.rsplit("/", 1)[-1]), issues


# ---------------------------------------------------------------------------
# The content
# ---------------------------------------------------------------------------


def _parse_amount(text: str) -> Decimal | None:
    """A two-decimal amount, or None if the text isn't one (e.g. "12,5" or "1.234")."""
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError):
        return None
    return value if value.as_tuple().exponent == -AMOUNT_DECIMALS else None


def _parse_date(text: str) -> date | None:
    """A YYYY-MM-DD date, or None."""
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except (ValueError, TypeError):
        return None


def _row_issues(row: dict, line: int, identity: FileIdentity) -> list[Issue]:
    """Structural problems of one data row (business rules are left to Glue Data Quality)."""
    # A row with extra fields gets a None key; one with missing fields gets None values
    if None in row or any(value is None for value in row.values()):
        return [Issue(MALFORMED_ROW, f"Expected {len(COLUMNS)} columns", line)]

    # Dates and amounts must be readable
    issues = []
    for column in DATE_COLUMNS:
        if _parse_date(row[column]) is None:
            issues.append(Issue(UNPARSEABLE_VALUE, f"{column}={row[column]!r} is not YYYY-MM-DD", line))
    for column in AMOUNT_COLUMNS:
        if _parse_amount(row[column]) is None:
            issues.append(
                Issue(UNPARSEABLE_VALUE, f"{column}={row[column]!r} is not an amount with two decimals", line)
            )

    # The row must belong to this file: same entity, paid on the file's day
    if row["entity"] != identity.entity:
        issues.append(Issue(WRONG_ENTITY, f"Row of entity {row['entity']} in a {identity.entity} file", line))
    if _parse_date(row["payment_date"]) not in (None, identity.file_date):
        issues.append(
            Issue(WRONG_PAYMENT_DATE, f"payment_date {row['payment_date']} in the file of {identity.file_date}", line)
        )
    return issues


def _limit_examples(issues: list[Issue]) -> list[Issue]:
    """Keep at most MAX_EXAMPLES issues per code, so a broken file gives a readable report."""
    kept, seen = [], {}
    for issue in issues:
        seen[issue.code] = seen.get(issue.code, 0) + 1
        if seen[issue.code] <= MAX_EXAMPLES:
            kept.append(issue)
    return kept


def check_content(data: bytes, identity: FileIdentity) -> ContentCheck:
    """Check that a file is a complete, well-formed ERP export for its entity and day.

    Args:
        data: The file's bytes, exactly as received.
        identity: Entity and day from the S3 key (check_key).

    Returns:
        The check result: issues (empty if valid), record count, total and fingerprint.
    """
    result = ContentCheck(sha256=hashlib.sha256(data).hexdigest())

    # Readable text, and not empty
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result.issues.append(Issue(NOT_UTF8, "The file is not UTF-8 text"))
        return result
    if not text.strip():
        result.issues.append(Issue(EMPTY_FILE, "The file is empty"))
        return result

    # The header must be exactly the contract (src/common/schema.py), in order
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != COLUMNS:
        result.issues.append(
            Issue(BAD_HEADER, f"Header {reader.fieldnames} differs from the contract {list(COLUMNS)}", 1)
        )
        return result

    # The last line must be the control trailer; everything before it is data
    rows = list(reader)
    if not rows or rows[-1]["payment_id"] != TRAILER_MARKER:
        result.issues.append(Issue(MISSING_TRAILER, "The last line is not the control trailer (file cut in transit?)"))
        return result
    data_rows, trailer = rows[:-1], rows[-1]

    # Every data row: structure, readable values, right entity and day (line 2 = first data row)
    issues: list[Issue] = []
    for line, row in enumerate(data_rows, start=2):
        issues.extend(_row_issues(row, line, identity))

    # The trailer must match what actually arrived
    total = sum((_parse_amount(row["paid_amount"] or "") or Decimal("0") for row in data_rows), Decimal("0"))
    result.record_count, result.total_paid = len(data_rows), f"{total:.{AMOUNT_DECIMALS}f}"
    trailer_line = len(rows) + 1
    if trailer[TRAILER_COUNT_COLUMN] != str(len(data_rows)):
        issues.append(Issue(TRAILER_COUNT_MISMATCH,
                            f"Trailer says {trailer[TRAILER_COUNT_COLUMN]} records, the file has {len(data_rows)}",
                            trailer_line))  # fmt: skip
    if _parse_amount(trailer[TRAILER_TOTAL_COLUMN] or "") != total:
        issues.append(Issue(TRAILER_TOTAL_MISMATCH,
                            f"Trailer total {trailer[TRAILER_TOTAL_COLUMN]} differs from the rows' sum {total:.2f}",
                            trailer_line))  # fmt: skip

    result.issues.extend(_limit_examples(issues))
    return result
