"""The demo day end to end on local Spark: the injected duplicate is a HIGH suspect of that day."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from src.evaluation.grading import grade
from src.generators.history import main as generate
from src.generators.reference import MXN_PER_USD
from src.glue_jobs import daily_ap_kpis
from tests.glue_jobs.conftest import job_args


def test_the_demo_duplicate_is_found_on_the_demo_day(spark, tmp_path: Path) -> None:
    # The history plus the demo day's correct files, laid out as the validated zone would be
    generate(["--demo", "--last-day", "2026-11-04", "--out", str(tmp_path)])
    for path in (tmp_path / "demo" / "valid").glob("*.csv"):
        entity = path.name[:4]
        shutil.copy(path, tmp_path / "landing" / "ap" / f"entity={entity}" / path.name)

    rows, reference = daily_ap_kpis.extract(spark, job_args(tmp_path))
    tables = daily_ap_kpis.transform(rows, reference, job_args(tmp_path))
    suspects = [row.asDict() for row in tables["gold/duplicate_suspects"].collect()]

    # The email of the demo day would list it (HIGH, paid on the demo day)
    with (tmp_path / "truth" / "planted_problems.csv").open(encoding="utf-8") as handle:
        truth = list(csv.DictReader(handle))
    injected = truth[-1]["problem_payment_id"]
    today_high = {s["suspect_payment_id"] for s in suspects if s["confidence"] == "HIGH" and s["dt"] == "2026-11-04"}
    assert injected in today_high

    # And the detector stays perfect with the demo day included
    result = grade(suspects, truth, {"USD": 1.0, "MXN": 1 / MXN_PER_USD})
    assert result.recall_pct == result.precision_pct == 100.0
