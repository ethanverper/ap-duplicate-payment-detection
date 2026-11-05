"""Run every query in sql/ on Athena and save each result as CSV (the evidence of a real run).

Usage (after `make deploy`, `make upload-history` and `make kpis`):
    python -m scripts.run_queries --workgroup <wg> --database <db> --out docs/images/results
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from src.analytics.athena import AthenaRunner
from src.analytics.queries import list_queries, load_query
from src.common.aws import get_client
from src.common.logging_utils import get_logger
from src.common.settings import settings

logger = get_logger(__name__)


def write_csv(path: Path, rows: list[dict]) -> None:
    """Save a query's rows (nothing is written for an empty result)."""
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> None:
    """Run each query in the workgroup and database given, and save its rows."""
    parser = argparse.ArgumentParser(description="Run the sql/ queries on Athena and save the results.")
    parser.add_argument("--workgroup", required=True, help="terraform output athena_workgroup")
    parser.add_argument("--database", required=True, help="terraform output athena_database")
    parser.add_argument("--out", type=Path, default=Path("docs/images/results"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    # One runner for every query; each query logs what it scanned and cost
    runner = AthenaRunner(
        client=get_client("athena"),
        workgroup=args.workgroup,
        database=args.database,
        timeout_seconds=settings()["aws"]["athena"]["query_timeout_seconds"],
    )
    for name in list_queries():
        rows = runner.run(load_query(name))
        write_csv(args.out / f"{name}.csv", rows)
        print(f"{name}: {len(rows)} rows")


if __name__ == "__main__":
    main()
