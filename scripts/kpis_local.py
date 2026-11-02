"""Run the Glue AP job on this laptop, on the generated files, with a local Spark (no AWS).

Why: on AWS, Terraform passes the job its arguments from config/settings.yaml. This script does the
same locally and calls the very same job code. Locally there is no Lambda, so the job reads the
generator's landing/ files directly (every one of them passes validation: tests/lambdas prove it).

Usage (after `make generate`):
    python -m scripts.kpis_local --input data/generated --output data/lake-local
"""

from __future__ import annotations

import argparse
import json

from src.common.settings import settings
from src.glue_jobs import daily_ap_kpis


def job_argv(input_root: str, output_root: str) -> list[str]:
    """The same arguments Terraform will give the Glue job, built from config/settings.yaml."""
    config = settings()
    return [
        "--input_root", input_root,
        "--files_prefix", config["files"]["landing_prefix"],
        "--output_root", output_root,
        "--overpayment_tolerance", str(config["detection"]["overpayment_tolerance"]),
        "--resubmission_window_days", str(config["detection"]["resubmission_window_days"]),
    ]  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point: run the job locally and print its summary."""
    parser = argparse.ArgumentParser(description="Run the AP KPI job locally.")
    parser.add_argument("--input", default="data/generated", help="folder with landing/ and reference/")
    parser.add_argument("--output", default="data/lake-local", help="folder for the output tables")
    args = parser.parse_args(argv)

    spark = daily_ap_kpis.build_spark("daily-ap-kpis-local")
    summary = daily_ap_kpis.run(spark, daily_ap_kpis.parse_args(job_argv(args.input, args.output)))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
