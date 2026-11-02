"""Write the payment history as ERP files, plus the master data and the ground truth, to a local folder.

Where it sits in the pipeline:
    The output folder mirrors the S3 bucket, so one `aws s3 sync` uploads it (level 4):

        <out>/landing/ap/entity=MX01/MX01_ap_YYYYMMDD.csv   one file per entity per business day
        <out>/reference/suppliers/suppliers.csv              category and terms per supplier and entity
        <out>/reference/entities/entities.csv                currency and USD rate per entity
        <out>/truth/planted_problems.csv                     what was planted (local only, never uploaded:
                                                             it is the answer key the tests grade the
                                                             pipeline against)

Usage:
    python -m src.generators.history --out data/generated
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.common.logging_utils import get_logger
from src.common.settings import settings
from src.generators import reference
from src.generators.files import write_file
from src.generators.simulator import SimulationResult, is_business_day, simulate

logger = get_logger(__name__)

# Simulation settings (config/settings.yaml -> simulation, files)
_SIMULATION = settings()["simulation"]
LANDING_PREFIX: str = settings()["files"]["landing_prefix"]


def default_last_day() -> date:
    """Yesterday (UTC): the history ends the day before the live demo files."""
    return datetime.now(timezone.utc).date() - timedelta(days=1)


def write_landing_files(result: SimulationResult, out_dir: Path) -> dict[str, int]:
    """Write one file per entity per business day (empty days included).

    Args:
        result: The simulation output.
        out_dir: Root output folder.

    Returns:
        Number of files written per entity.
    """
    # Group the payment rows by entity and payment date
    by_file: dict[tuple[str, date], list[dict]] = defaultdict(list)
    for payment in result.payments:
        by_file[(payment.invoice.entity, payment.payment_date)].append(payment.to_row())

    # Every business day gets a file for every entity, even with no payments
    files = dict.fromkeys(reference.ENTITIES, 0)
    day = result.first_day
    while day <= result.last_day:
        if is_business_day(day):
            for entity in reference.ENTITIES:
                folder = out_dir / LANDING_PREFIX / f"entity={entity}"
                write_file(folder, entity, day, by_file.get((entity, day), []))
                files[entity] += 1
        day += timedelta(days=1)
    return files


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write rows to a CSV with a header, creating the folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def truth_rows(result: SimulationResult) -> list[dict]:
    """The planted problems as rows: the answer key for grading the duplicate detection."""
    return [
        {
            "kind": problem.kind,
            "entity": problem.entity,
            "supplier_id": problem.supplier_id,
            "original_payment_id": problem.original_payment.payment_id,
            "problem_payment_id": problem.problem_payment.payment_id,
            "amount_at_risk": f"{problem.amount_at_risk:.2f}",
            "currency": problem.problem_payment.invoice.currency,
        }
        for problem in result.planted
    ]


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point: simulate the history and write every output under `--out`."""
    # Options, with defaults from the spec
    parser = argparse.ArgumentParser(description="Write the AP payment history as ERP files.")
    parser.add_argument("--days", type=int, default=_SIMULATION["history_days"], help="history days")
    parser.add_argument("--last-day", type=date.fromisoformat, default=None, help="last day (default yesterday)")
    parser.add_argument("--seed", type=int, default=_SIMULATION["seed"], help="random seed")
    parser.add_argument("--out", type=Path, default=Path("data/generated"), help="output folder")
    args = parser.parse_args(argv)

    # Simulate the window
    last_day = args.last_day or default_last_day()
    result = simulate(last_day - timedelta(days=args.days - 1), last_day, args.seed)

    # Write the ERP files, the master data and the answer key
    files = write_landing_files(result, args.out)
    write_csv(args.out / "reference" / "suppliers" / "suppliers.csv", reference.suppliers_rows())
    write_csv(args.out / "reference" / "entities" / "entities.csv", reference.entities_rows())
    write_csv(args.out / "truth" / "planted_problems.csv", truth_rows(result))

    logger.info(
        "History written",
        extra={
            "fields": {
                "payments": len(result.payments),
                "files_per_entity": files,
                "planted_problems": len(result.planted),
                "first_day": result.first_day.isoformat(),
                "last_day": result.last_day.isoformat(),
                "out": str(args.out),
            }
        },
    )


if __name__ == "__main__":
    main()
