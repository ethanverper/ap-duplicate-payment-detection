"""The problems planted in the data (docs/spec.md, section 3), as plain values and small pure rules.

Why a separate module:
    The simulator decides WHEN and WHERE each problem happens; this module holds WHAT each problem
    looks like (for example, how a person retypes an invoice number), so each rule can be tested
    on its own and the values stay in config/settings.yaml.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from src.common.settings import settings

_SIMULATION = settings()["simulation"]
_INCIDENTS = _SIMULATION["incidents"]

# The kinds of planted problem, as they appear in the ground-truth file
FORMAT_DUPLICATE = "FORMAT_DUPLICATE"  # problem 1: same invoice, number typed differently, paid twice
RESUBMITTED_INVOICE = "RESUBMITTED_INVOICE"  # problem 2: same invoice sent again with a new number
ADVANCE_OVERPAID = "ADVANCE_OVERPAID"  # problem 3: advance paid, then the full amount anyway


@dataclass(frozen=True)
class Incidents:
    """Where and how often each problem happens (config/settings.yaml -> simulation.incidents)."""

    format_duplicate_entity: str
    format_duplicate_count: int
    format_duplicate_days: tuple[int, int]
    resubmitted_entity: str
    resubmitted_supplier: str
    resubmitted_count: int
    resubmitted_days: tuple[int, int]
    advance_overpaid_count: int
    late_entity: str
    late_supplier: str
    late_extra_days: tuple[int, int]


def load_incidents() -> Incidents:
    """Read the incident settings into one typed object."""
    fmt, resub, adv, late = (
        _INCIDENTS["format_duplicate"],
        _INCIDENTS["resubmitted_invoice"],
        _INCIDENTS["advance_overpaid"],
        _INCIDENTS["late_supplier"],
    )
    return Incidents(
        format_duplicate_entity=fmt["entity"],
        format_duplicate_count=int(fmt["count"]),
        format_duplicate_days=tuple(fmt["days_after_original"]),
        resubmitted_entity=resub["entity"],
        resubmitted_supplier=resub["supplier"],
        resubmitted_count=int(resub["count"]),
        resubmitted_days=tuple(resub["days_after_original"]),
        advance_overpaid_count=int(adv["count"]),
        late_entity=late["entity"],
        late_supplier=late["supplier"],
        late_extra_days=tuple(late["extra_days"]),
    )


# ---------------------------------------------------------------------------
# How a person retypes an invoice number
# ---------------------------------------------------------------------------

# A number is "prefix + digits", e.g. FAC-0457 -> prefix "FAC-", digits "0457"
_NUMBER_PARTS = re.compile(r"^(?P<prefix>.*?)(?P<digits>\d+)$")


def retyped_invoice_number(number: str, rng: random.Random) -> str:
    """Return the same invoice number as a person might type it again, in a different format.

    The three variants seen in real AP data:
      - separators dropped:      "FAC-0457" -> "FAC0457"
      - leading zeros dropped:   "FAC-0457" -> "FAC-457"
      - lowercase, space, no zeros: "FAC-0457" -> "fac 457"

    Args:
        number: The invoice number as first entered.
        rng: Random generator (the variant is picked at random).

    Returns:
        A different string that names the same invoice.
    """
    # Split into the letters/separators part and the digits part
    match = _NUMBER_PARTS.match(number)
    prefix, digits = (match["prefix"], match["digits"]) if match else (number, "")
    letters = re.sub(r"[^A-Za-z]", "", prefix)
    short_digits = digits.lstrip("0") or "0"

    # Every variant that actually differs from the original
    variants = [
        letters + digits,  # separators dropped
        prefix + short_digits,  # leading zeros dropped
        f"{letters.lower()} {short_digits}".strip(),  # lowercase, space, no zeros
    ]
    variants = [variant for variant in variants if variant != number]

    # Pick one (the number always has at least one alternative way of being typed)
    return rng.choice(variants)
