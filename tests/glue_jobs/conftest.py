"""Shared fixtures for the Glue job tests: a local Spark session and the generated history on disk.

The tests run the real PySpark code of src/glue_jobs/ on a laptop or in CI: no Glue, no S3, no
cost. They are skipped when PySpark isn't installed, so the rest of the suite runs anywhere.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

pytest.importorskip("pyspark")

from pyspark.sql import DataFrame, SparkSession  # noqa: E402  (only importable after the skip check)

from src.common.settings import settings  # noqa: E402
from src.generators.history import main as generate  # noqa: E402
from src.glue_jobs import daily_ap_kpis  # noqa: E402

LAST_DAY = "2026-10-30"


@pytest.fixture(scope="session")
def spark() -> SparkSession:
    """One small local Spark session for every test."""
    session = (
        SparkSession.builder.master("local[2]")
        .appName("ap-glue-tests")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.sources.partitionOverwriteMode", "dynamic")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    yield session
    session.stop()


@pytest.fixture(scope="session")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The 90-day history written to disk exactly as `make generate` does."""
    root = tmp_path_factory.mktemp("generated")
    generate(["--last-day", LAST_DAY, "--out", str(root)])
    return root


@pytest.fixture(scope="session")
def truth(generated: Path) -> list[dict]:
    """The answer key the simulator wrote."""
    with (generated / "truth" / "planted_problems.csv").open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def job_args(
    input_root: Path, output_root: Path | str = "unused", window_days: int | None = None
) -> daily_ap_kpis.JobArgs:
    """The job arguments, from config/settings.yaml (as Terraform and scripts/kpis_local.py build them)."""
    config = settings()
    return daily_ap_kpis.JobArgs(
        input_root=str(input_root),
        files_prefix=config["files"]["landing_prefix"],
        output_root=str(output_root),
        overpayment_tolerance=config["detection"]["overpayment_tolerance"],
        resubmission_window_days=window_days or config["detection"]["resubmission_window_days"],
    )


@pytest.fixture(scope="session")
def extracted(spark, generated) -> tuple[DataFrame, dict[str, DataFrame]]:
    """(file rows, reference tables), read with the job's own extract()."""
    rows, reference = daily_ap_kpis.extract(spark, job_args(generated))
    return rows.cache(), {name: frame.cache() for name, frame in reference.items()}


@pytest.fixture(scope="session")
def tables(extracted, generated) -> dict[str, DataFrame]:
    """Every output table, computed once on the full history."""
    rows, reference = extracted
    return {
        path: frame.cache() for path, frame in daily_ap_kpis.transform(rows, reference, job_args(generated)).items()
    }
