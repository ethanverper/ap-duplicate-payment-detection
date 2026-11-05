"""The files of the live demo: one more business day, plus the problems we want to watch happen.

Where it sits in the pipeline:
    `make generate-demo` simulates the history up to the demo day and keeps that last day out of
    landing/. This module turns the demo day into the files uploaded one by one during the deploy
    (scripts/demo.sh), so each reaction of the pipeline can be seen and captured:

        1. MX01 file with ONE AMOUNT EDITED       → the Lambda rejects it (trailer total mismatch) → email
        2. the correct MX01 file                  → VALIDATED
        3. the same correct MX01 file again       → DUPLICATE: a harmless retry, ignored, no email
        4. the US01 file                          → VALIDATED
        5. Glue runs                              → the injected duplicate below → HIGH → email to AP Mexico

Why inject a duplicate on the demo day:
    The duplicate email only covers suspects paid on the newest day (docs/decisions.md, ADR-009).
    The planted problems fall on random days, so one is placed on the demo day on purpose: an
    invoice paid a few days earlier is paid again today under a retyped number.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from src.common.schema import FULL
from src.generators import story
from src.generators.files import render_file
from src.generators.reference import ENTITIES
from src.generators.simulator import Payment, PlantedProblem, SimulationResult

# The duplicate is planted in this entity, re-paying an invoice paid within this many days
DEMO_DUPLICATE_ENTITY = "MX01"
DEMO_LOOKBACK_DAYS = 10

# The edit that corrupts the "bad" copy of the MX01 file (as if someone changed an amount by hand)
CORRUPTION_DELTA = Decimal("1000.00")


@dataclass
class DemoFiles:
    """The demo day's files and the duplicate planted in them.

    Attributes:
        day: The demo day.
        valid: Entity -> correct file content.
        corrupt: Entity -> content of the hand-edited copy (its trailer no longer matches).
        injected: The duplicate planted on the demo day (added to the answer key).
    """

    day: date
    valid: dict[str, str]
    corrupt: dict[str, str]
    injected: PlantedProblem


def _next_payment_id(payments: list[Payment], entity: str) -> str:
    """The next ERP payment id of an entity (after the highest one used so far)."""
    highest = max(int(p.payment_id.rsplit("-", 1)[1]) for p in payments if p.invoice.entity == entity)
    return f"PAY-{entity}-{highest + 1:06d}"


def inject_demo_duplicate(result: SimulationResult, day: date, rng: random.Random) -> tuple[Payment, PlantedProblem]:
    """Pay again, on the demo day, an invoice paid a few days earlier, under a retyped number.

    Args:
        result: The simulation (history + demo day).
        day: The demo day.
        rng: Random generator (which invoice, how it is retyped).

    Returns:
        (the duplicate payment, its ground-truth record).
    """
    # A normal full payment of the demo entity, made shortly before the demo day
    candidates = [
        p
        for p in result.payments
        if p.invoice.entity == DEMO_DUPLICATE_ENTITY
        and p.payment_type == FULL
        and p.invoice.supplier.recurring_fee is None
        and day - timedelta(days=DEMO_LOOKBACK_DAYS) <= p.payment_date < day
    ]
    original = rng.choice(candidates)

    # Today it is keyed again with the number typed differently, and paid again
    duplicate = replace(
        original,
        payment_date=day,
        typed_number=story.retyped_invoice_number(original.invoice.number, rng),
        payment_id=_next_payment_id(result.payments, DEMO_DUPLICATE_ENTITY),
    )
    planted = PlantedProblem(
        story.FORMAT_DUPLICATE,
        DEMO_DUPLICATE_ENTITY,
        original.invoice.supplier.supplier_id,
        original,
        duplicate,
        original.paid_amount,
    )
    return duplicate, planted


def corrupt(rows: list[dict]) -> str:
    """The file with one paid amount edited after export: the trailer still has the old total."""
    # Render the correct file first, so its trailer carries the original total...
    text = render_file(rows)
    if not rows:
        # An empty day has no amount to edit: cut the trailer instead (a file cut in transit)
        return "\n".join(text.splitlines()[:-1]) + "\n"

    # ...then change one amount in the data rows only
    lines = text.splitlines()
    fields = lines[1].split(",")
    fields[10] = f"{Decimal(fields[10]) + CORRUPTION_DELTA:.2f}"
    lines[1] = ",".join(fields)
    return "\n".join(lines) + "\n"


def build_demo(result: SimulationResult, day: date, seed: int) -> DemoFiles:
    """Build every demo file for the demo day.

    Args:
        result: The simulation, whose last day is the demo day.
        day: The demo day.
        seed: Random seed (reproducible demo).

    Returns:
        The demo files and the injected duplicate.
    """
    rng = random.Random(seed + 2)

    # The demo day's payments per entity, plus the injected duplicate
    duplicate, injected = inject_demo_duplicate(result, day, rng)
    rows: dict[str, list[dict]] = {entity: [] for entity in ENTITIES}
    for payment in [*result.payments, duplicate]:
        if payment.payment_date == day:
            rows[payment.invoice.entity].append(payment.to_row())

    # Correct files, and the hand-edited copy of each
    return DemoFiles(
        day=day,
        valid={entity: render_file(entity_rows) for entity, entity_rows in rows.items()},
        corrupt={entity: corrupt(entity_rows) for entity, entity_rows in rows.items()},
        injected=injected,
    )
