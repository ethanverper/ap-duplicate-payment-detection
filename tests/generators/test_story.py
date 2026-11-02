"""How invoice numbers get retyped (planted problem 1)."""

from __future__ import annotations

import random
import re

import pytest

from src.generators.story import retyped_invoice_number


def _canonical(number: str) -> str:
    """What a duplicate detector must reduce every variant to: letters + digits without leading zeros."""
    letters = re.sub(r"[^A-Z]", "", number.upper())
    digits = re.sub(r"\D", "", number).lstrip("0")
    return letters + digits


@pytest.mark.parametrize("number", ["FAC-0457", "VX-01234", "NC000812", "MTC/00055", "AND-1145", "TN-001716"])
def test_retyped_numbers_differ_but_name_the_same_invoice(number: str) -> None:
    """Every variant is a different string, and reduces to the same canonical number."""
    rng = random.Random(7)
    for _ in range(20):
        variant = retyped_invoice_number(number, rng)
        assert variant != number
        assert _canonical(variant) == _canonical(number)
