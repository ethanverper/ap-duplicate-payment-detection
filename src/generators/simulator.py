"""Simulates the accounts-payable process of the company: invoices arrive, payments go out.

Where it sits in the pipeline:
    history.py calls `simulate()` and writes its payments as one ERP file per entity per day
    (files.py). The pipeline then has to find, in those files alone, the problems planted here.

How a run works:
    1. Invoices: every business day each entity receives new invoices from its suppliers (random
       amounts within each supplier's range), plus fixed monthly invoices on the 1st.
    2. Payments: each invoice is scheduled the way that entity pays (discount deadline or due
       date, then the next payment-run weekday), sometimes late, sometimes as advance + final.
    3. Problems: the planted duplicates and overpayments are added (story.py, docs/spec.md).
    4. Only payments inside the history window are kept, then numbered like an ERP would.

The ground truth (which payments are planted problems) is returned separately and never written
into the ERP files: the pipeline must find the problems without being told.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import date, timedelta

from src.common.schema import ADVANCE, FINAL, FULL
from src.common.settings import settings
from src.generators import story
from src.generators.reference import ENTITIES, SUPPLIERS, Supplier, suppliers_of, to_currency

_SIMULATION = settings()["simulation"]

# Volume and payment behaviour (config/settings.yaml -> simulation)
INVOICES_PER_DAY: dict[str, int] = dict(_SIMULATION["invoices_per_day"])
PAYMENT_WEEKDAYS: dict[str, tuple[int, ...]] = {
    entity: tuple(days) for entity, days in _SIMULATION["payment_weekdays"].items()
}
TAKE_DISCOUNTS: dict[str, bool] = dict(_SIMULATION["take_discounts"])
ADVANCE_SHARE: float = _SIMULATION["advance_share"]
ADVANCE_PCT: float = _SIMULATION["advance_pct"]
ADVANCE_AFTER_DAYS: int = _SIMULATION["advance_after_days"]

# Business days are Monday (0) to Friday (4)
LAST_WEEKDAY = 4


@dataclass(frozen=True)
class Invoice:
    """A supplier invoice, as entered in the ERP."""

    entity: str
    supplier: Supplier
    number: str
    invoice_date: date
    amount: float
    currency: str

    @property
    def due_date(self) -> date:
        """Invoice date plus the net days of the supplier's terms."""
        return self.supplier.terms.due_date(self.invoice_date)


@dataclass(frozen=True)
class Payment:
    """One payment of (part of) an invoice. One row of an ERP file once numbered."""

    invoice: Invoice
    payment_date: date
    paid_amount: float
    payment_type: str
    typed_number: str  # the invoice number as typed for THIS payment (differs on format duplicates)
    payment_id: str = ""  # assigned at the end, in date order, like an ERP sequence

    def to_row(self) -> dict:
        """The payment as a file row (the column order is in src/common/schema.py)."""
        invoice = self.invoice
        return {
            "payment_id": self.payment_id,
            "entity": invoice.entity,
            "supplier_id": invoice.supplier.supplier_id,
            "supplier_invoice_number": self.typed_number,
            "invoice_date": invoice.invoice_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "invoice_amount": f"{invoice.amount:.2f}",
            "currency": invoice.currency,
            "payment_terms": invoice.supplier.terms.text,
            "payment_date": self.payment_date.isoformat(),
            "paid_amount": f"{self.paid_amount:.2f}",
            "payment_type": self.payment_type,
        }


@dataclass(frozen=True)
class PlantedProblem:
    """Ground truth for one planted problem: which payment is wrong and how much it overpays."""

    kind: str
    entity: str
    supplier_id: str
    original_payment: Payment
    problem_payment: Payment
    amount_at_risk: float  # money paid that should not have been, in the invoice currency


@dataclass
class SimulationResult:
    """Everything a run produces."""

    first_day: date
    last_day: date
    payments: list[Payment]  # numbered, sorted by entity and date, inside the window
    planted: list[PlantedProblem]  # ground truth, never written into the ERP files


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def is_business_day(day: date) -> bool:
    """Monday to Friday."""
    return day.weekday() <= LAST_WEEKDAY


def next_payment_day(day: date, entity: str) -> date:
    """The first day on or after `day` on which the entity runs payments (used for late payments)."""
    weekdays = PAYMENT_WEEKDAYS[entity]
    while day.weekday() not in weekdays:
        day += timedelta(days=1)
    return day


