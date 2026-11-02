"""Master data rules: payment terms, currency conversion and the reference exports."""

from __future__ import annotations

from datetime import date

import pytest

from src.generators import reference
from src.generators.reference import parse_terms, to_currency


def test_terms_with_discount() -> None:
    """ "2/10 NET 30": 2% if paid within 10 days, otherwise due in 30."""
    terms = parse_terms("2/10 NET 30")

    assert (terms.discount_pct, terms.discount_days, terms.net_days) == (2.0, 10, 30)
    assert terms.discount_deadline(date(2026, 10, 1)) == date(2026, 10, 11)
    assert terms.due_date(date(2026, 10, 1)) == date(2026, 10, 31)


def test_terms_without_discount() -> None:
    """ "NET 45" has no discount window."""
    terms = parse_terms("net 45")

    assert not terms.has_discount
    assert terms.discount_deadline(date(2026, 10, 1)) is None
    assert terms.net_days == 45


def test_unreadable_terms_fail_loudly() -> None:
    """Bad master data must stop the run, not produce wrong due dates."""
    with pytest.raises(ValueError):
        parse_terms("30 days")


def test_mexico_amounts_are_converted_to_pesos() -> None:
    """Invoices of the Mexican entity are in MXN at the configured rate."""
    assert to_currency(100.0, "MXN") == round(100.0 * reference.MXN_PER_USD, 2)
    assert to_currency(100.0, "USD") == 100.0


def test_every_supplier_has_valid_master_data() -> None:
    """Each supplier bills a known entity, in a known category, with a sensible amount range."""
    for supplier in reference.SUPPLIERS.values():
        assert set(supplier.entities) <= set(reference.ENTITIES)
        assert supplier.category in reference.CATEGORIES
        assert 0 < supplier.amount_usd[0] <= supplier.amount_usd[1]


def test_reference_export_has_one_row_per_supplier_and_entity() -> None:
    """The Glue job joins on (entity, supplier_id): every combination must be there once."""
    rows = reference.suppliers_rows()
    keys = [(row["entity"], row["supplier_id"]) for row in rows]

    assert len(keys) == len(set(keys)) == sum(len(s.entities) for s in reference.SUPPLIERS.values())
