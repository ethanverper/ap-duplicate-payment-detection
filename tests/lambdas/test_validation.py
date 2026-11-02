"""File integrity rules: a real ERP file passes, and every way of breaking it is caught with its reason."""

from __future__ import annotations

from datetime import date

from src.lambdas.file_validator import validation as v
from src.lambdas.file_validator.validation import FileIdentity, check_content, check_key
from tests.lambdas.conftest import FILE_DAY, landing_key

IDENTITY = FileIdentity("MX01", FILE_DAY, "MX01_ap_20261023.csv")
ENTITIES = {"MX01", "US01"}


def _codes(text: str, identity: FileIdentity = IDENTITY) -> set[str]:
    """Issue codes found in a file's text."""
    return {issue.code for issue in check_content(text.encode("utf-8"), identity).issues}


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_a_well_formed_key_gives_entity_and_day() -> None:
    identity, issues = check_key(landing_key(), ENTITIES)
    assert issues == []
    assert (identity.entity, identity.file_date) == ("MX01", FILE_DAY)


def test_keys_that_lie_or_make_no_sense_are_refused() -> None:
    """Folder and name disagree, unknown entity, impossible date, wrong naming."""
    cases = {
        "landing/ap/entity=US01/MX01_ap_20261023.csv": v.BAD_KEY,
        "landing/ap/entity=CA01/CA01_ap_20261023.csv": v.UNKNOWN_ENTITY,
        "landing/ap/entity=MX01/MX01_ap_20261332.csv": v.BAD_KEY,
        "landing/ap/entity=MX01/payments_final_v2.csv": v.BAD_KEY,
    }
    for key, code in cases.items():
        _identity, issues = check_key(key, ENTITIES)
        assert code in {issue.code for issue in issues}, key


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def test_a_real_erp_file_is_valid(mx_file, day_rows) -> None:
    """The generator's own file passes, with the right count and total."""
    result = check_content(mx_file.encode("utf-8"), IDENTITY)

    assert result.is_valid, result.issues
    assert result.record_count == len(day_rows["MX01"]) > 50
    assert len(result.sha256) == 64


def test_an_empty_day_file_is_valid() -> None:
    """Header + "0 records" trailer: the export ran, nothing was paid."""
    from src.generators.files import render_file

    assert _codes(render_file([])) == set()


def test_a_file_cut_in_transit_is_refused(mx_file) -> None:
    """The last lines (and the trailer) are missing."""
    assert _codes("\n".join(_lines(mx_file)[:-10]) + "\n") == {v.MISSING_TRAILER}


def test_a_row_lost_on_the_way_breaks_the_trailer(mx_file) -> None:
    """One data row removed: count and total no longer match the trailer."""
    lines = _lines(mx_file)
    assert _codes("\n".join(lines[:5] + lines[6:]) + "\n") == {v.TRAILER_COUNT_MISMATCH, v.TRAILER_TOTAL_MISMATCH}


def test_an_altered_amount_breaks_the_total(mx_file) -> None:
    """Someone edits one paid amount: the count still matches, the total doesn't."""
    lines = _lines(mx_file)
    fields = lines[3].split(",")
    fields[10] = f"{float(fields[10]) + 1000:.2f}"
    lines[3] = ",".join(fields)
    assert _codes("\n".join(lines) + "\n") == {v.TRAILER_TOTAL_MISMATCH}


def test_a_changed_header_is_refused(mx_file) -> None:
    """A renamed column means a changed export: no row can be trusted."""
    assert _codes(mx_file.replace("paid_amount", "amount_paid", 1)) == {v.BAD_HEADER}


def test_malformed_and_unreadable_rows_are_reported_with_their_line(mx_file) -> None:
    """An extra column, a date in another format, an amount without decimals."""
    lines = _lines(mx_file)
    lines[2] += ",EXTRA"
    lines[3] = lines[3].replace(FILE_DAY.isoformat(), FILE_DAY.strftime("%d/%m/%Y"))
    fields = lines[4].split(",")
    fields[6] = fields[6].split(".")[0]
    lines[4] = ",".join(fields)

    issues = check_content(("\n".join(lines) + "\n").encode("utf-8"), IDENTITY).issues
    by_line = {(issue.code, issue.line) for issue in issues}
    assert (v.MALFORMED_ROW, 3) in by_line
    assert (v.UNPARSEABLE_VALUE, 4) in by_line
    assert (v.UNPARSEABLE_VALUE, 5) in by_line


def test_rows_of_another_entity_or_day_are_refused(day_rows) -> None:
    """A US01 row inside the MX01 file; a payment of another day inside this day's file."""
    from src.generators.files import render_file

    stray = dict(day_rows["MX01"][0], entity="US01")
    late = dict(day_rows["MX01"][1], payment_date=date(2026, 10, 22).isoformat())
    text = render_file([stray, late, *day_rows["MX01"][2:]])
    assert _codes(text) == {v.WRONG_ENTITY, v.WRONG_PAYMENT_DATE}


def test_not_a_text_file_and_empty_bytes_are_refused() -> None:
    assert {i.code for i in check_content(b"\xff\xfe\x00binary", IDENTITY).issues} == {v.NOT_UTF8}
    assert {i.code for i in check_content(b"", IDENTITY).issues} == {v.EMPTY_FILE}


def test_a_badly_broken_file_gives_a_readable_report(mx_file) -> None:
    """Thousands of bad rows would be an unreadable email: at most 5 examples per issue."""
    broken = mx_file.replace(FILE_DAY.isoformat(), "yesterday")
    issues = check_content(broken.encode("utf-8"), IDENTITY).issues
    assert sum(issue.code == v.UNPARSEABLE_VALUE for issue in issues) == v.MAX_EXAMPLES