def last_payment_day_by(deadline: date, entity: str, not_before: date) -> date:
    """The last payment-run day on or before `deadline`, the way AP schedules a payment to be on time.

    Why: AP pays as late as possible without missing the date that matters (cash stays in the
    company longer). If no run day fits between `not_before` and the deadline, the next run day
    after `not_before` is used (the payment is then late, which the KPIs will show).

    Args:
        deadline: The date to meet (discount deadline or due date).
        entity: The paying entity (its payment-run weekdays).
        not_before: The earliest day the invoice can be paid (it must be entered and approved).

    Returns:
        The payment day.
    """
    weekdays = PAYMENT_WEEKDAYS[entity]
    day = deadline
    while day >= not_before:
        if day.weekday() in weekdays:
            return day
        day -= timedelta(days=1)
    return next_payment_day(not_before, entity)


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------


class APSimulator:
    """Generates invoices and payments for every entity, with the planted problems."""

    def __init__(self, first_day: date, last_day: date, seed: int) -> None:
        """Prepare a run.

        Args:
            first_day: First day of the history window (first file date).
            last_day: Last day of the history window.
            seed: Random seed: the same seed always gives the same files.
        """
        self.first_day = first_day
        self.last_day = last_day
        self.rng = random.Random(seed)
        self.incidents = story.load_incidents()

        # Invoices start earlier, so the first files already contain payments of older invoices
        self.invoices_from = first_day - timedelta(days=_SIMULATION["warm_up_days"])

        # Each supplier numbers its invoices with one running counter (shared by all entities)
        self._next_number = {supplier_id: self.rng.randint(100, 900) for supplier_id in SUPPLIERS}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> SimulationResult:
        """Simulate the whole history and return the numbered payments plus the ground truth."""
        # 1-2. Normal invoices and their payments
        invoices = self._all_invoices()
        payments = [payment for invoice in invoices for payment in self._pay(invoice)]

        # 3. The planted problems change or add payments
        payments, planted = self._plant_problems(payments)

        # 4. Keep the window, number the payments, and point the ground truth at the numbered ones
        return self._finish(payments, planted)

    # ------------------------------------------------------------------
    # 1. Invoices
    # ------------------------------------------------------------------

    def _all_invoices(self) -> list[Invoice]:
        """Every invoice of every entity, from the warm-up start to the last day."""
        invoices: list[Invoice] = []
        day = self.invoices_from
        while day <= self.last_day:
            # Invoices are entered on business days only
            if is_business_day(day):
                for entity in ENTITIES:
                    invoices.extend(self._invoices_of_day(entity, day))
            day += timedelta(days=1)
        return invoices

    def _invoices_of_day(self, entity: str, day: date) -> list[Invoice]:
        """The invoices one entity receives on one business day."""
        suppliers = suppliers_of(entity)
        invoices = []

        # Monthly subscriptions: one invoice on the first business day of the month, same amount every time
        if day.day <= 3 and not any(is_business_day(day.replace(day=d)) for d in range(1, day.day)):
            for supplier in suppliers:
                if supplier.recurring_fee is not None:
                    invoices.append(self._new_invoice(entity, supplier, day, supplier.recurring_fee))

        # Normal invoices: about the configured daily volume (±20%), suppliers picked by weight
        normal = [supplier for supplier in suppliers if supplier.recurring_fee is None and supplier.weight > 0]
        count = round(INVOICES_PER_DAY[entity] * self.rng.uniform(0.8, 1.2))
        for supplier in self.rng.choices(normal, weights=[s.weight for s in normal], k=count):
            amount_usd = self.rng.uniform(*supplier.amount_usd)
            invoices.append(self._new_invoice(entity, supplier, day, amount_usd))
        return invoices

    def _new_invoice(self, entity: str, supplier: Supplier, day: date, amount_usd: float) -> Invoice:
        """Create one invoice with the supplier's next number, in the entity's currency."""
        currency = ENTITIES[entity].currency
        number = supplier.invoice_number(self._next_number[supplier.supplier_id])
        self._next_number[supplier.supplier_id] += 1
        return Invoice(entity, supplier, number, day, to_currency(amount_usd, currency), currency)

    # ------------------------------------------------------------------
    # 2. Payments
    # ------------------------------------------------------------------

    def _pay_day(self, invoice: Invoice) -> date:
        """The day an invoice is paid, by the entity's policy.

        - Terms with a discount, at an entity that takes discounts: the last payment day before the
          discount deadline. Otherwise: the last payment day before the due date.
        - The late supplier (spec problem 5): a run day well after the due date.
        - A small share of invoices slip a few days for approval delays, everywhere.
        """
        entity = invoice.entity
        earliest = invoice.invoice_date + timedelta(days=1)

        # Problem 5: one supplier is chronically paid late
        if (entity, invoice.supplier.supplier_id) == (self.incidents.late_entity, self.incidents.late_supplier):
            late = invoice.due_date + timedelta(days=self.rng.randint(*self.incidents.late_extra_days))
            return next_payment_day(late, entity)

        # The date AP aims for, by policy
        discount_deadline = invoice.supplier.terms.discount_deadline(invoice.invoice_date)
        target = discount_deadline if (discount_deadline and TAKE_DISCOUNTS[entity]) else invoice.due_date

        # Ordinary approval delays on a small share of invoices: paid on the next run after the target
        if self.rng.random() < 0.08:
            return next_payment_day(target + timedelta(days=self.rng.randint(1, 7)), entity)
        return last_payment_day_by(target, entity, earliest)

    def _pay(self, invoice: Invoice) -> list[Payment]:
        """Schedule the payment(s) of one invoice: FULL, or ADVANCE + FINAL for some services."""
        pay_day = self._pay_day(invoice)

        # Some service invoices get an advance first, and the balance on the normal day
        if invoice.supplier.advances and self.rng.random() < ADVANCE_SHARE:
            advance = round(invoice.amount * ADVANCE_PCT, 2)
            advance_day = next_payment_day(invoice.invoice_date + timedelta(days=ADVANCE_AFTER_DAYS), invoice.entity)
            return [
                Payment(invoice, advance_day, advance, ADVANCE, invoice.number),
                Payment(invoice, max(pay_day, advance_day), round(invoice.amount - advance, 2), FINAL, invoice.number),
            ]

        # Everything else: the whole amount at once
        return [Payment(invoice, pay_day, invoice.amount, FULL, invoice.number)]

    # ------------------------------------------------------------------
    # 3. Planted problems
    # ------------------------------------------------------------------

    def _in_window(self, day: date) -> bool:
        """True if a day falls inside the history window (only those payments reach a file)."""
        return self.first_day <= day <= self.last_day

    def _plant_problems(self, payments: list[Payment]) -> tuple[list[Payment], list[PlantedProblem]]:
        """Add the three duplicate/overpayment problems of the spec. Returns new lists."""
        planted: list[PlantedProblem] = []
        payments = list(payments)
        planted += self._plant_format_duplicates(payments)
        planted += self._plant_resubmissions(payments)
        planted += self._plant_advance_overpayments(payments)
        return payments, planted

    def _candidates(
        self, payments: list[Payment], entity: str | None, supplier_id: str | None, kind: str
    ) -> list[Payment]:
        """Payments a problem can be planted on: right entity/supplier/type, safely inside the window."""
        return [
            payment
            for payment in payments
            if payment.payment_type == kind
            and (entity is None or payment.invoice.entity == entity)
            and (supplier_id is None or payment.invoice.supplier.supplier_id == supplier_id)
            and payment.invoice.supplier.recurring_fee is None
            # Leave room after the original so the problem payment also lands inside the window
            and self.first_day <= payment.payment_date <= self.last_day - timedelta(days=25)
        ]

    def _plant_format_duplicates(self, payments: list[Payment]) -> list[PlantedProblem]:
        """Problem 1: the same invoice is keyed again with its number in another format, and paid twice."""
        incidents = self.incidents
        candidates = self._candidates(payments, incidents.format_duplicate_entity, None, FULL)
        chosen = self.rng.sample(candidates, min(incidents.format_duplicate_count, len(candidates)))
        planted = []
        for original in chosen:
            # A few days later, the same invoice is paid again under a retyped number
            later = original.payment_date + timedelta(days=self.rng.randint(*incidents.format_duplicate_days))
            duplicate = replace(
                original,
                payment_date=next_payment_day(later, original.invoice.entity),
                typed_number=story.retyped_invoice_number(original.invoice.number, self.rng),
            )
            payments.append(duplicate)
            planted.append(
                PlantedProblem(story.FORMAT_DUPLICATE, original.invoice.entity, original.invoice.supplier.supplier_id,
                               original, duplicate, original.paid_amount)
            )  # fmt: skip
        return planted

    def _plant_resubmissions(self, payments: list[Payment]) -> list[PlantedProblem]:
        """Problem 2: the supplier sends the same invoice again with a NEW number and AP pays it again."""
        incidents = self.incidents
        candidates = self._candidates(payments, incidents.resubmitted_entity, incidents.resubmitted_supplier, FULL)
        planted = []
        for original in self.rng.sample(candidates, min(incidents.resubmitted_count, len(candidates))):
            # A new invoice record: new number, same amount, dated a few days after the original
            source = original.invoice
            resubmitted = self._new_invoice(source.entity, source.supplier, source.invoice_date, 0.0)
            resubmitted = replace(
                resubmitted,
                invoice_date=source.invoice_date + timedelta(days=self.rng.randint(*incidents.resubmitted_days)),
                amount=source.amount,
            )

            # It is paid like any other invoice (same entity rules)
            pay_day = max(self._pay_day(resubmitted), next_payment_day(original.payment_date, source.entity))
            duplicate = Payment(resubmitted, pay_day, resubmitted.amount, FULL, resubmitted.number)
            payments.append(duplicate)
            planted.append(
                PlantedProblem(story.RESUBMITTED_INVOICE, source.entity, source.supplier.supplier_id,
                               original, duplicate, duplicate.paid_amount)
            )  # fmt: skip
        return planted

    def _plant_advance_overpayments(self, payments: list[Payment]) -> list[PlantedProblem]:
        """Problem 3: after an advance, the FULL amount is paid instead of the balance."""
        # FINAL payments whose ADVANCE is also inside the window (both must reach a file)
        advance_days = {id(p.invoice): p.payment_date for p in payments if p.payment_type == ADVANCE}
        finals = [
            final
            for final in self._candidates(payments, None, None, FINAL)
            if self._in_window(advance_days[id(final.invoice)])
        ]
        by_entity = {entity: [p for p in finals if p.invoice.entity == entity] for entity in ENTITIES}
        per_entity = self.incidents.advance_overpaid_count // len(ENTITIES)

        planted = []
        for entity, entity_finals in by_entity.items():
            for final in self.rng.sample(entity_finals, min(per_entity, len(entity_finals))):
                # Swap the FINAL (balance) for a FULL payment of the whole invoice
                overpaid = replace(final, paid_amount=final.invoice.amount, payment_type=FULL)
                payments[payments.index(final)] = overpaid
                advance = next(p for p in payments if p.invoice is final.invoice and p.payment_type == ADVANCE)
                planted.append(
                    PlantedProblem(story.ADVANCE_OVERPAID, entity, final.invoice.supplier.supplier_id,
                                   advance, overpaid, advance.paid_amount)
                )  # fmt: skip
        return planted

    # ------------------------------------------------------------------
    # 4. Window and numbering
    # ------------------------------------------------------------------

    def _finish(self, payments: list[Payment], planted: list[PlantedProblem]) -> SimulationResult:
        """Keep the window, number payments per entity in date order, and renumber the ground truth."""
        # Only payments made inside the window are exported
        kept = [payment for payment in payments if self._in_window(payment.payment_date)]
        kept.sort(key=lambda p: (p.invoice.entity, p.payment_date, p.invoice.supplier.supplier_id, p.typed_number))

        # ERP-style ids: one running sequence per entity
        numbered: list[Payment] = []
        counters = dict.fromkeys(ENTITIES, 0)
        index: dict[int, Payment] = {}
        for payment in kept:
            counters[payment.invoice.entity] += 1
            with_id = replace(
                payment, payment_id=f"PAY-{payment.invoice.entity}-{counters[payment.invoice.entity]:06d}"
            )
            numbered.append(with_id)
            index[id(payment)] = with_id

        # The ground truth must name the numbered payments (both sides are inside the window by construction)
        truth = [
            replace(problem, original_payment=index[id(problem.original_payment)],
                    problem_payment=index[id(problem.problem_payment)])
            for problem in planted
            if id(problem.original_payment) in index and id(problem.problem_payment) in index
        ]  # fmt: skip
        return SimulationResult(self.first_day, self.last_day, numbered, truth)


def simulate(first_day: date, last_day: date, seed: int) -> SimulationResult:
    """Convenience entry point: run the simulator over a window."""
    return APSimulator(first_day, last_day, seed).run()
