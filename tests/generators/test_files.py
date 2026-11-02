"""ERP files: every file the generator writes passes its own control-trailer check."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from src.common.schema import COLUMNS
from src.generators.files import read_file, render_file
from src.generators.history import main


def test_empty_day_still_gets_a_file_with_a_zero_trailer() -> None:
    """No payments that day: the file still proves the export ran."""
    rows, trailer = read_file(render_file([]))
    assert rows == []
    assert (trailer["entity"], trailer["paid_amount"]) == ("0", "0.00")


def test_every_generated_file_matches_its_trailer(tmp_path: Path) -> None:
    """Write the whole history to disk, read every file back: count and total agree with the trailer."""
    main(["--last-day", "2026-10-30", "--out", str(tmp_path)])
    files = sorted((tmp_path / "landing" / "ap").rglob("*.csv"))

    assert len(files) == 2 * 65  # 2 entities x 65 business days
    for path in files:
        rows, trailer = read_file(path.read_text(encoding="utf-8"))
        assert list(rows[0]) == list(COLUMNS) if rows else True
        assert int(trailer["entity"]) == len(rows)
        assert Decimal(trailer["paid_amount"]) == sum((Decimal(r["paid_amount"]) for r in rows), Decimal("0"))

    # The answer key and the master data are written too
    assert (tmp_path / "truth" / "planted_problems.csv").exists()
    assert (tmp_path / "reference" / "suppliers" / "suppliers.csv").exists()
    assert date(2026, 10, 30).strftime("%Y%m%d") in files[-1].name
