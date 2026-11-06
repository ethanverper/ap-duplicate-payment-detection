"""The README charts are drawn from the saved Athena results of the real run, with no AWS."""

from __future__ import annotations

from pathlib import Path

from scripts.make_charts import main

RESULTS = Path(__file__).resolve().parents[1] / "docs" / "images" / "results"


def test_every_chart_is_drawn_from_the_saved_results(tmp_path: Path) -> None:
    """Three non-empty PNGs, one per KPI chart."""
    main(["--results", str(RESULTS), "--out", str(tmp_path), "--on-time-target", "90"])

    names = {"duplicates_at_risk.png", "on_time_by_supplier.png", "discounts_lost.png"}
    assert {path.name for path in tmp_path.glob("*.png")} == names
    assert all((tmp_path / name).stat().st_size > 20_000 for name in names)
