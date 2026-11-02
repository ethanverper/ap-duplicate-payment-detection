"""End-to-end checks of the simulated AP history: reproducible, realistic, telling the planted story."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import pytest

from src.common.schema import ADVANCE, FINAL, FULL
from src.generators import story
from src.generators.simulator import PAYMENT_WEEKDAYS, SimulationResult, simulate

FIRST_DAY, LAST_DAY = date(2026, 8, 2), date(2026, 10, 30)


@pytest.fixture(scope="module")
def result() -> SimulationResult:
    """The 90-day history, simulated once for the whole module."""
    return simulate(FIRST_DAY, LAST_DAY, seed=42)


def test_same_seed_same_history() -> None:
    """Reproducibility: the README numbers must come out the same on every run."""
    first = simulate(date(2026, 9, 1), date(2026, 9, 30), seed=3)
    second = simulate(date(2026, 9, 1), date(2026, 9, 30), seed=3)
    assert [p.to_row() for p in first.payments] == [p.to_row() for p in second.payments]


def test_payments_leave_only_on_payment_run_days(result) -> None:
    """MX01 pays on business days; US01 only on Fridays (spec problem 4)."""
    for payment in result.payments:
        assert FIRST_DAY <= payment.payment_date <= LAST_DAY
        assert payment.payment_date.weekday() in PAYMENT_WEEKDAYS[payment.invoice.entity]


def test_payment_ids_are_unique(result) -> None:
    """The ERP never reuses a payment id: a duplicate PAYMENT is a new id for the same invoice."""
    ids = [payment.payment_id for payment in result.payments]
    assert len(ids) == len(set(ids))


def test_planted_problems_match_the_spec(result) -> None:
    """12 format duplicates in MX01, 8 resubmissions by the US01 carrier, 6 overpaid advances."""
    by_kind = defaultdict(list)
    for problem in result.planted:
        by_kind[problem.kind].append(problem)

    assert len(by_kind[story.FORMAT_DUPLICATE]) == 12
    assert {p.entity for p in by_kind[story.FORMAT_DUPLICATE]} == {"MX01"}
    assert len(by_kind[story.RESUBMITTED_INVOICE]) == 8
    assert {(p.entity, p.supplier_id) for p in by_kind[story.RESUBMITTED_INVOICE]} == {("US01", "SUP-TRANSNORTE")}
    assert len(by_kind[story.ADVANCE_OVERPAID]) == 6
    assert {p.entity for p in by_kind[story.ADVANCE_OVERPAID]} == {"MX01", "US01"}


def test_format_duplicates_pay_the_same_invoice_under_another_number(result) -> None:
    """Same invoice, same amount, the number typed differently, paid later."""
    for problem in result.planted:
        if problem.kind == story.FORMAT_DUPLICATE:
            original, duplicate = problem.original_payment, problem.problem_payment
            assert duplicate.invoice is original.invoice
            assert duplicate.typed_number != original.typed_number
            assert duplicate.paid_amount == original.paid_amount
            assert duplicate.payment_date > original.payment_date


def test_resubmissions_have_a_new_number_and_the_same_amount(result) -> None:
    """The carrier's resubmitted invoice: new number, same amount, dated a few days later."""
    for problem in result.planted:
        if problem.kind == story.RESUBMITTED_INVOICE:
            original, again = problem.original_payment.invoice, problem.problem_payment.invoice
            assert again.number != original.number
            assert again.amount == original.amount
            assert 1 <= (again.invoice_date - original.invoice_date).days <= 5


def test_legitimate_advances_add_up_to_the_invoice(result) -> None:
    """ADVANCE + FINAL = invoice amount; the planted overpayments pay MORE than the invoice."""
    # Every payment of every invoice, grouped by invoice
    by_invoice = defaultdict(list)
    for payment in result.payments:
        by_invoice[id(payment.invoice)].append(payment)
    overpaid = {id(p.problem_payment.invoice) for p in result.planted if p.kind == story.ADVANCE_OVERPAID}

    # Legitimate split payments: both the advance and the final are inside the window
    legit = [pays for key, pays in by_invoice.items()
             if {p.payment_type for p in pays} == {ADVANCE, FINAL} and key not in overpaid]  # fmt: skip
    assert len(legit) > 20  # many more legitimate advances than planted overpayments
    for pays in legit:
        assert sum(p.paid_amount for p in pays) == pytest.approx(pays[0].invoice.amount, abs=0.01)

    # The planted ones: advance + full amount
    for key in overpaid:
        assert sum(p.paid_amount for p in by_invoice[key]) > by_invoice[key][0].invoice.amount


def test_monthly_subscriptions_repeat_the_same_amount(result) -> None:
    """CloudOps bills the same amount every month: the detector must NOT call that a duplicate."""
    cloudops = [
        p for p in result.payments if p.invoice.supplier.supplier_id == "SUP-CLOUDOPS" and p.invoice.entity == "US01"
    ]
    assert len(cloudops) >= 2
    assert len({p.invoice.amount for p in cloudops}) == 1
    assert len({p.invoice.number for p in cloudops}) == len(cloudops)


def test_late_supplier_is_paid_after_the_due_date(result) -> None:
    """Andina in MX01 is always paid late (spec problem 5); almost every other payment is on time."""
    andina = [p for p in result.payments if p.invoice.supplier.supplier_id == "SUP-ANDINA" and p.payment_type == FULL]
    others = [p for p in result.payments if p.invoice.supplier.supplier_id != "SUP-ANDINA" and p.payment_type == FULL]

    assert all(p.payment_date > p.invoice.due_date for p in andina)
    on_time = sum(p.payment_date <= p.invoice.due_date for p in others) / len(others)
    assert on_time > 0.9


def test_fridays_only_loses_discounts(result) -> None:
    """US01 (Fridays only) misses far more 2/10 discount windows than MX01 (every business day)."""

    def missed(entity: str) -> float:
        eligible = [p for p in result.payments if p.invoice.entity == entity and p.payment_type == FULL
                    and p.invoice.supplier.terms.has_discount]  # fmt: skip
        late = [
            p for p in eligible if p.payment_date > p.invoice.supplier.terms.discount_deadline(p.invoice.invoice_date)
        ]
        return len(late) / len(eligible)

    assert missed("US01") > 0.5
    assert missed("MX01") < 0.2
