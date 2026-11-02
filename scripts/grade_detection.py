"""Grade the duplicate detector of a local run against the simulator's answer key.

Usage (after `make generate` and `make kpis-local`):
    python -m scripts.grade_detection --lake data/lake-local --truth data/generated/truth/planted_problems.csv
"""

from __future__ import annotations

import argparse
import csv

from src.evaluation.grading import grade, report
from src.generators.reference import MXN_PER_USD


def main(argv: list[str] | None = None) -> None:
    """Read the suspects (Parquet) and the answer key (CSV), print the grade."""
    parser = argparse.ArgumentParser(description="Grade the duplicate detector against the answer key.")
    parser.add_argument("--lake", default="data/lake-local")
    parser.add_argument("--truth", default="data/generated/truth/planted_problems.csv")
    args = parser.parse_args(argv)

    # The suspects, read with the same local Spark the job used
    from src.glue_jobs.daily_ap_kpis import build_spark

    spark = build_spark("grade-detection")
    suspects = [row.asDict() for row in spark.read.parquet(f"{args.lake}/gold/duplicate_suspects").collect()]
    with open(args.truth, encoding="utf-8") as handle:
        truth = list(csv.DictReader(handle))

    print(report(grade(suspects, truth, {"USD": 1.0, "MXN": 1 / MXN_PER_USD})))


if __name__ == "__main__":
    main()
