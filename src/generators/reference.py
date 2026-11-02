"""Master data for the simulated company: entities, suppliers and payment terms (what an ERP holds).

Where it sits in the pipeline:
    The simulator uses it to create invoices and payments, and `history.py` exports it as CSV to
    the lake (reference/), where the Glue job reads supplier categories and terms to build the KPIs.

Where the values live:
    config/master_data.yaml (entities, categories, suppliers) and config/settings.yaml (exchange
    rate). This module turns them into typed objects and holds the small rules that must agree
    everywhere, such as how "2/10 NET 30" is read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from src.common.settings import master_data, settings

# ---------------------------------------------------------------------------
# Payment terms
# ---------------------------------------------------------------------------

# "2/10 NET 30" or "NET 45": an optional discount part, then the net days
_TERMS_PATTERN = re.compile(r"^\s*(?:(?P<pct>\d+(?:\.\d+)?)/(?P<days>\d+)\s+)?NET\s+(?P<net>\d+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class PaymentTerms:
    """Parsed payment terms.

    Attributes:
        text: The terms as printed, e.g. "2/10 NET 30".
        net_days: Days after the invoice date the invoice is due.
        discount_pct: Discount for paying early, in percent (0 if the terms have none).
        discount_days: Days after the invoice date the discount is available (0 if none).
    """

    text: str
    net_days: int
    discount_pct: float = 0.0
    discount_days: int = 0

    @property
    def has_discount(self) -> bool:
        """True when paying early earns a discount."""
        return self.discount_pct > 0

    def due_date(self, invoice_date: date) -> date:
        """Last day to pay the full amount on time."""
        return invoice_date + timedelta(days=self.net_days)

    def discount_deadline(self, invoice_date: date) -> date | None:
        """Last day to pay with the discount, or None if the terms have no discount."""
        return invoice_date + timedelta(days=self.discount_days) if self.has_discount else None


def parse_terms(text: str) -> PaymentTerms:
    """Read payment terms in the usual AP notation.

    Args:
        text: "NET 30", "2/10 NET 30", "1.5/15 NET 45"...

    Returns:
        The parsed terms.

    Raises:
        ValueError: If the text is not in that notation (master data must fail loudly).
    """
    # Match the whole string against the notation
    match = _TERMS_PATTERN.match(text)
    if not match:
        raise ValueError(f"Unrecognized payment terms: {text!r}")

    # Without a discount part, the discount fields stay at zero
    return PaymentTerms(
        text=text.strip().upper(),
        net_days=int(match["net"]),
        discount_pct=float(match["pct"]) if match["pct"] else 0.0,
        discount_days=int(match["days"]) if match["days"] else 0,
    )


# ---------------------------------------------------------------------------
# Entities and suppliers (values from config/master_data.yaml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    """A legal entity that pays suppliers."""

    entity_id: str
    name: str
    currency: str


@dataclass(frozen=True)
class Supplier:
    """A supplier as the ERP knows it.

    Attributes:
        supplier_id: e.g. "SUP-PRECISA".
        name: Display name.
        category: DIRECT_MATERIALS, LOGISTICS, SERVICES or IT_UTILITIES.
        entities: Entities it bills.
        terms: Parsed payment terms.
        amount_usd: (min, max) invoice amount in USD.
        weight: How often it invoices, relative to the other suppliers of the same entity.
        invoice_format: How it numbers invoices, e.g. "FAC-{n:04d}".
        recurring_fee: Fixed monthly amount in USD, or None for normal invoicing.
        advances: True if its invoices may be settled with an advance plus a final payment.
    """

    supplier_id: str
    name: str
    category: str
    entities: tuple[str, ...]
    terms: PaymentTerms
    amount_usd: tuple[float, float]
    weight: float
    invoice_format: str
    recurring_fee: float | None = None
    advances: bool = False

    def invoice_number(self, n: int) -> str:
        """The invoice number this supplier prints for its n-th invoice."""
        return self.invoice_format.format(n=n)


_MASTER = master_data()

# Entity id -> Entity
ENTITIES: dict[str, Entity] = {
    entity_id: Entity(entity_id, spec["name"], spec["currency"]) for entity_id, spec in _MASTER["entities"].items()
}

# Category id -> description
CATEGORIES: dict[str, str] = dict(_MASTER["categories"])

# Supplier id -> Supplier, with types converted and terms parsed
SUPPLIERS: dict[str, Supplier] = {
    supplier_id: Supplier(
        supplier_id=supplier_id,
        name=spec["name"],
        category=spec["category"],
        entities=tuple(spec["entities"]),
        terms=parse_terms(spec["terms"]),
        amount_usd=(float(spec["amount_usd"][0]), float(spec["amount_usd"][1])),
        weight=float(spec["weight"]),
        invoice_format=spec["invoice_format"],
        recurring_fee=float(spec["recurring_fee"]) if spec.get("recurring_fee") else None,
        advances=bool(spec.get("advances", False)),
    )
    for supplier_id, spec in _MASTER["suppliers"].items()
}

# Fixed reporting rate (config/settings.yaml -> reporting.mxn_per_usd)
MXN_PER_USD: float = float(settings()["reporting"]["mxn_per_usd"])


def suppliers_of(entity_id: str) -> list[Supplier]:
    """Suppliers that bill an entity, in master-data order."""
    return [supplier for supplier in SUPPLIERS.values() if entity_id in supplier.entities]


def to_currency(amount_usd: float, currency: str) -> float:
    """Convert a USD amount to an invoice currency, rounded to cents.

    Args:
        amount_usd: Amount in USD.
        currency: "USD" or "MXN".

    Returns:
        The amount in that currency.
    """
    rate = MXN_PER_USD if currency == "MXN" else 1.0
    return round(amount_usd * rate, 2)


# ---------------------------------------------------------------------------
# Exports (written as CSV by history.py for the lake's reference/ folder)
# ---------------------------------------------------------------------------


def suppliers_rows() -> list[dict]:
    """One row per supplier and entity it bills: category and terms for the KPIs."""
    return [
        {
            "entity": entity_id,
            "supplier_id": supplier.supplier_id,
            "supplier_name": supplier.name,
            "category": supplier.category,
            "payment_terms": supplier.terms.text,
            "net_days": supplier.terms.net_days,
            "discount_pct": supplier.terms.discount_pct,
            "discount_days": supplier.terms.discount_days,
        }
        for supplier in SUPPLIERS.values()
        for entity_id in supplier.entities
    ]


def entities_rows() -> list[dict]:
    """One row per entity, with its currency and the rate used to report it in USD."""
    return [
        {
            "entity": entity.entity_id,
            "entity_name": entity.name,
            "currency": entity.currency,
            "usd_rate": 1.0 if entity.currency == "USD" else round(1 / MXN_PER_USD, 8),
        }
        for entity in ENTITIES.values()
    ]
